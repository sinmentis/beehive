#!/usr/bin/env python
"""Entrypoint for the durable Research worker (ADR-0009): one long-running process draining both
Research Runs (research.orchestrator) and Research Chat replies (research.conversation) through
beehive.collector.research_worker.ResearchWorker's two independent bounded pools.

``--reconcile-once`` instead runs a single idempotent expired-lease recovery sweep and exits --
no Research Run or chat request is claimed or executed -- for a separate periodic timer unit
that backstops the always-on worker process (see
deploy/quadlet/beehive-research-reconcile.container/.timer)."""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from beehive.collector.research_worker import ResearchWorker, load_worker_config, reconcile_once
from beehive.db.connection import connect, init_schema


def _init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()


async def _run_worker(worker: ResearchWorker) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:
            # add_signal_handler is Unix-only. Every deployed target (Podman/systemd on Linux)
            # supports it -- this is defense in depth for an unsupported platform, not something
            # this entrypoint depends on to function.
            pass
    await worker.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "/data/beehive.db"))
    parser.add_argument(
        "--reconcile-once", action="store_true",
        help="Run one idempotent expired-lease recovery sweep and exit -- claims/executes "
             "nothing. For the reconcile timer unit, never the always-on worker.")
    args = parser.parse_args(argv)

    try:
        config = load_worker_config(os.environ, args.db_path)
    except ValueError as exc:
        print(f"[run-research-worker] invalid configuration: {exc}", file=sys.stderr)
        return 1

    try:
        _init_schema(config.db_path)
    except Exception as exc:  # noqa: BLE001 -- fatal startup failure; never a secret value
        print(
            f"[run-research-worker] failed to initialize schema: {type(exc).__name__}: {exc}",
            file=sys.stderr)
        return 1

    if args.reconcile_once:
        try:
            result = reconcile_once(config)
        except Exception as exc:  # noqa: BLE001 -- fatal; never a secret value
            print(
                f"[run-research-worker] reconcile-once failed: {type(exc).__name__}: {exc}",
                file=sys.stderr)
            return 1
        print(
            "[run-research-worker] reconcile-once recovered "
            f"{result.recovered_research_runs} research run(s) and "
            f"{result.recovered_chat_requests} chat request(s)")
        return 0

    worker = ResearchWorker(config)
    try:
        asyncio.run(_run_worker(worker))
    except Exception as exc:  # noqa: BLE001 -- fatal; never a secret value
        print(f"[run-research-worker] fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
