"""A typed handle over one Channel's persisted Items that turns a successful connector fetch into
the right persistence and actionable-event effects for the Channel's kind, reading every decision
from its ChannelDefinition rather than branching on the raw kind string.

Persistence follows the definition's persistence_mode:
  * APPEND (editorial): each fetched RawItem is inserted once and never mutated; a genuinely new
    row stages a DISCOVERED event.
  * MUTABLE_SNAPSHOT (monitor/tracker): each fetched RawItem refreshes the single current row for
    its stable external_id in place (preserving id/read/interactions), a newly inserted row stages
    DISCOVERED, and an in-place change can stage PRICE_DROP / BACK_IN_STOCK; once the whole snapshot
    is ingested, listings absent from it are reconciled to inactive. Reconciliation is why ingest
    must be called ONLY after a complete, successful fetch -- doing it on a partial/failed fetch
    would wrongly retire everything the connector could not return this time.

Which EmailEventTypes a kind may stage is exactly its definition.email_event_types, so a tracker
(whose definition permits only DISCOVERED) never stages a price/stock event even though it shares
the mutable path with monitor.

Staged events are pending until AI scoring decides the Item's fate. The collector calls
settle_item_events after each Item is scored: at or above the Channel's minimum_score the Item's
pending events become ready for Email Group delivery, below it they are suppressed. An Item that
staged nothing (an editorial backlog row, an unchanged listing) simply has nothing to settle."""
from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass

from beehive.ai.prompt_builder import ProductCandidate
from beehive.channels.definitions import (
    ChannelDefinition,
    get_definition,
    require_channel_kind,
)
from beehive.channels.events import (
    DetectedEvent,
    detect_discovered,
    detect_snapshot_events,
)
from beehive.connectors.base import RawItem
from beehive.db.connection import write_transaction
from beehive.db.item_events import (
    mark_item_events_ready,
    record_or_coalesce_event,
    suppress_item_events,
)
from beehive.db.items import (
    MutableUpsertOutcome,
    absent_active_item_ids,
    count_active_items,
    insert_new_returning_id,
    mark_items_inactive,
    upsert_mutable_item,
)
from beehive.db.sources import clear_retire_hold, get_retire_hold, set_retire_hold
from beehive.domain.channels import ChannelKind, PersistenceMode

# A snapshot is a suspected mass retirement when it would retire at least this many listings and
# at least this share of the Source's active catalogue. The absolute floor keeps small catalogues
# (a 39-item outlet selling out) from ever being held.
MASS_RETIREMENT_MIN_LISTINGS = 20
MASS_RETIREMENT_MIN_SHARE = 0.5


def is_mass_retirement(absent_count: int, active_count: int) -> bool:
    return (
        absent_count >= MASS_RETIREMENT_MIN_LISTINGS
        and absent_count >= active_count * MASS_RETIREMENT_MIN_SHARE
    )

# The raw_metadata keys a listing ranker (ai.prompt_builder.ProductCandidate) actually consumes, so
# a change to any of them re-enters the item into the ranking backlog while a change to any other
# key (an image URL, a house-keeping field) refreshes in place without a needless rerank. Derived
# from the dataclass rather than listed by hand, so a field added to the ranker can never be
# silently ignored by snapshot reranking (a hand-kept copy once missed `discount_percent`).
# title/description are compared as item columns by db/items.py, and item_key is the identity.
RANKING_METADATA_KEYS: frozenset[str] = frozenset(
    field.name for field in dataclasses.fields(ProductCandidate)
) - {"item_key", "title", "description"}


