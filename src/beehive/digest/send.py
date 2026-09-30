"""Compose and send one periodic digest per due email group, driven entirely by actionable
item_events (a discovery, a price drop, a return to stock) rather than by any item's fetched_at
watermark. This is what stops a deployment from backfilling: a historical item that was never the
subject of a recorded, AI-approved event simply has nothing to deliver.

Each email group owns its own send cadence plus two checkpoints: last_checked_at (advanced every
time a due group is evaluated, even when it finds nothing to send) and last_sent_at (advanced only
when an email actually goes out). A due group with no ready events and no current Source warnings
sends nothing and advances only last_checked_at, so scheduling.email_group_is_due still paces it
without ever pretending an email was sent. A group that does have content sends exactly one email
covering every member Channel (quiet ones get only their status line), marks exactly the included
event ids delivered, and advances both checkpoints. Which events a Channel contributes is decided
by digest/selection.py: expired events are closed unsent, events from a stale snapshot Source are
held, and the events left beyond a Channel's highlight_count cap stay undelivered for a later
email until they expire. Failures (a delivery error, or a missing recipient
when content genuinely exists) are collected after every independent group has been attempted and
raised as one ExceptionGroup, so one group's failure never blocks the others and its events retry
untouched next cycle."""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from beehive.channels.definitions import get_definition, require_channel_kind
from beehive.db.channels import mark_digest_sent
from beehive.db.email_groups import (
    list_email_groups,
    list_member_channels,
    mark_checked,
    mark_error,
    mark_sent,
)
from beehive.db.item_events import (
    list_delivered_editorial_events_for_channels,
    list_ready_events_for_channels,
    mark_events_delivered,
    suppress_events,
)
from beehive.db.items import count_active_items_by_channel
from beehive.db.sources import list_by_channel as list_sources
from beehive.digest.compose import (
    compose_channel_digest,
    render_digest_email,
    render_digest_email_html,
)
from beehive.digest.selection import news_fingerprint, select_channel_events
from beehive.domain.channels import PersistenceMode
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    resolve_group_email,
)
from beehive.localization import Localizer
from beehive.notify import Notifier
from beehive.scheduling import (
    DEFAULT_SCHEDULE_TIMEZONE,
    email_group_is_due,
    email_group_send_period,
)
from beehive.source_health import is_stale
from beehive.source_labels import source_display_name

_LOGGER = logging.getLogger(__name__)


class RecipientDeliveryError(RuntimeError):
    def __init__(self, recipient: str, error: Exception):
        super().__init__(f"Digest delivery to {recipient} failed: {error}")
        self.recipient = recipient
        self.error = error


@dataclass(frozen=True)
class EmailGroupDigestPreview:
    subject: str
    plain_text: str
    html: str
    event_count: int
    warning_count: int
    channel_count: int


@dataclass(frozen=True)
class _GroupDigestContent:
    channel_digests: list
    delivered_event_ids: list[int]
    duplicate_event_ids: list[int]
    expired_event_ids: list[int]
    included_channel_ids: list[int]
    warning_count: int


def _format_subject(template: str, digest_date: str) -> str:
    try:
        return template.format(date=digest_date)
    except (KeyError, IndexError):
        # A malformed user-entered placeholder (e.g. "{oops}") must never break sending --
        # fall back to the raw template text verbatim.
        return template


def _digest_date(group: dict, run_time: datetime) -> str:
    timezone_name = group.get("schedule_timezone") or DEFAULT_SCHEDULE_TIMEZONE
    return run_time.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def _events_by_channel(events: list[dict]) -> dict[int, list[dict]]:
    """Group the flat deliverable events by their Channel id; digest/selection.py ranks each
    Channel's slice."""
    grouped: dict[int, list[dict]] = defaultdict(list)
    for event in events:
        grouped[event["channel_id"]].append(event)
    return grouped


def _local_date(timestamp: str, timezone_name: str) -> str:
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def _source_warning(source: dict, localizer: Localizer, timezone_name: str) -> str:
    """The warning line for a failing Source, named the way the Owner sees it in the admin UI.
    From the second automatic failure in a row it also says how long the Source has been down."""
    warning = localizer.text(
        "background.source_fetch_warning",
        source_type=source_display_name(source, localizer),
        error=source["last_fetch_error"],
    )
    failures = int(source.get("consecutive_failures") or 0)
    if failures < 2:
        return warning
    last_success = source.get("last_fetch_at")
    since = (
        _local_date(last_success, timezone_name)
        if last_success
        else localizer.text("background.source_never_succeeded")
    )
    streak = localizer.text(
        "background.source_failure_streak", count=failures, last_success=since)
    return f"{warning} {streak}"


