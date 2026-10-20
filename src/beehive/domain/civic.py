"""Pure Beehive Civic domain types and state-transition rules.

No sqlite3, no I/O, no connectors, no prompts: every type here is a frozen, self-validating
dataclass and every rule is a table of legal transitions. `beehive.db.civic_*` loads rows back
into these dataclasses, so a row that violates an invariant here can never be observed as a
valid domain object -- the schema's own CHECK constraints mirror each rule (see schema.sql's
civic_* section) so the two layers agree rather than one silently permitting what the other
rejects.

Three durability rules shape almost every type below, and all three are stated once here so the
repository Modules do not each re-invent them:

* **Lease vs. deadline.** A Civic Run carries a short worker lease (claim_token/
  lease_expires_at) AND a fixed overall deadline_at, exactly like a Research Run. The lease says
  "one worker owns this right now"; the deadline says "this run's total wall-clock budget across
  every attempt". A lease may never be granted or renewed past the deadline, so a crash-and-retry
  cycle buys more attempts, never more total time.
* **Sealed means immutable.** A Civic Batch's content_hash is computed once, at seal, over the
  Candidates it contains. The canonical destination projection later binds a separate
  payload_hash once. Delivery, retry, quarantine and receipt replay compare both identities.
* **Publication requires evidence.** A Civic Candidate can only reach VERIFIED through a
  Civic Verification, and a Civic Verification can only be SUPPORTED with corroborating Civic
  Evidence behind it. The counts are validated here; which rows exist is the repository's job.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

# A Civic Candidate may only be published once its statement is supported by evidence at or
# above this tier, or by two independent lower-tier sources (`CivicCandidate.is_corroborated`).
# Ordered strongest-first; the index in this tuple IS the tier's rank.
_EVIDENCE_TIER_ORDER: tuple[str, ...] = (
    "official", "party_primary", "reputable_media", "other_media", "aggregator")

# Tiers that stand alone: one such source is enough to consider a statement sourced.
_SELF_SUFFICIENT_TIERS = frozenset({"official", "party_primary"})

# How many independent lower-tier sources substitute for one self-sufficient source.
_CORROBORATION_REQUIRED = 2


class CivicWatchStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class CivicVocabularyStatus(str, Enum):
    """A Civic Vocabulary version's lifecycle. Only PUBLISHED versions may be referenced by a
    Civic Run; DRAFT is still being edited, SUPERSEDED has been replaced by a later version but
    is kept forever so an already-published Civic Batch stays reproducible."""
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class CivicTermRole(str, Enum):
    """What a Civic Term does to matching material. REQUIRED terms gate admission (material
    matching none of them is out of scope), OPTIONAL terms only inform ranking, and EXCLUDED
    terms veto -- a veto always wins over any number of matches."""
    REQUIRED = "required"
    OPTIONAL = "optional"
    EXCLUDED = "excluded"


class CivicTermCategory(str, Enum):
    PARTY = "party"
    PERSON = "person"
    TOPIC = "topic"
    PLACE = "place"
    ORGANISATION = "organisation"


class CivicRunStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CivicRunPhase(str, Enum):
    GATHERING = "gathering"
    SCREENING = "screening"
    RESEARCHING = "researching"
    VERIFYING = "verifying"
    PUBLISHING = "publishing"


class CivicBatchStatus(str, Enum):
    """A Civic Batch's envelope lifecycle. BUILDING accepts Candidates; SEALED has a fixed
    content_hash and accepts none; DELIVERING is claimed by a delivery worker; DELIVERED is
    terminal and receipted; QUARANTINED is terminal and requires Owner action (never retried
    automatically); FAILED is terminal after the retry budget is exhausted."""
    BUILDING = "building"
    SEALED = "sealed"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class CivicCandidateStatus(str, Enum):
    """A Civic Candidate's lifecycle.

    Two states are settled without being final, and they are settled for different reasons:

    * DEFERRED_CAPACITY -- the Candidate was VERIFIED and ready, and the only thing that kept it
      out of today's envelope was the daily publication cap. A later Run with room adopts it
      straight back to VERIFIED: no requalification, no second Candidate for the same
      development.
    * DEFERRED_AI -- the Candidate was qualified and its generation was still unfinished when the
      Run ran out of its fixed AI call budget, or a transient model failure outlived its bounded
      retry. It is emphatically not a judgment: the evidence decision stands, the AI stage
      receipts it already earned stand, and a later Run adopts it back to VERIFYING to resume at
      the first stage that has no receipt. Recording that as a rejection would destroy a
      publishable statement for a reason that has nothing to do with it, and recording it as
      DEFERRED_CAPACITY would claim it was ready to publish when it was not."""
    DRAFTED = "drafted"
    RESEARCHING = "researching"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    DEFERRED_CAPACITY = "deferred_capacity"
    DEFERRED_AI = "deferred_ai"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class CivicEvidenceTier(str, Enum):
    OFFICIAL = "official"
    PARTY_PRIMARY = "party_primary"
    REPUTABLE_MEDIA = "reputable_media"
    OTHER_MEDIA = "other_media"
    AGGREGATOR = "aggregator"

    @property
    def rank(self) -> int:
        """0 is strongest. Comparable across tiers so callers never hard-code an ordering."""
        return _EVIDENCE_TIER_ORDER.index(self.value)

    @property
    def is_self_sufficient(self) -> bool:
        return self.value in _SELF_SUFFICIENT_TIERS


class CivicEvidenceStance(str, Enum):
    SUPPORTING = "supporting"
    CORROBORATING = "corroborating"
    CONTRADICTING = "contradicting"
    CONTEXT = "context"


class CivicResearchOutcome(str, Enum):
    """Result of one bounded extra research pass on an under-evidenced Civic Candidate."""
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    ERROR = "error"


class CivicVerificationVerdict(str, Enum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"
    ERROR = "error"


class CivicNotificationStatus(str, Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class CivicDeliveryChannel(str, Enum):
    SPOOL = "spool"
    EMAIL = "email"


_TERMINAL_RUN_STATUSES = frozenset({
    CivicRunStatus.COMPLETED, CivicRunStatus.CANCELLED, CivicRunStatus.FAILED})
_TERMINAL_BATCH_STATUSES = frozenset({
    CivicBatchStatus.DELIVERED, CivicBatchStatus.QUARANTINED, CivicBatchStatus.FAILED})
# "Terminal" here has always meant *settled for its slot*, not "can never move again": VERIFIED
# is in this set and still moves to SUPERSEDED when tomorrow publishes a correction. It is what
# `settled_at` is keyed on and what the Civic Batch seal requires of every Candidate before it
# will freeze an envelope. DEFERRED_CAPACITY belongs here for exactly that reason -- a Candidate
# held back by the daily cap is finished with today's Run -- while still being adoptable
# tomorrow. Use `is_final_candidate_status` for the genuinely dead-end states.
_TERMINAL_CANDIDATE_STATUSES = frozenset({
    CivicCandidateStatus.VERIFIED, CivicCandidateStatus.DEFERRED_CAPACITY,
    CivicCandidateStatus.DEFERRED_AI, CivicCandidateStatus.REJECTED,
    CivicCandidateStatus.SUPERSEDED})
# Statuses with no outgoing transition at all: nothing can be done with these Candidates again.
_FINAL_CANDIDATE_STATUSES = frozenset({
    CivicCandidateStatus.REJECTED, CivicCandidateStatus.SUPERSEDED})
# The statuses a Candidate that once reached VERIFIED can hold. Only these may carry a
# `sealed_into_batch_at` stamp, which is what keeps a sealed Civic Batch's membership immutable
# across a later correction (VERIFIED -> SUPERSEDED).
_PUBLISHABLE_CANDIDATE_STATUSES = frozenset({
    CivicCandidateStatus.VERIFIED, CivicCandidateStatus.SUPERSEDED})
_TERMINAL_NOTIFICATION_STATUSES = frozenset({
    CivicNotificationStatus.SENT, CivicNotificationStatus.FAILED,
    CivicNotificationStatus.SUPPRESSED})

# A retired Civic Watch is deliberately terminal: retiring is how an Owner permanently ends a
# commitment, and re-activating it would silently resume publication under a subject the Owner
# already withdrew. A new Watch is created instead.
_WATCH_TRANSITIONS = {
    CivicWatchStatus.ACTIVE: frozenset({CivicWatchStatus.PAUSED, CivicWatchStatus.RETIRED}),
    CivicWatchStatus.PAUSED: frozenset({CivicWatchStatus.ACTIVE, CivicWatchStatus.RETIRED}),
    CivicWatchStatus.RETIRED: frozenset(),
}

# A published Civic Vocabulary version is never edited back to draft: an already-delivered Civic
# Batch was screened against exactly these terms, so the only forward move is being superseded
# by a later version.
_VOCABULARY_TRANSITIONS = {
    CivicVocabularyStatus.DRAFT: frozenset({CivicVocabularyStatus.PUBLISHED}),
    CivicVocabularyStatus.PUBLISHED: frozenset({CivicVocabularyStatus.SUPERSEDED}),
    CivicVocabularyStatus.SUPERSEDED: frozenset(),
}

# PROCESSING -> PENDING is recovery requeueing an expired lease, exactly like a Research Run.
_RUN_TRANSITIONS = {
    CivicRunStatus.PENDING: frozenset({CivicRunStatus.PROCESSING, CivicRunStatus.CANCELLED,
                                       CivicRunStatus.FAILED}),
    CivicRunStatus.PROCESSING: frozenset({CivicRunStatus.COMPLETED, CivicRunStatus.CANCELLED,
                                          CivicRunStatus.FAILED, CivicRunStatus.PENDING}),
    CivicRunStatus.COMPLETED: frozenset(),
    CivicRunStatus.CANCELLED: frozenset(),
    CivicRunStatus.FAILED: frozenset(),
}

# A Civic Run's phases only ever move forward. Re-entering an earlier phase after a crash would
# repeat work whose output is already persisted (and, for PUBLISHING, could mint a second Batch).
_RUN_PHASE_ORDER = (CivicRunPhase.GATHERING, CivicRunPhase.SCREENING, CivicRunPhase.RESEARCHING,
                    CivicRunPhase.VERIFYING, CivicRunPhase.PUBLISHING)

# DELIVERING -> SEALED is a released claim (expired lease reclaimed, or a retryable delivery
# failure returning the envelope to the queue). SEALED -> QUARANTINED lets an Owner or an
# integrity check pull a sealed envelope out of the queue before it is ever delivered.
_BATCH_TRANSITIONS = {
    CivicBatchStatus.BUILDING: frozenset({CivicBatchStatus.SEALED, CivicBatchStatus.FAILED}),
    CivicBatchStatus.SEALED: frozenset({CivicBatchStatus.DELIVERING,
                                        CivicBatchStatus.QUARANTINED, CivicBatchStatus.FAILED}),
    CivicBatchStatus.DELIVERING: frozenset({CivicBatchStatus.DELIVERED, CivicBatchStatus.SEALED,
                                            CivicBatchStatus.QUARANTINED,
                                            CivicBatchStatus.FAILED}),
    CivicBatchStatus.DELIVERED: frozenset(),
    CivicBatchStatus.QUARANTINED: frozenset(),
    CivicBatchStatus.FAILED: frozenset(),
}

# VERIFIED is terminal-but-supersedable: a later Civic Run that publishes a correction marks the
# earlier Candidate SUPERSEDED rather than editing it, so the published record stays append-only.
_CANDIDATE_TRANSITIONS = {
    CivicCandidateStatus.DRAFTED: frozenset({CivicCandidateStatus.RESEARCHING,
                                             CivicCandidateStatus.VERIFYING,
                                             CivicCandidateStatus.REJECTED}),
    CivicCandidateStatus.RESEARCHING: frozenset({CivicCandidateStatus.VERIFYING,
                                                 CivicCandidateStatus.REJECTED}),
    # VERIFYING -> DEFERRED_AI is the Run's AI budget or a transient model failure ending this
    # slot's attempt at generating the Candidate, and DEFERRED_AI -> VERIFYING is a later Run
    # resuming it. Reversible for the same reason the capacity pair is: neither reason has
    # anything to do with whether the statement is publishable.
    CivicCandidateStatus.VERIFYING: frozenset({CivicCandidateStatus.VERIFIED,
                                               CivicCandidateStatus.REJECTED,
                                               CivicCandidateStatus.RESEARCHING,
                                               CivicCandidateStatus.DEFERRED_AI}),
    # VERIFIED -> DEFERRED_CAPACITY is the daily cap holding a ready Candidate back, and
    # DEFERRED_CAPACITY -> VERIFIED is a later Run adopting it. The pair is deliberately
    # reversible: capacity is the one reason for not publishing that a later day simply fixes.
    # A carryover may also be REJECTED outright, so an Owner can drop one that has gone stale
    # rather than being forced to publish it eventually.
    CivicCandidateStatus.VERIFIED: frozenset({CivicCandidateStatus.SUPERSEDED,
                                              CivicCandidateStatus.DEFERRED_CAPACITY}),
    CivicCandidateStatus.DEFERRED_CAPACITY: frozenset({CivicCandidateStatus.VERIFIED,
                                                       CivicCandidateStatus.REJECTED}),
    # A resumed Candidate goes back to VERIFYING, not to VERIFIED: it still has generation
    # stages to finish and an independent verifier to face. REJECTED is the bounded lifetime,
    # for a Candidate that has been deferred as often as the pipeline allows.
    CivicCandidateStatus.DEFERRED_AI: frozenset({CivicCandidateStatus.VERIFYING,
                                                 CivicCandidateStatus.REJECTED}),
    CivicCandidateStatus.REJECTED: frozenset(),
    CivicCandidateStatus.SUPERSEDED: frozenset(),
}

# SENDING -> PENDING is recovery requeueing an expired send lease.
_NOTIFICATION_TRANSITIONS = {
    CivicNotificationStatus.PENDING: frozenset({CivicNotificationStatus.SENDING,
                                                CivicNotificationStatus.SUPPRESSED,
                                                CivicNotificationStatus.FAILED}),
    CivicNotificationStatus.SENDING: frozenset({CivicNotificationStatus.SENT,
                                                CivicNotificationStatus.PENDING,
                                                CivicNotificationStatus.FAILED,
                                                CivicNotificationStatus.SUPPRESSED}),
    CivicNotificationStatus.SENT: frozenset(),
    CivicNotificationStatus.FAILED: frozenset(),
    CivicNotificationStatus.SUPPRESSED: frozenset(),
}


def require_watch_transition(current: CivicWatchStatus, target: CivicWatchStatus) -> None:
    if target not in _WATCH_TRANSITIONS[current]:
        raise ValueError(f"invalid Civic Watch transition: {current.value} -> {target.value}")


def require_vocabulary_transition(
    current: CivicVocabularyStatus, target: CivicVocabularyStatus,
) -> None:
    if target not in _VOCABULARY_TRANSITIONS[current]:
        raise ValueError(
            f"invalid Civic Vocabulary transition: {current.value} -> {target.value}")


def require_run_transition(current: CivicRunStatus, target: CivicRunStatus) -> None:
    if target not in _RUN_TRANSITIONS[current]:
        raise ValueError(f"invalid Civic Run transition: {current.value} -> {target.value}")


def require_run_phase_advance(current: CivicRunPhase, target: CivicRunPhase) -> None:
    """Civic Run phases only move forward, and never to themselves -- re-entering a phase would
    redo work whose output is already committed."""
    if _RUN_PHASE_ORDER.index(target) <= _RUN_PHASE_ORDER.index(current):
        raise ValueError(
            f"invalid Civic Run phase advance: {current.value} -> {target.value}")


def require_batch_transition(current: CivicBatchStatus, target: CivicBatchStatus) -> None:
    if target not in _BATCH_TRANSITIONS[current]:
        raise ValueError(f"invalid Civic Batch transition: {current.value} -> {target.value}")


def require_candidate_transition(
    current: CivicCandidateStatus, target: CivicCandidateStatus,
) -> None:
    if target not in _CANDIDATE_TRANSITIONS[current]:
        raise ValueError(
            f"invalid Civic Candidate transition: {current.value} -> {target.value}")


def require_candidate_publication_rejection(
    candidate: CivicCandidate, batch: CivicBatch,
) -> None:
    """Publication preflight may reject VERIFIED work only before its immutable seal.

    Verification is an evidence judgment, not a promise that the destination can represent it.
    Keep this transition separate from generic rejection, which must still refuse published work.
    """
    if (candidate.status is not CivicCandidateStatus.VERIFIED
            or candidate.sealed_into_batch_at is not None
            or batch.status is not CivicBatchStatus.BUILDING
            or candidate.batch_id != batch.id
            or candidate.run_id != batch.run_id
            or candidate.watch_id != batch.watch_id):
        raise ValueError("publication rejection requires an unsealed VERIFIED Candidate")


def require_candidate_ai_refresh(candidate: CivicCandidate, batch: CivicBatch) -> None:
    if (candidate.status not in {CivicCandidateStatus.VERIFIED, CivicCandidateStatus.VERIFYING}
            or candidate.sealed_into_batch_at is not None
            or batch.status is not CivicBatchStatus.BUILDING
            or (candidate.batch_id, candidate.run_id, candidate.watch_id)
            != (batch.id, batch.run_id, batch.watch_id)):
        raise ValueError("AI refresh requires an unsealed VERIFIED or VERIFYING Candidate")


def require_candidate_ai_deferral(candidate: CivicCandidate, batch: CivicBatch) -> None:
    if (candidate.status in {CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING}
            and candidate.ai_revision > 0 and candidate.ai_refresh_reason
            and candidate.sealed_into_batch_at is None
            and batch.status is CivicBatchStatus.BUILDING
            and (candidate.batch_id, candidate.run_id, candidate.watch_id)
            == (batch.id, batch.run_id, batch.watch_id)):
        return
    require_candidate_transition(candidate.status, CivicCandidateStatus.DEFERRED_AI)


def require_candidate_refresh_adoption(
    candidate: CivicCandidate, target: CivicCandidateStatus,
) -> None:
    if (candidate.status is not CivicCandidateStatus.DEFERRED_AI
            or not candidate.ai_refresh_reason
            or (candidate.ai_revision <= 0
                and not candidate.ai_refresh_reason.startswith("source_refresh:"))
            or candidate.sealed_into_batch_at is not None
            or target not in {CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING}):
        raise ValueError("refresh adoption requires an unsealed AI revision awaiting qualification")


def require_candidate_source_hold(candidate: CivicCandidate, batch: CivicBatch) -> None:
    if (candidate.status not in {
            CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING,
            CivicCandidateStatus.VERIFYING, CivicCandidateStatus.VERIFIED,
            CivicCandidateStatus.DEFERRED_AI,
        } or candidate.sealed_into_batch_at is not None
            or batch.status is not CivicBatchStatus.BUILDING
            or (candidate.batch_id, candidate.run_id, candidate.watch_id)
            != (batch.id, batch.run_id, batch.watch_id)):
        raise ValueError("source hold requires a current unsealed Candidate")


def require_candidate_source_refresh(candidate: CivicCandidate, batch: CivicBatch) -> None:
    require_candidate_source_hold(candidate, batch)
    if candidate.status is CivicCandidateStatus.DEFERRED_AI:
        raise ValueError("source refresh requires an active unsealed Candidate")


def require_notification_transition(
    current: CivicNotificationStatus, target: CivicNotificationStatus,
) -> None:
    if target not in _NOTIFICATION_TRANSITIONS[current]:
        raise ValueError(
            f"invalid Civic Notification transition: {current.value} -> {target.value}")


def is_terminal_run_status(status: CivicRunStatus) -> bool:
    return status in _TERMINAL_RUN_STATUSES


def is_terminal_batch_status(status: CivicBatchStatus) -> bool:
    return status in _TERMINAL_BATCH_STATUSES


def is_terminal_candidate_status(status: CivicCandidateStatus) -> bool:
    return status in _TERMINAL_CANDIDATE_STATUSES


def is_final_candidate_status(status: CivicCandidateStatus) -> bool:
    """Whether the Candidate has no move left at all. Narrower than
    `is_terminal_candidate_status`, which means "settled for its slot" and includes VERIFIED (it
    can still be superseded), DEFERRED_CAPACITY (it can still be adopted) and DEFERRED_AI (it can
    still be resumed)."""
    return status in _FINAL_CANDIDATE_STATUSES


def is_terminal_notification_status(status: CivicNotificationStatus) -> bool:
    return status in _TERMINAL_NOTIFICATION_STATUSES


def normalize_term(term: str) -> str:
    """The comparison form of a Civic Term: case-folded, whitespace-collapsed. Two terms with
    the same normalized form are the same term, which is what the per-version uniqueness
    constraint is enforced on."""
    return " ".join(term.casefold().split())


@dataclass(frozen=True)
class CivicWatch:
    """The Owner's recurring commitment to one civic subject, on one daily slot."""
    id: int | None
    subject: str
    question: str
    status: CivicWatchStatus
    schedule_timezone: str
    schedule_time: str
    created_at: datetime
    paused_at: datetime | None = None
    retired_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("Civic Watch subject must not be empty")
        if not self.question.strip():
            raise ValueError("Civic Watch question must not be empty")
        if not self.schedule_timezone.strip():
            raise ValueError("Civic Watch needs a schedule timezone")
        _require_hh_mm(self.schedule_time, "Civic Watch schedule_time")
        _require_stamp_matches_status(
            self.status is CivicWatchStatus.PAUSED, self.paused_at, "paused Civic Watch",
            "paused_at")
        _require_stamp_matches_status(
            self.status is CivicWatchStatus.RETIRED, self.retired_at, "retired Civic Watch",
            "retired_at")

    @property
    def is_schedulable(self) -> bool:
        return self.status is CivicWatchStatus.ACTIVE


