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
``civic-run`` schedules and executes due Civic Watches, while the Civic outcome, recovery and
maintenance modes maintain the two-phase filesystem spool. Civic modes do not take the jobs lock.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import traceback
from typing import TYPE_CHECKING
from dataclasses import asdict
from datetime import datetime, timezone
from time import monotonic

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
from beehive.civic.ai_gateway import CopilotCivicGateway, require_available_civic_models
from beehive.civic.events import CivicEventLogger
from beehive.db.channels import list_channels
from beehive.db.civic_notifications import recover_expired_notifications
from beehive.db.connection import SCHEMA_VERSION, connect, init_schema, schema_version
from beehive.email_routing import EmailConfigurationError
from beehive.localization import load_localizer

if TYPE_CHECKING:
    from beehive.civic.spool import CivicSpool


class _CivicReportedFailure(RuntimeError):
    """A Civic command already emitted its safe events and final summary."""


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


def _civic_shadow_mode() -> bool:
    raw = os.environ.get("CIVIC_SPOOL_SHADOW", "0").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    raise ValueError("CIVIC_SPOOL_SHADOW must be 0/1, true/false, or yes/no")


def _civic_spool(event_logger: CivicEventLogger | None = None) -> CivicSpool:
    # The Civic stack pulls in the article extractor, so only Civic modes import it.
    from beehive.civic.spool import CivicSpool

    return CivicSpool(
        os.environ.get("CIVIC_SPOOL_ROOT", "/spool"),
        shadow=_civic_shadow_mode(),
        event_logger=event_logger,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_civic_command(mode: str, operation) -> None:
    started_at = monotonic()
    event_logger = CivicEventLogger()
    try:
        operation(event_logger)
    except _CivicReportedFailure:
        raise SystemExit(1) from None
    except Exception as exc:
        event_logger.emit(
            "civic.command.failed",
            phase=mode,
            status="failed",
            error_code="civic_command_failed",
            error_class=type(exc).__name__,
            elapsed_ms=(monotonic() - started_at) * 1000,
        )
        raise SystemExit(1) from None


def _emit_civic_batches(conn, spool: CivicSpool, now: datetime):
    projection = spool.project_deliverable_batches(conn, now)
    return projection.writes, tuple(
        {"batch_id": failure.batch_id, "error": type(failure.error).__name__}
        for failure in projection.failures
    )


async def run_civic_cycle(
    db_path: str,
    *,
    event_logger: CivicEventLogger | None = None,
    catch_up_at: datetime | None = None,
) -> None:
    from beehive.civic.worker import CivicRunExecutionStatus, CivicWorker
    from beehive.connectors.civic_sources import enabled_sources as enabled_civic_sources

    event_logger = event_logger or CivicEventLogger()
    conn = connect(db_path)
    init_schema(conn)
    try:
        model = load_model(conn)
        event_logger.emit("civic.models.started", phase="preflight", status="started")
        try:
            await require_available_civic_models(model)
        except Exception as exc:
            event_logger.emit(
                "civic.models.failed", phase="preflight", status="failed",
                error_code=getattr(exc, "error_code", "model_preflight_failed"),
                error_class=type(exc).__name__, detail=str(exc))
            raise
        event_logger.emit("civic.models.finished", phase="preflight", status="completed")
        if catch_up_at is not None:
            from beehive.civic.scheduling import enqueue_partymap_catch_up

            recovery = enqueue_partymap_catch_up(conn, catch_up_at, _utc_now())
            event_logger.emit(
                "civic.catch_up.enqueued", phase="scheduling",
                status=recovery.status, run_id=recovery.id, watch_id=recovery.watch_id)
        worker = CivicWorker(
            conn,
            CopilotCivicGateway(),
            source_entries=enabled_civic_sources(os.environ),
            model=model,
            event_logger=event_logger,
        )
        cycle = await worker.run_due_cycle()
        now = worker.now_fn()
        writes, spool_errors = _emit_civic_batches(
            conn, _civic_spool(event_logger), now)
        print(json.dumps({
            "scheduled": [
                {
                    "watch_id": result.watch.id,
                    "slot_at": result.slot_at.isoformat(),
                    "disposition": result.disposition.value,
                }
                for result in cycle.schedules
            ],
            "runs": [
                {
                    "run_id": result.run_id,
                    "status": result.status.value,
                    "batch_id": result.batch_id,
                    "source_failures": [
                        {
                            "source_key": failure.source_key,
                            "status": failure.status.value,
                            "detail": failure.status.value,
                        }
                        for failure in result.source_failures
                    ],
                    "source_holds": [
                        {
                            "source_key": hold.source_key,
                            "status": hold.status.value,
                            "detail": hold.detail,
                        }
                        for hold in result.source_holds
                    ],
                    "error_code": result.error_code,
                    "error_detail": result.error_code or "",
                }
                for result in cycle.runs
            ],
            "spool_writes": [
                {
                    "batch_id": result.batch_id,
                    "path": result.path.name,
                    "payload_hash": result.payload_hash,
                    "replayed": result.replayed,
                }
                for result in writes
            ],
            "spool_errors": spool_errors,
        }, sort_keys=True), flush=True)
        failed = [
            result for result in cycle.runs
            if result.status is not CivicRunExecutionStatus.COMPLETED
            or result.source_failures
        ]
        if failed or spool_errors:
            raise _CivicReportedFailure(
                f"{len(failed)} Civic Run(s) failed/lost claims/had source failures; "
                f"{len(spool_errors)} Civic Batch spool write(s) failed")
    finally:
        conn.close()


def run_civic_outcomes(
    db_path: str,
    *,
    event_logger: CivicEventLogger | None = None,
) -> None:
    from beehive.civic.spool import CivicOutcomeDisposition

    event_logger = event_logger or CivicEventLogger()
    conn = connect(db_path)
    init_schema(conn)
    try:
        results = _civic_spool(event_logger).process_outcomes(
            conn, _utc_now(), now_fn=_utc_now)
        print(json.dumps([
            {
                "path": result.path.name,
                "disposition": result.disposition.value,
                "batch_id": result.batch_id,
                "detail": (
                    result.detail
                    if result.disposition is not CivicOutcomeDisposition.ERROR
                    else "outcome_processing_failed"
                ),
            }
            for result in results
        ], sort_keys=True), flush=True)
        errors = [
            result for result in results
            if result.disposition is CivicOutcomeDisposition.ERROR]
        if errors:
            raise _CivicReportedFailure(
                f"{len(errors)} Civic outcome file(s) failed validation")
    finally:
        conn.close()


def run_civic_recovery(
    db_path: str,
    *,
    event_logger: CivicEventLogger | None = None,
) -> None:
    from beehive.db.civic_batches import recover_expired_batch_deliveries
    from beehive.db.civic_runs import recover_expired_civic_runs

    event_logger = event_logger or CivicEventLogger()
    conn = connect(db_path)
    init_schema(conn)
    try:
        now = _utc_now()
        started_at = monotonic()
        event_logger.emit(
            "civic.recovery.started", phase="recovery", status="started")
        try:
            runs = recover_expired_civic_runs(conn, now, now_fn=_utc_now)
            batches = recover_expired_batch_deliveries(conn, now)
            notifications = recover_expired_notifications(conn, now)
        except Exception as exc:
            event_logger.emit(
                "civic.recovery.failed",
                phase="recovery",
                status="failed",
                error_code="recovery_failed",
                error_class=type(exc).__name__,
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
            raise
        event_logger.emit(
            "civic.recovery.finished",
            phase="recovery",
            status="completed",
            run_count=runs.requeued_count + runs.deadline_exceeded_count,
            released_count=batches.released_count + notifications.released_count,
            failed_count=(
                runs.deadline_exceeded_count
                + batches.failed_count
                + notifications.failed_count
            ),
            elapsed_ms=(monotonic() - started_at) * 1000,
        )
        print(json.dumps({
            "runs_requeued": runs.requeued_count,
            "runs_deadline_exceeded": runs.deadline_exceeded_count,
            "batches_released": batches.released_count,
            "batches_failed": batches.failed_count,
            "notifications_released": notifications.released_count,
            "notifications_failed": notifications.failed_count,
        }, sort_keys=True), flush=True)
    finally:
        conn.close()


def run_civic_maintenance(
    db_path: str,
    *,
    event_logger: CivicEventLogger | None = None,
) -> None:
    from beehive.civic.spool import CivicOutcomeDisposition

    started_at = monotonic()
    event_logger = event_logger or CivicEventLogger()
    event_logger.emit(
        "civic.maintenance.started", phase="maintenance", status="started")
    run_civic_recovery(db_path, event_logger=event_logger)
    conn = connect(db_path)
    init_schema(conn)
    try:
        now = _utc_now()
        spool = _civic_spool(event_logger)
        writes, spool_errors = _emit_civic_batches(conn, spool, now)
        outcomes = spool.process_outcomes(conn, now, now_fn=_utc_now)
        errors = [
            result for result in outcomes
            if result.disposition is CivicOutcomeDisposition.ERROR]
        if errors or spool_errors:
            event_logger.emit(
                "civic.maintenance.failed",
                phase="maintenance",
                status="failed",
                outcome_count=len(outcomes),
                failed_count=len(errors) + len(spool_errors),
                error_code="maintenance_failed",
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
        else:
            event_logger.emit(
                "civic.maintenance.finished",
                phase="maintenance",
                status="completed",
                batch_count=len(writes),
                outcome_count=len(outcomes),
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
        print(json.dumps({
            "spool_writes": len(writes),
            "spool_errors": spool_errors,
            "outcomes": [
                {
                    "path": result.path.name,
                    "disposition": result.disposition.value,
                    "batch_id": result.batch_id,
                    "detail": (
                        result.detail
                        if result.disposition is not CivicOutcomeDisposition.ERROR
                        else "outcome_processing_failed"
                    ),
                }
                for result in outcomes
            ],
        }, sort_keys=True), flush=True)
        if errors or spool_errors:
            raise _CivicReportedFailure(
                f"{len(errors)} Civic outcome file(s) and "
                f"{len(spool_errors)} spool write(s) failed")
    finally:
        conn.close()


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
            "civic-run",
            "civic-outcomes",
            "civic-recover",
            "civic-maintenance",
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
    parser.add_argument(
        "--civic-catch-up-at", type=datetime.fromisoformat,
        help="Explicit recovery slot after a failed daily run; replay the same timestamp safely.")
    args = parser.parse_args()
    if args.civic_catch_up_at is not None and args.mode != "civic-run":
        parser.error("--civic-catch-up-at is only valid with --mode civic-run")

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
    elif args.mode == "civic-run":
        recovery_options = (
            {"catch_up_at": args.civic_catch_up_at}
            if args.civic_catch_up_at is not None else {})
        _run_civic_command(
            "civic_run",
            lambda event_logger: asyncio.run(
                run_civic_cycle(
                    args.db_path, event_logger=event_logger, **recovery_options)),
        )
    elif args.mode == "civic-outcomes":
        _run_civic_command(
            "civic_outcomes",
            lambda event_logger: run_civic_outcomes(
                args.db_path, event_logger=event_logger),
        )
    elif args.mode == "civic-recover":
        _run_civic_command(
            "civic_recovery",
            lambda event_logger: run_civic_recovery(
                args.db_path, event_logger=event_logger),
        )
    elif args.mode == "civic-maintenance":
        _run_civic_command(
            "civic_maintenance",
            lambda event_logger: run_civic_maintenance(
                args.db_path, event_logger=event_logger),
        )
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
