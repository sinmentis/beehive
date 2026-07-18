# src/beehive/research/enrichment.py
"""Turns collector.py's in-memory Candidates into durable, canonical Evidence Items, and
selectively deepens the most relevant ones into full-article text (ADR-0007, ADR-0010).

Three distinct pieces of work live here, always in this order for one Candidate:

1. `persist_candidate_snippet` -- every Candidate becomes (or refreshes) a canonical Evidence
   Item via the claim-fenced `beehive.db.evidence_items.upsert_evidence_item_if_claimed`,
   carrying at minimum the connector's own snippet/body text. This is cheap and safe to do for
   every collected Candidate regardless of the run's remaining deep-fetch budget: CONTEXT.md's
   Evidence Item contract is satisfiable from snippet text alone. `preserve_existing_full_text`
   is always passed as True here, so a Candidate re-collected in a later round never wipes out
   full_text a prior `reserve_and_deep_fetch` already durably persisted for this same canonical
   row -- the repository itself protects that, this module no longer has to look it up.
2. `select_relevant_candidates` -- a small, deterministic (no AI, no network) relevance
   ranking of which of those Candidates are worth the expensive full-article fetch, since
   ADR-0010's 30-deep-fetch-per-run ceiling means not every Candidate can be deepened.
3. `reserve_and_deep_fetch` -- for a selected, already-snippet-persisted Evidence Item:
   reserve one of the run's 30 deep-fetch slots via
   `beehive.db.research_runs.reserve_deep_fetch` (a transactional, fenced-on-claim_token
   reservation) *before* any I/O; only if that reservation succeeds does it call the
   injected, SSRF-safe `ArticleFetcher.fetch`, then `beehive.deep_read.extract
   .extract_full_article_text` (the UNCAPPED extractor -- deliberately not
   `extract_article_text`'s prompt-oriented capped sibling) to durably persist the complete
   article text via the same claim-fenced `upsert_evidence_item_if_claimed`. `project_for_prompt`
   is the one place a *bounded* projection of that uncapped text is produced, and only ever for
   feeding an AI prompt (sufficiency.py) -- never for what gets persisted.

Every canonical Evidence Item write in this module goes through
`upsert_evidence_item_if_claimed`, so this module IS claim-fenced at the point of every write,
closing the exact race orchestrator.py's own `_require_active`/`_still_claimed` checkpoints
cannot: a worker can pass those checkpoints while still holding an active claim, then lose that
claim to a reclaim mid-flight during the network fetch itself (the one call in this module with
no fencing of its own), and only discover that loss when it tries to write. Losing the fence at
that final write is reported back to the caller rather than silently dropped or retried:
`persist_candidate_snippet` returns None, and `reserve_and_deep_fetch` returns
`DeepFetchStatus.CLAIM_LOST`, in both cases making zero writes and leaving whatever a newer
claim already wrote completely untouched. orchestrator.py still separately checks claim/cancel
state before and after each external call (connector fetch, AI call) via `_require_active` --
that is unrelated to this module's own fenced writes and still orchestrator.py's job, since
only it holds the run_id/claim_token/now triple across an entire round."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from beehive.db.evidence_items import upsert_evidence_item_if_claimed
from beehive.db.research_runs import reserve_deep_fetch
from beehive.deep_read.extract import ExtractionQuality, PartialReason, extract_full_article_text
from beehive.deep_read.fetch import ArticleFetcher, FetchedArticle, FetchFailure, FetchFailureReason
from beehive.domain.research import EvidenceItem, EvidenceQuality
from beehive.research.collector import Candidate
from beehive.research.limits import MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Tie-break order when two Candidates score equally on relevance: prefer the application's
# own quality classification (collector.py's quality_for_connector_type), never anything a
# connector or the AI claims about itself. Lower rank sorts first.
_QUALITY_RANK: dict[EvidenceQuality, int] = {
    EvidenceQuality.PRIMARY: 0,
    EvidenceQuality.REPORTING: 1,
    EvidenceQuality.ANALYSIS: 2,
    EvidenceQuality.COMMUNITY: 3,
    EvidenceQuality.AGGREGATOR: 4,
}


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _relevance_score(candidate: Candidate, question_tokens: set[str]) -> int:
    """Deterministic, AI-free relevance signal: how many of the Research Question's own
    keyword tokens appear in the Candidate's title+body. No network, no LLM call -- this
    only decides which already-collected Candidates are worth an expensive deep fetch, it
    never itself reads or scores full article text."""
    if not question_tokens:
        return 0
    candidate_tokens = _tokenize(candidate.raw_item.title) | _tokenize(candidate.raw_item.body)
    return len(question_tokens & candidate_tokens)


def select_relevant_candidates(
    candidates: list[Candidate], research_question: str, limit: int,
) -> list[Candidate]:
    """Ranks candidates by keyword overlap with the Research Question (descending), tie-broken
    by application-assigned EvidenceQuality, then by original collection order (stable sort),
    and returns at most `limit` of them. Never raises; an empty `candidates` list or a
    non-positive `limit` simply yields an empty selection."""
    if limit <= 0:
        return []
    question_tokens = _tokenize(research_question)
    ranked = sorted(
        enumerate(candidates),
        key=lambda pair: (
            -_relevance_score(pair[1], question_tokens),
            _QUALITY_RANK.get(pair[1].quality, len(_QUALITY_RANK)),
            pair[0],
        ),
    )
    return [candidate for _, candidate in ranked[:limit]]


def project_for_prompt(
    evidence_item: EvidenceItem, *, max_chars: int = MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT,
) -> str:
    """Bounded projection of an Evidence Item's text for an AI prompt (sufficiency.py) --
    prefers the durably persisted, uncapped full_text when present, else the snippet. Never
    mutates or re-persists anything; the underlying Evidence Item's own full_text/snippet
    columns stay exactly as `reserve_and_deep_fetch`/`persist_candidate_snippet` wrote them."""
    text = evidence_item.full_text or evidence_item.snippet or ""
    return text[:max_chars]


def persist_candidate_snippet(
    conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int, now: datetime,
    candidate: Candidate,
) -> EvidenceItem | None:
    """Upserts one Candidate as a canonical Evidence Item carrying (at minimum) its connector-
    provided snippet/body text, via the claim-fenced
    `beehive.db.evidence_items.upsert_evidence_item_if_claimed`. Citation-stable and
    idempotent: re-collecting the same (research_source_id, external_id) across rounds/runs
    refreshes this same row's content in place rather than allocating a new citation_number
    (beehive.db.evidence_items's own contract).

    Always passes `preserve_existing_full_text=True`: a snippet-only refresh must never wipe
    out full_text a prior `reserve_and_deep_fetch` already durably persisted for this same
    canonical row just because this shallower call has nothing but snippet text to report --
    the repository itself protects against that (via COALESCE), so this caller no longer needs
    to look up and pass back a prior full_text of its own.

    Returns None, making zero writes, the instant (run_id, claim_token) is no longer this
    run's active 'processing' claim -- e.g. the lease expired and another worker's recovery
    reclaimed it between the caller's last checkpoint and this call. The caller MUST treat
    None as "stop collecting immediately", the same way every other fenced write in this
    package is treated on a denied/lost claim."""
    return upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, candidate.research_source_id,
        candidate.raw_item.external_id, candidate.raw_item.title, candidate.raw_item.url,
        candidate.quality, now, snippet=candidate.raw_item.body,
        raw_metadata=candidate.raw_item.raw_metadata, preserve_existing_full_text=True)


class DeepFetchStatus(str, Enum):
    RESERVATION_DENIED = "reservation_denied"  # ceiling reached, or claim no longer active
    FETCH_FAILED = "fetch_failed"               # ArticleFetcher itself returned a FetchFailure
    EXTRACTION_UNUSABLE = "extraction_unusable"  # fetched fine, nothing extractable
    EXTRACTION_PARTIAL = "extraction_partial"    # extracted, but degraded (see PartialReason)
    EXTRACTION_COMPLETE = "extraction_complete"
    CLAIM_LOST = "claim_lost"  # reservation succeeded, but the claim was lost by write time --
                               # e.g. reclaimed by another worker's recovery mid-fetch; distinct
                               # from RESERVATION_DENIED because that failure is known BEFORE any
                               # I/O, while this one is only discovered AFTER the network fetch
                               # already ran


@dataclass(frozen=True)
class DeepFetchResult:
    status: DeepFetchStatus
    evidence_item: EvidenceItem
    fetch_failure_reason: FetchFailureReason | None = None
    extraction_reasons: tuple[PartialReason, ...] = ()


def reserve_and_deep_fetch(
    conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int,
    evidence_item: EvidenceItem, now: datetime, *, fetcher: ArticleFetcher,
) -> DeepFetchResult:
    """Reserves one of the run's 30 deep-fetch slots (transactionally, fenced on
    (run_id, claim_token) -- ADR-0010) BEFORE any I/O; only a successful reservation proceeds
    to call the injected, SSRF-safe `fetcher.fetch(evidence_item.url)`. A denied reservation
    (run at its 30-fetch ceiling, or this worker's claim is no longer the active one) returns
    RESERVATION_DENIED immediately, touching neither the network nor evidence_items -- the
    caller (orchestrator.py) already persisted the snippet-only Evidence Item beforehand via
    persist_candidate_snippet, so that evidence is retained either way (ADR-0010: "Partial
    full-text fetch retains snippet evidence").

    A FetchFailure or an UNUSABLE extraction likewise persists nothing further and returns the
    unchanged evidence_item -- its snippet/full_text stay exactly as already persisted. Only a
    PARTIAL or COMPLETE extraction re-upserts the same canonical row (same
    research_source_id/external_key, so citation_number is untouched) with the newly
    extracted, UNCAPPED full_text -- extract_full_article_text, never the capped
    extract_article_text, since durable evidence must never be truncated for an AI prompt's
    sake (that bounding happens separately and only at prompt-build time, in
    project_for_prompt).

    That final write goes through the claim-fenced
    `beehive.db.evidence_items.upsert_evidence_item_if_claimed` (preserve_existing_full_text
    left at its default False, since a completed/partial extraction IS the fresh full_text to
    write). The reservation above already confirmed the claim was active moments earlier, but
    the network fetch and extraction in between are exactly the window a lease can expire and
    get reclaimed by another worker's recovery -- so this write re-checks the claim itself,
    atomically, immediately before writing. If that fence rejects the write, CLAIM_LOST is
    returned with the unchanged (pre-fetch) evidence_item and zero further side effects: no
    row is touched, so whatever a newer claim already wrote (e.g. its own, fresher deep fetch)
    is left completely unclobbered by this stale worker."""
    reserved = reserve_deep_fetch(conn, run_id, claim_token, count=1)
    if not reserved:
        return DeepFetchResult(status=DeepFetchStatus.RESERVATION_DENIED, evidence_item=evidence_item)

    outcome = fetcher.fetch(evidence_item.url)
    if isinstance(outcome, FetchFailure):
        return DeepFetchResult(
            status=DeepFetchStatus.FETCH_FAILED, evidence_item=evidence_item,
            fetch_failure_reason=outcome.reason)

    assert isinstance(outcome, FetchedArticle)  # noqa: S101 -- FetchOutcome is exactly these two
    extraction = extract_full_article_text(outcome.html, transport_truncated=outcome.truncated)
    if extraction.quality is ExtractionQuality.UNUSABLE:
        return DeepFetchResult(
            status=DeepFetchStatus.EXTRACTION_UNUSABLE, evidence_item=evidence_item,
            extraction_reasons=extraction.reasons)

    updated_item = upsert_evidence_item_if_claimed(
        conn, run_id, claim_token, session_id, evidence_item.research_source_id,
        evidence_item.external_key, evidence_item.title, evidence_item.url,
        evidence_item.quality, now, snippet=evidence_item.snippet, full_text=extraction.text,
        raw_metadata=evidence_item.raw_metadata)
    if updated_item is None:
        return DeepFetchResult(
            status=DeepFetchStatus.CLAIM_LOST, evidence_item=evidence_item,
            extraction_reasons=extraction.reasons)

    status = (
        DeepFetchStatus.EXTRACTION_COMPLETE if extraction.quality is ExtractionQuality.COMPLETE
        else DeepFetchStatus.EXTRACTION_PARTIAL)
    return DeepFetchResult(status=status, evidence_item=updated_item, extraction_reasons=extraction.reasons)


__all__ = [
    "select_relevant_candidates",
    "project_for_prompt",
    "persist_candidate_snippet",
    "DeepFetchStatus",
    "DeepFetchResult",
    "reserve_and_deep_fetch",
]
