"""The durable Civic AI pipeline: screen-and-generate, then independent verification.

`beehive.civic.pipeline` decides whether a Candidate *may* be published. This module writes up
that decision in four languages and proves the write-up before anything can be sealed. It is the
crash-safe half of the AI stages: the models are called here, but nothing a model says is
believed until `beehive.civic.candidate_policy` has checked it mechanically and an independent
verifier has agreed.

The shape of one Candidate's work:

```text
qualification (publishable)      beehive.civic.pipeline
  -> build_candidate_snapshot    the immutable material both stages see
  -> generate                    one call: screening, plus all four locales when it passes
  -> deterministic checks        aliases, entities, numbers, dashes, locale parity
  -> verification                a fresh isolated call over the evidence and all four locales
  -> mark_candidate_verified     only on a supporting verdict
```

**Two calls per Candidate, because the budget is fixed.** Plan 7.4 allows 30 AI calls per Run and
plan 5.2 allows ten published Candidates in an election slot. Screening, drafting and three
localizations as separate calls would need 60. Generating the four locales together costs 20 for
an election slot and 12 for a normal one, which leaves the rest of the ceiling for the single
bounded retry each stage may take -- and makes the locales easier to keep in step, since one call
writes them with the canonical Chinese text in front of it.

Six properties are what make it safe to run twice, or twice at once:

* **Every stage is reserved before it is called and recorded after.** No model call is ever made
  with a transaction open, so a two-minute model call cannot hold SQLite's single write lock; and
  the reservation is committed first, so a crash costs an attempt rather than being refunded.
* **Replay is free within the active revision.** A matching success calls nothing. Snapshot,
  model, prompt or schema drift starts a bounded new revision and defers requalification instead
  of reusing stale output or terminating unrelated Candidates.
* **Two workers cannot run the same stage.** The reservation takes a partial unique index, so the
  loser reserves nothing and is told so, exactly like a lost Run claim elsewhere in this package.
* **A rejection settles.** A screening refusal, malformed output, a failed deterministic check
  and a verifier rejection all end with the Candidate REJECTED under an explicit code, and with a
  Civic Verification recorded when the verifier is what refused. A Candidate is never left in an
  unexplained state, because an unsettled Candidate blocks the seal.
* **Only a supporting verdict publishes.** `mark_candidate_verified` is the single path to
  VERIFIED and it re-checks both gates under its own lock. This module never writes that status
  itself, and a Candidate missing any of its four locales never reaches the verifier at all.
* **Running out of budget is not a verdict.** A Run that exhausts its 30 calls, or a stage whose
  single transient retry also fails, defers the Candidate to a later Run (DEFERRED_AI) with every
  receipt it earned intact. Rejecting it would destroy a publishable statement and permanently
  consume the source items behind it; calling it a capacity deferral would claim it was ready to
  publish. The carryover has a bounded lifetime so it cannot circle between Runs forever.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from beehive.ai.model_selection import DEFAULT_MODEL
from beehive.ai.structured_response import StructuredResponseError
from beehive.civic.ai_gateway import (CIVIC_MODEL_PROVIDER, CivicAiStage, CivicModelError,
                                      CivicModelGateway, CivicModelRequest,
                                      require_supported_model, verifier_model_for)
from beehive.civic.candidate_parser import (CivicGenerationResult, CivicLocaleContent,
                                            CivicScreeningVerdict, CivicVerifierReport,
                                            parse_generation_response,
                                            parse_verification_response)
from beehive.civic.candidate_policy import (CivicContentError, CivicContentIssue,
                                            check_candidate_content, check_generation_result,
                                            citation_evidence_ids, summary_lacks_substance)
from beehive.civic.generation import (CIVIC_PROMPT_VERSION, CIVIC_SCHEMA_VERSION,
                                      CivicCandidateSnapshot, CivicSnapshotError,
                                      build_candidate_snapshot, build_generation_prompt,
                                      build_verification_prompt, prompt_hash)
from beehive.civic.limits import (AI_CALL_TIMEOUT_SECONDS, MAX_AI_CALLS_PER_RUN,
                                  MAX_AI_STAGE_ATTEMPTS, REQUIRED_LOCALES, CivicLocale)
from beehive.db.civic_ai_stages import (CivicAiBudgetExhaustedError, CivicAiStageExhaustedError,
                                        CivicAiStageDriftError,
                                        CivicAiStageReceipt, find_recorded_stage,
                                        record_ai_stage_failure, record_ai_stage_success,
                                        reserve_ai_stage)
from beehive.db.civic_batches import (
    count_ai_deferrals,
    defer_candidate_for_ai,
    refresh_candidate_for_ai_if_claimed,
)
from beehive.db.civic_candidates import (
    get_candidate,
    mark_candidate_verified_if_claimed,
    reject_candidate_if_claimed,
)
from beehive.db.civic_qualifications import get_latest_qualification
from beehive.db.civic_verifications import record_verification_if_claimed
from beehive.domain.civic import (CivicCandidate, CivicCandidateStatus,
                                  CivicVerificationVerdict)

# The two calls one Candidate costs, in execution order. Counted here so a caller can plan a Run
# against the fixed budget rather than discovering it: six Candidates cost 12 calls, ten cost 20,
# and the plan's ceiling is 30.
AI_CALLS_PER_CANDIDATE = len(CivicAiStage)


class CivicAiPipelineError(ValueError):
    """A Candidate this module cannot work on: not in the Run, not qualified, not in VERIFYING."""


class CivicClaimLostError(RuntimeError):
    """The Run's claim moved on mid-pipeline. Nothing was written; stop, never retry blindly."""


