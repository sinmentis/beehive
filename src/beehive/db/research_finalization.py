"""Atomic Research Run finalization (ADR-0009/0010): the one deep repository entry point that
turns a run's final, deterministic cluster grouping and curation-filtered active evidence ids
into a sealed Evidence Snapshot, its Evidence Clusters, and the next Evidence State Revision --
all inside a SINGLE `BEGIN IMMEDIATE` transaction, claim- and deadline-fenced end to end.

Why this exists (the race it closes): research.orchestrator.py used to run finalization as a
sequence of separately-fenced calls -- check claim, persist clusters, check claim, seal the
snapshot, check claim, create the revision -- each individually safe but with real gaps between
them. A worker's own heartbeat can fail a run for `error_code='deadline_exceeded'` (db/
research_runs.py's `heartbeat_research_run`) or a reconciliation sweep can fail/requeue it
(`recover_expired_research_runs`) from a WHOLLY SEPARATE connection at any instant between two
of those calls -- including the exact instant the run's fixed deadline_at arrives. A checkpoint
taken even microseconds earlier cannot see that. `finalize_snapshot_if_claimed` closes this by
making the entire write -- clusters, seal, revision -- one atomic unit gated on a SINGLE
claim+deadline check taken under the same transaction as every subsequent INSERT/UPDATE, exactly
like db/research_syntheses.py's `create_synthesis_if_claimed` and db/research_chat_requests.py's
`complete_chat_request_with_reply` already do for their own multi-table writes.

Cluster grouping itself is computed OUTSIDE this transaction: research.clustering.
group_evidence_items is a pure, deterministic function of the run's final evidence membership,
so calling it inside (or before) the transaction makes no difference to correctness and keeps
the write lock held for as short a time as possible. Callers pass the already-computed group ids
(cluster_groups) and the already curation-filtered active evidence ids (active_evidence_item_ids)
in; this module performs no grouping or curation logic of its own, only the fenced persistence.

Deadline handling mirrors heartbeat_research_run's own `now >= deadline_at` fencing exactly: if
the run's fixed deadline has arrived by the time this transaction actually acquires the write
lock AND `cancel_requested` is NOT set, the run is atomically failed right here
(error_code='deadline_exceeded', phase/claim/lease cleared) and NOTHING else is written -- no
cluster row, no sealed snapshot, no revision. If some other heartbeat/recovery sweep already
failed, requeued, or reclaimed the run before this transaction's own SELECT runs, the row simply
no longer matches (run_id, claim_token, status='processing') and this again writes nothing,
returning the same claim-lost outcome -- callers cannot tell the two failure shapes apart from
the DB side effects (both leave zero new cluster/snapshot/revision rows), only from
`failure_reason`, which is purely informational.

Cancellation-finalization grace (Task A): the one exception to the paragraph above. If
`cancel_requested` IS set, a past-arrived deadline_at does NOT fail the run here -- clusters,
seal, and revision are still written normally (subject to every other fence below), so
orchestration can preserve whatever evidence an already-cancelled run collected before
terminalizing CANCELLED via its own subsequent `complete_research_run_if_claimed` call. This is
never new connector/fetch/LLM work -- it is exactly the same local-only clusters+seal+revision
write this function always performs, gated the same way `db.research_runs.heartbeat_research_run`
and `claim_research_run` gate their own matching cancellation carve-outs. A run whose
cancel_requested is NOT set is unaffected by this and still fails outright exactly as before.

Immutability/idempotency: only a still-'building' Evidence Snapshot belonging to exactly this
run and session is ever accepted -- a retry against an already-'sealed' snapshot (e.g. a second,
stale attempt racing a first one that already committed) fails closed instead of appending a
second set of cluster rows or a duplicate revision. Row-to-object mapping for the successful
result is delegated to the same read helpers the rest of db/ already uses
(evidence_clusters.get_evidence_cluster, research_snapshots.get_snapshot,
evidence_state.get_evidence_state_revision) -- plain SELECTs, safe to call once this transaction
has committed -- rather than duplicating that mapping logic here."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from beehive.db.evidence_clusters import get_evidence_cluster
from beehive.db.evidence_state import get_evidence_state_revision
from beehive.db.research_snapshots import get_snapshot
from beehive.domain.research import (EvidenceCluster, EvidenceSnapshot, EvidenceSnapshotStatus,
                                      EvidenceStateRevision)


class FinalizationFailureReason(str, Enum):
    """Why `finalize_snapshot_if_claimed` wrote nothing. All four are reported purely for
    observability -- a caller must treat them exactly the same way (stop, make no further
    writes) -- since a losing worker cannot always distinguish "someone else's heartbeat already
    failed/requeued/reclaimed this run" from "this transaction itself just discovered the
    deadline has arrived (and cancel_requested was NOT set -- see the module docstring's
    cancellation-finalization grace, which is never reported as this failure)" from "the
    snapshot's membership changed underneath a stale precomputation" from "the snapshot's
    current membership is itself invalid". Only MEMBERSHIP_CHANGED is ever safe for a caller to
    retry (reread membership, recompute groups/curation, try again) while its own claim remains
    active -- CLAIM_LOST and DEADLINE_EXCEEDED both mean this run's claim is no longer good for
    anything further, and INVALID_MEMBERSHIP means the snapshot's CURRENT membership itself
    violates an invariant that must hold before any cluster/seal/revision write is ever attempted
    (empty, or containing an Evidence Item belonging to a foreign session) -- a data-integrity
    condition, not a race a bounded retry can resolve, but -- like MEMBERSHIP_CHANGED -- never a
    reason to fail the run outright, since the claim itself may still be perfectly live."""
    CLAIM_LOST = "claim_lost"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    MEMBERSHIP_CHANGED = "membership_changed"
    INVALID_MEMBERSHIP = "invalid_membership"


@dataclass(frozen=True)
class SnapshotFinalizationResult:
    """The single typed outcome of `finalize_snapshot_if_claimed`. `failure_reason` is None iff
    the finalization committed: in that case `snapshot` (now sealed), `clusters` (possibly empty
    -- a run with no qualifying groups seals zero clusters, not an error), and `revision` are all
    populated. Whenever `failure_reason` is set, all three are left at their empty defaults --
    exactly nothing was written -- and callers must never treat this as "finalization is still
    pending", only as "this call made no progress and must not proceed to synthesis"."""
    failure_reason: FinalizationFailureReason | None
    snapshot: EvidenceSnapshot | None = None
    clusters: tuple[EvidenceCluster, ...] = ()
    revision: EvidenceStateRevision | None = None

    @property
    def ok(self) -> bool:
        return self.failure_reason is None


