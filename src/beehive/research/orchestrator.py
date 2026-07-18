# src/beehive/research/orchestrator.py
"""The sole owner of a Research Run's plan -> collect -> enrich -> cluster -> assess ->
revise/stop -> synthesize loop (ADR-0007, ADR-0009, ADR-0010). No other module in this package
or elsewhere in beehive drives this sequence or writes research_runs/research_snapshots/
research_evidence_* rows on its own -- planner.py, connector_policy.py, collector.py,
enrichment.py, clustering.py, and sufficiency.py are all pure building blocks this module
composes, and research.synthesis.generate_synthesis is the one further call this module makes
on the same (run_id, claim_token) before the run is allowed to reach a terminal state; the
worker only claims a run and hands its (run_id, claim_token) to `run_research_orchestration`.

=== Why clustering happens once, not every round ===
Clustering (`research.clustering.group_evidence_items`) and its persistence has no update/delete
counterpart -- every finalization appends new cluster rows via `db.research_finalization.
finalize_snapshot_if_claimed`. Re-clustering the same (mostly unchanged) snapshot membership
every revision round would therefore accumulate duplicate clusters for the same items. Clustering
therefore runs exactly ONCE, against the run's final snapshot membership, immediately before that
snapshot is sealed -- so ResearchRunPhase.CLUSTERING is only ever entered as part of finalization,
never mid-round. Evidence Sufficiency, by contrast, is assessed every round (ASSESSING), since it
is what decides whether another revision round happens at all.

=== Deadline, ceiling, claim, and cancellation discipline ===
- `deadline_at` comes from the ALREADY-CLAIMED run row (set once by
  `beehive.db.research_runs.claim_research_run`, COALESCEd across reclaims) -- this module
  never computes or resets it, per ADR-0009's fixed-budget guarantee. Remaining time is passed
  as the `timeout` for every AI call this module makes (the Research Plan prompt, the Evidence
  Sufficiency prompt, and the Research Synthesis calls `_finalize` makes at the very end).
- The 30-deep-fetch ceiling is enforced entirely by `reserve_deep_fetch`'s own transactional,
  claim-fenced reservation (called from enrichment.reserve_and_deep_fetch) *before* any
  fetch I/O; a denied reservation simply stops further deep fetches for the rest of the run,
  it does not itself end the run (snippets already collected remain valid evidence).
- Every canonical Evidence Item write (every `persist_candidate_snippet` call, and
  `reserve_and_deep_fetch`'s own final full-text write) goes through
  `beehive.db.evidence_items.upsert_evidence_item_if_claimed`, which is itself fenced on
  (run_id, claim_token, status='processing') and returns None the instant this worker's claim
  is no longer active -- this closes the specific race the checkpoints below cannot: a claim
  lost *during* enrichment.reserve_and_deep_fetch's network fetch, discovered only once that
  call tries its final write. `persist_candidate_snippet` returning None, or
  `reserve_and_deep_fetch` returning `DeepFetchStatus.CLAIM_LOST`, is treated exactly like a
  claim loss caught by `_require_active`: stop immediately with STALE_CLAIM, making no further
  writes and never overwriting whatever a newer claim already wrote.
- Every phase transition goes through `advance_research_run_phase`, which is itself fenced on
  (run_id, claim_token, status='processing') and returns False the instant this worker's claim
  is no longer active -- checked after every call.
- For writes beehive.db has NOT given a claim_token parameter to (create_research_source,
  create_plan_revision), this module re-checks the run's claim status itself immediately
  beforehand via `_require_active`/`_still_claimed` below. A worker that has lost its claim
  (crashed and been reclaimed, recovered past its lease) is stopped from writing at the next
  checkpoint even though these particular repository calls cannot enforce that fencing on
  their own. Snapshot item append (`db.research_snapshots.add_snapshot_items_if_claimed`) and
  finalization (clusters + seal + Evidence State Revision, `db.research_finalization.
  finalize_snapshot_if_claimed`) are the two exceptions: both are claim- *and* deadline-fenced
  atomically inside their own single transaction, not via a separate `_still_claimed` checkpoint
  beforehand -- see "Finalization is one atomic write" below and add_snapshot_items_if_claimed's
  own docstring.
- `cancel_requested` is checked at the very same checkpoints as claim loss inside the round loop
  (`_require_active` raises a distinct `_RunCancelled` for it) -- a cooperative stop-and-
  finalize, never a hard abort: whatever evidence is already durably persisted for this run is
  still sealed and pinned as an Evidence State Revision (ADR-0010: evidence survives
  cancellation), it just happens before any further collection/enrichment work runs. The one
  exception is the very start of a call: this module resolves (resumes or creates) this run's
  Evidence Snapshot BEFORE honoring `cancel_requested` -- see "Crash recovery" below for why.

=== Finalization is one atomic write ===
Sealing the snapshot, persisting its Evidence Clusters, and pinning the next Evidence State
Revision used to be three separately claim-checked steps here, with real gaps between them: a
heartbeat on another connection can fail this run for `error_code='deadline_exceeded'` (or a
recovery sweep can fail/requeue it) at any instant in one of those gaps, including the exact
instant the run's own fixed deadline arrives -- a `_still_claimed` check taken even microseconds
earlier cannot see that. `db.research_finalization.finalize_snapshot_if_claimed` closes this: this
module computes the deterministic cluster groups (`research.clustering.group_evidence_items`,
pure, no DB access) and the curation-filtered active evidence ids OUTSIDE any transaction, then
hands both to that one repository call -- along with the exact membership those groups/curation
were computed against (`expected_snapshot_item_ids`) -- which verifies the claim, the deadline
(sampled via `now_fn` only AFTER its own `BEGIN IMMEDIATE` has acquired the write lock, never
before), the snapshot's eligibility, that its CURRENT membership is non-empty and entirely
`session_id`'s own, and that both the precomputed membership and the precomputed active ids agree
EXACTLY with what is read fresh under that same lock -- current snapshot membership for the
former, `research_evidence_curation`'s current exclusions for the latter -- then writes every
cluster row, the seal, and the revision inside that single transaction -- all or nothing. This is
also what closes the curation race: an Owner excluding an item (`db.evidence_curation.
set_evidence_curation`, e.g. via `research.synthesis.exclude_evidence_item`) on a wholly separate
connection, committing between this module's precomputation and finalize's own lock, makes the
precomputed active ids stale in exactly the same way a concurrent membership append does. A
failed fence (claim lost, the deadline arrived inside that very transaction, the snapshot's
membership was itself invalid, or the membership/curation changed underneath the precomputation
-- e.g. a still-active worker's own append landed, a curation mutation committed first, or a
stale worker's append was correctly fenced out by `add_snapshot_items_if_claimed` but a legitimate
one was not) leaves no cluster row, no sealed snapshot, and no revision. Claim loss and deadline
failures are treated exactly like any other stale-claim checkpoint (STALE_CLAIM, no further
writes, synthesis never attempted); an invalid-membership or membership/curation change alone
never fails the run (the claim may still be perfectly live) -- the latter is retried exactly ONCE
-- reread membership and curation, recompute groups/active ids, try again -- before this module
gives up and reports stale rather than looping indefinitely. If THIS transaction instead wins the
lock first, its revision is valid for that instant; a curation mutation that commits afterward is
free to build its own later, newer revision the normal way, exactly as designed.

=== Crash recovery: resuming a run's own snapshot, building OR sealed ===
A run can be reclaimed after crashing at any point past snapshot creation -- mid-collection
(already having created a 'building' snapshot and staged Evidence Items into it), or even AFTER
`db.research_finalization.finalize_snapshot_if_claimed` already won its atomic clusters+seal+
revision write but before this worker went on to synthesize or complete the run. `run_research_
orchestration` resolves that exact run's own Evidence Snapshot FIRST via `db.research_snapshots.
get_or_create_snapshot_if_claimed` (Task D), before this call does anything else (including
honoring `cancel_requested`) -- the schema's UNIQUE(run_id) index on research_snapshots guarantees
at most one ever exists, so this ONE atomic call alone is enough to know both whether a snapshot
exists at all AND resolve it, entirely inside a single claim- and deadline-fenced BEGIN IMMEDIATE
transaction:

* Already 'sealed': a PRIOR attempt of this exact run already won atomic finalization before
  crashing or losing its claim -- `_resume_sealed_run` is called instead, skipping collection,
  clustering, and sealing entirely (all already done) and going straight to
  `_synthesize_and_terminate`'s shared tail: reuse an already-persisted synthesis for that exact
  revision if one exists (crash after synthesis persistence but before the run's terminal write),
  otherwise generate one against the pinned revision using the current claim/deadline fence
  exactly as normal, then complete the run. Never re-clusters, re-seals, mints another revision,
  recollects, or creates a second Research Synthesis for the same revision.
* Still 'building' (an earlier attempt of this exact run staged items into it before crashing or
  being reclaimed, or `get_or_create_snapshot_if_claimed` just created a genuinely new one for a
  run that never had one): this call re-enters the normal plan -> collect -> enrich -> assess
  round loop exactly as if this were the run's first attempt, picking up wherever the crashed
  attempt left off. A genuinely new snapshot is created only when this run has never created one
  of its own, and only ever copies forward from the Research Session's latest SEALED snapshot
  (never a stray, still-'building' one some other run left behind, whose membership is not fixed
  yet and must never be copied) -- both handled atomically inside `get_or_create_snapshot_if_
  claimed` itself. This resolution runs before the cancellation check specifically so that a run
  recovered in an already-cancelled state seals and pins any staged evidence
  (CANCELLED_WITH_EVIDENCE) instead of reporting CANCELLED_NO_EVIDENCE while the staged snapshot
  sits unreachable.

Why one atomic call instead of a plain read followed by a separate create (Task D): a stale
worker whose claim has already been reclaimed by a new owner (its lease expired, recovered, and
reclaimed by `claim_research_run` on another connection) could otherwise race that new owner to
be the one whose `create_snapshot` call wins the row for this run_id -- and because
`create_snapshot` raises ValueError the instant it sees `run_id` already has a snapshot,
whichever side's call happened to run SECOND, even the genuinely current owner, could receive a
spurious ValueError purely from losing that low-level race. `get_or_create_snapshot_if_claimed`
closes this: a stale worker's own call is rejected by the SAME claim fence every other atomic
write in this module already relies on (CLAIM_LOST), never by racing to create a row and losing.
Its deadline check carries the same cancellation-finalization grace `finalize_snapshot_if_
claimed` does (Task A): an already-cancelled run whose deadline has also passed can still resolve
(resume or create) its own snapshot, so `_finalize` gets the chance to seal whatever evidence
already exists (or correctly report CANCELLED_NO_EVIDENCE) instead of this call hard-failing the
run for deadline_exceeded before finalization is ever attempted.

=== The returned outcome ===
`SealedEvidenceOutcome` reports whether sealed, citable evidence ends up existing for this
Research Session, why the run stopped, the last Evidence Sufficiency assessment (if any), and
now also whether a Research Synthesis was produced (`synthesis_id`). A Research Run is no longer
allowed to reach `ResearchRunStatus.COMPLETED` (or, evidence/cancellation permitting, `CANCELLED`
with evidence) without this module having attempted `research.synthesis.generate_synthesis`
against the pinned Evidence State Revision (or reused an already-persisted synthesis for it) --
see `_synthesize_and_terminate` below for exactly when that attempt is skipped (no evidence at
all, or the run is cancelled -- a fresh Research Synthesis is always new AI-authored work, so
cancellation blocks it exactly like it blocks a new connector/fetch call, regardless of how much
of the run's own budget remains) versus made (evidence exists and the run is not cancelled)
versus made-but-failed (`RunOutcomeStatus.SYNTHESIS_FAILED`, which -- unlike every other status
without a `synthesis_id` -- still reports its `snapshot_id`/`evidence_state_revision_id`, since
the sealed evidence itself remains fully valid; only the synthesis attempt failed)."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from beehive.ai.llm_client import run_data_only_prompt
from beehive.ai.model_selection import DEFAULT_MODEL
from beehive.connectors.base import SourceConnector
from beehive.connectors.registry import get as _default_connector_resolver
from beehive.db.evidence_curation import list_evidence_curation
from beehive.db.evidence_items import get_evidence_items
from beehive.db.evidence_state import (get_evidence_state_revision,
                                        get_evidence_state_revision_for_snapshot,
                                        get_latest_evidence_state_revision_for_snapshot)
from beehive.db.research_finalization import (FinalizationFailureReason,
                                               finalize_snapshot_if_claimed)
from beehive.db.research_plan_revisions import create_plan_revision
from beehive.db.research_runs import (advance_research_run_phase,
                                       complete_research_run_if_claimed, get_research_run)
from beehive.db.research_snapshots import (add_snapshot_items_if_claimed,
                                            get_or_create_snapshot_if_claimed,
                                            list_snapshot_item_ids)
from beehive.db.research_sources import create_research_source, list_research_sources
from beehive.db.research_syntheses import (SynthesisAdmissionStatus, admit_synthesis_if_claimed,
                                            get_synthesis_for_revision, get_synthesis_for_run)
from beehive.deep_read.fetch import ArticleFetcher
from beehive.domain.research import (EvidenceItem, EvidenceSnapshot, EvidenceSnapshotStatus,
                                      EvidenceStateRevision, ResearchRun, ResearchRunPhase,
                                      ResearchRunStatus, ResearchSource, ResearchSourceOrigin,
                                      SufficiencyAssessment, SufficiencyState)
from beehive.localization import Localizer
from beehive.research.clustering import group_evidence_items
from beehive.research.collector import Candidate, CollectionOutcome, collect
from beehive.research.enrichment import (DeepFetchStatus, persist_candidate_snippet,
                                          project_for_prompt, reserve_and_deep_fetch,
                                          select_relevant_candidates)
from beehive.research.limits import (MAX_DEEP_FETCHES_PER_ROUND, MAX_ERROR_DETAIL_LENGTH,
                                      MAX_REVISION_ROUNDS, NOVELTY_STOP_ROUNDS)
from beehive.research.planner import (ResearchPlan, build_initial_plan_prompt,
                                       build_revision_plan_prompt, parse_plan_response)
from beehive.research.structured_response import StructuredResponseError
from beehive.research.sufficiency import (EvidenceProjection, build_sufficiency_prompt,
                                           parse_sufficiency_response)
from beehive.research.synthesis import (SynthesisCancelledError, SynthesisClaimLostError,
                                         generate_synthesis)

# Minimum timeout handed to an AI call even when the run's deadline is nearly exhausted: zero
# or a negative timeout would be meaningless to the SDK, and the deadline check at the top of
# each round already stops a new round from starting once truly out of time.
_MIN_AI_CALL_TIMEOUT_SECONDS = 1.0
# Ceiling on how much of the remaining deadline any single AI call may claim, so one very slow
# call cannot silently spend the run's entire remaining budget with nothing left for
# finalization (clustering/sealing/pinning) to run afterward.
_MAX_AI_CALL_TIMEOUT_SECONDS = 110.0


class RunOutcomeStatus(str, Enum):
    """Why `run_research_orchestration` stopped and what it produced. Every member except
    STALE_CLAIM corresponds to a completed database transition (COMPLETED, CANCELLED, or
    FAILED, per beehive.domain.research.ResearchRunStatus) -- STALE_CLAIM means this call made
    no further writes at all past the point its claim was found to be no longer active.
    SYNTHESIS_FAILED means the sealed Evidence Snapshot and pinned Evidence State Revision are
    both fully valid and durable, but the mandatory Research Synthesis attempt against them
    raised (malformed AI output, a timeout, or any other unexpected error) -- the run is FAILED,
    never left 'processing', and the evidence is never discarded. RESUMED_SEALED_SNAPSHOT and
    SEALED_SNAPSHOT_INVALID are both specific to the crash-recovery resume path (see
    `_resume_sealed_run`): RESUMED_SEALED_SNAPSHOT means a PRIOR attempt of this exact run had
    already won `db.research_finalization.finalize_snapshot_if_claimed`'s atomic clusters+seal+
    revision write before crashing or losing its claim, and this call resumed straight to
    synthesize-or-reuse-then-complete against that same snapshot/revision -- never re-clustering,
    re-sealing, or minting a second one. SEALED_SNAPSHOT_INVALID means that resumed snapshot had
    NO matching Evidence State Revision at all, which can only mean the schema's own atomic
    finalize+revision pairing was somehow violated (a data-integrity failure, not a retryable
    race) -- the run is FAILED with error_code='sealed_snapshot_missing_revision', reporting no
    snapshot_id/evidence_state_revision_id/synthesis_id, exactly like every other no-citable-
    evidence status below."""
    SUFFICIENT = "sufficient"
    LIMITS_REACHED = "limits_reached"
    NOVELTY_EXHAUSTED = "novelty_exhausted"
    CANCELLED_WITH_EVIDENCE = "cancelled_with_evidence"
    CANCELLED_NO_EVIDENCE = "cancelled_no_evidence"
    FAILED_NO_EVIDENCE = "failed_no_evidence"
    SYNTHESIS_FAILED = "synthesis_failed"
    STALE_CLAIM = "stale_claim"
    RESUMED_SEALED_SNAPSHOT = "resumed_sealed_snapshot"
    SEALED_SNAPSHOT_INVALID = "sealed_snapshot_invalid"


@dataclass(frozen=True)
class SealedEvidenceOutcome:
    """The typed result `run_research_orchestration` always returns instead of raising for any
    routine stopping condition (sufficiency reached, limits reached, cancellation, or zero
    evidence) -- only a programmer error (bad arguments) raises. `snapshot_id` and
    `evidence_state_revision_id` are both None whenever `status` has no durable, sealed,
    citable evidence to point to (CANCELLED_NO_EVIDENCE, FAILED_NO_EVIDENCE, STALE_CLAIM,
    SEALED_SNAPSHOT_INVALID) -- callers must never treat a None snapshot_id here as "the empty/
    latest snapshot", only as "there is nothing citable from this run". `synthesis_id` is
    populated only when a Research Synthesis was actually produced OR reused and persisted THIS
    call -- it is None for every status above, and also None for SYNTHESIS_FAILED (evidence
    sealed, but no synthesis) and for a CANCELLED_WITH_EVIDENCE run whose synthesis attempt was
    skipped (cancellation blocks a NEW Research Synthesis exactly like it blocks any other new
    AI-authored write, regardless of how much of the run's own budget remains -- see
    `_synthesize_and_terminate`'s own atomic admission gate) or itself failed; callers must
    never infer "no synthesis exists at all for this Research Session" from a None here -- an
    EARLIER run's synthesis may still be the session's latest."""
    status: RunOutcomeStatus
    run_id: int
    snapshot_id: int | None
    evidence_state_revision_id: int | None
    synthesis_id: int | None
    sufficiency: SufficiencyAssessment | None
    rounds_completed: int
    source_failures: tuple[SourceFailureRecord, ...]


@dataclass(frozen=True)
class SourceFailureRecord:
    """One round's per-source connector failure, carried through to the final outcome purely
    for observability -- collection itself already isolated it and moved on (collector.py)."""
    round_number: int
    research_source_id: int
    connector_type: str
    error_code: str
    detail: str


def _stale_outcome(
        run_id: int, sufficiency: SufficiencyAssessment | None, rounds_completed: int,
        source_failures: tuple[SourceFailureRecord, ...]) -> SealedEvidenceOutcome:
    """The one STALE_CLAIM SealedEvidenceOutcome shape, used at every checkpoint in this module
    that finds (run_id, claim_token) no longer active -- centralized so every one of those many
    checkpoints nulls exactly the same fields (snapshot_id, evidence_state_revision_id,
    synthesis_id) rather than each repeating the same dataclass literal by hand."""
    return SealedEvidenceOutcome(
        status=RunOutcomeStatus.STALE_CLAIM, run_id=run_id, snapshot_id=None,
        evidence_state_revision_id=None, synthesis_id=None, sufficiency=sufficiency,
        rounds_completed=rounds_completed, source_failures=source_failures)


class _ClaimLost(Exception):
    """Internal control-flow signal: this worker's claim_token no longer matches an active
    'processing' research_runs row (crashed and reclaimed, recovered past its lease, or the run
    already reached a terminal state some other way). Never surfaced to callers -- every path
    that can raise this is caught and turned into a STALE_CLAIM SealedEvidenceOutcome, making no
    further writes."""


class _RunCancelled(Exception):
    """Internal control-flow signal: cancel_requested was observed on an otherwise-still-active
    claim. Distinct from _ClaimLost -- this is a cooperative, orchestrator-level stop-and-
    finalize, not a fencing failure, so the caller still seals/pins whatever evidence already
    exists before returning."""


def _get_claimed_run(conn: sqlite3.Connection, run_id: int, claim_token: str) -> ResearchRun | None:
    """Returns the run row iff it is still 'processing' under exactly this claim_token, else
    None. Never raises -- this is the read-only primitive both `_require_active` (which raises
    for control flow) and `_still_claimed` (a plain bool check used inside finalization) are
    built from."""
    run = get_research_run(conn, run_id)
    if run is None or run.status is not ResearchRunStatus.PROCESSING or run.claim_token != claim_token:
        return None
    return run


def _require_active(conn: sqlite3.Connection, run_id: int, claim_token: str) -> ResearchRun:
    """The single checkpoint used before and after every external call (connector fetch, AI
    call, article fetch) and before every progressive write this module makes directly. Raises
    _ClaimLost if the claim is no longer active, else _RunCancelled if cancellation was
    requested, else returns the live run row."""
    run = _get_claimed_run(conn, run_id, claim_token)
    if run is None:
        raise _ClaimLost(f"Research Run {run_id} claim {claim_token!r} is no longer active")
    if run.cancel_requested:
        raise _RunCancelled(f"Research Run {run_id} cancellation requested")
    return run


def _still_claimed(conn: sqlite3.Connection, run_id: int, claim_token: str) -> bool:
    return _get_claimed_run(conn, run_id, claim_token) is not None


def _remaining_seconds(run: ResearchRun, now: datetime) -> float:
    if run.deadline_at is None:
        return _MAX_AI_CALL_TIMEOUT_SECONDS
    return (run.deadline_at - now).total_seconds()


def _ai_call_timeout(run: ResearchRun, now: datetime) -> float:
    """Clamps the run's remaining deadline into a sane per-call timeout: never below
    _MIN_AI_CALL_TIMEOUT_SECONDS (a zero/negative timeout is meaningless to the SDK -- the
    deadline check at the top of the round loop is what actually stops a new round starting
    once truly out of time) and never above _MAX_AI_CALL_TIMEOUT_SECONDS (so one call can never
    claim the whole remaining budget, leaving nothing for finalization)."""
    remaining = _remaining_seconds(run, now)
    return max(_MIN_AI_CALL_TIMEOUT_SECONDS, min(remaining, _MAX_AI_CALL_TIMEOUT_SECONDS))


def _plan_to_json(plan: ResearchPlan) -> str:
    return json.dumps({
        "plan_summary": plan.summary,
        "sources": [
            {"connector_type": s.connector_type, "config": s.config, "rationale": s.rationale}
            for s in plan.sources
        ],
    }, sort_keys=True)


def _config_key(config: dict) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(config.items()))


def _persist_plan_sources(
    conn: sqlite3.Connection, session_id: int, plan: ResearchPlan, now: datetime,
) -> list[ResearchSource]:
    """Persists every source in `plan` as a beehive.db.research_sources row, reusing an already-
    persisted ResearchSource (same connector_type + config) from an earlier round/run of this
    same Research Session instead of creating a duplicate row -- important for citation
    stability, since evidence_items.upsert_evidence_item_if_claimed's canonical key is
    (research_source_id, external_key): a duplicate research_sources row for the same
    connector_type/config would fragment what should be one citation-stable Evidence Item
    across two different research_source_id values."""
    existing = list_research_sources(conn, session_id)
    by_key: dict[tuple[str, tuple[tuple[str, str], ...]], ResearchSource] = {
        (source.connector_type, _config_key(source.config)): source for source in existing
    }
    persisted: list[ResearchSource] = []
    for plan_source in plan.sources:
        key = (plan_source.connector_type, _config_key(plan_source.config))
        source = by_key.get(key)
        if source is None:
            source = create_research_source(
                conn, session_id, plan_source.connector_type, plan_source.config,
                ResearchSourceOrigin.PLAN, now)
            by_key[key] = source
        persisted.append(source)
    return persisted


def _collection_sources(
    conn: sqlite3.Connection, session_id: int, plan_sources: Sequence[ResearchSource],
) -> list[ResearchSource]:
    """Every Research Source this round's collection must execute: the deduped union of every
    already-persisted origin=OWNER source for this Research Session with this round's own
    freshly-persisted plan sources, owner sources first. The Research Plan AI may freely
    supplement the Owner's own source selection, but it can never remove one by simply not
    mentioning it -- an Owner-selected source is executed every round regardless of what the AI
    proposes. Deduped on the exact same (connector_type, config) key `_persist_plan_sources`
    itself already reuses rows on, so a source the Owner selected AND the plan independently
    re-proposed is still only ever fetched once per round, never twice."""
    owner_sources = [
        source for source in list_research_sources(conn, session_id)
        if source.origin is ResearchSourceOrigin.OWNER
    ]
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    combined: list[ResearchSource] = []
    for source in (*owner_sources, *plan_sources):
        key = (source.connector_type, _config_key(source.config))
        if key in seen:
            continue
        seen.add(key)
        combined.append(source)
    return combined


async def _call_data_only(
    prompt: str, *, model: str, timeout: float, client: object | None,
) -> str:
    """Calls `run_data_only_prompt`, forwarding `client` only when one was actually given --
    tests patch this module's own `run_data_only_prompt` name with fakes that predate the
    `client` parameter, so passing `client=None` explicitly (rather than omitting it) would
    break every such fake with an unexpected-keyword-argument error for no behavioral benefit,
    since `client=None` and omitting it are equivalent to the real implementation anyway."""
    if client is not None:
        return await run_data_only_prompt(prompt, model=model, timeout=timeout, client=client)
    return await run_data_only_prompt(prompt, model=model, timeout=timeout)


async def _generate_plan(
    question: str, prior_plan: ResearchPlan | None, gaps: Sequence[str], localizer: Localizer,
    model: str, timeout: float, *, client: object | None = None,
) -> ResearchPlan:
    """Builds the exact same prompts planner.py's own generate_initial_plan/
    generate_revision_plan would build, and parses the response with the exact same
    parse_plan_response -- but calls run_data_only_prompt directly so this module can pass the
    run's own remaining-deadline timeout, which planner.py's high-level entry points do not
    expose a parameter for (and this package must not change planner.py's behavior to add
    one)."""
    if prior_plan is None:
        prompt = build_initial_plan_prompt(question, localizer.language)
    else:
        prompt = build_revision_plan_prompt(question, prior_plan, gaps, localizer.language)
    raw_response = await _call_data_only(prompt, model=model, timeout=timeout, client=client)
    return parse_plan_response(raw_response)


async def _assess(
    question: str, evidence: Sequence[EvidenceProjection], prior_gaps: Sequence[str],
    localizer: Localizer, model: str, timeout: float, *, client: object | None = None,
) -> SufficiencyAssessment:
    prompt = build_sufficiency_prompt(question, evidence, prior_gaps, localizer.language)
    raw_response = await _call_data_only(prompt, model=model, timeout=timeout, client=client)
    return parse_sufficiency_response(raw_response)


def _project_snapshot_evidence(items_by_id: dict[int, EvidenceItem]) -> list[EvidenceProjection]:
    return [
        EvidenceProjection(
            citation_number=item.citation_number, title=item.title, quality=item.quality,
            text=project_for_prompt(item), has_full_text=bool(item.full_text))
        for item in sorted(items_by_id.values(), key=lambda item: item.citation_number)
    ]


def _apply_curation_overlay(conn: sqlite3.Connection, item_ids: list[int]) -> list[int]:
    """Filters a candidate Evidence State Revision's item ids down to whatever
    evidence_curation.py's live Owner overlay currently says is not excluded -- applied every
    time finalization creates a NEW revision, including a refresh run's, not only a Research
    Session's first one. A refresh snapshot's membership is cumulative (copy-forward), but an
    Owner's earlier exclusion of an item must never silently reactivate just because a later
    run's snapshot still contains it. Mirrors research.synthesis._rebuild_evidence_state_
    revision's own curation filter exactly -- both are the only two places a new Evidence State
    Revision gets built, and both must agree on what "active" means."""
    if not item_ids:
        return []
    curation = list_evidence_curation(conn, item_ids)
    return [
        item_id for item_id in item_ids
        if not (curation.get(item_id) is not None and curation[item_id].is_excluded)
    ]



async def _finalize(
    conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int,
    snapshot_id: int | None, now: datetime, *, question: str, localizer: Localizer,
    synthesis_model: str, with_evidence_status: RunOutcomeStatus,
    sufficiency: SufficiencyAssessment | None, rounds_completed: int,
    source_failures: tuple[SourceFailureRecord, ...], now_fn: Callable[[], datetime],
    client: object | None = None,
) -> SealedEvidenceOutcome:
    """The single exit path for every routine stopping condition (sufficient / limits reached /
    novelty exhausted / cancelled). Seals+pins evidence, then hands off to
    `_synthesize_and_terminate` for the synthesize-or-reuse-then-complete tail shared with the
    crash-recovery resume path (`_resume_sealed_run`), if any evidence exists; otherwise leaves
    the (still-'building', if it exists at all) snapshot completely untouched -- it is never
    sealed, so it is never a citable snapshot (ADR-0010) -- and marks the run terminal with no
    snapshot/revision/synthesis to point to."""
    item_ids = list_snapshot_item_ids(conn, snapshot_id) if snapshot_id is not None else []

    if not item_ids:
        # Always REQUEST FAILED here, even for a call the caller already knows is a
        # cancellation (`with_evidence_status=CANCELLED_WITH_EVIDENCE`'s sibling call sites all
        # reach this branch too, whenever no evidence was actually collected): cancel_requested
        # is a monotonic flag (db.research_runs.request_cancel_research_run only ever sets it,
        # never clears it), so complete_research_run_if_claimed's own `honor_cancel=True`
        # default reread is always at least as informed as anything this call could have
        # observed earlier, and correctly commits CANCELLED (clearing error_code) whenever the
        # flag is set -- whether that was already known or a real Owner cancellation just
        # committed on another connection immediately before this write reached the lock. This
        # is what makes sure a cancellation that committed before this terminal transaction can
        # never surface as FAILED_NO_EVIDENCE.
        result = complete_research_run_if_claimed(
            conn, run_id, claim_token, ResearchRunStatus.FAILED, now, now_fn=now_fn,
            error_code="no_evidence_collected")
        if not result.ok:
            return _stale_outcome(run_id, sufficiency, rounds_completed, source_failures)
        outcome_status = (
            RunOutcomeStatus.CANCELLED_NO_EVIDENCE
            if result.committed_status is ResearchRunStatus.CANCELLED
            else RunOutcomeStatus.FAILED_NO_EVIDENCE)
        return SealedEvidenceOutcome(
            status=outcome_status, run_id=run_id, snapshot_id=None,
            evidence_state_revision_id=None, synthesis_id=None, sufficiency=sufficiency,
            rounds_completed=rounds_completed, source_failures=source_failures)

    if not _still_claimed(conn, run_id, claim_token):
        return _stale_outcome(run_id, sufficiency, rounds_completed, source_failures)
    if not advance_research_run_phase(conn, run_id, claim_token, ResearchRunPhase.CLUSTERING):
        return _stale_outcome(run_id, sufficiency, rounds_completed, source_failures)

    # Cluster grouping (research.clustering.group_evidence_items) and curation filtering are a
    # pure, deterministic function of the snapshot's CURRENT membership -- computed here,
    # OUTSIDE any transaction, so the one atomic finalization write below holds its BEGIN
    # IMMEDIATE lock for as short a time as possible. `expected_snapshot_item_ids` hands that
    # exact membership through to finalize_snapshot_if_claimed, which re-verifies it against
    # the snapshot's then-current membership AND research_evidence_curation's then-current
    # exclusions under its own lock: a stale worker's own append cannot land at all
    # (db.research_snapshots.add_snapshot_items_if_claimed fences that on (run_id, claim_token,
    # status='processing') the instant it is reclaimed), but if the membership genuinely changed
    # -- or a curation mutation committed -- between this read and that lock for any other
    # reason, the precomputed groups/active ids are stale and the fence fails closed with
    # MEMBERSHIP_CHANGED, writing nothing. Retried exactly ONCE -- reread membership and curation,
    # recompute groups/active ids fresh -- before giving up; a persistent mismatch is reported
    # exactly like a stale claim (no further writes) but never marks the run terminal on its own,
    # since the claim itself may still be perfectly live.
    finalized = None
    for _attempt in range(2):
        item_ids = list_snapshot_item_ids(conn, snapshot_id)
        items = list(get_evidence_items(conn, item_ids).values())
        cluster_groups = group_evidence_items(items)
        active_ids = _apply_curation_overlay(conn, item_ids)

        # The ONE atomic write for clusters + seal + revision: claim-, deadline-, and
        # membership-fenced end to end under a single BEGIN IMMEDIATE inside
        # finalize_snapshot_if_claimed, so a heartbeat/recovery sweep racing on another
        # connection -- including one that fires exactly when this run's fixed deadline
        # arrives -- can never leave a half-finalized snapshot (some clusters but no seal, or a
        # seal with no revision) behind. A failed fence writes nothing at all: no cluster row,
        # no sealed snapshot, no revision. `now_fn` is passed through so the authoritative
        # deadline/timestamp check happens only AFTER that transaction's own BEGIN IMMEDIATE has
        # acquired the write lock, never against the possibly-stale `now` sampled before this
        # call could even attempt to acquire it.
        finalized = finalize_snapshot_if_claimed(
            conn, run_id, claim_token, session_id, snapshot_id, cluster_groups, active_ids, now,
            now_fn=now_fn, expected_snapshot_item_ids=item_ids)
        if finalized.failure_reason != FinalizationFailureReason.MEMBERSHIP_CHANGED:
            break
    if not finalized.ok:
        return _stale_outcome(run_id, sufficiency, rounds_completed, source_failures)

    return await _synthesize_and_terminate(
        conn, run_id, claim_token, session_id, finalized.snapshot, finalized.revision, now,
        question=question, localizer=localizer, synthesis_model=synthesis_model,
        with_evidence_status=with_evidence_status, sufficiency=sufficiency,
        rounds_completed=rounds_completed, source_failures=source_failures, now_fn=now_fn,
        client=client)


async def _synthesize_and_terminate(
    conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int,
    sealed: EvidenceSnapshot, revision: EvidenceStateRevision, now: datetime, *, question: str,
    localizer: Localizer, synthesis_model: str,
    with_evidence_status: RunOutcomeStatus, sufficiency: SufficiencyAssessment | None,
    rounds_completed: int, source_failures: tuple[SourceFailureRecord, ...],
    now_fn: Callable[[], datetime], client: object | None = None,
) -> SealedEvidenceOutcome:
    """The shared tail of both a fresh finalization (`_finalize`, immediately after
    `finalize_snapshot_if_claimed` just sealed `sealed`/pinned `revision` THIS call) and a
    crash-recovery resume (`_resume_sealed_run`, against a snapshot/revision a PRIOR attempt of
    this exact run already sealed/pinned before crashing or losing its claim): advance to
    SYNTHESIZING, reuse an already-persisted Research Synthesis for `revision.id` if one exists
    (`db.research_syntheses.get_synthesis_for_revision` -- always None for a freshly-created
    revision, since no synthesis could possibly reference an id that did not exist a moment ago;
    populated only on a resume where synthesis persisted before the run's terminal write did),
    otherwise ask for atomic admission before attempting to generate one, and only THEN mark the
    run terminal.

    The synthesis attempt itself: reusing an existing synthesis never calls
    `db.research_syntheses.admit_synthesis_if_claimed` at all (no new AI work is ever attempted,
    so there is nothing to admit) -- the terminal write at the end still rereads cancellation
    fresh regardless, exactly as it always does. Otherwise, `admit_synthesis_if_claimed` is
    called FIRST, immediately before either of the two new LLM calls a fresh synthesis needs:
    this re-verifies claim/session/deadline/cancellation atomically, under its own lock, rather
    than trusting this call's own possibly-stale local state (an orchestration-level `cancelled`
    boolean captured earlier, well before finalization even began, or a plain unfenced clock
    read) -- closing the race where a cancellation (or the deadline) that committed in the
    meantime would otherwise let a whole new AI round-trip start whose output was always going
    to be discarded anyway.
      * CLAIM_LOST or DEADLINE_EXCEEDED: no AI call is attempted, and no further terminal write
        of this call's own is attempted either -- admission itself already made the run terminal
        for DEADLINE_EXCEEDED (error_code='deadline_exceeded'), and CLAIM_LOST means some other
        write already did. This returns STALE_CLAIM, exactly like every other claim/deadline
        loss in this module -- never a false success.
      * CANCEL_REQUESTED: admission wrote nothing at all and left the run 'processing' -- the AI
        calls are skipped entirely (`synthesis_id` stays None) and control falls through to the
        unconditional terminal write below, which rereads cancellation fresh and commits
        CANCELLED.
      * ALLOWED: the two AI calls are made, with a freshly re-sampled clock (never the `now`
        sampled before finalization began) for the per-call timeout. A raised
        `SynthesisCancelledError` means cancellation was instead discovered only once
        persistence itself reran this same check at its own, later lock (the two AI calls take
        long enough that cancellation committed while they were in flight) -- `synthesis_id`
        stays None and control falls through to the terminal write exactly like the
        CANCEL_REQUESTED admission outcome above, never treated as a lost claim. A raised
        `SynthesisClaimLostError` (its `SynthesisDeadlineExceededError` subclass included) means
        the claim was instead stolen, or the deadline arrived, exactly at persistence time --
        treated exactly like every other claim loss in this module (STALE_CLAIM, no further
        writes). Any OTHER exception (malformed AI output, a structured-response validation
        failure, an AI-call timeout, or anything else) is caught and turned into a safe, typed
        stop below -- its own type name and message (capped to
        `research.limits.MAX_ERROR_DETAIL_LENGTH`, never the Research Question, evidence, or a
        raw prompt) are captured as `error_detail` on that same terminal write, exactly like
        collector/research_worker.py's own `_classify_error` does for a run that instead crashes
        uncaught, so a synthesis failure is diagnosable from the DB row instead of only a bare
        `error_code='synthesis_failed'`.

    Every terminal write here goes through `complete_research_run_if_claimed` with its
    `honor_cancel=True` default, and the reported `RunOutcomeStatus` is always built from its
    ACTUAL committed status, never a status this call merely requested or a local guess about
    cancellation: a synthesis failure requests FAILED (error_code='synthesis_failed')
    unconditionally, and the success path requests COMPLETED unconditionally -- either commits
    CANCELLED instead the instant `cancel_requested` is found set, reported as
    CANCELLED_WITH_EVIDENCE with no `synthesis_id` (a `synthesis_failed`-and-cancelled race
    reports CANCELLED_WITH_EVIDENCE too, never SYNTHESIS_FAILED, per the same precedence). If
    this worker's claim was stolen or its deadline arrived at the very last terminal write
    (extremely unlikely, but not impossible, immediately after a successful synthesis attempt),
    this returns STALE_CLAIM rather than ever reporting a completed/cancelled/failed success
    this call did not actually win."""
    if not advance_research_run_phase(conn, run_id, claim_token, ResearchRunPhase.SYNTHESIZING):
        return _stale_outcome(run_id, sufficiency, rounds_completed, source_failures)

    existing_synthesis = get_synthesis_for_run(conn, run_id)
    if existing_synthesis is not None and (
            existing_synthesis.evidence_state_revision_id != revision.id):
        persisted_revision = get_evidence_state_revision(
            conn, existing_synthesis.evidence_state_revision_id)
        if persisted_revision is not None:
            revision = persisted_revision

    synthesis_id: int | None = None
    synthesis_failed = False
    synthesis_error_detail: str | None = None
    if existing_synthesis is not None:
        # A prior attempt of this exact run already persisted a synthesis for this exact
        # revision before crashing or losing its claim at the terminal write -- reuse it rather
        # than making a second, redundant AI call and minting a duplicate synthesis version. The
        # terminal write below still rereads cancellation fresh regardless.
        synthesis_id = existing_synthesis.id
    else:
        # Atomic admission immediately before any new LLM work: re-verifies claim/session/
        # deadline/cancellation under its own lock, rather than trusting this call's own
        # possibly-stale local state. See admit_synthesis_if_claimed's own docstring and this
        # function's docstring above for exactly what each outcome means.
        admission = admit_synthesis_if_claimed(
            conn, run_id, claim_token, session_id, now, now_fn=now_fn)
        if admission.status in (
                SynthesisAdmissionStatus.CLAIM_LOST, SynthesisAdmissionStatus.DEADLINE_EXCEEDED):
            # Either some other write already made this claim stale, or admission itself just
            # atomically failed the run outright for deadline_exceeded -- either way, no further
            # writes or terminal requests of this call's own are safe to attempt.
            return _stale_outcome(run_id, sufficiency, rounds_completed, source_failures)
        if admission.status is SynthesisAdmissionStatus.ALLOWED:
            # Re-sample the clock now that admission just reconfirmed this claim is active and
            # not yet past its deadline -- never the `now` sampled before finalization began,
            # which could be stale by however long clustering/sealing took.
            admission_now = now_fn()
            run_row = get_research_run(conn, run_id)
            timeout = (
                _ai_call_timeout(run_row, admission_now) if run_row is not None
                else _MIN_AI_CALL_TIMEOUT_SECONDS)
            sufficiency_state = (
                sufficiency.state if sufficiency is not None else SufficiencyState.PARTIAL)
            gaps = sufficiency.gaps if sufficiency is not None else ()
            contradictions = sufficiency.contradictions if sufficiency is not None else ()
            try:
                synthesis = await generate_synthesis(
                    conn, session_id, run_id, claim_token, question, revision.id,
                    sufficiency_state, localizer, admission_now, prior_gaps=gaps,
                    prior_contradictions=contradictions, model=synthesis_model, timeout=timeout,
                    now_fn=now_fn, reuse_existing_for_run=True, client=client)
            except SynthesisCancelledError:
                # Cancellation committed while the two AI calls were still in flight, discovered
                # only once persistence itself rereran this same check at its own, later lock --
                # `synthesis_id` stays None; the terminal write below rereads cancellation fresh
                # and commits CANCELLED, exactly like the CANCEL_REQUESTED admission outcome.
                pass
            except SynthesisClaimLostError:
                return _stale_outcome(run_id, sufficiency, rounds_completed, source_failures)
            except Exception as exc:
                # Never the Research Question, evidence, or a raw prompt -- only the exception's
                # own type name and its own message, capped, matching collector/research_worker.
                # py's _classify_error convention so both diagnostic paths stay consistent.
                synthesis_failed = True
                synthesis_error_detail = (
                    f"{type(exc).__name__}: {exc}"[:MAX_ERROR_DETAIL_LENGTH])
            else:
                synthesis_id = synthesis.id
        # else: admission.status is CANCEL_REQUESTED -- it wrote nothing and left the run
        # 'processing'; skip the two AI calls entirely (synthesis_id stays None) and fall
        # through to the same terminal write below, which rereads cancellation fresh and
        # commits CANCELLED.

    if synthesis_failed:
        # Always REQUEST FAILED here, even though cancellation may already be suspected:
        # cancel_requested is a monotonic flag, so complete_research_run_if_claimed's own
        # honor_cancel=True default reread is always at least as informed as anything this call
        # could have observed earlier, and correctly commits CANCELLED (clearing error_code)
        # whenever the flag is set -- a synthesis failure that races a real cancellation must
        # never surface as SYNTHESIS_FAILED.
        result = complete_research_run_if_claimed(
            conn, run_id, claim_token, ResearchRunStatus.FAILED, now, now_fn=now_fn,
            error_code="synthesis_failed", error_detail=synthesis_error_detail)
        if not result.ok:
            return _stale_outcome(run_id, sufficiency, rounds_completed, source_failures)
        if result.committed_status is ResearchRunStatus.CANCELLED:
            return SealedEvidenceOutcome(
                status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE, run_id=run_id,
                snapshot_id=sealed.id, evidence_state_revision_id=revision.id,
                synthesis_id=None, sufficiency=sufficiency, rounds_completed=rounds_completed,
                source_failures=source_failures)
        return SealedEvidenceOutcome(
            status=RunOutcomeStatus.SYNTHESIS_FAILED, run_id=run_id, snapshot_id=sealed.id,
            evidence_state_revision_id=revision.id, synthesis_id=None, sufficiency=sufficiency,
            rounds_completed=rounds_completed, source_failures=source_failures)

    # Always REQUEST completed here, even when cancellation is already suspected: cancel_
    # requested is a monotonic flag (db.research_runs.request_cancel_research_run only ever
    # sets it, never clears it), so complete_research_run_if_claimed's own honor_cancel=True
    # default reread is always at least as informed as anything this call could have observed
    # earlier, and correctly commits CANCELLED whenever it is set -- whether that was already
    # known or a real Owner cancellation just committed on another connection immediately
    # before this write reached the lock.
    result = complete_research_run_if_claimed(
        conn, run_id, claim_token, ResearchRunStatus.COMPLETED, now, now_fn=now_fn)
    if not result.ok:
        return _stale_outcome(run_id, sufficiency, rounds_completed, source_failures)
    final_with_evidence_status = (
        RunOutcomeStatus.CANCELLED_WITH_EVIDENCE
        if result.committed_status is ResearchRunStatus.CANCELLED else with_evidence_status)
    return SealedEvidenceOutcome(
        status=final_with_evidence_status, run_id=run_id, snapshot_id=sealed.id,
        evidence_state_revision_id=revision.id, synthesis_id=synthesis_id, sufficiency=sufficiency,
        rounds_completed=rounds_completed, source_failures=source_failures)


async def _resume_sealed_run(
    conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int,
    snapshot: EvidenceSnapshot, now: datetime, *, question: str, localizer: Localizer,
    synthesis_model: str, now_fn: Callable[[], datetime], client: object | None = None,
) -> SealedEvidenceOutcome:
    """Resumes a run whose `snapshot` (this run's own -- at most one ever exists, per the
    schema's UNIQUE(run_id) index on research_snapshots) is already 'sealed': a PRIOR attempt of
    this exact run already won `db.research_finalization.finalize_snapshot_if_claimed`'s atomic
    clusters+seal+revision write before crashing or losing its claim, so clustering/sealing/
    pinning must NEVER be repeated here -- only `_synthesize_and_terminate`'s shared
    synthesize-or-reuse-then-complete tail ever runs.

    Because finalize_snapshot_if_claimed always creates the seal and its Evidence State Revision
    in the SAME atomic transaction, `snapshot` having no matching revision at all
    (`db.evidence_state.get_evidence_state_revision_for_snapshot` returning None) is impossible
    under normal operation -- if it is ever observed anyway, this is an explicit data-integrity
    failure, not a retryable race: the run is failed outright
    (error_code='sealed_snapshot_missing_revision') rather than silently minting a replacement
    revision, which would break the atomic finalize+revision pairing this module otherwise
    guarantees everywhere else.

    Which revision to resume against -- R1 or a newer curation revision: `initial_revision`
    (`get_evidence_state_revision_for_snapshot`, the EARLIEST/lowest-version revision for this
    snapshot) is the exact one finalize_snapshot_if_claimed created atomically together with the
    seal itself -- call it R1. If R1 already has a persisted Research Synthesis, that synthesis
    IS this run's original, already-committed output from before the crash: it is reused exactly
    as-is, even if Owner curation has since built newer revisions for this same sealed snapshot
    (`research.synthesis.exclude_evidence_item`/`restore_evidence_item`), because that earlier
    output already persisted and must never be silently replaced. Only when R1 has NO synthesis
    yet does this resolve the snapshot's LATEST revision instead
    (`get_latest_evidence_state_revision_for_snapshot`) -- an Owner may have curated evidence
    while this run sat crashed or pending reclaim, building one or more newer revisions (R2, ...)
    against this SAME still-sealed snapshot (sealed snapshot membership never changes; only which
    members are "active" is re-derived). Resuming synthesis against the now-stale R1 in that case
    would be rejected outright by `research.synthesis.pin_evidence_for_synthesis` (it requires
    the Research Session's own current revision) -- surfacing as a spurious SYNTHESIS_FAILED for
    a run that has perfectly valid sealed evidence. `get_latest_evidence_state_revision_for_
    snapshot` is scoped to this exact `snapshot.id`, so it can only ever return R1 itself (no
    curation has happened) or a later revision built from that same snapshot -- defense in depth
    additionally refuses (falling back to R1) a result whose `session_id` does not match this
    exact run's own `session_id`, though `db.evidence_state.create_evidence_state_revision`'s own
    validation already makes that combination impossible to persist in the first place.

    Cancellation is honored exactly like `_finalize` honors it for a freshly-collected run: the
    persisted `cancel_requested` flag (re-read fresh here, since this call is itself the crash-
    recovery entry point that runs BEFORE any other cancellation check) decides CANCELLED vs.
    COMPLETED, but the sealed snapshot and its revision are never discarded either way -- only
    whether the run's own terminal status is CANCELLED_WITH_EVIDENCE or
    RESUMED_SEALED_SNAPSHOT. The missing-revision integrity failure above is the OPPOSITE: it
    calls `complete_research_run_if_claimed` with `honor_cancel=False` -- a data-integrity
    violation this severe must commit FAILED (error_code='sealed_snapshot_missing_revision')
    exactly as requested, never silently hidden behind a cancellation that merely happened to
    race it."""
    initial_revision = get_evidence_state_revision_for_snapshot(conn, snapshot.id)
    if initial_revision is None:
        result = complete_research_run_if_claimed(
            conn, run_id, claim_token, ResearchRunStatus.FAILED, now, now_fn=now_fn,
            error_code="sealed_snapshot_missing_revision", honor_cancel=False)
        if not result.ok:
            return _stale_outcome(run_id, None, 0, ())
        return SealedEvidenceOutcome(
            status=RunOutcomeStatus.SEALED_SNAPSHOT_INVALID, run_id=run_id, snapshot_id=None,
            evidence_state_revision_id=None, synthesis_id=None, sufficiency=None,
            rounds_completed=0, source_failures=())

    if get_synthesis_for_revision(conn, initial_revision.id) is not None:
        revision = initial_revision
    else:
        latest_for_snapshot = get_latest_evidence_state_revision_for_snapshot(conn, snapshot.id)
        revision = (
            latest_for_snapshot
            if latest_for_snapshot is not None and latest_for_snapshot.session_id == session_id
            else initial_revision)

    run = _get_claimed_run(conn, run_id, claim_token)
    if run is None:
        return _stale_outcome(run_id, None, 0, ())
    with_evidence_status = (
        RunOutcomeStatus.CANCELLED_WITH_EVIDENCE if run.cancel_requested
        else RunOutcomeStatus.RESUMED_SEALED_SNAPSHOT)

    return await _synthesize_and_terminate(
        conn, run_id, claim_token, session_id, snapshot, revision, now,
        question=question, localizer=localizer, synthesis_model=synthesis_model,
        with_evidence_status=with_evidence_status, sufficiency=None,
        rounds_completed=0, source_failures=(), now_fn=now_fn, client=client)


async def run_research_orchestration(
    conn: sqlite3.Connection,
    run_id: int,
    claim_token: str,
    session_id: int,
    question: str,
    localizer: Localizer,
    *,
    now_fn: Callable[[], datetime],
    fetcher: ArticleFetcher,
    connector_resolver: Callable[[str], SourceConnector] = _default_connector_resolver,
    planner_model: str = DEFAULT_MODEL,
    sufficiency_model: str = DEFAULT_MODEL,
    synthesis_model: str = DEFAULT_MODEL,
    max_deep_fetches_per_round: int = MAX_DEEP_FETCHES_PER_ROUND,
    max_rounds: int = MAX_REVISION_ROUNDS,
    client: object | None = None,
) -> SealedEvidenceOutcome:
    """Runs an already-claimed Research Run (run.status == 'processing', run.claim_token ==
    claim_token, per db/research_runs.py's claim_research_run) through as many
    plan -> collect -> enrich -> assess rounds as Evidence Sufficiency and the run's own
    deadline/claim allow, then finalizes (clusters, seals, pins, synthesizes) exactly once.
    Never raises for a routine stopping condition -- see SealedEvidenceOutcome and
    RunOutcomeStatus. Raises ValueError only for a caller error (blank question).

    `client` (e.g. from `ai.llm_client.tool_free_client()`) is passed through, unchanged, to
    every `run_data_only_prompt` call this run makes (plan generation, sufficiency assessment,
    synthesis) so a caller running the whole plan-collect-enrich-assess-synthesize lifecycle can
    pay the SDK's per-process startup cost once instead of once per call. Omitting it (the
    default) preserves the exact original one-client-per-call behavior."""
    if not question or not question.strip():
        raise ValueError("question must be non-empty")

    run = _get_claimed_run(conn, run_id, claim_token)
    if run is None:
        return _stale_outcome(run_id, None, 0, ())

    # Crash recovery + Task D: resolve (resume OR create) this run's own Evidence Snapshot --
    # atomically claim- and deadline-fenced (db.research_snapshots.get_or_create_snapshot_if_
    # claimed), BEFORE honoring cancel_requested below -- see the module docstring's "Crash
    # recovery" section for why this ordering matters (a recovered, already-cancelled run must
    # still seal/complete rather than reporting CANCELLED_NO_EVIDENCE and stranding evidence) and
    # why this must be one atomic call rather than a separate read-then-create (a stale worker
    # whose claim was already reclaimed can never win -- or spuriously lose via ValueError -- a
    # race to create this run's snapshot; it simply gets CLAIM_LOST). A failed fence (claim lost,
    # or the deadline arrived while this run is NOT cancelled) makes no further writes here.
    resolved = get_or_create_snapshot_if_claimed(
        conn, run_id, claim_token, session_id, now_fn(), now_fn=now_fn)
    if not resolved.ok:
        return _stale_outcome(run_id, None, 0, ())
    snapshot = resolved.snapshot

    # If that snapshot is already 'sealed', a PRIOR attempt of this exact run already won
    # finalize_snapshot_if_claimed's atomic clusters+seal+revision write before crashing or
    # losing its claim: resume straight to _resume_sealed_run's synthesize-or-reuse-then-complete
    # tail, never re-entering collection, re-clustering, re-sealing, or creating a second
    # snapshot for this run.
    if snapshot.status is EvidenceSnapshotStatus.SEALED:
        return await _resume_sealed_run(
            conn, run_id, claim_token, session_id, snapshot, now_fn(),
            question=question, localizer=localizer, synthesis_model=synthesis_model,
            now_fn=now_fn, client=client)

    seen_evidence_ids: set[int] = set(list_snapshot_item_ids(conn, snapshot.id))

    if run.cancel_requested:
        return await _finalize(
            conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
            question=question, localizer=localizer, synthesis_model=synthesis_model,
            with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
            sufficiency=None, rounds_completed=0, source_failures=(), now_fn=now_fn, client=client)

    prior_plan: ResearchPlan | None = None
    gaps: list[str] = []
    no_novelty_rounds = 0
    last_assessment: SufficiencyAssessment | None = None
    rounds_completed = 0
    source_failures: list[SourceFailureRecord] = []
    stop_status: RunOutcomeStatus = RunOutcomeStatus.LIMITS_REACHED

    for round_number in range(1, max_rounds + 1):
        try:
            run = _require_active(conn, run_id, claim_token)
        except _ClaimLost:
            return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))
        except _RunCancelled:
            return await _finalize(
                conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
                question=question, localizer=localizer, synthesis_model=synthesis_model,
                with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
                sufficiency=last_assessment, rounds_completed=rounds_completed,
                source_failures=tuple(source_failures), now_fn=now_fn, client=client)

        if _remaining_seconds(run, now_fn()) <= 0:
            stop_status = RunOutcomeStatus.LIMITS_REACHED
            break

        # --- PLANNING ---------------------------------------------------------------------
        if not advance_research_run_phase(conn, run_id, claim_token, ResearchRunPhase.PLANNING):
            return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))

        try:
            plan = await _generate_plan(
                question, prior_plan, gaps, localizer, planner_model,
                _ai_call_timeout(run, now_fn()), client=client)
        except StructuredResponseError:
            # A malformed Research Plan response cannot safely be used to collect anything
            # this round -- treat it the same as "no new plan this round" and fall through
            # to assessment/finalization with whatever evidence already exists.
            break

        try:
            run = _require_active(conn, run_id, claim_token)
        except _ClaimLost:
            return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))
        except _RunCancelled:
            return await _finalize(
                conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
                question=question, localizer=localizer, synthesis_model=synthesis_model,
                with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
                sufficiency=last_assessment, rounds_completed=rounds_completed,
                source_failures=tuple(source_failures), now_fn=now_fn, client=client)

        create_plan_revision(conn, run_id, _plan_to_json(plan), plan.summary, True, now_fn())
        plan_sources = _persist_plan_sources(conn, session_id, plan, now_fn())
        sources = _collection_sources(conn, session_id, plan_sources)

        # --- COLLECTING --------------------------------------------------------------------
        if not advance_research_run_phase(conn, run_id, claim_token, ResearchRunPhase.COLLECTING):
            return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))

        collection: CollectionOutcome = collect(sources, connector_resolver=connector_resolver)
        for failure in collection.failures:
            source_failures.append(SourceFailureRecord(
                round_number=round_number, research_source_id=failure.research_source_id,
                connector_type=failure.connector_type, error_code=failure.error_code,
                detail=failure.detail))

        try:
            run = _require_active(conn, run_id, claim_token)
        except _ClaimLost:
            return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))
        except _RunCancelled:
            return await _finalize(
                conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
                question=question, localizer=localizer, synthesis_model=synthesis_model,
                with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
                sufficiency=last_assessment, rounds_completed=rounds_completed,
                source_failures=tuple(source_failures), now_fn=now_fn, client=client)

        # --- ENRICHING -----------------------------------------------------------------------
        if not advance_research_run_phase(conn, run_id, claim_token, ResearchRunPhase.ENRICHING):
            return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))

        new_item_ids: list[int] = []
        new_candidate_pairs: list[tuple[Candidate, EvidenceItem]] = []
        for candidate in collection.candidates:
            try:
                _require_active(conn, run_id, claim_token)
            except _ClaimLost:
                return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))
            except _RunCancelled:
                return await _finalize(
                    conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
                    question=question, localizer=localizer, synthesis_model=synthesis_model,
                    with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
                    sufficiency=last_assessment, rounds_completed=rounds_completed,
                    source_failures=tuple(source_failures), now_fn=now_fn, client=client)

            item = persist_candidate_snippet(conn, run_id, claim_token, session_id, now_fn(),
                                              candidate)
            if item is None:
                # The claim-fenced write itself found (run_id, claim_token) no longer active --
                # e.g. lost between the _require_active check above and this write. Stop
                # immediately, exactly like any other claim loss: no further writes this round.
                return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))
            if item.id not in seen_evidence_ids:
                seen_evidence_ids.add(item.id)
                new_item_ids.append(item.id)
                new_candidate_pairs.append((candidate, item))

        if new_item_ids:
            try:
                _require_active(conn, run_id, claim_token)
            except _ClaimLost:
                return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))
            except _RunCancelled:
                return await _finalize(
                    conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
                    question=question, localizer=localizer, synthesis_model=synthesis_model,
                    with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
                    sufficiency=last_assessment, rounds_completed=rounds_completed,
                    source_failures=tuple(source_failures), now_fn=now_fn, client=client)
            # Claim- and deadline-fenced under its own BEGIN IMMEDIATE, atomically with the
            # append itself -- not just via the _require_active check above -- so a stale
            # worker that passed that application-level check, then paused (GC pause, thread
            # scheduling delay, etc.) across a reclaim, can never land this append once its
            # claim_token no longer matches an active 'processing' row. See
            # db.research_snapshots.add_snapshot_items_if_claimed's own docstring for exactly
            # why this must be one atomic fenced write rather than a separate check-then-write.
            append_result = add_snapshot_items_if_claimed(
                conn, run_id, claim_token, session_id, snapshot.id, new_item_ids, now_fn(),
                now_fn=now_fn)
            if not append_result.ok:
                # Claim lost or the deadline was reached exactly at this fenced append (found
                # atomically inside add_snapshot_items_if_claimed's own transaction, after the
                # _require_active check above already reconfirmed activity a moment earlier) --
                # stop immediately, exactly like any other claim/deadline loss: no further
                # writes this round.
                return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))

        selected = select_relevant_candidates(
            [candidate for candidate, _ in new_candidate_pairs], question,
            max_deep_fetches_per_round)
        selected_ids = {id(candidate) for candidate in selected}
        for candidate, item in new_candidate_pairs:
            if id(candidate) not in selected_ids:
                continue
            try:
                _require_active(conn, run_id, claim_token)
            except _ClaimLost:
                return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))
            except _RunCancelled:
                return await _finalize(
                    conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
                    question=question, localizer=localizer, synthesis_model=synthesis_model,
                    with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
                    sufficiency=last_assessment, rounds_completed=rounds_completed,
                    source_failures=tuple(source_failures), now_fn=now_fn, client=client)

            result = reserve_and_deep_fetch(
                conn, run_id, claim_token, session_id, item, now_fn(), fetcher=fetcher)

            if result.status is DeepFetchStatus.CLAIM_LOST:
                # The claim-fenced full-text write itself found (run_id, claim_token) no
                # longer active -- lost sometime during the fetcher's network call, after the
                # reservation above had already confirmed it was active. Unlike
                # RESERVATION_DENIED (known before any I/O, and possibly just "ceiling
                # reached"), this can only mean the claim is genuinely gone: stop immediately
                # rather than falling through to another round with a dead claim. The fenced
                # write made no changes, so whatever a newer claim already persisted for this
                # Evidence Item is left completely untouched.
                return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))

            try:
                _require_active(conn, run_id, claim_token)
            except _ClaimLost:
                return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))
            except _RunCancelled:
                return await _finalize(
                    conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
                    question=question, localizer=localizer, synthesis_model=synthesis_model,
                    with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
                    sufficiency=last_assessment, rounds_completed=rounds_completed,
                    source_failures=tuple(source_failures), now_fn=now_fn, client=client)

            if result.status is DeepFetchStatus.RESERVATION_DENIED:
                # Either the run-wide 30-fetch ceiling was reached (claim just reconfirmed
                # active above) -- stop attempting further deep fetches for the rest of the
                # run, snippet evidence already persisted for every candidate remains valid.
                break

        # --- ASSESSING -----------------------------------------------------------------------
        if not advance_research_run_phase(conn, run_id, claim_token, ResearchRunPhase.ASSESSING):
            return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))

        try:
            run = _require_active(conn, run_id, claim_token)
        except _ClaimLost:
            return _stale_outcome(run_id, last_assessment, rounds_completed, tuple(source_failures))
        except _RunCancelled:
            return await _finalize(
                conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
                question=question, localizer=localizer, synthesis_model=synthesis_model,
                with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
                sufficiency=last_assessment, rounds_completed=rounds_completed,
                source_failures=tuple(source_failures), now_fn=now_fn, client=client)

        all_item_ids = list_snapshot_item_ids(conn, snapshot.id)
        items_by_id = get_evidence_items(conn, all_item_ids)
        projections = _project_snapshot_evidence(items_by_id)

        try:
            assessment = await _assess(
                question, projections, gaps, localizer, sufficiency_model,
                _ai_call_timeout(run, now_fn()), client=client)
        except StructuredResponseError:
            # A malformed Evidence Sufficiency response cannot safely drive another revision
            # round -- stop here with whatever evidence/assessment already exists.
            stop_status = RunOutcomeStatus.LIMITS_REACHED
            break

        try:
            _require_active(conn, run_id, claim_token)
        except _ClaimLost:
            return _stale_outcome(run_id, assessment, rounds_completed, tuple(source_failures))
        except _RunCancelled:
            return await _finalize(
                conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
                question=question, localizer=localizer, synthesis_model=synthesis_model,
                with_evidence_status=RunOutcomeStatus.CANCELLED_WITH_EVIDENCE,
                sufficiency=assessment, rounds_completed=rounds_completed,
                source_failures=tuple(source_failures), now_fn=now_fn, client=client)

        last_assessment = assessment
        rounds_completed = round_number
        prior_plan = plan
        gaps = list(assessment.gaps)
        no_novelty_rounds = 0 if new_item_ids else no_novelty_rounds + 1

        if assessment.is_sufficient and items_by_id:
            stop_status = RunOutcomeStatus.SUFFICIENT
            break
        if no_novelty_rounds >= NOVELTY_STOP_ROUNDS:
            stop_status = RunOutcomeStatus.NOVELTY_EXHAUSTED
            break
    else:
        stop_status = RunOutcomeStatus.LIMITS_REACHED

    return await _finalize(
        conn, run_id, claim_token, session_id, snapshot.id, now_fn(),
        question=question, localizer=localizer, synthesis_model=synthesis_model,
        with_evidence_status=stop_status, sufficiency=last_assessment,
        rounds_completed=rounds_completed, source_failures=tuple(source_failures),
        now_fn=now_fn, client=client)


__all__ = [
    "RunOutcomeStatus",
    "SealedEvidenceOutcome",
    "SourceFailureRecord",
    "run_research_orchestration",
]