@dataclass(frozen=True)
class CivicAiPipelineResult:
    """What one pass over a Candidate concluded."""

    candidate_id: int
    verified: bool
    candidate: CivicCandidate
    screening: CivicScreeningVerdict | None = None
    contents: tuple[CivicLocaleContent, ...] = ()
    verification: CivicVerifierReport | None = None
    rejection_code: str | None = None
    deferral_reason: str | None = None
    deferral_count: int = 0
    ai_calls_made: int = 0
    replayed_stages: tuple[CivicAiStage, ...] = ()

    @property
    def is_rejected(self) -> bool:
        return self.rejection_code is not None

    @property
    def is_deferred(self) -> bool:
        """Held for a later Run rather than judged: the evidence decision and every stage receipt
        this Candidate earned are still its own."""
        return self.deferral_reason is not None


@dataclass
class _StageRun:
    """Mutable bookkeeping for one pass: how many calls were really made, what was replayed."""

    calls: int = 0
    replayed: list[CivicAiStage] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.replayed is None:
            self.replayed = []


async def _run_stage(
    conn: sqlite3.Connection,
    *,
    snapshot: CivicCandidateSnapshot,
    run_id: int,
    claim_token: str,
    stage: CivicAiStage,
    prompt: str,
    model: str,
    gateway: CivicModelGateway,
    parse: Callable[[str], object],
    normalize: Callable[[object], object],
    now: datetime,
    now_fn: Callable[[], datetime] | None,
    tracker: _StageRun,
    timeout: float,
    call_budget: int = MAX_AI_CALLS_PER_RUN,
) -> object:
    """One stage, end to end: replay, or reserve, call without a lock, validate, record.

    **One retry, and only for a transient failure.** A `CivicModelError` -- a timeout, a
    transport failure, a provider error -- is not evidence about the Candidate, so the failed
    attempt is recorded and a second is reserved under exactly the same fencing and the same Run
    budget. A response that arrived and failed its schema, content or policy checks gets no
    second attempt: plan 7.8 makes that a rejection for this batch, and re-asking a model to
    correct itself is the     self-repair loop this pipeline deliberately does not have. A third attempt in the same
    revision and Run is impossible; later slots still obey the Candidate's deferral limit.

    Raises `CivicContentError` / `StructuredResponseError` for output that cannot be published,
    `CivicClaimLostError` when the Run's claim moved on, `CivicModelError` when the retry is also
    transiently lost, and `CivicAiStageExhaustedError` / `CivicAiBudgetExhaustedError` when a
    bound stops the retry from being reserved at all."""
    rendered_hash = prompt_hash(prompt)
    stored = find_recorded_stage(
        conn, candidate_id=snapshot.candidate_id, stage=stage,
        input_hash=snapshot.snapshot_hash, provider=CIVIC_MODEL_PROVIDER, model=model,
        prompt_version=CIVIC_PROMPT_VERSION, prompt_hash=rendered_hash,
        schema_version=CIVIC_SCHEMA_VERSION)
    if stored is not None:
        tracker.replayed.append(stage)
        return normalize(stored.output)

    transient: CivicModelError | None = None
    for _attempt in range(MAX_AI_STAGE_ATTEMPTS):
        reservation = reserve_ai_stage(
            conn, candidate_id=snapshot.candidate_id, run_id=run_id, claim_token=claim_token,
            stage=stage, provider=CIVIC_MODEL_PROVIDER, model=model,
            prompt_version=CIVIC_PROMPT_VERSION, prompt_hash=rendered_hash,
            schema_version=CIVIC_SCHEMA_VERSION, input_hash=snapshot.snapshot_hash, now=now,
            now_fn=now_fn, max_attempts=MAX_AI_STAGE_ATTEMPTS, call_budget=call_budget)
        if reservation is None:
            raise CivicClaimLostError(
                f"Civic Candidate {snapshot.candidate_id}: stage {stage.value} could not be "
                "reserved; the run claim moved on or another worker holds it")

        request = CivicModelRequest(
            stage=stage, prompt=prompt, model=model, timeout=timeout)
        tracker.calls += 1
        try:
            response = await gateway.complete(request)
        except CivicModelError as exc:
            if not _fail(conn, reservation, exc.error_code, exc.detail, now, now_fn):
                raise CivicClaimLostError(
                    f"Civic Candidate {snapshot.candidate_id}: stage {stage.value} failed after "
                    "its Run claim moved") from exc
            transient = exc
            continue
        except BaseException as exc:  # noqa: BLE001 -- re-raised; the reservation must be settled
            _fail(conn, reservation, "model_call_failed", repr(exc)[:200], now, now_fn)
            raise

        try:
            parsed = parse(response.text)
        except StructuredResponseError as exc:
            if not _fail(
                    conn, reservation, "schema_rejected", str(exc), now, now_fn):
                raise CivicClaimLostError(
                    f"Civic Candidate {snapshot.candidate_id}: stage {stage.value} response "
                    "arrived after its Run claim moved") from exc
            raise
        except CivicContentError as exc:
            if not _fail(
                    conn, reservation, exc.issue.value, exc.detail, now, now_fn):
                raise CivicClaimLostError(
                    f"Civic Candidate {snapshot.candidate_id}: stage {stage.value} response "
                    "arrived after its Run claim moved") from exc
            raise

        recorded = record_ai_stage_success(
            conn, reservation=reservation, output=_serialize(parsed), now=now, now_fn=now_fn)
        if recorded is None:
            raise CivicClaimLostError(
                f"Civic Candidate {snapshot.candidate_id}: stage {stage.value} finished but its "
                "claim had moved on, so nothing was recorded")
        return parsed

    raise transient if transient is not None else CivicClaimLostError(
        f"Civic Candidate {snapshot.candidate_id}: stage {stage.value} made no attempt")


