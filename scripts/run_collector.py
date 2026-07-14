#!/usr/bin/env python
"""Entrypoint for the timer-driven collector. Three modes share this one script/image
(deploy/quadlet/beehive-fetch.container, beehive-fetch-manual.container, and
beehive-digest.container each invoke this with a different --mode): `fetch` runs the
per-Channel fetch->AI-rank cycle for every Channel (every few hours, timer-triggered);
`fetch-channel` runs it for exactly one Channel (started only by beehive-fetch-manual.path
when the admin UI's "Fetch now" button writes a trigger marker); `digest` composes and sends
the one daily 08:00 email. Importing beehive.connectors.reddit/google_news/hackernews/official_feeds registers those
connectors as a side effect (Task 8) before any Channel's Sources are processed. All three modes call
init_schema on every run -- schema.sql is all CREATE TABLE IF NOT EXISTS, so this is a cheap,
idempotent bootstrap that guarantees a fresh beehive-data.volume gets its tables on first run,
rather than requiring a separate migration step before any of the timers/path units ever fire."""
from __future__ import annotations

import argparse
import asyncio
import os

from beehive.connectors import google_news, hackernews, official_feeds, reddit  # noqa: F401  (registers the connectors)
from beehive.db.channels import get_channel, list_channels
from beehive.db.connection import connect, init_schema
from beehive.collector.manual_trigger import consume_pending_manual_trigger
from beehive.collector.run_cycle import run_channel_cycle
from beehive.digest.send import send_daily_digest
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    resolve_channel_email,
    resolve_default_email,
)
from beehive.notify import Notifier, build_notifier


def _build_delivery_context(
    conn,
) -> tuple[Notifier, ResolvedRecipient]:
    default_recipient = resolve_default_email(
        conn, os.environ.get("DIGEST_EMAIL_TO"))
    notifier = build_notifier(
        os.environ, default_to_addr=default_recipient.address)
    return notifier, default_recipient


async def run_fetch(db_path: str) -> None:
    conn = connect(db_path)
    init_schema(conn)
    try:
        notifier, default_recipient = _build_delivery_context(conn)
        alert_delivery_failures: list[EmailConfigurationError] = []
        for channel in list_channels(conn):
            try:
                recipient = resolve_channel_email(channel, default_recipient)
            except EmailConfigurationError as exc:
                print(f"[fetch] Channel \"{channel['name']}\" has an invalid email "
                      f"recipient, skipping it: {exc}")
                continue
            try:
                await run_channel_cycle(
                    conn, channel, notifier, recipient=recipient.address)
            except EmailConfigurationError as exc:
                print(f"[fetch] Channel \"{channel['name']}\" could not deliver an alert "
                      f"email, continuing with the other Channels: {exc}")
                alert_delivery_failures.append(exc)
        if alert_delivery_failures:
            raise ExceptionGroup(
                "One or more Channels could not deliver alert emails",
                alert_delivery_failures)
    finally:
        conn.close()


async def run_fetch_channel(db_path: str) -> None:
    conn = connect(db_path)
    init_schema(conn)
    try:
        data_dir = os.path.dirname(db_path)
        channel_id = consume_pending_manual_trigger(data_dir)
        if channel_id is None:
            print("[fetch-channel] no valid trigger marker found; nothing to do")
            return
        channel = get_channel(conn, channel_id)
        if channel is None:
            print(f"[fetch-channel] Channel {channel_id} no longer exists; nothing to do")
            return
        notifier, default_recipient = _build_delivery_context(conn)
        try:
            recipient = resolve_channel_email(channel, default_recipient)
        except EmailConfigurationError as exc:
            print(f"[fetch-channel] Channel \"{channel['name']}\" has an invalid email "
                  f"recipient, skipping it: {exc}")
            return
        try:
            await run_channel_cycle(
                conn, channel, notifier, recipient=recipient.address,
                force_fetch=True)
        except EmailConfigurationError as exc:
            print(f"[fetch-channel] Channel \"{channel['name']}\" could not deliver an "
                  f"alert email: {exc}")
            raise
    finally:
        conn.close()


def run_digest(db_path: str) -> None:
    conn = connect(db_path)
    init_schema(conn)
    try:
        notifier, default_recipient = _build_delivery_context(conn)
        send_daily_digest(conn, notifier, default_recipient)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["fetch", "fetch-channel", "digest"], required=True)
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "/data/beehive.db"))
    args = parser.parse_args()

    if args.mode == "fetch":
        asyncio.run(run_fetch(args.db_path))
    elif args.mode == "fetch-channel":
        asyncio.run(run_fetch_channel(args.db_path))
    else:
        run_digest(args.db_path)


if __name__ == "__main__":
    main()
