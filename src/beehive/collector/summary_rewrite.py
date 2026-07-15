# src/beehive/collector/summary_rewrite.py
"""Callable orchestration for the unread-summary rewrite tool: walks eligible unread items
(db/items.py's list_unread_rewrite_candidates), regenerates each one's ai_summary through
ai/summary_rewrite.py, and applies the result through db/summary_rewrites.py's
apply_summary_rewrite -- the single atomic seam that writes items.ai_summary (re-checking it
is still unread and still within the caller-supplied high-water set) and the corresponding
summary_rewrite_log row together, in one transaction. Score, rationale, votes,
best_comment_summary, and read/open state are never read or written by this module; only
ai_summary ever changes.

That combined write+log seam is what makes:
  - a rerun of the same run_id idempotent -- an item already logged under this run_id is
    skipped before spending another LLM call on it, whether the previous invocation completed
    or was interrupted partway through a canary/full pass, and apply_summary_rewrite's own
    idempotency check backstops that even if this orchestrator's earlier check and the write
    race against each other;
  - a rewrite crash-safe -- since the ai_summary UPDATE and its log INSERT commit together,
    there is no window where a crash (or any exception) leaves a rewritten summary with no
    audit/rollback row, unlike an earlier revision of this module that called a separate
    committing UPDATE followed by a separate committing log insert;
  - a run reversible -- rollback_summary_rewrite below replays a run's log entries in reverse,
    restoring each item's previous_summary (only if nothing else has changed it since), then
    clears the run's log so the same run_id can cleanly reprocess those items afterward.

This module is deliberately exposed under beehive.collector rather than wired into
scripts/run_collector.py here -- that CLI integration (a one-shot collector mode) is being
added concurrently elsewhere; this is the callable seam that CLI is expected to call into."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from beehive.ai.summary_rewrite import DEFAULT_MODEL, RewriteItemContext, rewrite_item_summary
from beehive.db.items import list_unread_rewrite_candidates
from beehive.db.summary_rewrites import (
    apply_summary_rewrite,
    list_for_run,
    revert_summary_rewrite_entry,
    was_migrated,
)
from beehive.localization import Localizer

_DEFAULT_PAGE_SIZE = 20


@dataclass(frozen=True)
class SummaryRewriteRunResult:
    """Progress counters for one call to run_summary_rewrite. `rewritten` counts items actually
    written this call (or, for a dry run, that WOULD have been written -- no LLM call, no
    write, no log entry); it does NOT include items this or an earlier invocation of the same
    run_id already logged (see `already_migrated`). `last_item_id` is the id of the last
    candidate this call looked at (0 if none were), suitable to pass back as the next call's
    `after_id` to resume a canary into a fuller pass."""
    run_id: str
    dry_run: bool
    considered: int
    rewritten: int
    already_migrated: int
    no_longer_eligible: int
    failed: int
    last_item_id: int


@dataclass(frozen=True)
class SummaryRewriteRollbackResult:
    run_id: str
    entries_found: int
    reverted: int
    changed_since: int


async def run_summary_rewrite(
    conn: sqlite3.Connection,
    high_water_item_id: int,
    run_id: str,
    localizer: Localizer,
    *,
    model: str = DEFAULT_MODEL,
    page_size: int = _DEFAULT_PAGE_SIZE,
    canary_limit: int | None = None,
    dry_run: bool = False,
    after_id: int = 0,
    now: datetime | None = None,
) -> SummaryRewriteRunResult:
    """Confirmed execution when dry_run=False (the default): calls the LLM and writes results.
    dry_run=True previews exactly which/how many items would be rewritten -- no LLM call, no
    write, no log entry for any candidate, real or skipped. canary_limit, if given, caps the
    number of items this call actually rewrites (or, in a dry run, would rewrite) at that
    count; it does not cap how many candidates are considered while skipping already-migrated
    ones, and it does not cap the number of eligible rows scanned across pages -- pagination
    always walks forward via db/items.py's keyset cursor regardless of canary_limit, so a
    canary run can never re-see or re-skip the same candidates as a previous canary call
    started from `after_id=0`; pass the returned `last_item_id` back as the next call's
    `after_id` to continue past a canary."""
    if high_water_item_id < 0:
        raise ValueError("high_water_item_id must be >= 0")
    if canary_limit is not None and canary_limit < 0:
        raise ValueError("canary_limit must be >= 0 if given")

    run_now = now or datetime.now(timezone.utc)
    language = localizer.language

    considered = 0
    rewritten = 0
    already_migrated_count = 0
    no_longer_eligible = 0
    failed = 0
    cursor = after_id

    while canary_limit is None or rewritten < canary_limit:
        candidates = list_unread_rewrite_candidates(
            conn, high_water_item_id, after_id=cursor, limit=page_size)
        if not candidates:
            break

        for candidate in candidates:
            considered += 1
            cursor = candidate["id"]

            if was_migrated(conn, run_id, candidate["id"]):
                already_migrated_count += 1
            elif dry_run:
                # Preview only: no LLM call, no write, no log entry -- just count it as
                # "would be rewritten" the same as a real success below.
                rewritten += 1
            else:
                context = RewriteItemContext(
                    title=candidate["title"], body=candidate["body"],
                    source_type=candidate["source_type"],
                    source_name=candidate["raw_metadata"].get("source_name"))
                try:
                    result = await rewrite_item_summary(
                        candidate["id"], context, language, model=model)
                except Exception as exc:
                    failed += 1
                    print(f"[summary-rewrite] run={run_id} item={candidate['id']} failed: {exc}")
                    result = None

                if result is not None:
                    # Single atomic seam: the ai_summary UPDATE and its summary_rewrite_log
                    # row are written (and committed) together here -- see
                    # db/summary_rewrites.py's apply_summary_rewrite docstring for why that
                    # matters. `None` covers both "already migrated" (should not normally
                    # happen, since was_migrated was just checked above, but is possible if a
                    # concurrent run raced this one) and "no longer eligible" (read, or pushed
                    # above the high-water mark, since this candidate was read from the DB);
                    # either way nothing was written, so it is counted the same way.
                    previous_summary = apply_summary_rewrite(
                        conn, run_id, candidate["id"], result.summary, high_water_item_id,
                        run_now)
                    if previous_summary is not None:
                        rewritten += 1
                    elif was_migrated(conn, run_id, candidate["id"]):
                        already_migrated_count += 1
                    else:
                        no_longer_eligible += 1

            # Only an actual (or, in a dry run, a would-be) rewrite counts against
            # canary_limit -- already-migrated/failed/no-longer-eligible candidates never do,
            # so a canary always delivers up to canary_limit real results, not just attempts.
            if canary_limit is not None and rewritten >= canary_limit:
                break

    return SummaryRewriteRunResult(
        run_id=run_id, dry_run=dry_run, considered=considered, rewritten=rewritten,
        already_migrated=already_migrated_count, no_longer_eligible=no_longer_eligible,
        failed=failed, last_item_id=cursor)


def rollback_summary_rewrite(conn: sqlite3.Connection,
                              run_id: str) -> SummaryRewriteRollbackResult:
    """Reverts entries whose replacement is still live.

    Entries changed by a later run or manual edit remain in the log, allowing an ordered rollback
    to retry them after the later change has been removed.
    """
    entries = list_for_run(conn, run_id)
    reverted = 0
    changed_since = 0
    for entry in entries:
        did_revert = revert_summary_rewrite_entry(conn, entry)
        if did_revert:
            reverted += 1
        else:
            changed_since += 1
    return SummaryRewriteRollbackResult(
        run_id=run_id, entries_found=len(entries), reverted=reverted, changed_since=changed_since)