def _fail(
    conn: sqlite3.Connection,
    reservation: CivicAiStageReceipt,
    error_code: str,
    detail: str,
    now: datetime,
    now_fn: Callable[[], datetime] | None,
) -> bool:
    """Settle a reservation that produced nothing publishable. Never masks the original failure:
    a claim that moved on simply leaves the reservation for recovery, and the caller still sees
    the exception that brought it here."""
    return record_ai_stage_failure(
        conn, reservation=reservation, error_code=error_code, error_detail=detail, now=now,
        now_fn=now_fn) is not None


def _serialize(parsed: object) -> object:
    """The normalized parsed output, as plain JSON data. Stored instead of the raw model text so
    a replay returns exactly what was validated, never something re-parsed later.

    One stage, one receipt: a `generate` response is stored whole, screening decision and all
    four locales together, because that is what one call actually produced. Splitting it into
    five rows would make the receipt history claim five model calls that never happened."""
    if isinstance(parsed, CivicGenerationResult):
        return {
            "decision": parsed.screening.decision.value,
            "editorial_exclusion": (
                parsed.screening.editorial_exclusion.value
                if parsed.screening.editorial_exclusion else None),
            "reason": parsed.screening.reason,
            "entry": None if parsed.contents is None else {
                locale.value: _serialize_content(parsed.contents[locale])
                for locale in REQUIRED_LOCALES
            },
        }
    if isinstance(parsed, CivicVerifierReport):
        return {
            "verdict": parsed.verdict.value,
            "issues": [
                {
                    "code": issue.code.value,
                    "locale": issue.locale.value if issue.locale else None,
                    "detail": issue.detail,
                }
                for issue in parsed.issues
            ],
        }
    raise CivicAiPipelineError(f"cannot serialize a {type(parsed).__name__} stage output")


