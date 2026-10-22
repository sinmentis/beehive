"""Prepare bounded replacement captures without mutating evidence or editorial history.

The integration owner must reserve the shared Run hydration budget before executing a plan.
Only a PREPARED result may be installed, atomically, through an append-only supersession API
that rechecks the Run lease, deadline, Batch seal, original capture fingerprint and AI revision.
Preparing captures is not installing them, refreshing AI receipts, or permission to publish.
Plan only after adopting unsealed carryover into the claimed BUILDING Batch.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from beehive.civic.documents import CivicDocument
from beehive.civic.production import (
    CivicHydrationResult,
    CivicHydrationStatus,
)
from beehive.civic.source_extraction import (
    has_current_article_capture,
    has_full_article_extraction,
    text_sha256,
)
from beehive.civic.source_provenance import has_current_official_capture
from beehive.connectors.civic_sources import PARTY_MEDIA_CATALOG
from beehive.domain.civic import (
    CivicBatch,
    CivicBatchStatus,
    CivicCandidate,
    CivicCandidateStatus,
    CivicEvidence,
)

MAX_SOURCE_REFRESH_DOCUMENTS = 25
_REFRESHABLE_STATES = frozenset({
    CivicCandidateStatus.DRAFTED,
    CivicCandidateStatus.RESEARCHING,
    CivicCandidateStatus.VERIFYING,
    CivicCandidateStatus.VERIFIED,
})


class EvidenceRefreshStatus(StrEnum):
    CURRENT = "current"
    PRESERVED = "preserved"
    PLANNED = "planned"
    PREPARED = "prepared"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class EvidenceCaptureInput:
    evidence: CivicEvidence
    source_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceRefreshTarget:
    evidence_id: int
    document: CivicDocument
    expected_evidence_fingerprint: str


@dataclass(frozen=True)
class EvidenceRefreshPlan:
    candidate_id: int
    batch_id: int
    run_id: int
    expected_ai_revision: int
    expected_capture_fingerprint: str
    status: EvidenceRefreshStatus
    targets: tuple[EvidenceRefreshTarget, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class PreparedEvidenceCapture:
    supersedes_evidence_id: int
    document: CivicDocument
    expected_evidence_fingerprint: str


@dataclass(frozen=True)
class EvidenceRefreshAttempt:
    evidence_id: int
    error_code: str | None


@dataclass(frozen=True)
class PreparedEvidenceRefresh:
    plan: EvidenceRefreshPlan
    status: EvidenceRefreshStatus
    captures: tuple[PreparedEvidenceCapture, ...] = ()
    attempts: tuple[EvidenceRefreshAttempt, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is not EvidenceRefreshStatus.PREPARED and self.captures:
            raise ValueError("only a fully prepared source refresh may offer replacement captures")
        if self.status is EvidenceRefreshStatus.PREPARED and (
            not self.captures or len(self.captures) != len(self.plan.targets)
        ):
            raise ValueError("source refresh must replace every planned capture together")
        if self.status is EvidenceRefreshStatus.PREPARED:
            targets = {target.evidence_id: target for target in self.plan.targets}
            if (
                len(targets) != len(self.plan.targets)
                or len({capture.supersedes_evidence_id for capture in self.captures})
                != len(self.captures)
            ):
                raise ValueError("source refresh needs distinct planned evidence identities")
            for capture in self.captures:
                target = targets.get(capture.supersedes_evidence_id)
                if (
                    target is None
                    or capture.expected_evidence_fingerprint != target.expected_evidence_fingerprint
                    or capture.document.dedupe_key != target.document.dedupe_key
                    or capture.document.canonical_url != target.document.canonical_url
                ):
                    raise ValueError("source refresh capture does not match its planned evidence")


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"unsupported source capture value: {type(value).__name__}")


def capture_fingerprint(captures: Sequence[EvidenceCaptureInput]) -> str:
    """Bind the proposed supersession to the complete original active evidence set."""
    encoded = json.dumps(
        [
            {
                "evidence": asdict(capture.evidence),
                "source_metadata": dict(capture.source_metadata),
            }
            for capture in sorted(captures, key=lambda item: item.evidence.id or 0)
        ],
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=_json_default,
    )
    return text_sha256(encoded)


def _document(capture: EvidenceCaptureInput) -> CivicDocument:
    evidence = capture.evidence
    prefix = f"{evidence.source_ref}:"
    if not evidence.external_key.startswith(prefix) or evidence.external_key == prefix:
        raise ValueError(f"Civic Evidence {evidence.id} has no source-namespaced identity")
    return CivicDocument(
        source_key=evidence.source_ref,
        external_key=evidence.external_key[len(prefix):],
        canonical_url=evidence.canonical_url or evidence.url,
        title=evidence.title,
        tier=evidence.tier,
        publisher=evidence.publisher,
        first_seen_at=evidence.captured_at,
        published_at=evidence.published_at,
        excerpt=evidence.excerpt,
        raw_metadata=dict(capture.source_metadata),
    )


def is_refreshable_evidence(evidence: CivicEvidence) -> bool:
    from beehive.civic.generation import is_citable

    return is_citable(
        replace(evidence, accessible_full_text=True),
        admissible_ids=frozenset({evidence.external_key}),
    )


def captured_refreshable_documents(
    captures: Sequence[EvidenceCaptureInput],
) -> tuple[CivicDocument, ...]:
    """Citable evidence whose current capture can witness its date.

    A listing date from an outlet whose articles are never captured cannot: the article it
    dates can never be cited."""
    documents = []
    for capture in captures:
        if not is_refreshable_evidence(capture.evidence):
            continue
        document = _document(capture)
        if has_current_article_capture(document) or has_current_official_capture(document):
            documents.append(document)
    return tuple(documents)


def has_capturable_evidence(captures: Sequence[EvidenceCaptureInput]) -> bool:
    """Whether any citable evidence is captured or could ever be captured.

    False when every source is an outlet whose articles are never captured (Stuff, The Post):
    such a Candidate can never pass the source date gate, so holding it only wastes Runs."""
    for capture in captures:
        if not is_refreshable_evidence(capture.evidence):
            continue
        document = _document(capture)
        if has_current_article_capture(document) or has_current_official_capture(document):
            return True
        if document.source_key in PARTY_MEDIA_CATALOG.keys:
            entry = PARTY_MEDIA_CATALOG.get(document.source_key)
            if entry.may_be_evidence and entry.article_capture:
                return True
    return False


def plan_evidence_refresh(
    candidate: CivicCandidate,
    batch: CivicBatch,
    captures: Sequence[EvidenceCaptureInput],
    *,
    remaining_hydrations: int,
) -> EvidenceRefreshPlan:
    if not 0 <= remaining_hydrations <= MAX_SOURCE_REFRESH_DOCUMENTS:
        raise ValueError("remaining source hydration budget must be between 0 and 25")
    if candidate.id is None or batch.id is None:
        raise ValueError("source refresh requires persisted Candidate and Batch identities")
    if (
        candidate.batch_id != batch.id or candidate.run_id != batch.run_id
        or candidate.watch_id != batch.watch_id
    ):
        raise ValueError("source refresh Candidate does not belong to the supplied Batch")
    base = EvidenceRefreshPlan(
        candidate_id=candidate.id,
        batch_id=batch.id,
        run_id=batch.run_id,
        expected_ai_revision=candidate.ai_revision,
        expected_capture_fingerprint=capture_fingerprint(captures),
        status=EvidenceRefreshStatus.PRESERVED,
        reason="source_refresh_immutable",
    )
    if (
        batch.status is not CivicBatchStatus.BUILDING
        or candidate.sealed_into_batch_at is not None
        or candidate.status not in _REFRESHABLE_STATES
    ):
        return base
    if not captures:
        return replace(
            base, status=EvidenceRefreshStatus.DEFERRED, reason="source_refresh_evidence_missing",
        )
    ids = [capture.evidence.id for capture in captures]
    if None in ids or len(set(ids)) != len(ids) or any(
        capture.evidence.candidate_id != candidate.id for capture in captures
    ):
        raise ValueError("source refresh needs distinct persisted evidence owned by its Candidate")
    targets: list[EvidenceRefreshTarget] = []
    for capture in sorted(captures, key=lambda item: item.evidence.id or 0):
        evidence_id = capture.evidence.id
        if evidence_id is None or evidence_id <= 0:
            raise ValueError("source refresh needs a positive persisted evidence identity")
        # A missing body is refreshable, but a link/type the snapshot cannot cite must not
        # block a valid neighbour. Keep every row in the complete-set fingerprint above.
        if not is_refreshable_evidence(capture.evidence):
            continue
        document = _document(capture)
        if has_current_article_capture(document) or has_current_official_capture(document):
            continue
        if (
            document.source_key not in PARTY_MEDIA_CATALOG.keys
            or not PARTY_MEDIA_CATALOG.get(document.source_key).may_be_evidence
        ):
            return replace(
                base, status=EvidenceRefreshStatus.DEFERRED,
                reason=f"source_refresh_unsupported:{document.source_key}",
            )
        if not PARTY_MEDIA_CATALOG.get(document.source_key).article_capture:
            # Its article pages carry no text: never fetch, never block a capturable neighbour.
            continue
        # Legacy persisted publication values may be promoted lastmod. Never let the hydrator
        # use them as its connector-published fallback, even when the old excerpt is nonempty.
        targets.append(EvidenceRefreshTarget(
            evidence_id=evidence_id,
            document=replace(document, published_at=None, raw_metadata={}),
            expected_evidence_fingerprint=capture_fingerprint((capture,)),
        ))
    if not targets:
        return replace(base, status=EvidenceRefreshStatus.CURRENT, reason=None)
    if len(targets) > remaining_hydrations:
        return replace(
            base, status=EvidenceRefreshStatus.DEFERRED,
            reason="source_refresh_hydration_limit",
        )
    return replace(
        base, status=EvidenceRefreshStatus.PLANNED, targets=tuple(targets), reason=None,
    )


async def prepare_evidence_refresh(
    plan: EvidenceRefreshPlan,
    *,
    hydrate: Callable[[CivicDocument, datetime], Awaitable[CivicHydrationResult]],
    now_fn: Callable[[], datetime],
    deadline_at: datetime,
) -> PreparedEvidenceRefresh:
    """Refetch only a reserved plan. The caller keeps its lease alive around this await."""
    if plan.status is not EvidenceRefreshStatus.PLANNED:
        return PreparedEvidenceRefresh(plan, plan.status, reason=plan.reason)
    if not 1 <= len(plan.targets) <= MAX_SOURCE_REFRESH_DOCUMENTS:
        raise ValueError("invalid bounded source refresh plan")
    if deadline_at.tzinfo is None:
        raise ValueError("source refresh deadline must be timezone-aware")
    captures: list[PreparedEvidenceCapture] = []
    attempts: list[EvidenceRefreshAttempt] = []
    for target in plan.targets:
        now = now_fn()
        if now >= deadline_at:
            return PreparedEvidenceRefresh(
                plan, EvidenceRefreshStatus.DEFERRED,
                attempts=tuple(attempts), reason="source_refresh_deadline",
            )
        result = await hydrate(target.document, now)
        if not isinstance(result, CivicHydrationResult):
            raise TypeError("source refresh hydrator returned an invalid result")
        if (
            result.document.dedupe_key != target.document.dedupe_key
            or result.document.canonical_url != target.document.canonical_url
        ):
            raise ValueError("source refresh changed the captured article identity")
        if result.status is not CivicHydrationStatus.HYDRATED:
            reason = result.error_code or "source_refresh_not_hydrated"
            attempts.append(EvidenceRefreshAttempt(target.evidence_id, reason))
            return PreparedEvidenceRefresh(
                plan, EvidenceRefreshStatus.DEFERRED,
                attempts=tuple(attempts), reason=reason,
            )
        if not has_full_article_extraction(result.document):
            raise ValueError("source refresh result has no full-article extraction provenance")
        attempts.append(EvidenceRefreshAttempt(target.evidence_id, None))
        captures.append(PreparedEvidenceCapture(
            target.evidence_id, result.document, target.expected_evidence_fingerprint,
        ))
    if now_fn() >= deadline_at:
        return PreparedEvidenceRefresh(
            plan, EvidenceRefreshStatus.DEFERRED,
            attempts=tuple(attempts), reason="source_refresh_deadline",
        )
    return PreparedEvidenceRefresh(
        plan, EvidenceRefreshStatus.PREPARED, tuple(captures), tuple(attempts),
    )
