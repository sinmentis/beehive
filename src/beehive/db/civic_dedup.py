"""Building the pure engine's dedup context out of what this database already published.

`beehive.civic.dedup` decides whether a Candidate is a repeat, and it is pure: it compares against
records handed to it rather than reading anything. This module is the other half -- the read that
turns sealed Candidates and delivered Batches into exactly those records.

Two different facts, two different windows (plan 7.6):

* **Sealed Candidates, seven days.** Membership is `sealed_into_batch_at`, the same immutable
  stamp the envelope hash is computed over, never `status`. A Candidate superseded by tomorrow's
  correction was still published today, and dedup has to keep saying so.
* **Delivered fingerprints, fourteen days.** A fingerprint only counts as *published* once
  PartyMap has receipted the Batch that carried it, so a Batch still sitting in the spool cannot
  suppress tomorrow's Candidate for something that never went out.

Both are loaded over a bounded horizon rather than the whole history. The horizon is wider than
either window on purpose: `evaluate_dedup` reports "there is a prior match, but it is outside the
window" as a finding in its own right, and cutting the read at exactly seven days would silently
turn that finding into "no prior match at all".
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from beehive.civic.dedup import (PUBLISHED_FINGERPRINT_WINDOW, CivicDedupContext,
                                 PublishedFingerprintRecord, SealedCandidateRecord)

# How far back the "outside the window" finding stays visible. Anything older is neither a
# duplicate nor a near miss, so loading it would only make the read grow without bound.
DEDUP_HORIZON = PUBLISHED_FINGERPRINT_WINDOW + timedelta(days=16)


def build_dedup_context(
    conn: sqlite3.Connection,
    watch_id: int,
    now: datetime,
    *,
    exclude_candidate_id: int | None = None,
    horizon: timedelta = DEDUP_HORIZON,
) -> CivicDedupContext:
    """Everything this Watch has sealed or delivered recently, as the pure engine wants it.

    `exclude_candidate_id` keeps a Candidate from being compared against itself, which matters on
    the resume path: a Candidate sealed by the Run that is now re-qualifying it would otherwise
    look like yesterday's duplicate of itself.

    A sealed Candidate is emitted once per URL it touched -- the sources it consumed and the
    sources it cited -- because the canonical-URL rule has to catch a repeat that arrives under a
    fresh fingerprint. All records for one Candidate share its `sealed_at`, so which of them the
    engine picks never changes the age it reports."""
    since = (now - horizon).isoformat()
    rows = conn.execute(
        "SELECT c.id AS id, c.dedupe_key AS dedupe_key, "
        "c.sealed_into_batch_at AS sealed_at, c.batch_id AS batch_id "
        "FROM civic_candidates c "
        "WHERE c.watch_id = ? AND c.sealed_into_batch_at IS NOT NULL "
        "AND c.sealed_into_batch_at >= ? AND c.sealed_into_batch_at <= ? "
        "AND (? IS NULL OR c.id != ?) ORDER BY c.id",
        (watch_id, since, now.isoformat(), exclude_candidate_id,
         exclude_candidate_id)).fetchall()

    sealed: list[SealedCandidateRecord] = []
    published: list[PublishedFingerprintRecord] = []
    for row in rows:
        sealed_at = datetime.fromisoformat(row["sealed_at"])
        for url in _candidate_urls(conn, row["id"]):
            sealed.append(SealedCandidateRecord(
                candidate_id=str(row["id"]),
                fingerprint=row["dedupe_key"],
                canonical_url=url,
                sealed_at=sealed_at))
        delivered_at = _delivered_at(conn, row["batch_id"])
        if delivered_at is not None and delivered_at <= now:
            published.append(PublishedFingerprintRecord(
                fingerprint=row["dedupe_key"],
                batch_id=str(row["batch_id"]),
                published_at=delivered_at))
    return CivicDedupContext.build(
        now, sealed_candidates=sealed, published_fingerprints=published)


def _candidate_urls(conn: sqlite3.Connection, candidate_id: int) -> list[str]:
    """Every canonical URL a Candidate touched: the items it consumed and the sources it cited.

    Consumed items are included because an article can be the reason a Candidate exists without
    surviving the evidence rules as a citation, and it is still the same article when it comes
    round again tomorrow."""
    rows = conn.execute(
        "SELECT canonical_url AS url FROM civic_consumed_items WHERE candidate_id = ? "
        "UNION SELECT CASE WHEN canonical_url != '' THEN canonical_url ELSE url END AS url "
        "FROM civic_candidate_evidence WHERE candidate_id = ?",
        (candidate_id, candidate_id)).fetchall()
    return sorted({row["url"] for row in rows if row["url"]})


def _delivered_at(conn: sqlite3.Connection, batch_id: int) -> datetime | None:
    """When PartyMap receipted this Batch, or None if it has not been delivered.

    The earliest receipt wins: a replayed delivery writes no second receipt, but a Batch is
    published from the moment the first one lands."""
    row = conn.execute(
        "SELECT MIN(delivered_at) AS delivered_at FROM civic_delivery_receipts "
        "WHERE batch_id = ?", (batch_id,)).fetchone()
    return datetime.fromisoformat(row["delivered_at"]) if row["delivered_at"] else None


__all__ = ["DEDUP_HORIZON", "build_dedup_context"]