def _serialize_content(content: CivicLocaleContent) -> dict:
    return {
        "title": content.title,
        "summary": content.summary,
        "event_date": content.event_date.isoformat(),
        "policy_status": content.policy_status.value,
        "uncertainty": content.uncertainty,
        "entities": list(content.entities),
        "claims": [
            {"text": claim.text, "citations": list(claim.citations)}
            for claim in content.claims
        ],
    }


def _restore_generation(payload: object) -> CivicGenerationResult:
    from beehive.civic.candidate_parser import (CivicEditorialExclusion, CivicScreeningDecision)

    data = dict(payload)  # type: ignore[arg-type]
    exclusion = data["editorial_exclusion"]
    verdict = CivicScreeningVerdict(
        decision=CivicScreeningDecision(data["decision"]),
        editorial_exclusion=(
            CivicEditorialExclusion(exclusion) if exclusion is not None else None),
        reason=data["reason"])
    entry = data["entry"]
    contents = None if entry is None else {
        locale: _restore_content(entry[locale.value], locale) for locale in REQUIRED_LOCALES
    }
    return CivicGenerationResult(screening=verdict, contents=contents)


def _restore_content(payload: object, locale: CivicLocale) -> CivicLocaleContent:
    from datetime import date

    from beehive.civic.candidate_parser import CivicDraftClaim, CivicPolicyStatus

    data = dict(payload)  # type: ignore[arg-type]
    return CivicLocaleContent(
        locale=locale,
        title=data["title"],
        summary=data["summary"],
        event_date=date.fromisoformat(data["event_date"]),
        policy_status=CivicPolicyStatus(data["policy_status"]),
        uncertainty=data["uncertainty"],
        entities=tuple(data["entities"]),
        claims=tuple(
            CivicDraftClaim(text=claim["text"], citations=tuple(claim["citations"]))
            for claim in data["claims"]))


def _restore_verification(payload: object) -> CivicVerifierReport:
    from beehive.civic.candidate_parser import (CivicVerifierIssue, CivicVerifierIssueCode,
                                                CivicVerifierVerdict)

    data = dict(payload)  # type: ignore[arg-type]
    return CivicVerifierReport(
        verdict=CivicVerifierVerdict(data["verdict"]),
        issues=tuple(
            CivicVerifierIssue(
                code=CivicVerifierIssueCode(issue["code"]),
                locale=CivicLocale(issue["locale"]) if issue["locale"] else None,
                detail=issue["detail"])
            for issue in data["issues"]))


