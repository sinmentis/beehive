#!/usr/bin/env python
"""Entrypoint for scheduled, path-triggered, and maintenance work.

Every mode shares one image and selects its role through ``--mode``. ``fetch`` runs the scheduled
per-Channel fetch and AI-rank cycle; ``fetch-channel`` handles one admin-triggered Channel;
``digest`` sends the daily email; ``deep-read`` drains queued article briefs; the rewrite modes
migrate or restore existing unread summaries. Connector imports register source adapters before any
Channel is processed. Every mode initializes the idempotent SQLite schema on startup.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict

from beehive.connectors import (  # noqa: F401  (registers the connectors)
    google_news,
    hackernews,
    land_sea_collection,
    official_feeds,
    reddit,
    shopify_collection,
)
from beehive.db.channels import get_channel, list_channels
from beehive.db.connection import connect, init_schema
from beehive.collector.manual_trigger import consume_pending_manual_triggers
from beehive.collector.summary_rewrite import (
    SummaryRewriteRollbackResult,
    SummaryRewriteRunResult,
    rollback_summary_rewrite,
    run_summary_rewrite,
)
from beehive.collector.deep_read_worker import process_deep_read_queue
from beehive.collector.run_cycle import run_channel_cycle
from beehive.ai.model_selection import load_model
from beehive.digest.send import send_daily_digest
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    resolve_channel_email,
    resolve_default_email,
)
from beehive.localization import load_localizer
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
        localizer = load_localizer(conn)
        model = load_model(conn)
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
                    conn, channel, notifier, recipient=recipient.address,
                    localizer=localizer, model=model)
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
        localizer = load_localizer(conn)
        model = load_model(conn)
        data_dir = os.path.dirname(db_path)
        channel_ids = consume_pending_manual_triggers(data_dir)
        if channel_ids is None:
            print("[fetch-channel] no valid trigger marker found; nothing to do")
            return
        channels = []
        for channel_id in channel_ids:
            channel = get_channel(conn, channel_id)
            if channel is None:
                print(f"[fetch-channel] Channel {channel_id} no longer exists; skipping it")
                continue
            channels.append(channel)
        if not channels:
            return
        notifier, default_recipient = _build_delivery_context(conn)
        alert_delivery_failures: list[EmailConfigurationError] = []
        for channel in channels:
            try:
                recipient = resolve_channel_email(channel, default_recipient)
            except EmailConfigurationError as exc:
                print(f"[fetch-channel] Channel \"{channel['name']}\" has an invalid email "
                      f"recipient, skipping it: {exc}")
                continue
            try:
                await run_channel_cycle(
                    conn, channel, notifier, recipient=recipient.address,
                    localizer=localizer, model=model, force_fetch=True)
            except EmailConfigurationError as exc:
                print(f"[fetch-channel] Channel \"{channel['name']}\" could not deliver an "
                      f"alert email: {exc}")
                alert_delivery_failures.append(exc)
        if len(alert_delivery_failures) == 1:
            raise alert_delivery_failures[0]
        if alert_delivery_failures:
            raise ExceptionGroup(
                "Multiple manually fetched Channels could not deliver alert emails",
                alert_delivery_failures,
            )
    finally:
        conn.close()


def run_digest(db_path: str) -> None:
    conn = connect(db_path)
    init_schema(conn)
    try:
        localizer = load_localizer(conn)
        notifier, default_recipient = _build_delivery_context(conn)
        send_daily_digest(conn, notifier, default_recipient, localizer)
    finally:
        conn.close()


async def run_deep_read(db_path: str) -> None:
    conn = connect(db_path)
    init_schema(conn)
    try:
        await process_deep_read_queue(conn, os.path.dirname(db_path))
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


def run_unread_summary_rollback(db_path: str, *, run_id: str) -> SummaryRewriteRollbackResult:
    conn = connect(db_path)
    init_schema(conn)
    try:
        result = rollback_summary_rewrite(conn, run_id)
        print(json.dumps(asdict(result), sort_keys=True))
        return result
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=[
            "fetch",
            "fetch-channel",
            "digest",
            "deep-read",
            "rewrite-unread-summaries",
            "rollback-unread-summaries",
        ],
        required=True,
    )
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "/data/beehive.db"))
    parser.add_argument("--high-water-item-id", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--canary-limit", type=int)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-rewrite", action="store_true")
    parser.add_argument("--confirm-rollback", action="store_true")
    args = parser.parse_args()

    if args.mode == "fetch":
        asyncio.run(run_fetch(args.db_path))
    elif args.mode == "fetch-channel":
        asyncio.run(run_fetch_channel(args.db_path))
    elif args.mode == "deep-read":
        asyncio.run(run_deep_read(args.db_path))
    elif args.mode == "rewrite-unread-summaries":
        if args.run_id is None or args.high_water_item_id is None:
            parser.error("rewrite-unread-summaries requires --run-id and --high-water-item-id")
        if args.dry_run == args.confirm_rewrite:
            parser.error(
                "rewrite-unread-summaries requires exactly one of "
                "--dry-run or --confirm-rewrite"
            )
        result = asyncio.run(run_unread_summary_rewrite(
            args.db_path,
            high_water_item_id=args.high_water_item_id,
            run_id=args.run_id,
            dry_run=args.dry_run,
            canary_limit=args.canary_limit,
            after_id=args.after_id,
        ))
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