def _channel_warnings(
    channel: dict,
    sources: list[dict],
    localizer: Localizer,
    timezone_name: str,
    now: datetime,
) -> list[str]:
    snapshot = _is_snapshot(channel)
    warnings = []
    for source in sources:
        if source["paused_at"]:
            continue
        # A Source that silently stopped fetching (a dead timer, a stuck job) is worth a line in
        # every kind of Channel; only snapshot Channels also hold its updates back.
        stale = is_stale(source, channel, now)
        if source["last_fetch_error"]:
            warning = _source_warning(source, localizer, timezone_name)
        elif stale:
            warning = localizer.text(
                "background.source_not_fetched_since",
                source=source_display_name(source, localizer),
                last_success=_local_date(source["last_fetch_at"], timezone_name),
            )
        else:
            warning = None
        if warning is not None:
            if stale and snapshot:
                warning = f"{warning} {localizer.text('background.source_stale_hold')}"
            warnings.append(warning)
        if source.get("retire_hold_at"):
            warnings.append(
                localizer.text(
                    "background.source_retire_hold",
                    count=int(source.get("retire_hold_count") or 0),
                    source=source_display_name(source, localizer),
                )
            )
    return warnings


def _is_snapshot(channel: dict) -> bool:
    return (
        get_definition(require_channel_kind(channel["kind"])).persistence_mode
        is PersistenceMode.MUTABLE_SNAPSHOT
    )


def _channel_status(
    conn: sqlite3.Connection,
    channel: dict,
    sources: list[dict],
    localizer: Localizer,
    *,
    quiet: bool,
    now: datetime,
) -> str:
    """One line saying the Channel was checked: sources OK, listings tracked, and whether
    anything changed. It is what makes "no news" distinguishable from "broken"."""
    active = [source for source in sources if not source["paused_at"]]
    healthy = sum(
        1
        for source in active
        if not source["last_fetch_error"] and not is_stale(source, channel, now)
    )
    parts = [
        localizer.text("background.digest_status_sources", ok=healthy, total=len(active))
    ]
    if _is_snapshot(channel):
        parts.append(
            localizer.text(
                "background.digest_status_listings",
                count=count_active_items_by_channel(conn, channel["id"]),
            )
        )
    if quiet:
        parts.append(localizer.text("background.digest_status_no_changes"))
    return " \u00b7 ".join(parts)


def _build_group_content(
    conn: sqlite3.Connection,
    group: dict,
    localizer: Localizer,
    *,
    now: datetime,
) -> _GroupDigestContent:
    """Everything one group email would contain. Channels without events or warnings are still
    listed with their status line when the email goes out for another Channel, but they never make
    an email go out on their own."""
    group_timezone = group.get("schedule_timezone") or DEFAULT_SCHEDULE_TIMEZONE
    delivery_period = email_group_send_period(group)
    member_channels = list_member_channels(conn, group["id"])
    channel_ids = [channel["id"] for channel in member_channels]
    grouped_events = _events_by_channel(list_ready_events_for_channels(conn, channel_ids))
    seen_fingerprints = frozenset(
        fingerprint
        for event in list_delivered_editorial_events_for_channels(conn, channel_ids)
        if (
            fingerprint := news_fingerprint(
                event.get("item_title"),
                event.get("item_raw_metadata"),
            )
        )
        is not None
    )
    sections = []
    delivered_event_ids: list[int] = []
    duplicate_event_ids: list[int] = []
    expired_event_ids: list[int] = []
    included_channel_ids: list[int] = []
    warning_count = 0
    for channel in member_channels:
        sources = list_sources(conn, channel["id"])
        selection = select_channel_events(
            grouped_events.get(channel["id"], []),
            channel,
            {source["id"]: source for source in sources},
            now=now,
            seen_fingerprints=seen_fingerprints,
            delivery_period=delivery_period,
        )
        seen_fingerprints |= selection.fingerprints
        duplicate_event_ids.extend(selection.duplicate_ids)
        expired_event_ids.extend(selection.expired_ids)
        warnings = _channel_warnings(channel, sources, localizer, group_timezone, now)
        has_content = bool(selection.delivered or warnings)
        sections.append(
            (
                compose_channel_digest(
                    channel["name"],
                    channel["kind"],
                    selection.delivered,
                    warnings,
                    localizer,
                    status=_channel_status(
                        conn,
                        channel,
                        sources,
                        localizer,
                        # Held updates are changes too, just not sendable yet; the warning says so.
                        quiet=not selection.delivered and not selection.held_count,
                        now=now,
                    ),
                    remaining_count=selection.remaining_count,
                ),
                has_content,
            )
        )
        if has_content:
            delivered_event_ids.extend(event["id"] for event in selection.delivered)
            included_channel_ids.append(channel["id"])
            warning_count += len(warnings)
    channel_digests = (
        [digest for digest, _ in sections] if included_channel_ids else []
    )
    return _GroupDigestContent(
        channel_digests=channel_digests,
        delivered_event_ids=delivered_event_ids,
        duplicate_event_ids=duplicate_event_ids,
        expired_event_ids=expired_event_ids,
        included_channel_ids=included_channel_ids,
        warning_count=warning_count,
    )