async def generate_civic_candidate(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate_id: int,
    gateway: CivicModelGateway,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    model: str = DEFAULT_MODEL,
    verifier_model: str | None = None,
    timeout: float = AI_CALL_TIMEOUT_SECONDS,
    call_budget: int = MAX_AI_CALLS_PER_RUN,
) -> CivicAiPipelineResult:
    """Run both AI stages for one qualified Candidate and settle it.

    Two model calls: `generate` screens the material and writes all four locales, `verification`
    judges the result on a different model family with no knowledge of how it was produced. A
    Candidate whose stages are already on record replays them and calls nothing.

    The Candidate must be VERIFYING -- the status `beehive.civic.pipeline` leaves a qualified
    Candidate in, and the status a resumed AI carryover is adopted back into -- and must carry a
    publishable qualification. `verified` is True only when the verifier passed and
    `mark_candidate_verified` accepted. An editorial refusal or unpublishable output settles the
    Candidate REJECTED; an exhausted call budget or a transient failure that outlived its retry
    defers it to a later Run instead, because neither is a judgment about the statement.

    Raises `CivicClaimLostError` when the Run's claim moves on mid-pipeline (nothing further is
    written). Stale cached inputs, model, prompt or schema request a bounded fresh revision and
    requalification in a later slot; they do not constitute a negative content judgment.

    `call_budget` may only ever narrow the plan's fixed ceiling -- `reserve_ai_stage` refuses a
    larger one -- and exists so a worker can hold back headroom for the Candidates still queued
    behind this one."""
    model = require_supported_model(model)
    verifier = require_supported_model(verifier_model or verifier_model_for(model))

    candidate = get_candidate(conn, candidate_id)
    if candidate is None or candidate.run_id != run_id:
        raise CivicAiPipelineError(
            f"no Civic Candidate with id={candidate_id} in Civic Run {run_id}")
    if candidate.status is not CivicCandidateStatus.VERIFYING:
        raise CivicAiPipelineError(
            f"Civic Candidate {candidate_id} is {candidate.status.value}; only a qualified "
            "Candidate awaiting verification may be generated")
    from beehive.db.civic_evidence_captures import require_current_source_capture

    require_current_source_capture(conn, candidate_id, publication_only=True)
    qualification = get_latest_qualification(conn, candidate_id)
    if qualification is None:
        raise CivicAiPipelineError(
            f"Civic Candidate {candidate_id} has no recorded qualification")

    tracker = _StageRun()
    try:
        snapshot = build_candidate_snapshot(
            conn, candidate_id=candidate_id, qualification=qualification)
    except CivicSnapshotError as exc:
        return _reject(
            conn, candidate_id, run_id, claim_token, "snapshot_unusable", now, tracker,
            now_fn=now_fn, detail=str(exc))

    try:
        generated = await _run_stage(
            conn, snapshot=snapshot, run_id=run_id, claim_token=claim_token,
            stage=CivicAiStage.GENERATE, prompt=build_generation_prompt(snapshot), model=model,
            gateway=gateway, parse=parse_generation_response, normalize=_restore_generation,
            now=now, now_fn=now_fn, tracker=tracker, timeout=timeout, call_budget=call_budget)
        outcome = check_generation_result(snapshot, qualification, generated)
        if not outcome.accepted:
            return _reject(
                conn, candidate_id, run_id, claim_token, outcome.rejection_code, now, tracker,
                now_fn=now_fn,
                screening=generated.screening)

        contents = generated.contents or {}
        ordered = check_candidate_content(snapshot, contents)
        for content in ordered:
            citation_evidence_ids(snapshot, content)
        if summary_lacks_substance(snapshot, contents[CivicLocale.EN]):
            refreshed = _refresh_later(
                conn, candidate, run_id, claim_token, "summary_missing_substance", now,
                tracker, now_fn=now_fn)
            if refreshed is None:
                raise CivicClaimLostError(
                    f"Civic Candidate {candidate_id}: claim moved during AI refresh")
            return refreshed

        report = await _run_stage(
            conn, snapshot=snapshot, run_id=run_id, claim_token=claim_token,
            stage=CivicAiStage.VERIFICATION,
            prompt=build_verification_prompt(snapshot, contents), model=verifier,
            gateway=gateway, parse=parse_verification_response,
            normalize=_restore_verification, now=now, now_fn=now_fn, tracker=tracker,
            timeout=timeout, call_budget=call_budget)
    except CivicAiStageDriftError as exc:
        refreshed = _refresh_later(
            conn, candidate, run_id, claim_token, f"ai_stage_{exc.field}_changed", now,
            tracker, now_fn=now_fn)
        if refreshed is None:
            raise CivicClaimLostError(
                f"Civic Candidate {candidate_id}: claim moved during AI refresh") from exc
        return refreshed
    except CivicContentError as exc:
        return _reject(
            conn, candidate_id, run_id, claim_token, f"content_{exc.issue.value}", now,
            tracker, now_fn=now_fn, detail=exc.detail)
    except StructuredResponseError as exc:
        return _reject(
            conn, candidate_id, run_id, claim_token, "ai_schema_rejected", now, tracker,
            now_fn=now_fn, detail=str(exc))
    except CivicAiBudgetExhaustedError as exc:
        return _defer(
            conn, candidate_id, run_id, claim_token, "ai_budget_exhausted", now, tracker,
            now_fn=now_fn, detail=str(exc))
    except (CivicModelError, CivicAiStageExhaustedError) as exc:
        return _defer(
            conn, candidate_id, run_id, claim_token, "ai_transient_failed", now, tracker,
            now_fn=now_fn, detail=str(exc))

    if not report.is_pass:
        recorded = record_verification_if_claimed(
            conn, run_id=run_id, claim_token=claim_token, candidate_id=candidate_id,
            verdict=CivicVerificationVerdict.INSUFFICIENT, model=verifier, now=now,
            now_fn=now_fn, rationale=_rationale(report))
        if recorded is None:
            raise CivicClaimLostError(
                f"Civic Candidate {candidate_id}: verifier finished after its Run claim moved")
        return _reject(
            conn, candidate_id, run_id, claim_token, "verification_rejected", now, tracker,
            now_fn=now_fn,
            screening=generated.screening, contents=ordered, verification=report)

    recorded = record_verification_if_claimed(
        conn, run_id=run_id, claim_token=claim_token, candidate_id=candidate_id,
        verdict=CivicVerificationVerdict.SUPPORTED, model=verifier, now=now, now_fn=now_fn,
        rationale=_rationale(report))
    if recorded is None:
        raise CivicClaimLostError(
            f"Civic Candidate {candidate_id}: verifier finished after its Run claim moved")
    verified = mark_candidate_verified_if_claimed(
        conn, run_id=run_id, claim_token=claim_token, candidate_id=candidate_id, now=now,
        now_fn=now_fn)
    if verified is None:
        raise CivicClaimLostError(
            f"Civic Candidate {candidate_id}: Run claim moved before verification committed")
    return CivicAiPipelineResult(
        candidate_id=candidate_id, verified=True, candidate=verified,
        screening=generated.screening, contents=ordered, verification=report,
        ai_calls_made=tracker.calls, replayed_stages=tuple(tracker.replayed))