@dataclass(frozen=True)
class ChannelCollection:
    """One Channel's Item collection, resolved to its kind and ChannelDefinition. Build it with
    ``ChannelCollection.for_channel(channel_row)``; it validates the stored kind through the
    definition registry, so an unknown kind fails loudly rather than defaulting."""

    channel: dict
    kind: ChannelKind

    @classmethod
    def for_channel(cls, channel: dict) -> ChannelCollection:
        return cls(channel=channel, kind=require_channel_kind(channel["kind"]))

    @property
    def definition(self) -> ChannelDefinition:
        return get_definition(self.kind)

    @property
    def is_mutable(self) -> bool:
        """Whether this Channel persists as a reconciled snapshot (monitor/tracker) rather than an
        append-only feed (editorial)."""
        return self.definition.persistence_mode is PersistenceMode.MUTABLE_SNAPSHOT

    def ingest_fetch(
        self,
        conn: sqlite3.Connection,
        source_id: int,
        raw_items: list[RawItem],
        *,
        now_iso: str,
        baseline: bool = False,
    ) -> int:
        """Persist one complete, successful fetch for source_id and stage its actionable events.
        Returns the count of genuinely inserted rows (for last_fetch_new_count). MUST NOT be called
        for a failed or partial fetch: a MUTABLE_SNAPSHOT Channel reconciles absent listings to
        inactive here, which would wrongly retire everything a failed fetch omitted.

        The whole fetch is one transaction: readers never see a half-applied snapshot, a failure
        part-way leaves the previous state untouched, and the database commits once rather than
        once per listing.

        baseline marks a snapshot Source's first successful fetch: its listings are recorded
        without DISCOVERED events, because a catalogue's existing stock is not news. Without it,
        adding a store queued its whole catalogue for email."""
        with write_transaction(conn):
            if self.is_mutable:
                return self._ingest_snapshot(
                    conn, source_id, raw_items, now_iso=now_iso, baseline=baseline)
            return self._ingest_append(conn, source_id, raw_items, now_iso=now_iso)

    def _ingest_append(
        self,
        conn: sqlite3.Connection,
        source_id: int,
        raw_items: list[RawItem],
        *,
        now_iso: str,
    ) -> int:
        permitted = self.definition.email_event_types
        new_count = 0
        for raw_item in raw_items:
            item_id = insert_new_returning_id(conn, source_id, raw_item)
            if item_id is None:
                continue
            new_count += 1
            self._stage_events(conn, item_id, detect_discovered(permitted), now_iso)
        return new_count

    def _ingest_snapshot(
        self,
        conn: sqlite3.Connection,
        source_id: int,
        raw_items: list[RawItem],
        *,
        now_iso: str,
        baseline: bool = False,
    ) -> int:
        permitted = self.definition.email_event_types
        new_count = 0
        present_external_ids: list[str] = []
        for raw_item in raw_items:
            present_external_ids.append(raw_item.external_id)
            result = upsert_mutable_item(
                conn,
                source_id,
                raw_item,
                now_iso=now_iso,
                ranking_metadata_keys=RANKING_METADATA_KEYS,
            )
            if result.outcome is MutableUpsertOutcome.INSERTED:
                new_count += 1
                events = [] if baseline else detect_discovered(permitted)
            else:
                events = detect_snapshot_events(
                    result.before_metadata or {}, result.after_metadata, permitted
                )
            self._stage_events(conn, result.item_id, events, now_iso)
        # Reconcile only after the full snapshot is ingested, so a listing present later in the same
        # fetch is never briefly retired. Safe with an empty snapshot: every active listing for the
        # Source then goes inactive, which is the correct meaning of "the connector returned none"
        # -- unless the retirement guard below holds it for a second, confirming snapshot.
        absent_item_ids = absent_active_item_ids(conn, source_id, present_external_ids)
        if self._hold_mass_retirement(conn, source_id, absent_item_ids, now_iso):
            return new_count
        mark_items_inactive(conn, absent_item_ids, now_iso=now_iso)
        for item_id in absent_item_ids:
            suppress_item_events(conn, item_id, now_iso)
        return new_count

    def _hold_mass_retirement(
        self,
        conn: sqlite3.Connection,
        source_id: int,
        absent_item_ids: list[int],
        now_iso: str,
    ) -> bool:
        """Debounce a snapshot that would retire most of a catalogue at once. A storefront that
        briefly returns an empty or partial page looks exactly like a mass delisting, and acting on
        it would retire every listing and permanently suppress their pending events. The first such
        snapshot is held (listings stay active, the hold is recorded and logged); a second one in a
        row confirms it and is applied. Any ordinary snapshot in between clears the hold."""
        if not self.definition.guard_mass_retirement:
            return False
        active_count = count_active_items(conn, source_id)
        suspicious = is_mass_retirement(len(absent_item_ids), active_count)
        held_since = get_retire_hold(conn, source_id)
        if suspicious and held_since is None:
            set_retire_hold(conn, source_id, held_at=now_iso, count=len(absent_item_ids))
            print(
                f"[ingest] source {source_id}: snapshot would retire {len(absent_item_ids)} of "
                f"{active_count} active listings; holding until the next fetch confirms",
                flush=True,
            )
            return True
        if held_since is not None:
            clear_retire_hold(conn, source_id)
            if suspicious:
                print(
                    f"[ingest] source {source_id}: second snapshot in a row confirms "
                    f"{len(absent_item_ids)} retirements held since {held_since}",
                    flush=True,
                )
        return False

    def _stage_events(
        self,
        conn: sqlite3.Connection,
        item_id: int,
        events: list[DetectedEvent],
        now_iso: str,
    ) -> None:
        for event in events:
            record_or_coalesce_event(
                conn, item_id, event.event_type.value, event.payload, now_iso
            )

    def settle_item_events(
        self,
        conn: sqlite3.Connection,
        item_id: int,
        score: float,
        *,
        now_iso: str,
    ) -> None:
        """Gate an Item's pending actionable events on its fresh AI score: at or above the Channel's
        minimum_score they become ready for Email Group delivery, below it they are suppressed. A
        no-op for an Item that staged no events (an unchanged listing or an editorial backlog row),
        since both calls only touch still-open events."""
        if score >= self.channel["minimum_score"]:
            mark_item_events_ready(conn, item_id, now_iso)
        else:
            suppress_item_events(conn, item_id, now_iso)