def build_email_group_digest_preview(
    conn: sqlite3.Connection,
    group: dict,
    localizer: Localizer,
    *,
    now: datetime | None = None,
) -> EmailGroupDigestPreview | None:
    run_time = now or datetime.now(timezone.utc)
    digest_date = _digest_date(group, run_time)
    content = _build_group_content(conn, group, localizer, now=run_time)
    if not content.channel_digests:
        return None
    subject = _format_subject(group["subject_template"], digest_date)
    subject, plain_text = render_digest_email(
        content.channel_digests,
        digest_date,
        localizer,
        subject,
    )
    html = render_digest_email_html(
        content.channel_digests,
        digest_date,
        localizer,
        subject,
    )
    return EmailGroupDigestPreview(
        subject=subject,
        plain_text=plain_text,
        html=html,
        event_count=len(content.delivered_event_ids),
        warning_count=content.warning_count,
        channel_count=len(content.included_channel_ids),
    )


def send_email_group_digests(conn: sqlite3.Connection, notifier: Notifier,
                             default_recipient: ResolvedRecipient,
                             localizer: Localizer,
                             now: datetime | None = None) -> None:
    run_time = now or datetime.now(timezone.utc)
    checkpoint = run_time.isoformat()
    failures: list[Exception] = []

    for group in list_email_groups(conn):
        try:
            # The due-check is inside the try on purpose: it parses the group's own schedule
            # config, so one malformed row used to raise here and abandon every group after it in
            # the list -- a silent, ordering-dependent loss of unrelated digests.
            if not email_group_is_due(group, run_time):
                continue
            digest_date = _digest_date(group, run_time)
            _deliver_due_group(
                conn, group, notifier, default_recipient, localizer,
                checkpoint=checkpoint, digest_date=digest_date, run_time=run_time)
        except EmailConfigurationError as exc:
            print(f'[digest] Email group "{group["name"]}" has content to send but no valid '
                  f"email recipient, skipping it: {exc}")
            mark_error(
                conn,
                group["id"],
                error=str(exc),
                failed_at=checkpoint,
            )
            failures.append(exc)
        except RecipientDeliveryError as exc:
            mark_error(
                conn,
                group["id"],
                error=str(exc.error),
                failed_at=checkpoint,
            )
            failures.append(exc)
        except Exception as exc:  # noqa: BLE001 -- see below
            # Anything else (a rendering bug, a corrupt row, a transient DB error) is still one
            # group's problem. Without this, a single bad group stopped the whole run, and the
            # groups it starved were never marked as failed either, so nothing showed why.
            _LOGGER.exception('Email group "%s" failed unexpectedly', group["name"])
            mark_error(conn, group["id"], error=str(exc), failed_at=checkpoint)
            failures.append(exc)

    if failures:
        raise ExceptionGroup("One or more email groups failed", failures)


def _deliver_due_group(conn: sqlite3.Connection, group: dict, notifier: Notifier,
                       default_recipient: ResolvedRecipient, localizer: Localizer,
                       *, checkpoint: str, digest_date: str,
                       run_time: datetime) -> None:
    """Evaluate one already-due group. Sends at most one email; raises EmailConfigurationError or
    RecipientDeliveryError (leaving every checkpoint untouched, so the exact events retry) when a
    group with real content cannot be delivered. A group with nothing deliverable advances only
    last_checked_at and returns normally."""
    content = _build_group_content(conn, group, localizer, now=run_time)
    if not content.channel_digests:
        # No ready events and no Source warnings anywhere (including a group with no member
        # Channels): nothing to send, but the group was genuinely evaluated, so pace it forward
        # without claiming an email went out and without touching any Channel watermark.
        suppress_events(
            conn,
            content.duplicate_event_ids + content.expired_event_ids,
            suppressed_at=checkpoint,
        )
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
        content.channel_digests, digest_date, localizer, subject)
    html = render_digest_email_html(
        content.channel_digests, digest_date, localizer, subject)
    try:
        notifier.send(subject, plain_text, html, to_addr=recipient.address)
    except Exception as exc:
        raise RecipientDeliveryError(recipient.address, exc) from exc

    # Delivered: mark exactly the included event ids (never the capped-out remainder), advance the
    # included Channels' legacy digest watermark, and advance both group checkpoints.
    mark_events_delivered(
        conn,
        content.delivered_event_ids,
        delivered_at=checkpoint,
    )
    suppress_events(
        conn,
        content.duplicate_event_ids + content.expired_event_ids,
        suppressed_at=checkpoint,
    )
    mark_digest_sent(
        conn,
        content.included_channel_ids,
        sent_at=checkpoint,
        digest_date=digest_date,
    )
    mark_sent(conn, group["id"], sent_at=checkpoint)