@dataclass(frozen=True)
class CivicVocabularyVersion:
    """One immutable, numbered Civic Vocabulary. content_hash pins exactly which terms were in
    force, so a Civic Batch can name the vocabulary it was screened against and a later reader
    can prove that vocabulary has not been edited underneath it."""
    id: int | None
    watch_id: int
    version: int
    status: CivicVocabularyStatus
    content_hash: str
    created_at: datetime
    published_at: datetime | None = None
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.watch_id <= 0 or self.version <= 0:
            raise ValueError("Civic Vocabulary identifiers must be positive")
        if self.status is CivicVocabularyStatus.DRAFT:
            if self.content_hash:
                raise ValueError("draft Civic Vocabulary cannot carry a content_hash")
        elif not self.content_hash:
            raise ValueError(
                f"{self.status.value} Civic Vocabulary needs a content_hash")
        _require_stamp_matches_status(
            self.status is not CivicVocabularyStatus.DRAFT, self.published_at,
            f"{self.status.value} Civic Vocabulary", "published_at")
        _require_stamp_matches_status(
            self.status is CivicVocabularyStatus.SUPERSEDED, self.superseded_at,
            "superseded Civic Vocabulary", "superseded_at")


@dataclass(frozen=True)
class CivicTerm:
    id: int | None
    vocabulary_version_id: int
    term: str
    normalized_term: str
    category: CivicTermCategory
    role: CivicTermRole

    def __post_init__(self) -> None:
        if self.vocabulary_version_id <= 0:
            raise ValueError("vocabulary_version_id must be positive")
        if not self.term.strip():
            raise ValueError("Civic Term must not be empty")
        if self.normalized_term != normalize_term(self.term):
            raise ValueError(
                f"Civic Term {self.term!r} does not match its normalized form "
                f"{self.normalized_term!r}")


