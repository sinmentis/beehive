"""Compose and send one periodic digest per due email group, driven entirely by actionable
item_events (a discovery, a price drop, a return to stock) rather than by any item's fetched_at
watermark. This is what stops a deployment from backfilling: a historical item that was never the
subject of a recorded, AI-approved event simply has nothing to deliver.

Each email group owns its own send cadence plus two checkpoints: last_checked_at (advanced every
time a due group is evaluated, even when it finds nothing to send) and last_sent_at (advanced only
when an email actually goes out). A due group with no ready events and no current Source warnings
sends nothing and advances only last_checked_at, so scheduling.email_group_is_due still paces it
without ever pretending an email was sent. A group that does have content sends exactly one email
covering every member Channel with something to say, marks exactly the included event ids
delivered, and advances both checkpoints; the events left beyond a Channel's highlight_count cap
stay undelivered for the next due evaluation. Failures (a delivery error, or a missing recipient
when content genuinely exists) are collected after every independent group has been attempted and
raised as one ExceptionGroup, so one group's failure never blocks the others and its events retry
untouched next cycle."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from beehive.db.channels import mark_digest_sent
from beehive.db.email_groups import (
    list_email_groups,
    list_member_channels,
    mark_checked,
    mark_sent,
)
from beehive.db.item_events import list_ready_events_for_channels, mark_events_delivered
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


def _events_by_channel(events: list[dict]) -> dict[int, list[dict]]:
    """Group the flat, already-ordered deliverable events by their Channel id, preserving the
    (ai_score desc, observed_at asc, id asc) order the query established so a per-Channel slice
    keeps the highest-scored events."""
    grouped: dict[int, list[dict]] = defaultdict(list)
    for event in events:
        grouped[event["channel_id"]].append(event)
    return grouped


def send_email_group_digests(conn: sqlite3.Connection, notifier: Notifier,
                             default_recipient: ResolvedRecipient,
                             localizer: Localizer,
                             now: datetime | None = None) -> None:
    run_time = now or datetime.now(timezone.utc)
    checkpoint = run_time.isoformat()
    digest_date = run_time.date().isoformat()
    failures: list[Exception] = []

    for group in list_email_groups(conn):
        if not email_group_is_due(group, run_time):
            continue
        try:
            _deliver_due_group(
                conn, group, notifier, default_recipient, localizer,
                checkpoint=checkpoint, digest_date=digest_date)
        except EmailConfigurationError as exc:
            print(f'[digest] Email group "{group["name"]}" has content to send but no valid '
                  f"email recipient, skipping it: {exc}")
            failures.append(exc)
        except RecipientDeliveryError as exc:
            failures.append(exc)

    if failures:
        raise ExceptionGroup("One or more email groups failed", failures)


def _deliver_due_group(conn: sqlite3.Connection, group: dict, notifier: Notifier,
                       default_recipient: ResolvedRecipient, localizer: Localizer,
                       *, checkpoint: str, digest_date: str) -> None:
    """Evaluate one already-due group. Sends at most one email; raises EmailConfigurationError or
    RecipientDeliveryError (leaving every checkpoint untouched, so the exact events retry) when a
    group with real content cannot be delivered. A group with nothing deliverable advances only
    last_checked_at and returns normally."""
    member_channels = list_member_channels(conn, group["id"])
    channel_ids = [channel["id"] for channel in member_channels]
    grouped_events = _events_by_channel(list_ready_events_for_channels(conn, channel_ids))

    channel_digests = []
    delivered_event_ids: list[int] = []
    included_channel_ids: list[int] = []
    for channel in member_channels:
        capped = grouped_events.get(channel["id"], [])[: channel["highlight_count"]]
        warnings = [
            localizer.text("background.source_fetch_warning",
                           source_type=source["type"], error=source["last_fetch_error"])
            for source in list_sources(conn, channel["id"])
            if source["last_fetch_error"]
        ]
        if not capped and not warnings:
            # This Channel has nothing to contribute this cycle -- it gets no section, and its
            # legacy digest watermark is left where it is.
            continue
        channel_digests.append(compose_channel_digest(
            channel["name"], channel["kind"], capped, warnings, localizer))
        delivered_event_ids.extend(event["id"] for event in capped)
        included_channel_ids.append(channel["id"])

    if not channel_digests:
        # No ready events and no Source warnings anywhere (including a group with no member
        # Channels): nothing to send, but the group was genuinely evaluated, so pace it forward
        # without claiming an email went out and without touching any Channel watermark.
        mark_checked(conn, group["id"], checked_at=checkpoint)
        return

    # Content exists, so a recipient is now genuinely required -- resolve it only here, so an
    # empty group with no configured recipient stays a silent check above, not an error.
    recipient = resolve_group_email(group, default_recipient)
    if recipient.address is None:
        raise EmailConfigurationError(
            f'Email group "{group["name"]}" has no email recipient')

    subject = _format_subject(group["subject_template"], digest_date)
    subject, plain_text = render_digest_email(
        channel_digests, digest_date, localizer, subject)
    html = render_digest_email_html(
        channel_digests, digest_date, localizer, subject)
    try:
        notifier.send(subject, plain_text, html, to_addr=recipient.address)
    except Exception as exc:
        raise RecipientDeliveryError(recipient.address, exc) from exc

    # Delivered: mark exactly the included event ids (never the capped-out remainder), advance the
    # included Channels' legacy digest watermark, and advance both group checkpoints.
    mark_events_delivered(conn, delivered_event_ids, delivered_at=checkpoint)
    mark_digest_sent(
        conn, included_channel_ids, sent_at=checkpoint, digest_date=digest_date)
    mark_sent(conn, group["id"], sent_at=checkpoint)
