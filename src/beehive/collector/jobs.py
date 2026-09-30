"""One function per background job, shared by the jobs worker (collector/jobs_worker.py) and the
one-shot `scripts/run_collector.py` modes, so both run exactly the same code.

The `run_*_job` functions are thread entry points: each opens its own SQLite connection, runs
its job to the end (with its own event loop when it needs one) and closes the connection. The
worker calls them through collector/threads.py so blocking connector, HTTP and email calls never
stall its loop.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

from beehive.ai.model_selection import load_model
from beehive.collector.run_cycle import run_channel_cycle
from beehive.db.channels import get_channel
from beehive.db.connection import connect
from beehive.db.deep_reads import DeepRead
from beehive.digest.send import send_email_group_digests
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    resolve_channel_email,
    resolve_default_email,
)
from beehive.localization import Localizer, load_localizer
from beehive.notify import Notifier, build_notifier
from beehive.research.notifications import send_research_completion_notifications
from beehive.tracker_reminders import send_due_tracker_reminders

if TYPE_CHECKING:
    from beehive.collector.deep_read_worker import DeepReadOutcome


def delivery_context(conn: sqlite3.Connection) -> tuple[Notifier, ResolvedRecipient]:
    """The notifier and the default recipient every email-sending job uses."""
    default_recipient = resolve_default_email(conn, os.environ.get("DIGEST_EMAIL_TO"))
    notifier = build_notifier(os.environ, default_to_addr=default_recipient.address)
    return notifier, default_recipient


async def fetch_channel(
    conn: sqlite3.Connection,
    channel: dict,
    *,
    force: bool,
    notifier: Notifier,
    default_recipient: ResolvedRecipient,
    localizer: Localizer,
    model: str,
) -> bool:
    """One Channel's fetch and ranking cycle. Returns False, having fetched nothing, when the
    Channel's alert address is invalid. An alert that cannot be delivered raises
    EmailConfigurationError after the cycle's own work is saved."""
    try:
        recipient = resolve_channel_email(channel, default_recipient)
    except EmailConfigurationError as exc:
        print(
            f'[fetch] Channel "{channel["name"]}" has an invalid email recipient, '
            f"skipping it: {exc}",
            flush=True,
        )
        return False
    await run_channel_cycle(
        conn,
        channel,
        notifier,
        recipient=recipient.address,
        localizer=localizer,
        model=model,
        force_fetch=force,
    )
    return True


def run_fetch_job(db_path: str, channel_id: int, *, force: bool) -> None:
    """Thread entry: fetch one Channel. `force` is "Fetch now", which skips the due checks."""
    conn = connect(db_path)
    try:
        channel = get_channel(conn, channel_id)
        if channel is None:
            print(f"[fetch] Channel {channel_id} no longer exists; skipping it", flush=True)
            return
        notifier, default_recipient = delivery_context(conn)
        asyncio.run(fetch_channel(
            conn,
            channel,
            force=force,
            notifier=notifier,
            default_recipient=default_recipient,
            localizer=load_localizer(conn),
            model=load_model(conn),
        ))
    finally:
        conn.close()


def run_deep_read_job(db_path: str, claimed: DeepRead) -> DeepReadOutcome:
    """Thread entry: write the brief for one deep read the worker already claimed."""
    # Imported here: article extraction (trafilatura) costs about 0.5 s to load, and only the
    # worker and `--mode deep-read` need it.
    from beehive.collector.deep_read_worker import process_claimed_deep_read
    from beehive.deep_read.fetch import ArticleFetcher

    conn = connect(db_path)
    fetcher = ArticleFetcher()
    try:
        return asyncio.run(process_claimed_deep_read(conn, claimed, fetcher=fetcher))
    finally:
        fetcher.close()
        conn.close()


def send_digests(conn: sqlite3.Connection) -> None:
    """Research completion emails, then every Email group digest that is due. Raises one
    ExceptionGroup listing every delivery that failed, after trying all of them."""
    notifier, default_recipient = delivery_context(conn)
    localizer = load_localizer(conn)
    failures: list[Exception] = []
    try:
        send_research_completion_notifications(conn, notifier, default_recipient, localizer)
    except ExceptionGroup as exc:
        failures.extend(exc.exceptions)
    try:
        send_email_group_digests(conn, notifier, default_recipient, localizer)
    except ExceptionGroup as exc:
        failures.extend(exc.exceptions)
    if failures:
        raise ExceptionGroup("One or more scheduled emails failed", failures)


def send_reminders(
    conn: sqlite3.Connection, *, on_claim: Callable[[str], None] | None = None
) -> None:
    """Follow-up reminders for watched Tracker items that are due. `on_claim` gets the claim
    token of the reminders being sent (see tracker_reminders.send_due_tracker_reminders)."""
    notifier, default_recipient = delivery_context(conn)
    send_due_tracker_reminders(
        conn, notifier, default_recipient, load_localizer(conn), on_claim=on_claim)


def run_digest_job(db_path: str) -> None:
    conn = connect(db_path)
    try:
        send_digests(conn)
    finally:
        conn.close()


def run_reminder_job(db_path: str, *, on_claim: Callable[[str], None] | None = None) -> None:
    conn = connect(db_path)
    try:
        send_reminders(conn, on_claim=on_claim)
    finally:
        conn.close()