@dataclass(frozen=True)
class CivicRun:
    """One durable execution of a Civic Watch for exactly one slot. slot_at is the identity of
    the run within its Watch (one run per slot, enforced by the schema's UNIQUE(watch_id,
    slot_at)) -- a rerun of the same slot resumes this row, it never mints a second one."""
    id: int | None
    watch_id: int
    slot_at: datetime
    status: CivicRunStatus
    phase: CivicRunPhase | None
    requested_at: datetime
    vocabulary_version_id: int | None = None
    # Which publication cap this slot runs under (plan 5.2). Application config, decided when the
    # Run is enqueued and never read from anything remote, so nothing a source publishes can
    # widen how much a day is allowed to say. Held as the plain value rather than
    # beehive.civic.batch_policy.CivicRunMode so this module stays free of the policy layer that
    # imports it.
    run_mode: str = "normal"
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    completed_at: datetime | None = None
    claim_token: str | None = None
    cancel_requested: bool = False
    error_code: str | None = None
    error_detail: str | None = None

    def __post_init__(self) -> None:
        if self.watch_id <= 0:
            raise ValueError("watch_id must be positive")
        if self.slot_at.tzinfo is None or self.slot_at.utcoffset() is None:
            raise ValueError("Civic Run slot_at must be timezone-aware")
        if self.run_mode not in ("normal", "election"):
            raise ValueError(f"unknown Civic Run mode: {self.run_mode!r}")
        if self.vocabulary_version_id is not None and self.vocabulary_version_id <= 0:
            raise ValueError("vocabulary_version_id must be positive")
        if self.status is CivicRunStatus.PROCESSING:
            if self.phase is None or self.claim_token is None:
                raise ValueError("processing Civic Run needs phase and claim_token")
            if self.started_at is None or self.deadline_at is None:
                raise ValueError("processing Civic Run needs started_at and deadline_at")
        elif self.phase is not None:
            raise ValueError("only a processing Civic Run can have a phase")
        if is_terminal_run_status(self.status) and self.completed_at is None:
            raise ValueError("terminal Civic Run needs completed_at")
        if not is_terminal_run_status(self.status) and self.completed_at is not None:
            raise ValueError("non-terminal Civic Run cannot have completed_at")
        if self.error_code is not None and self.status is not CivicRunStatus.FAILED:
            raise ValueError("only a failed Civic Run can have an error_code")


