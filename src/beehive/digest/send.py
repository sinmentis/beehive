"""Compose and send one periodic digest per due, non-empty email group.

Each Channel owns its own content watermark (last_digest_sent_at); each email group owns its
own send cadence and last_sent_at. A successful group's send advances every one of its member
Channels' watermarks plus its own last_sent_at; failures are collected after all independent
groups have been attempted, so one group's failure never blocks the others."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from beehive.db.channels import mark_digest_sent
from beehive.db.email_groups import list_email_groups, list_member_channels, mark_sent
from beehive.db.items import list_new_since
from beehive.db.sources import list_by_channel as list_sources
from beehive.digest.compose import (
    compose_channel_digest,
    render_digest_email,
    render_digest_email_html,
)
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    resolve_group_email,
)
from beehive.localization import Localizer
from beehive.notify import Notifier
from beehive.scheduling import email_group_is_due

_EPOCH = "1970-01-01T00:00:00"


class RecipientDeliveryError(RuntimeError):
    def __init__(self, recipient: str, error: Exception):
        super().__init__(f"Digest delivery to {recipient} failed: {error}")
        self.recipient = recipient
        self.error = error


def _format_subject(template: str, digest_date: str) -> str:
    try:
        return template.format(date=digest_date)
    except (KeyError, IndexError):
        # A malformed user-entered placeholder (e.g. "{oops}") must never break sending --
        # fall back to the raw template text verbatim.
        return template


def send_email_group_digests(conn: sqlite3.Connection, notifier: Notifier,
                             default_recipient: ResolvedRecipient,
                             localizer: Localizer,
                             now: datetime | None = None) -> None:
    run_time = now or datetime.now(timezone.utc)
    sent_at = run_time.isoformat()
    digest_date = run_time.date().isoformat()
    failures: list[Exception] = []

    for group in list_email_groups(conn):
        if not email_group_is_due(group, run_time):
            continue
        member_channels = list_member_channels(conn, group["id"])
        if not member_channels:
            # An empty group (no Channels assigned yet) never sends.
            continue

        try:
            recipient = resolve_group_email(group, default_recipient)
            if recipient.address is None:
                raise EmailConfigurationError(
                    f'Email group "{group["name"]}" has no email recipient')
        except EmailConfigurationError as exc:
            print(f'[digest] Email group "{group["name"]}" has no valid email recipient, '
                  f'skipping it: {exc}')
            failures.append(exc)
            continue

        channel_ids = []
        channel_digests = []
        for channel in member_channels:
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
            channel_ids.append(channel["id"])
            channel_digests.append(digest)

        subject = _format_subject(group["subject_template"], digest_date)
        subject, plain_text = render_digest_email(
            channel_digests, digest_date, localizer, subject)
        html = render_digest_email_html(
            channel_digests, digest_date, localizer, subject)
        try:
            notifier.send(
                subject, plain_text, html, to_addr=recipient.address)
        except Exception as exc:
            failures.append(RecipientDeliveryError(recipient.address, exc))
            continue
        mark_digest_sent(
            conn, channel_ids, sent_at=sent_at, digest_date=digest_date)
        mark_sent(conn, group["id"], sent_at=sent_at)

    if failures:
        raise ExceptionGroup("One or more email groups failed", failures)