def _rationale(report: CivicVerifierReport) -> str:
    if report.is_pass:
        return "independent verifier passed all four locales"
    return "; ".join(
        f"{issue.code.value}"
        + (f"[{issue.locale.value}]" if issue.locale else "")
        + f": {issue.detail}"
        for issue in report.issues)[:500]


def _refresh_later(
    conn: sqlite3.Connection, candidate: CivicCandidate, run_id: int, claim_token: str,
    reason: str, now: datetime, tracker: _StageRun, *,
    now_fn: Callable[[], datetime] | None,
) -> CivicAiPipelineResult | None:
    """Open a fresh AI revision and carry the Candidate to a later slot; None if unclaimed.

    The next attempt regenerates from scratch instead of replaying this revision's receipts,
    within the bounded revision lifetime."""
    refreshed = refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run_id, claim_token=claim_token,
        expected_revision=candidate.ai_revision, reason=reason, now=now, now_fn=now_fn,
        defer=True)
    if refreshed is None:
        return None
    return CivicAiPipelineResult(
        candidate_id=candidate.id, verified=False, candidate=refreshed,
        rejection_code=refreshed.rejection_code,
        deferral_reason=(
            "ai_provenance_stale"
            if refreshed.status is CivicCandidateStatus.DEFERRED_AI else None),
        deferral_count=count_ai_deferrals(conn, candidate.id),
        ai_calls_made=tracker.calls, replayed_stages=tuple(tracker.replayed))


def _defer(
    conn: sqlite3.Connection,
    candidate_id: int,
    run_id: int,
    claim_token: str,
    reason: str,
    now: datetime,
    tracker: _StageRun,
    *,
    now_fn: Callable[[], datetime] | None = None,
    detail: str = "",
) -> CivicAiPipelineResult:
    """Hand the Candidate to a later Run instead of judging it.

    An exhausted AI budget and a transient failure that outlived its bounded retry are both
    facts about this Run, not about the statement. Rejecting the Candidate would destroy a
    publishable statement and permanently consume the source items behind it, and recording it as
    a capacity deferral would claim it was ready to publish. It becomes DEFERRED_AI, keeps every
    receipt it earned, and resumes at the first stage without one -- unless it has already been
    carried this way as often as `MAX_AI_DEFERRALS_PER_CANDIDATE` allows, at which point the
    bounded lifetime ends and it is settled REJECTED rather than circling forever."""
    result = defer_candidate_for_ai(
        conn, candidate_id=candidate_id, run_id=run_id, claim_token=claim_token, reason=reason,
        now=now, now_fn=now_fn)
    candidate = get_candidate(conn, candidate_id)
    if result is None:
        raise CivicClaimLostError(
            f"Civic Candidate {candidate_id}: {reason} could not be recorded; the run claim "
            f"moved on ({detail})")
    return CivicAiPipelineResult(
        candidate_id=candidate_id, verified=False, candidate=candidate,
        rejection_code=(f"{reason}_lifetime_exhausted" if result.exhausted else None),
        deferral_reason=None if result.exhausted else reason,
        deferral_count=result.deferral_count,
        ai_calls_made=tracker.calls, replayed_stages=tuple(tracker.replayed))