@dataclass(frozen=True)
class CivicBatch:
    """The content-hashed envelope of one Civic Run's publishable Candidates.

    content_hash is None while BUILDING and set exactly once at seal. payload_hash remains None
    until the first canonical spool projection and is then write-once. Every later state --
    DELIVERING, a retry back to SEALED, DELIVERED, QUARANTINED -- reads that same hash, which is
    what lets a redelivery be proven identical rather than merely assumed to be."""
    id: int | None
    run_id: int
    watch_id: int
    status: CivicBatchStatus
    idempotency_key: str
    attempt_count: int
    max_attempts: int
    created_at: datetime
    content_hash: str | None = None
    candidate_count: int = 0
    sealed_at: datetime | None = None
    claim_token: str | None = None
    settled_at: datetime | None = None
    next_attempt_at: datetime | None = None
    error_code: str | None = None
    error_detail: str | None = None
    payload_hash: str | None = None

    def __post_init__(self) -> None:
        if self.run_id <= 0 or self.watch_id <= 0:
            raise ValueError("Civic Batch identifiers must be positive")
        if not self.idempotency_key.strip():
            raise ValueError("Civic Batch needs an idempotency_key")
        if self.attempt_count < 0:
            raise ValueError("attempt_count must not be negative")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.candidate_count < 0:
            raise ValueError("candidate_count must not be negative")
        _require_optional_sha256(self.payload_hash, "Civic Batch payload_hash")
        if self.status is CivicBatchStatus.BUILDING:
            if self.content_hash is not None or self.sealed_at is not None:
                raise ValueError("building Civic Batch cannot be sealed")
        elif self.content_hash is None or self.sealed_at is None:
            raise ValueError(
                f"{self.status.value} Civic Batch needs a content_hash and sealed_at")
        if self.status is CivicBatchStatus.DELIVERING and self.claim_token is None:
            raise ValueError("delivering Civic Batch needs a claim_token")
        if self.status is not CivicBatchStatus.DELIVERING and self.claim_token is not None:
            raise ValueError("only a delivering Civic Batch can hold a claim_token")
        if is_terminal_batch_status(self.status) and self.settled_at is None:
            raise ValueError("terminal Civic Batch needs settled_at")
        if not is_terminal_batch_status(self.status) and self.settled_at is not None:
            raise ValueError("non-terminal Civic Batch cannot have settled_at")

    @property
    def is_sealed(self) -> bool:
        return self.status is not CivicBatchStatus.BUILDING

    @property
    def retries_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempt_count)


