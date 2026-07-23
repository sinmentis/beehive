"""Staging for the regular Email Group event path (see schema.sql's item_events table). An Item's
deliverable state changes -- first discovery, a price drop, a return to stock -- are recorded here
as they are detected, held while AI scoring decides whether to keep or drop the Item, and only then
made eligible for a group email. Every kind that a ChannelDefinition marks with an EmailEventType
uses this table, editorial included: an editorial Item stages a DISCOVERED event here for the Email
Group path. Monitor additionally stages price_drop / back_in_stock; tracker, whose definition
permits only DISCOVERED, stages just that. Regular Email Group delivery is entirely event-driven;
the legacy Channel digest timestamps remain observability fields, not content watermarks.

The lifecycle of one row is: recorded (observed_at set, ready_at/suppressed_at/delivered_at all
NULL) -> either marked ready (AI kept the Item) or suppressed (AI dropped it) -> if ready, listed
for delivery and finally marked delivered. The partial unique index idx_item_events_open_unique
guarantees at most one *open* (undelivered, unsuppressed) event per (item_id, event_type), so
record_or_coalesce_event folds a fresh observation into that single open row instead of inserting
a duplicate: a listing whose price ticks down repeatedly between two sends still delivers one
up-to-date price_drop, while a delivered event never blocks the next genuinely new one.

Every *_at value is a caller-supplied timestamp string (the deep_reads / research convention), so
ordering and readiness stay deterministic under frozen time in tests; this module never reads the
wall clock itself."""
from __future__ import annotations

import json
import sqlite3

from beehive.domain.channels import EmailEventType

# The canonical deliverable event types, taken from the domain enum so this module and the table
# CHECK can never drift from each other.
_EVENT_TYPES: frozenset[str] = frozenset(event_type.value for event_type in EmailEventType)


def _serialize_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _require_event_type(event_type: str) -> str:
    if event_type not in _EVENT_TYPES:
        raise ValueError(f"unknown item event type: {event_type!r}")
    return event_type


def record_or_coalesce_event(
    conn: sqlite3.Connection,
    item_id: int,
    event_type: str,
    payload: dict,
    observed_at: str,
) -> int:
    """Record a pending event for an Item, or coalesce into the existing open one of the same type.

    If an undelivered, unsuppressed event of this (item_id, event_type) already exists, its payload
    and observed_at are refreshed in place and ready_at is cleared, because the new observation is
    ranking-relevant and must pass the Channel's AI gate again before delivery. Its id is returned.
    Otherwise a fresh pending event is inserted. Returns the affected event id."""
    _require_event_type(event_type)
    payload_json = _serialize_payload(payload)

    existing = conn.execute(
        "SELECT id FROM item_events "
        "WHERE item_id = ? AND event_type = ? "
        "AND delivered_at IS NULL AND suppressed_at IS NULL",
        (item_id, event_type),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE item_events SET payload = ?, observed_at = ?, ready_at = NULL WHERE id = ?",
            (payload_json, observed_at, existing["id"]),
        )
        conn.commit()
        return existing["id"]

    cur = conn.execute(
        "INSERT INTO item_events (item_id, event_type, payload, observed_at) "
        "VALUES (?, ?, ?, ?)",
        (item_id, event_type, payload_json, observed_at),
    )
    conn.commit()
    return cur.lastrowid


def mark_item_events_ready(
    conn: sqlite3.Connection, item_id: int, ready_at: str
) -> int:
    """Promote every still-pending event for an Item to ready (AI scoring kept it). Only events
    that are unready, unsuppressed, and undelivered are touched, so this never revives a suppressed
    or already-delivered event. Returns how many events became ready."""
    cur = conn.execute(
        "UPDATE item_events SET ready_at = ? "
        "WHERE item_id = ? AND ready_at IS NULL "
        "AND suppressed_at IS NULL AND delivered_at IS NULL",
        (ready_at, item_id),
    )
    conn.commit()
    return cur.rowcount


def suppress_item_events(
    conn: sqlite3.Connection, item_id: int, suppressed_at: str
) -> int:
    """Suppress every open event for an Item (AI scoring dropped it). Targets all undelivered,
    unsuppressed events -- including any that were already marked ready -- so a listing that stops
    qualifying will not deliver a stale event. Returns how many events were suppressed."""
    cur = conn.execute(
        "UPDATE item_events SET suppressed_at = ? "
        "WHERE item_id = ? AND suppressed_at IS NULL AND delivered_at IS NULL",
        (suppressed_at, item_id),
    )
    conn.commit()
    return cur.rowcount


