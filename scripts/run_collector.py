#!/usr/bin/env python
"""One-shot commands for maintenance and for running a background job by hand.

The jobs worker (scripts/run_jobs.py) runs fetching, deep reads, digests and reminders on its own;
these modes run one pass of the same code now, which helps when testing locally or recovering
by hand. They hold the jobs lock while they run and refuse to start while another process holds
it, so a Channel is never fetched by two processes at once: stop the jobs worker first, or use
"Fetch now" in the admin. ``fetch`` evaluates every Channel's schedule once; ``digest`` sends any due email;
``tracker-reminders`` sends due follow-ups; ``deep-read`` drains a few queued briefs; the rewrite
modes migrate or restore existing unread summaries; ``migrate`` is the explicit release step that
brings the SQLite schema to this build's version. Every mode calls the version-gated
``init_schema`` on startup, which is a single PRAGMA read once the database is current.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import traceback
from dataclasses import asdict

import beehive.connectors.builtin  # noqa: F401 (registers every connector)
from beehive.ai.model_selection import load_model
from beehive.collector.jobs import delivery_context, fetch_channel, send_digests, send_reminders
from beehive.collector.jobs_lock import JobsLock
from beehive.collector.summary_rewrite import (
    SummaryRewriteRollbackResult,
    SummaryRewriteRunResult,
    rollback_summary_rewrite,
    run_summary_rewrite,
)
from beehive.db.channels import list_channels
from beehive.db.connection import SCHEMA_VERSION, connect, init_schema, schema_version
from beehive.email_routing import EmailConfigurationError
from beehive.localization import load_localizer


async def run_fetch(db_path: str) -> None:
    conn = connect(db_path)
    init_schema(conn)
    try:
        localizer = load_localizer(conn)
        model = load_model(conn)
        notifier, default_recipient = delivery_context(conn)
        failures: list[Exception] = []
        for channel in list_channels(conn):
            try:
                await fetch_channel(
                    conn,
                    channel,
                    force=False,
                    notifier=notifier,
                    default_recipient=default_recipient,
                    localizer=localizer,
                    model=model,
                )
            except EmailConfigurationError as exc:
                print(
                    f'[fetch] Channel "{channel["name"]}" could not deliver an alert '
                    f"email, continuing with the other Channels: {exc}"
                )
                failures.append(exc)
            except Exception as exc:  # noqa: BLE001
                # ADR-0002 promises per-Channel isolation: one Channel's failure (a corrupt row,
                # a ranking bug, a DB error) must not starve every Channel after it in the list.
                traceback.print_exc()
                print(
                    f'[fetch] Channel "{channel["name"]}" failed, continuing with the '
                    f"other Channels: {exc}"
                )
                failures.append(exc)
        if failures:
            raise ExceptionGroup("One or more Channels failed during the fetch cycle", failures)
    finally:
        conn.close()


def run_digest(db_path: str) -> None:
    conn = connect(db_path)
    init_schema(conn)
    try:
        send_digests(conn)
    finally:
        conn.close()


def run_tracker_reminders(db_path: str) -> None:
    conn = connect(db_path)
    init_schema(conn)
    try:
        send_reminders(conn)
    finally:
        conn.close()


run_auction_reminders = run_tracker_reminders


async def run_deep_read(db_path: str) -> None:
    # Imported here, not at module level: article extraction (trafilatura) took about 0.5s of
    # every mode's startup, and deep-read is the only mode that uses it.
    from beehive.collector.deep_read_worker import process_deep_read_queue

    conn = connect(db_path)
    init_schema(conn)
    try:
        await process_deep_read_queue(conn)
    finally:
        conn.close()


async def run_unread_summary_rewrite(
    db_path: str,
    *,
    high_water_item_id: int,
    run_id: str,
    dry_run: bool,
    canary_limit: int | None = None,
    after_id: int = 0,
) -> SummaryRewriteRunResult:
    conn = connect(db_path)
    init_schema(conn)
    try:
        result = await run_summary_rewrite(
            conn,
            high_water_item_id,
            run_id,
            load_localizer(conn),
            model=load_model(conn),
            dry_run=dry_run,
            canary_limit=canary_limit,
            after_id=after_id,
        )
        print(json.dumps(asdict(result), sort_keys=True))
        return result
    finally:
        conn.close()


def run_unread_summary_rollback(
    db_path: str, *, run_id: str
) -> SummaryRewriteRollbackResult:
    conn = connect(db_path)
    init_schema(conn)
    try:
        result = rollback_summary_rewrite(conn, run_id)
        print(json.dumps(asdict(result), sort_keys=True))
        return result
    finally:
        conn.close()


def run_migrate(db_path: str) -> None:
    """The explicit release step: migrate once, before new code is rolled out to every unit."""
    conn = connect(db_path)
    try:
        before = schema_version(conn)
        init_schema(conn)
        print(
            f"[migrate] schema version {before} -> {schema_version(conn)} "
            f"(this build: {SCHEMA_VERSION})"
        )
    finally:
        conn.close()


_JOB_MODES = frozenset({"fetch", "digest", "tracker-reminders", "auction-reminders", "deep-read"})


def take_jobs_lock(db_path: str) -> JobsLock:
    """The jobs lock for a job mode, or a clear exit when another process is running jobs."""
    lock = JobsLock(db_path)
    if not lock.try_acquire():
        raise SystemExit(
            "Another process is running background jobs (the jobs worker, or another "
            "run_collector job). The worker does this on its own: use Fetch now in the admin, or "
            "stop the worker (systemctl --user stop beehive-jobs) and try again.")
    return lock


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=[
            "fetch",
            "digest",
            "tracker-reminders",
            "auction-reminders",
            "deep-read",
            "migrate",
            "rewrite-unread-summaries",
            "rollback-unread-summaries",
        ],
        required=True,
    )
    parser.add_argument(
        "--db-path", default=os.environ.get("DB_PATH", "/data/beehive.db")
    )
    parser.add_argument("--high-water-item-id", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--canary-limit", type=int)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-rewrite", action="store_true")
    parser.add_argument("--confirm-rollback", action="store_true")
    args = parser.parse_args()

    lock = take_jobs_lock(args.db_path) if args.mode in _JOB_MODES else None
    try:
        _run_mode(parser, args)
    finally:
        if lock is not None:
            lock.release()


def _run_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.mode == "fetch":
        asyncio.run(run_fetch(args.db_path))
    elif args.mode == "deep-read":
        asyncio.run(run_deep_read(args.db_path))
    elif args.mode == "tracker-reminders":
        run_tracker_reminders(args.db_path)
    elif args.mode == "auction-reminders":
        run_auction_reminders(args.db_path)
    elif args.mode == "migrate":
        run_migrate(args.db_path)
    elif args.mode == "rewrite-unread-summaries":
        if args.run_id is None or args.high_water_item_id is None:
            parser.error(
                "rewrite-unread-summaries requires --run-id and --high-water-item-id"
            )
        if args.dry_run == args.confirm_rewrite:
            parser.error(
                "rewrite-unread-summaries requires exactly one of "
                "--dry-run or --confirm-rewrite"
            )
        result = asyncio.run(
            run_unread_summary_rewrite(
                args.db_path,
                high_water_item_id=args.high_water_item_id,
                run_id=args.run_id,
                dry_run=args.dry_run,
                canary_limit=args.canary_limit,
                after_id=args.after_id,
            )
        )
        if result.failed > 0:
            raise SystemExit(1)
    elif args.mode == "rollback-unread-summaries":
        if args.run_id is None or not args.confirm_rollback:
            parser.error(
                "rollback-unread-summaries requires --run-id and --confirm-rollback"
            )
        result = run_unread_summary_rollback(args.db_path, run_id=args.run_id)
        if result.changed_since > 0:
            raise SystemExit(1)
    else:
        run_digest(args.db_path)


if __name__ == "__main__":
    main()