@dataclass(frozen=True)
class CivicCandidate:
    """One proposed civic statement. dedupe_key is stable across days for the same underlying
    development, which is what stops the same story being published again tomorrow.

    `sealed_into_batch_at` is the immutable record of envelope membership: it is stamped once, by
    the seal of the Candidate's own Civic Batch, onto the Candidates that were VERIFIED at that
    instant, and never cleared. Membership therefore cannot be changed by anything that happens
    afterwards -- publishing a correction tomorrow moves this Candidate to SUPERSEDED, but it was
    still what the sealed envelope contained, and the Batch's content_hash must keep reflecting
    that."""
    id: int | None
    watch_id: int
    run_id: int
    batch_id: int
    dedupe_key: str
    title: str
    statement: str
    status: CivicCandidateStatus
    created_at: datetime
    research_attempt_count: int = 0
    settled_at: datetime | None = None
    sealed_into_batch_at: datetime | None = None
    rejection_code: str | None = None
    superseded_by_candidate_id: int | None = None
    ai_revision: int = 0
    ai_refresh_reason: str | None = None

    def __post_init__(self) -> None:
        if min(self.watch_id, self.run_id, self.batch_id) <= 0:
            raise ValueError("Civic Candidate identifiers must be positive")
        if not self.dedupe_key.strip():
            raise ValueError("Civic Candidate needs a dedupe_key")
        if not self.title.strip():
            raise ValueError("Civic Candidate title must not be empty")
        if not self.statement.strip():
            raise ValueError("Civic Candidate statement must not be empty")
        if self.research_attempt_count < 0:
            raise ValueError("research_attempt_count must not be negative")
        if self.ai_revision < 0:
            raise ValueError("ai_revision must not be negative")
        if is_terminal_candidate_status(self.status) and self.settled_at is None:
            raise ValueError("settled Civic Candidate needs settled_at")
        if not is_terminal_candidate_status(self.status) and self.settled_at is not None:
            raise ValueError("unsettled Civic Candidate cannot have settled_at")
        if (self.sealed_into_batch_at is not None
                and self.status not in _PUBLISHABLE_CANDIDATE_STATUSES):
            raise ValueError(
                "only a Civic Candidate that reached VERIFIED can be sealed into a Civic Batch")
        if self.rejection_code is not None and self.status is not CivicCandidateStatus.REJECTED:
            raise ValueError("only a rejected Civic Candidate can have a rejection_code")
        if self.status is CivicCandidateStatus.REJECTED and self.rejection_code is None:
            raise ValueError("rejected Civic Candidate needs a rejection_code")
        if self.superseded_by_candidate_id is not None:
            if self.status is not CivicCandidateStatus.SUPERSEDED:
                raise ValueError(
                    "only a superseded Civic Candidate can name a superseding Candidate")
            if self.superseded_by_candidate_id <= 0:
                raise ValueError("superseded_by_candidate_id must be positive")
            if self.superseded_by_candidate_id == self.id:
                raise ValueError("a Civic Candidate cannot supersede itself")
        elif self.status is CivicCandidateStatus.SUPERSEDED:
            raise ValueError("superseded Civic Candidate needs superseded_by_candidate_id")

    @property
    def is_published(self) -> bool:
        """The current published statement: VERIFIED and not yet corrected."""
        return self.status is CivicCandidateStatus.VERIFIED

    @property
    def is_sealed_into_batch(self) -> bool:
        """Whether this Candidate is part of its Batch's immutable sealed envelope. Stays True
        after a later correction supersedes it -- the envelope that went out is unchanged."""
        return self.sealed_into_batch_at is not None

    @property
    def is_settled(self) -> bool:
        """Whether this Candidate has reached a terminal status. A Civic Batch may only be
        sealed once every Candidate in it is settled."""
        return is_terminal_candidate_status(self.status)