def _row_to_event_dict(row: sqlite3.Row) -> dict:
    event = dict(row)
    event["payload"] = json.loads(event["payload"])
    return event


def list_ready_events_for_channels(
    conn: sqlite3.Connection, channel_ids: list[int]
) -> list[dict]:
    """Every ready, unsuppressed, undelivered event whose Item belongs to one of channel_ids,
    joined to the item/source/channel context an email needs, with the payload decoded.

    Ordered by channel, then the Item's AI score high-to-low, then oldest observed event and id.
    That is exactly the priority the Email Group path caps each Channel at highlight_count on: the
    highest-scored events go out first, and a deterministic (observed_at, id) tie-break keeps the
    order stable so the events left beyond the cap are the same on the next due evaluation. An empty
    channel_ids returns []."""
    if not channel_ids:
        return []
    placeholders = ", ".join("?" for _ in channel_ids)
    rows = conn.execute(
        "SELECT item_events.*, "
        "items.external_id AS item_external_id, items.title AS item_title, "
        "items.url AS item_url, items.body AS item_body, "
        "items.ai_score AS item_ai_score, items.ai_summary AS item_ai_summary, "
        "items.raw_metadata AS item_raw_metadata, "
        "sources.id AS source_id, sources.type AS source_type, "
        "sources.config AS source_config, "
        "channels.id AS channel_id, channels.name AS channel_name, "
        "channels.kind AS channel_kind "
        "FROM item_events "
        "JOIN items ON items.id = item_events.item_id "
        "JOIN sources ON sources.id = items.source_id "
        "JOIN channels ON channels.id = sources.channel_id "
        "WHERE item_events.ready_at IS NOT NULL "
        "AND item_events.suppressed_at IS NULL "
        "AND item_events.delivered_at IS NULL "
        "AND items.superseded_at IS NULL "
        "AND items.inactive_at IS NULL "
        f"AND channels.id IN ({placeholders}) "
        "ORDER BY channels.id ASC, items.ai_score DESC, "
        "item_events.observed_at ASC, item_events.id ASC",
        channel_ids,
    ).fetchall()
    events = []
    for row in rows:
        event = _row_to_event_dict(row)
        event["item_raw_metadata"] = json.loads(event["item_raw_metadata"])
        events.append(event)
    return events


def latest_actionable_events_for_items(
    conn: sqlite3.Connection, item_ids: list[int]
) -> dict[int, dict]:
    """The single most recently observed, non-suppressed event for each of item_ids, keyed by
    item_id with its payload decoded. This is the read the Monitor page model uses to badge a
    listing with its latest actionable change (discovered / price_drop / back_in_stock).

    Suppressed events (AI dropped the Item that cycle) are excluded so a stale change never badges
    a currently-kept listing; delivery state is otherwise ignored, since a change that already went
    out in an email is still the listing's latest actionable change on the page. An item_id with no
    qualifying event is simply absent from the result, and an empty item_ids returns {}."""
    if not item_ids:
        return {}
    placeholders = ", ".join("?" for _ in item_ids)
    rows = conn.execute(
        "SELECT item_id, event_type, payload, observed_at FROM item_events "
        f"WHERE suppressed_at IS NULL AND item_id IN ({placeholders}) "
        "ORDER BY item_id ASC, observed_at DESC, id DESC",
        list(item_ids),
    ).fetchall()
    latest: dict[int, dict] = {}
    for row in rows:
        # The first row seen per item_id is its most recent event (observed_at DESC, id DESC).
        if row["item_id"] not in latest:
            latest[row["item_id"]] = _row_to_event_dict(row)
    return latest


def mark_events_delivered(
    conn: sqlite3.Connection, event_ids: list[int], delivered_at: str
) -> int:
    """Mark exactly the given event ids delivered (idempotent: an already-delivered id is skipped
    via the delivered_at IS NULL guard). Only these ids are touched -- never a whole item's events
    -- so a partially successful send never marks events it did not actually deliver. Returns how
    many rows were newly marked delivered."""
    if not event_ids:
        return 0
    placeholders = ", ".join("?" for _ in event_ids)
    cur = conn.execute(
        f"UPDATE item_events SET delivered_at = ? "
        f"WHERE id IN ({placeholders}) AND delivered_at IS NULL",
        [delivered_at, *event_ids],
    )
    conn.commit()
    return cur.rowcount
