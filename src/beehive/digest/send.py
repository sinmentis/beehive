"""Compose and send one daily digest per effective recipient.

Each Channel owns its content watermark and delivered digest date. A successful recipient
group advances only its Channels; failures are collected after all independent groups have
been attempted."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from beehive.db.channels import list_channels, mark_digest_sent
from beehive.db.items import list_new_since
from beehive.db.sources import list_by_channel as list_sources
from beehive.digest.compose import (
    ChannelDigest,
    compose_channel_digest,
    render_digest_email,
    render_digest_email_html,
)
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    resolve_channel_email,
)
from beehive.localization import Localizer
from beehive.notify import Notifier

_EPOCH = "1970-01-01T00:00:00"


class RecipientDeliveryError(RuntimeError):
    def __init__(self, recipient: str, error: Exception):
        super().__init__(f"Digest delivery to {recipient} failed: {error}")
        self.recipient = recipient
        self.error = error


def send_daily_digest(conn: sqlite3.Connection, notifier: Notifier,
                      default_recipient: ResolvedRecipient,
                      localizer: Localizer,
                      now: datetime | None = None) -> None:
    run_time = now or datetime.now(timezone.utc)
    sent_at = run_time.isoformat()
    digest_date = run_time.date().isoformat()
    groups: dict[str, list[tuple[int, ChannelDigest]]] = {}
    failures: list[Exception] = []

    for channel in list_channels(conn):
        if channel["kind"] != "editorial":
            # 'monitor' Channels alert instantly on a detected state change instead of
            # batching into a daily digest -- see run_channel_cycle's ranking skip for the
            # matching half of this design.
            continue
        if channel["last_digest_date"] == digest_date:
            continue
        try:
            recipient = resolve_channel_email(channel, default_recipient)
            if recipient.address is None:
                raise EmailConfigurationError(
                    f'Channel "{channel["name"]}" has no email recipient')
        except EmailConfigurationError as exc:
            print(f'[digest] Channel "{channel["name"]}" has no valid email recipient, '
                  f'skipping it: {exc}')
            failures.append(exc)
            continue

        since = channel["last_digest_sent_at"] or _EPOCH
        new_items = [
            item for item in list_new_since(conn, channel["id"], since)
            if item["ai_score"] is not None
            and item["ai_score"] >= channel["minimum_score"]
        ]
        warnings = [
            localizer.text("background.source_fetch_warning",
                            source_type=source["type"], error=source["last_fetch_error"])
            for source in list_sources(conn, channel["id"])
            if source["last_fetch_error"]
        ]
        digest = compose_channel_digest(
            channel["name"],
            new_items,
            warnings,
            highlight_count=channel["highlight_count"],
        )
        groups.setdefault(recipient.address, []).append(
            (channel["id"], digest))

    for recipient, entries in groups.items():
        channel_ids = [channel_id for channel_id, _digest in entries]
        channel_digests = [digest for _channel_id, digest in entries]
        subject, plain_text = render_digest_email(
            channel_digests, digest_date, localizer)
        html = render_digest_email_html(
            channel_digests, digest_date, localizer)
        try:
            notifier.send(
                subject, plain_text, html, to_addr=recipient)
        except Exception as exc:
            failures.append(RecipientDeliveryError(recipient, exc))
            continue
        mark_digest_sent(
            conn, channel_ids, sent_at=sent_at, digest_date=digest_date)

    if failures:
        raise ExceptionGroup("One or more digest recipient groups failed", failures)