@dataclass(frozen=True)
class CivicEvidence:
    id: int | None
    candidate_id: int
    source_ref: str
    external_key: str
    url: str
    title: str
    tier: CivicEvidenceTier
    stance: CivicEvidenceStance
    captured_at: datetime
    publisher: str = ""
    excerpt: str = ""
    published_at: datetime | None = None
    # The qualification-policy inputs (beehive.civic.evidence). Recorded once with the source and
    # never rewritten, so the ownership and accessibility facts behind a published statement stay
    # exactly as they were when the decision was taken. Every default is the closed one: an
    # unregistered publisher, no subject, nothing accessible and no PartyMap source kind, which
    # together mean "discovery-only material" and can never become a citation.
    publisher_id: str = ""
    source_kind: str | None = None
    party_slug: str | None = None
    subject_id: str = ""
    claim_category: str | None = None
    event_date: date | None = None
    canonical_url: str = ""
    accessible_full_text: bool = False
    paywalled: bool = False
    anonymous_source_dependency: bool = False

    def __post_init__(self) -> None:
        if self.candidate_id <= 0:
            raise ValueError("candidate_id must be positive")
        if not self.source_ref.strip():
            raise ValueError("Civic Evidence needs a source_ref")
        if not self.external_key.strip():
            raise ValueError("Civic Evidence needs an external_key")
        if not self.url.strip():
            raise ValueError("Civic Evidence needs a url")
        if not self.title.strip():
            raise ValueError("Civic Evidence needs a title")
        if self.event_date is not None and (
                not isinstance(self.event_date, date) or isinstance(self.event_date, datetime)):
            raise ValueError("Civic Evidence event_date must be a date")

    @property
    def discovery_only(self) -> bool:
        """Whether this source is discovery material rather than evidence. Derived from the
        absence of a PartyMap source kind rather than stored separately, so the two can never
        disagree about whether something is publishable."""
        return self.source_kind is None


