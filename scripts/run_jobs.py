#!/usr/bin/env python
"""Entrypoint for the jobs worker (ADR-0012): the always-on process that fetches Channels (on
schedule and for "Fetch now"), writes deep-read briefs, and sends digests and reminders. See
src/beehive/collector/jobs_worker.py."""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

import beehive.connectors.builtin  # noqa: F401 (registers every connector)
from beehive.collector.jobs_worker import JobsWorker, JobsWorkerConfig
from beehive.db.connection import connect, init_schema


async def _run(worker: JobsWorker) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:
            pass  # not on Unix; every deployed target supports it
    await worker.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "/data/beehive.db"))
    args = parser.parse_args(argv)

    try:
        config = JobsWorkerConfig(db_path=args.db_path)
        conn = connect(config.db_path)
        try:
            init_schema(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- fatal startup failure; never a secret value
        print(f"[run-jobs] could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("[run-jobs] jobs worker started", flush=True)
    try:
        asyncio.run(_run(JobsWorker(config)))
    except Exception as exc:  # noqa: BLE001 -- fatal; never a secret value
        print(f"[run-jobs] fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("[run-jobs] jobs worker stopped", flush=True)
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # A job thread may still be unwinding after a graceful stop. Its claims were handed back, so
    # exit now instead of letting interpreter shutdown wait on it.
    os._exit(code)