def finalize_snapshot_if_claimed(
    conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int, snapshot_id: int,
    cluster_groups: Sequence[Sequence[int]], active_evidence_item_ids: Sequence[int],
    now: datetime, *, now_fn: Callable[[], datetime] | None = None,
    expected_snapshot_item_ids: Sequence[int] | None = None,
) -> SnapshotFinalizationResult:
    """The one atomic finalization write: inside a single `BEGIN IMMEDIATE` transaction --

    1. Verifies (run_id, claim_token, status='processing') and that the claimed run belongs to
       `session_id` -- the same fence every other claim-fenced write in this package uses.
    2. Verifies the run's fixed deadline_at has not yet arrived (`now < deadline_at`, exactly the
       instant heartbeat_research_run/recover_expired_research_runs/claim_research_run already
       treat as "arrived") -- UNLESS `cancel_requested` is set (Task A's cancellation-finalization
       grace: see point 2a). If it HAS arrived and the run is not cancelled, atomically fails the
       run right here (error_code='deadline_exceeded', phase/claim/lease cleared) and writes
       nothing else.
    2a. Cancellation-finalization grace: if `cancel_requested` IS set, a past-arrived deadline_at
        does NOT fail the run here -- this call proceeds to steps 3-6 exactly as if the deadline
        had not arrived, so an already-cancelled run's already-collected evidence can still be
        sealed and pinned. This is never new connector/fetch/LLM work, only the same local
        clusters+seal+revision write this function always performs; the run's own terminal
        CANCELLED status is decided later, by whichever `complete_research_run_if_claimed` call
        follows.
    3. Verifies the target Evidence Snapshot belongs to this exact run and session and is still
       'building' -- a retry against an already-'sealed' snapshot (this run's own earlier
       attempt that already committed, or any other unexpected state) fails closed rather than
       appending duplicate clusters or a second revision.
    4. Reads the snapshot's CURRENT membership fresh, under this same lock, and verifies it is
       non-empty and that every member Evidence Item belongs to `session_id` -- a data-integrity
       invariant that must hold before any cluster/seal/revision write is ever attempted. Either
       violation fails closed with `INVALID_MEMBERSHIP`, writing nothing (and never failing the
       run: the claim itself may still be perfectly live).
    5. Verifies that membership matches what `cluster_groups`/`active_evidence_item_ids` were
       actually computed against: if `expected_snapshot_item_ids` is given, it must equal the
       current membership exactly; every id in `cluster_groups` must be a member of it, with no
       id repeated across (or within) a group; and `active_evidence_item_ids`, with no id
       repeated, must equal EXACTLY the current membership's own authoritative active subset --
       every member of the current membership for which `research_evidence_curation` has no row
       or a row with `is_excluded = 0`, read fresh under this same lock. A still-active-but-stale
       worker's own append (`db.research_snapshots.add_snapshot_items_if_claimed`) landing
       between the precomputation and this lock, a curation mutation
       (`db.evidence_curation.set_evidence_curation`) committing first and changing which ids are
       authoritatively active, or a caller passing invalid/duplicate groups all fail this the
       same way: nothing is written, and the run is NOT failed (a membership or curation change,
       unlike a lost claim or an arrived deadline, does not mean this claim is bad, only that the
       precomputed groups/active ids are stale; a caller may safely reread membership and
       curation and retry once while its claim remains active). If THIS transaction instead wins
       the lock first, its revision is valid for that instant -- a curation mutation that commits
       afterward is free to build its own later, newer revision the normal way
       (`research.synthesis._rebuild_evidence_state_revision`), exactly as designed.
    6. Inserts one Evidence Cluster (+ its member rows) per group in `cluster_groups`, seals the
       snapshot, allocates the next Evidence State Revision version for `session_id`, and inserts
       it plus its `active_evidence_item_ids` membership.

    Every one of the steps above happens or none does -- any exception rolls back the entire
    transaction. Fence failures (stale claim, lost claim, deadline arrived while NOT cancelled,
    ineligible snapshot, invalid membership, or changed/invalid membership) do not raise: they
    return a `SnapshotFinalizationResult` with `failure_reason` set and every write field empty,
    exactly like every other claim-fenced repository call in this package (create_synthesis_if_
    claimed, upsert_evidence_item_if_claimed, complete_chat_request_with_reply) returns None (or
    its own typed failure) instead of raising for the same class of race.

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after `BEGIN IMMEDIATE`
    has actually acquired the write lock -- never before -- so a caller blocked behind another
    writer until after this run's deadline sees that arrival, rather than the stale `now` it
    happened to sample before ever attempting to acquire the lock. Every deadline comparison and
    every timestamp this call writes (cluster/seal/revision `created_at`/`sealed_at`,
    `completed_at` on a deadline failure) uses that lock-held instant. `now` itself is used
    verbatim only when `now_fn` is omitted (deterministic single-connection tests); every
    production caller MUST pass `now_fn` -- its own live clock callback, never a pre-sampled
    datetime."""
    failure: FinalizationFailureReason | None = None
    cluster_ids: list[int] = []
    revision_id: int | None = None

    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run_row = conn.execute(
            "SELECT session_id, deadline_at, cancel_requested FROM research_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if run_row is None or run_row["session_id"] != session_id:
            failure = FinalizationFailureReason.CLAIM_LOST

        if failure is None:
            deadline_at = (
                datetime.fromisoformat(run_row["deadline_at"])
                if run_row["deadline_at"] else None)
            deadline_arrived = deadline_at is not None and now >= deadline_at
            if deadline_arrived and not bool(run_row["cancel_requested"]):
                conn.execute(
                    "UPDATE research_runs SET status = 'failed', phase = NULL, "
                    "claim_token = NULL, lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (now.isoformat(), run_id, claim_token))
                failure = FinalizationFailureReason.DEADLINE_EXCEEDED
            # else: either the deadline has not arrived, or it has but cancel_requested is set
            # -- cancellation-finalization grace (Task A, point 2a above): proceed to steps 3-6
            # unchanged.

        if failure is None:
            snapshot_row = conn.execute(
                "SELECT session_id, run_id, status FROM research_snapshots WHERE id = ?",
                (snapshot_id,)).fetchone()
            eligible = (
                snapshot_row is not None
                and snapshot_row["session_id"] == session_id
                and snapshot_row["run_id"] == run_id
                and snapshot_row["status"] == EvidenceSnapshotStatus.BUILDING.value)
            if not eligible:
                failure = FinalizationFailureReason.CLAIM_LOST

        if failure is None:
            member_rows = conn.execute(
                "SELECT si.evidence_item_id AS evidence_item_id, "
                "ei.session_id AS item_session_id, "
                "COALESCE(cur.is_excluded, 0) AS is_excluded "
                "FROM research_snapshot_items si "
                "JOIN research_evidence_items ei ON ei.id = si.evidence_item_id "
                "LEFT JOIN research_evidence_curation cur "
                "ON cur.evidence_item_id = si.evidence_item_id "
                "WHERE si.snapshot_id = ?", (snapshot_id,)).fetchall()
            current_membership = frozenset(row["evidence_item_id"] for row in member_rows)
            foreign_session = any(
                row["item_session_id"] != session_id for row in member_rows)
            if not current_membership or foreign_session:
                failure = FinalizationFailureReason.INVALID_MEMBERSHIP

        if failure is None:
            authoritative_active = frozenset(
                row["evidence_item_id"] for row in member_rows if not row["is_excluded"])
            expected_ids = (
                tuple(expected_snapshot_item_ids)
                if expected_snapshot_item_ids is not None else None)
            expected_valid = (
                expected_ids is None
                or (
                    len(expected_ids) == len(set(expected_ids))
                    and frozenset(expected_ids) == current_membership
                )
            )
            if not expected_valid:
                failure = FinalizationFailureReason.MEMBERSHIP_CHANGED
            else:
                flattened = [item_id for group_ids in cluster_groups for item_id in group_ids]
                active_ids = tuple(active_evidence_item_ids)
                groups_valid = (
                    all(group_ids for group_ids in cluster_groups)
                    and len(flattened) == len(set(flattened))
                    and set(flattened) <= current_membership
                    and len(active_ids) == len(set(active_ids))
                    and frozenset(active_ids) == authoritative_active)
                if not groups_valid:
                    failure = FinalizationFailureReason.MEMBERSHIP_CHANGED

        if failure is None:
            now_iso = now.isoformat()
            for group_ids in cluster_groups:
                cur = conn.execute(
                    "INSERT INTO research_evidence_clusters (snapshot_id, created_at) "
                    "VALUES (?, ?)",
                    (snapshot_id, now_iso))
                cluster_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO research_evidence_cluster_items "
                    "(cluster_id, evidence_item_id) VALUES (?, ?)",
                    [(cluster_id, item_id) for item_id in group_ids])
                cluster_ids.append(cluster_id)

            conn.execute(
                "UPDATE research_snapshots SET status = 'sealed', sealed_at = ? WHERE id = ?",
                (now_iso, snapshot_id))

            version_row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
                "FROM research_evidence_state_revisions WHERE session_id = ?",
                (session_id,)).fetchone()
            version = version_row["next_version"]
            rev_cur = conn.execute(
                "INSERT INTO research_evidence_state_revisions "
                "(session_id, version, snapshot_id, created_at) VALUES (?, ?, ?, ?)",
                (session_id, version, snapshot_id, now_iso))
            revision_id = rev_cur.lastrowid
            conn.executemany(
                "INSERT INTO research_evidence_state_revision_items "
                "(revision_id, evidence_item_id) VALUES (?, ?)",
                [(revision_id, item_id) for item_id in active_evidence_item_ids])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()

    if failure is not None:
        return SnapshotFinalizationResult(failure_reason=failure)
    return SnapshotFinalizationResult(
        failure_reason=None,
        snapshot=get_snapshot(conn, snapshot_id),
        clusters=tuple(get_evidence_cluster(conn, cid) for cid in cluster_ids),
        revision=get_evidence_state_revision(conn, revision_id))


__all__ = ["FinalizationFailureReason", "SnapshotFinalizationResult", "finalize_snapshot_if_claimed"]