@dataclass(frozen=True)
class CivicResearchAttempt:
    """One bounded extra pass over an under-evidenced Civic Candidate. Append-only: attempts are
    numbered per Candidate and never rewritten, so the record of what was already tried survives
    a crash and stops the same pass being spent twice."""
    id: int | None
    candidate_id: int
    attempt_number: int
    outcome: CivicResearchOutcome
    evidence_added: int
    started_at: datetime
    completed_at: datetime
    notes: str = ""
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.candidate_id <= 0 or self.attempt_number <= 0:
            raise ValueError("Civic Research Attempt identifiers must be positive")
        if self.evidence_added < 0:
            raise ValueError("evidence_added must not be negative")
        if self.completed_at < self.started_at:
            raise ValueError("Civic Research Attempt cannot complete before it starts")
        if self.outcome is CivicResearchOutcome.ERROR:
            if self.error_code is None:
                raise ValueError("errored Civic Research Attempt needs an error_code")
        elif self.error_code is not None:
            raise ValueError("only an errored Civic Research Attempt can have an error_code")
        if self.outcome is CivicResearchOutcome.RESOLVED and self.evidence_added <= 0:
            raise ValueError("a resolved Civic Research Attempt must have added evidence")


@dataclass(frozen=True)
class CivicVerification:
    """One independent judgment on a Civic Candidate. Append-only and numbered per Candidate:
    a later verification never overwrites an earlier one, so the full verification history of a
    published statement is always readable."""
    id: int | None
    candidate_id: int
    attempt_number: int
    verdict: CivicVerificationVerdict
    supporting_evidence_count: int
    contradicting_evidence_count: int
    model: str
    recorded_at: datetime
    rationale: str = ""
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.candidate_id <= 0 or self.attempt_number <= 0:
            raise ValueError("Civic Verification identifiers must be positive")
        if self.supporting_evidence_count < 0 or self.contradicting_evidence_count < 0:
            raise ValueError("Civic Verification evidence counts must not be negative")
        if self.verdict is CivicVerificationVerdict.ERROR:
            if self.error_code is None:
                raise ValueError("errored Civic Verification needs an error_code")
        else:
            if self.error_code is not None:
                raise ValueError("only an errored Civic Verification can have an error_code")
            if not self.model.strip():
                raise ValueError("Civic Verification needs the model that produced it")
        if self.verdict is CivicVerificationVerdict.SUPPORTED:
            if self.supporting_evidence_count <= 0:
                raise ValueError("a supported Civic Verification needs supporting evidence")
            if self.contradicting_evidence_count > 0:
                raise ValueError(
                    "a Civic Verification with contradicting evidence cannot be SUPPORTED")
        if (self.verdict is CivicVerificationVerdict.CONTRADICTED
                and self.contradicting_evidence_count <= 0):
            raise ValueError("a contradicted Civic Verification needs contradicting evidence")

    @property
    def permits_publication(self) -> bool:
        return self.verdict is CivicVerificationVerdict.SUPPORTED