def _reject(
    conn: sqlite3.Connection,
    candidate_id: int,
    run_id: int,
    claim_token: str,
    rejection_code: str,
    now: datetime,
    tracker: _StageRun,
    *,
    now_fn: Callable[[], datetime] | None = None,
    detail: str = "",
    screening: CivicScreeningVerdict | None = None,
    contents: tuple[CivicLocaleContent, ...] = (),
    verification: CivicVerifierReport | None = None,
) -> CivicAiPipelineResult:
    """Settle the Candidate as rejected for this batch, explicitly.

    A Candidate that is already settled is left as it is: a worker that lost its claim may have
    been beaten to the settlement, and re-settling would either raise on an illegal transition or
    overwrite a decision someone else already recorded."""
    candidate = get_candidate(conn, candidate_id)
    if candidate is not None and not candidate.is_settled:
        candidate = reject_candidate_if_claimed(
            conn, run_id=run_id, claim_token=claim_token, candidate_id=candidate_id,
            rejection_code=rejection_code, now=now, now_fn=now_fn)
        if candidate is None:
            raise CivicClaimLostError(
                f"Civic Candidate {candidate_id}: rejection could not be recorded because its "
                "Run claim moved")
    return CivicAiPipelineResult(
        candidate_id=candidate_id, verified=False, candidate=candidate, screening=screening,
        contents=contents, verification=verification, rejection_code=rejection_code,
        ai_calls_made=tracker.calls, replayed_stages=tuple(tracker.replayed))


def stored_locale_contents(
    conn: sqlite3.Connection, candidate_id: int,
) -> dict[CivicLocale, CivicLocaleContent]:
    """The four locale entries recorded for a Candidate, rebuilt from its `generate` receipt.

    The receipt is the storage: what was validated is what was stored, so this never re-parses
    model text and never consults a model. A Candidate whose generation never succeeded, or whose
    generation refused it, simply has no entries here -- which is what makes
    `require_publishable_localization` able to refuse it."""
    from beehive.db.civic_ai_stages import get_stage_receipt

    receipt = get_stage_receipt(conn, candidate_id, CivicAiStage.GENERATE)
    if receipt is None:
        return {}
    result = _restore_generation(receipt.output)
    return dict(result.contents or {})


def require_publishable_localization(
    conn: sqlite3.Connection, candidate_id: int,
) -> tuple[CivicLocaleContent, ...]:
    """All four stored locales, or `CivicContentError`.

    The publication-side guard: a Candidate without a complete, recorded entry in every locale
    can never be published, whatever its verification history says."""
    from beehive.civic.candidate_policy import require_complete_localization

    contents = stored_locale_contents(conn, candidate_id)
    if not contents:
        raise CivicContentError(
            CivicContentIssue.LOCALE_MISSING,
            f"Civic Candidate {candidate_id} has no generated locales")
    return require_complete_localization(contents)


def next_incomplete_stage(
    conn: sqlite3.Connection, candidate_id: int,
) -> CivicAiStage | None:
    """The first stage with no recorded success, or None when the Candidate is fully generated.

    What a resumed AI carryover starts from: a Candidate deferred after its `generate` receipt
    was written resumes at the verifier, and its four locales are never regenerated."""
    from beehive.db.civic_ai_stages import get_stage_receipt

    for stage in CivicAiStage:
        if get_stage_receipt(conn, candidate_id, stage) is None:
            return stage
    return None


__all__ = [
    "AI_CALLS_PER_CANDIDATE",
    "CivicAiPipelineError",
    "CivicAiPipelineResult",
    "CivicClaimLostError",
    "generate_civic_candidate",
    "next_incomplete_stage",
    "require_publishable_localization",
    "stored_locale_contents",
]
