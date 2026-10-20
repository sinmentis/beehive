"""Civic duplicate detection: is this development already sealed into a batch or already
published downstream?

Three questions, three windows, one clock:

* **Same canonical URL.** A prior sealed Candidate that already cites this exact canonical URL
  is the same story with a new wrapper. No time window applies: an evidence URL is only ever
  cited once, so re-citing it is a duplicate however old the first citation is.
* **Same fingerprint within seven days.** A Candidate fingerprint is stable across days for one
  underlying development, so the same fingerprint sealed into a recent batch is tomorrow's
  re-run of yesterday's story.
* **Same fingerprint within fourteen days.** PartyMap keeps publishing what Beehive sealed, and
  a correction cycle can put the same fingerprint back in front of a reader for longer than the
  sealed window covers, so the downstream window is deliberately the wider of the two.

**Boundaries are inclusive.** A match whose age equals the window exactly is a duplicate:
comparisons use `<=`, never `<`. Publishing something the reader saw exactly seven days ago is
the failure this Module exists to prevent, and an exclusive comparison would let precisely that
one case through.

**The clock is injected.** `CivicDedupContext.now` is supplied by the caller, never read from
`datetime.now()`, so the same inputs decide the same way in a test, in a re-run, and in a
replayed batch. Every timestamp must be timezone-aware, and no record may be stamped after
`now`: a naive value would compare against an aware one by accident of local time, and a
future-dated record means the clock or the row is wrong, which fails closed as a ValueError
rather than quietly counting as "not a duplicate".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from beehive.civic.reasons import CivicPolicyReason

# Plan 7.6: a sealed Candidate fingerprint blocks a repeat for a week, a fingerprint PartyMap has
# already published blocks one for a fortnight.
SEALED_CANDIDATE_WINDOW = timedelta(days=7)
PUBLISHED_FINGERPRINT_WINDOW = timedelta(days=14)


def _require_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value


@dataclass(frozen=True)
class SealedCandidateRecord:
    """One Candidate already sealed into a Civic Batch, as far as dedup is concerned."""

    candidate_id: str
    fingerprint: str
    canonical_url: str
    sealed_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "sealed candidate id")
        _require_text(self.fingerprint, "sealed candidate fingerprint")
        _require_text(self.canonical_url, "sealed candidate canonical_url")
        _require_aware(self.sealed_at, "sealed_at")


@dataclass(frozen=True)
class PublishedFingerprintRecord:
    """One fingerprint PartyMap has already published, reported back through a batch receipt."""

    fingerprint: str
    batch_id: str
    published_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.fingerprint, "published fingerprint")
        _require_text(self.batch_id, "published fingerprint batch id")
        _require_aware(self.published_at, "published_at")


@dataclass(frozen=True)
class CivicDedupContext:
    """Everything already in flight or already published, plus the instant to compare against."""

    now: datetime
    sealed_candidates: tuple[SealedCandidateRecord, ...] = ()
    published_fingerprints: tuple[PublishedFingerprintRecord, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.now, "dedup context now")
        for record in self.sealed_candidates:
            if record.sealed_at > self.now:
                raise ValueError(
                    f"sealed candidate {record.candidate_id} is stamped after now")
        for record in self.published_fingerprints:
            if record.published_at > self.now:
                raise ValueError(
                    f"published fingerprint {record.fingerprint} is stamped after now")

    @classmethod
    def build(
        cls,
        now: datetime,
        *,
        sealed_candidates: Iterable[SealedCandidateRecord] = (),
        published_fingerprints: Iterable[PublishedFingerprintRecord] = (),
    ) -> CivicDedupContext:
        return cls(
            now=now,
            sealed_candidates=tuple(sealed_candidates),
            published_fingerprints=tuple(published_fingerprints))


@dataclass(frozen=True)
class CivicDedupDecision:
    """Why a Candidate is, or is not, a repeat.

    `reason` is always set: "no prior match" and "the match is older than its window" are
    findings a reader needs as much as "this is a duplicate", because they are what justifies
    publishing something that looks familiar."""

    reason: CivicPolicyReason
    is_duplicate: bool
    matched_candidate_id: str | None = None
    matched_batch_id: str | None = None
    matched_canonical_url: str | None = None
    matched_age: timedelta | None = None


def evaluate_dedup(
    *,
    fingerprint: str,
    canonical_urls: Sequence[str],
    context: CivicDedupContext,
) -> CivicDedupDecision:
    """Decide whether this fingerprint and this evidence have already been through the pipeline.

    Every canonical URL the Candidate carries is checked, including the ones the evidence rules
    excluded: a discovery item pointing at an already-cited article still means the story is a
    repeat, and dropping it from the comparison would let the repeat through."""
    _require_text(fingerprint, "candidate fingerprint")
    urls = frozenset(url for url in canonical_urls if url)

    # Every branch picks the freshest match and tie-breaks by id, so neither the decision nor the
    # match it names depends on the order the caller happened to load its rows in.
    sealed_ages = [
        (record, context.now - record.sealed_at) for record in context.sealed_candidates]
    url_matches = [pair for pair in sealed_ages if pair[0].canonical_url in urls]
    if url_matches:
        record, age = min(url_matches, key=lambda pair: (pair[1], pair[0].candidate_id))
        return CivicDedupDecision(
            reason=CivicPolicyReason.DUPLICATE_CANONICAL_URL,
            is_duplicate=True,
            matched_candidate_id=record.candidate_id,
            matched_canonical_url=record.canonical_url,
            matched_age=age)

    sealed_matches = [pair for pair in sealed_ages if pair[0].fingerprint == fingerprint]
    published_matches = [
        (record, context.now - record.published_at)
        for record in context.published_fingerprints if record.fingerprint == fingerprint]

    sealed_inside = [pair for pair in sealed_matches if pair[1] <= SEALED_CANDIDATE_WINDOW]
    if sealed_inside:
        record, age = min(sealed_inside, key=lambda pair: (pair[1], pair[0].candidate_id))
        return CivicDedupDecision(
            reason=CivicPolicyReason.DUPLICATE_SEALED_CANDIDATE_WITHIN_SEVEN_DAYS,
            is_duplicate=True,
            matched_candidate_id=record.candidate_id,
            matched_canonical_url=record.canonical_url,
            matched_age=age)
    published_inside = [
        pair for pair in published_matches if pair[1] <= PUBLISHED_FINGERPRINT_WINDOW]
    if published_inside:
        record, age = min(published_inside, key=lambda pair: (pair[1], pair[0].batch_id))
        return CivicDedupDecision(
            reason=CivicPolicyReason.DUPLICATE_PUBLISHED_FINGERPRINT_WITHIN_FOURTEEN_DAYS,
            is_duplicate=True,
            matched_batch_id=record.batch_id,
            matched_age=age)

    if sealed_matches:
        record, age = min(sealed_matches, key=lambda pair: (pair[1], pair[0].candidate_id))
        return CivicDedupDecision(
            reason=CivicPolicyReason.DEDUP_OUTSIDE_SEALED_WINDOW,
            is_duplicate=False,
            matched_candidate_id=record.candidate_id,
            matched_canonical_url=record.canonical_url,
            matched_age=age)
    if published_matches:
        record, age = min(published_matches, key=lambda pair: (pair[1], pair[0].batch_id))
        return CivicDedupDecision(
            reason=CivicPolicyReason.DEDUP_OUTSIDE_PUBLISHED_WINDOW,
            is_duplicate=False,
            matched_batch_id=record.batch_id,
            matched_age=age)
    return CivicDedupDecision(
        reason=CivicPolicyReason.DEDUP_NO_PRIOR_MATCH, is_duplicate=False)