@dataclass(frozen=True)
class CivicNotification:
    """An Owner-facing message about a Civic Watch's own operation. Carries its own send lease
    and retry budget so a failing mail provider can never stall or duplicate a Civic Batch.

    dedupe_key is the message's identity within its Watch (typically run id + kind), which is
    what makes queueing idempotent: a worker retrying the same operational path after a crash
    presents the same key and finds the message already queued instead of sending twice."""
    id: int | None
    watch_id: int
    run_id: int | None
    kind: str
    dedupe_key: str
    subject: str
    body: str
    status: CivicNotificationStatus
    attempt_count: int
    max_attempts: int
    queued_at: datetime
    claim_token: str | None = None
    settled_at: datetime | None = None
    next_attempt_at: datetime | None = None
    error_code: str | None = None
    error_detail: str | None = None

    def __post_init__(self) -> None:
        if self.watch_id <= 0:
            raise ValueError("watch_id must be positive")
        if self.run_id is not None and self.run_id <= 0:
            raise ValueError("run_id must be positive")
        if not self.kind.strip():
            raise ValueError("Civic Notification needs a kind")
        if not self.dedupe_key.strip():
            raise ValueError("Civic Notification needs a dedupe_key")
        if not self.subject.strip():
            raise ValueError("Civic Notification needs a subject")
        if self.attempt_count < 0:
            raise ValueError("attempt_count must not be negative")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.status is CivicNotificationStatus.SENDING and self.claim_token is None:
            raise ValueError("sending Civic Notification needs a claim_token")
        if self.status is not CivicNotificationStatus.SENDING and self.claim_token is not None:
            raise ValueError("only a sending Civic Notification can hold a claim_token")
        if is_terminal_notification_status(self.status) and self.settled_at is None:
            raise ValueError("terminal Civic Notification needs settled_at")
        if not is_terminal_notification_status(self.status) and self.settled_at is not None:
            raise ValueError("non-terminal Civic Notification cannot have settled_at")


@dataclass(frozen=True)
class CivicDeliveryReceipt:
    """Immutable proof that one Civic Batch was accepted downstream under `idempotency_key`.

    The key -- not the row id, and not the batch id -- is the identity: a redelivery presenting
    the same key is a replay of this exact receipt, never a second publication. content_hash is
    recorded alongside it so a replay whose envelope has somehow changed is detectable rather
    than silently accepted. payload_hash additionally binds the exact canonical v1 bytes; None
    is only valid for a legacy receipt."""
    id: int | None
    batch_id: int
    idempotency_key: str
    content_hash: str
    channel: CivicDeliveryChannel
    target: str
    delivered_at: datetime
    provider_reference: str = ""
    payload_hash: str | None = None

    def __post_init__(self) -> None:
        if self.batch_id <= 0:
            raise ValueError("batch_id must be positive")
        if not self.idempotency_key.strip():
            raise ValueError("Civic Delivery Receipt needs an idempotency_key")
        if not self.content_hash.strip():
            raise ValueError("Civic Delivery Receipt needs a content_hash")
        _require_optional_sha256(
            self.payload_hash, "Civic Delivery Receipt payload_hash")
        if not self.target.strip():
            raise ValueError("Civic Delivery Receipt needs a target")


def evidence_supports_publication(evidence: tuple[CivicEvidence, ...] | list[CivicEvidence]) -> bool:
    """Whether a Civic Candidate's own evidence is strong enough to publish, before any AI
    verification is consulted: one self-sufficient (official or party-primary) supporting source,
    or two independent supporting/corroborating publishers at any tier. Any contradicting piece
    of evidence vetoes outright -- an unresolved contradiction is never publishable, however much
    supporting material sits beside it."""
    supporting = [
        item for item in evidence
        if item.stance in {CivicEvidenceStance.SUPPORTING, CivicEvidenceStance.CORROBORATING}]
    if any(item.stance is CivicEvidenceStance.CONTRADICTING for item in evidence):
        return False
    if any(item.tier.is_self_sufficient for item in supporting):
        return True
    publishers = {item.publisher.casefold() or item.source_ref.casefold()
                  for item in supporting}
    return len(publishers) >= _CORROBORATION_REQUIRED


def _require_hh_mm(value: str, label: str) -> None:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"{label} must be HH:MM")


def _require_stamp_matches_status(
    expected: bool, stamp: datetime | None, label: str, field: str,
) -> None:
    if expected and stamp is None:
        raise ValueError(f"{label} needs {field}")
    if not expected and stamp is not None:
        raise ValueError(f"{label} cannot have {field}")


def _require_optional_sha256(value: str | None, label: str) -> None:
    if value is None:
        return
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
