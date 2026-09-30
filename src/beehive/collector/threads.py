"""Run one blocking job on its own daemon thread and await it from an asyncio loop.

Background jobs (research runs, fetches, deep reads, email) call synchronous connector and HTTP
code, so running them on a worker's event loop would stall every heartbeat and poll on it. Each
job gets a fresh daemon thread instead, and only its result or exception comes back to the loop
through a loop-owned future. No ThreadPoolExecutor: its atexit hook joins its threads, which
would make a stopping worker wait for a job that is still running.
"""
from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Callable


def _settle_result(fut: asyncio.Future, result: object) -> None:
    if not fut.done():
        fut.set_result(result)


def _settle_exception(fut: asyncio.Future, exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)


async def run_in_thread(func: Callable[[], object], *, name: str = "background-job") -> object:
    """Runs `func` on a new daemon thread and awaits its result on the calling loop."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def _runner() -> None:
        try:
            result = func()
        except BaseException as exc:  # noqa: BLE001 -- forwarded to the awaiting coroutine
            settle = functools.partial(_settle_exception, fut, exc)
        else:
            settle = functools.partial(_settle_result, fut, result)
        try:
            loop.call_soon_threadsafe(settle)
        except RuntimeError:
            pass  # the event loop is already closed (process shutting down); nothing to notify

    threading.Thread(target=_runner, daemon=True, name=name).start()
    return await fut
