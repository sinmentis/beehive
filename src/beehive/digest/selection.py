"""Which ready events go into one Channel's section of a digest. A pure function of the events,
the Channel, its Sources and the clock, so the whole delivery policy is testable without a
database.

1. Expired: an event older than its kind's max age (ChannelDefinition.event_max_age) is closed
   unsent. Editorial news ages from its publication time when the feed gives one, because an old
   article seen for the first time today is still old news. Listing changes age from when they
   were observed, because a product's listing date says nothing about a new price. The max age is
   never shorter than the group's own send period plus a day, so a weekly group still receives
   the whole week.
2. Held: in a snapshot Channel, events from a Source whose data is stale
   (source_health.is_stale) stay open but are left out, because the prices and stock behind them
   are out of date. They go out once the Source recovers, or expire.
3. Duplicates: an editorial headline already delivered, or already picked for this email, is
   closed unsent.
4. Ranked by AI score, newest first on ties. The first highlight_count are delivered; the rest
   stay open for a later email (until they expire) and are reported as "N more"."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from beehive.channels.definitions import get_definition, require_channel_kind
from beehive.domain.channels import EmailEventType, PersistenceMode
from beehive.source_health import is_stale

Fingerprint = tuple[str, str]

# Slack on top of the group's send period: a send that runs a little late, or fails once and is
# retried the next day, must not lose events that were waiting for it.
DELIVERY_SLACK = timedelta(days=1)


@dataclass(frozen=True)
class ChannelSelection:
    delivered: list[dict]
    remaining_count: int
    expired_ids: list[int]
    duplicate_ids: list[int]
    held_count: int
    fingerprints: frozenset[Fingerprint]


def _fingerprint_part(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    return normalized or None


def news_fingerprint(title: object, metadata: object) -> Fingerprint | None:
    """(publisher, headline), normalized, for spotting the same story republished."""
    if not isinstance(metadata, dict):
        return None
    publisher = _fingerprint_part(metadata.get("source_name"))
    normalized_title = _fingerprint_part(title)
    if publisher is None or normalized_title is None:
        return None
    return publisher, normalized_title


def _editorial_fingerprint(event: dict) -> Fingerprint | None:
    if event.get("channel_kind") != "editorial" or event.get("event_type") != "discovered":
        return None
    return news_fingerprint(event.get("item_title"), event.get("item_raw_metadata"))


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def event_age_anchor(event: dict, persistence_mode: PersistenceMode) -> datetime:
    observed = _as_utc(event["observed_at"])
    if persistence_mode is PersistenceMode.APPEND and event.get("event_type") == "discovered":
        created = event.get("item_created_at")
        if created:
            try:
                return min(observed, _as_utc(created))
            except ValueError:
                return observed
    return observed


def select_channel_events(
    events: list[dict],
    channel: dict,
    sources_by_id: dict[int, dict],
    *,
    now: datetime,
    seen_fingerprints: frozenset[Fingerprint],
    delivery_period: timedelta,
) -> ChannelSelection:
    """Apply the policy above to one Channel's ready events. seen_fingerprints holds editorial
    headlines already delivered or already picked earlier in this email; the headlines picked here
    are returned in `fingerprints` for the caller to carry into the next Channel. delivery_period
    is the Email Group's longest wait between two emails (scheduling.email_group_send_period)."""
    definition = get_definition(require_channel_kind(channel["kind"]))
    snapshot = definition.persistence_mode is PersistenceMode.MUTABLE_SNAPSHOT
    stale_source_ids = {
        source_id
        for source_id, source in sources_by_id.items()
        if snapshot and is_stale(source, channel, now)
    }

    expired_ids: list[int] = []
    eligible: list[dict] = []
    held_count = 0
    minimum_max_age = delivery_period + DELIVERY_SLACK
    for event in events:
        max_age = definition.event_max_age.get(EmailEventType(event["event_type"]))
        if max_age is not None:
            max_age = max(max_age, minimum_max_age)
        if max_age is not None and now - event_age_anchor(
            event, definition.persistence_mode
        ) > max_age:
            expired_ids.append(event["id"])
        elif event.get("source_id") in stale_source_ids:
            held_count += 1
        else:
            eligible.append(event)

    eligible.sort(
        key=lambda event: (
            -(event.get("item_ai_score") or 0),
            -_as_utc(event["observed_at"]).timestamp(),
            -event["id"],
        )
    )
    delivered: list[dict] = []
    duplicate_ids: list[int] = []
    picked: set[Fingerprint] = set()
    remaining_count = 0
    for event in eligible:
        fingerprint = _editorial_fingerprint(event)
        if fingerprint is not None and (fingerprint in seen_fingerprints or fingerprint in picked):
            duplicate_ids.append(event["id"])
        elif len(delivered) >= channel["highlight_count"]:
            remaining_count += 1
        else:
            delivered.append(event)
            if fingerprint is not None:
                picked.add(fingerprint)

    return ChannelSelection(
        delivered=delivered,
        remaining_count=remaining_count,
        expired_ids=expired_ids,
        duplicate_ids=duplicate_ids,
        held_count=held_count,
        fingerprints=frozenset(picked),
    )
