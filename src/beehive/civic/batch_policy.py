"""Deterministic Civic batch selection: which qualified Candidates go out in this slot.

Plan 5.2 puts a hard cap on how much a single day may publish -- six normally, ten during an
election period -- and this Module is the whole of that decision. It is deliberately separate
from `beehive.civic.evidence`: whether a Candidate *may* be published is an evidence question,
whether it is published *today* is a capacity question, and conflating them would make an
overflow look like an evidence failure in the record.

Three properties matter more than the arithmetic:

* **Rank order decides, nothing else.** The queue arrives already ranked; selection takes the
  highest ranked Candidates up to the cap. Ties break on candidate id, so two Candidates ranked
  equally always select in the same order however the caller loaded them.
* **Overflow defers, it never disappears.** A Candidate above the cap keeps its evidence and its
  reason and comes back to the next slot, recorded as `batch_cap_reached`.
* **Empty stays empty.** An empty queue selects nothing and defers nothing. Plan 5.3's silent
  success rule depends on that: no Candidates means no branch, no PR and no alert, so an empty
  slot must not be able to look like a failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from beehive.civic.evidence import SUPPORTED_RECORD_KINDS, CivicRecordKind
from beehive.civic.reasons import CivicPolicyReason


class CivicRunMode(str, Enum):
    """Whether the slot runs under normal or election-period rules."""

    NORMAL = "normal"
    ELECTION = "election"


# Plan 5.2. The election cap is higher, not unlimited: an election period produces more publish
# worthy material, and a day that could publish everything would stop being a daily digest.
PUBLISHED_CAP_BY_MODE: dict[CivicRunMode, int] = {
    CivicRunMode.NORMAL: 6,
    CivicRunMode.ELECTION: 10,
}


def published_cap(mode: CivicRunMode) -> int:
    return PUBLISHED_CAP_BY_MODE[mode]


@dataclass(frozen=True)
class CivicRankedCandidate:
    """One already-qualified Candidate waiting for a slot."""

    candidate_id: str
    rank: int
    fingerprint: str
    record_kind: CivicRecordKind

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("ranked candidate id must not be empty")
        if not self.fingerprint.strip():
            raise ValueError(f"{self.candidate_id}: ranked candidate needs a fingerprint")
        if self.rank < 1:
            raise ValueError(f"{self.candidate_id}: rank must be one or greater")
        if self.record_kind not in SUPPORTED_RECORD_KINDS:
            raise ValueError(
                f"{self.candidate_id}: {self.record_kind.value} is outside the automated "
                f"publication scope and cannot be queued for a batch")


@dataclass(frozen=True)
class CivicBatchDeferral:
    candidate_id: str
    reason: CivicPolicyReason


@dataclass(frozen=True)
class CivicBatchSelection:
    """What this slot publishes, what it holds back, and why."""

    mode: CivicRunMode
    published_cap: int
    published_candidate_ids: tuple[str, ...]
    deferred_candidate_ids: tuple[str, ...]
    reasons: tuple[CivicPolicyReason, ...]
    deferrals: tuple[CivicBatchDeferral, ...] = ()

    @property
    def deferral_reason_by_candidate_id(self) -> dict[str, CivicPolicyReason]:
        return {deferral.candidate_id: deferral.reason for deferral in self.deferrals}

    @property
    def is_empty(self) -> bool:
        return not self.published_candidate_ids and not self.deferred_candidate_ids


def select_batch(
    queue: Iterable[CivicRankedCandidate], *, mode: CivicRunMode,
) -> CivicBatchSelection:
    """Take the highest ranked Candidates up to this slot's cap and defer the rest.

    Pure and order independent: the queue is sorted by (rank, candidate id) before the cap is
    applied, so shuffling the input cannot change which Candidates go out."""
    entries = tuple(queue)
    seen: set[str] = set()
    for entry in entries:
        if entry.candidate_id in seen:
            raise ValueError(f"duplicate candidate in queue: {entry.candidate_id}")
        seen.add(entry.candidate_id)

    ordered = sorted(entries, key=lambda entry: (entry.rank, entry.candidate_id))
    cap = published_cap(mode)
    published = tuple(entry.candidate_id for entry in ordered[:cap])
    deferred = tuple(entry.candidate_id for entry in ordered[cap:])
    reason = (CivicPolicyReason.BATCH_CAP_REACHED if deferred
              else CivicPolicyReason.BATCH_CAP_WITHIN_LIMIT)
    return CivicBatchSelection(
        mode=mode,
        published_cap=cap,
        published_candidate_ids=published,
        deferred_candidate_ids=deferred,
        reasons=(reason,),
        deferrals=tuple(
            CivicBatchDeferral(candidate_id, CivicPolicyReason.BATCH_CAP_REACHED)
            for candidate_id in deferred))
