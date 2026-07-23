"""Unit tests for the pure actionable-event detection in beehive.channels.events: given a
listing's before/after raw_metadata and the events a Channel's definition permits, exactly which
EmailEventTypes fire and with what payload. No database or collector is involved here."""
from beehive.channels.events import (
    DetectedEvent,
    detect_discovered,
    detect_snapshot_events,
)
from beehive.domain.channels import EmailEventType

_ALL = frozenset(EmailEventType)
_DISCOVERED_ONLY = frozenset({EmailEventType.DISCOVERED})
_MONITOR = frozenset(
    {
        EmailEventType.DISCOVERED,
        EmailEventType.PRICE_DROP,
        EmailEventType.BACK_IN_STOCK,
    }
)


def test_detect_discovered_when_permitted():
    events = detect_discovered(_ALL)
    assert events == [DetectedEvent(EmailEventType.DISCOVERED, {})]


def test_detect_discovered_empty_when_not_permitted():
    assert detect_discovered(frozenset({EmailEventType.PRICE_DROP})) == []


def test_price_drop_fires_with_old_and_new_price():
    events = detect_snapshot_events({"price": 50.0}, {"price": 40.0}, _MONITOR)
    assert events == [
        DetectedEvent(EmailEventType.PRICE_DROP, {"old_price": 50.0, "new_price": 40.0})
    ]


def test_price_increase_does_not_fire():
    assert detect_snapshot_events({"price": 40.0}, {"price": 50.0}, _MONITOR) == []


def test_unchanged_price_does_not_fire():
    assert detect_snapshot_events({"price": 40.0}, {"price": 40.0}, _MONITOR) == []


def test_price_drop_requires_two_numeric_prices():
    # A missing, None, string, or boolean price on either side is not a numeric comparison.
    assert detect_snapshot_events({"price": None}, {"price": 40.0}, _MONITOR) == []
    assert detect_snapshot_events({"price": "50"}, {"price": 40.0}, _MONITOR) == []
    assert detect_snapshot_events({}, {"price": 40.0}, _MONITOR) == []
    assert detect_snapshot_events({"price": 50.0}, {"price": None}, _MONITOR) == []
    assert detect_snapshot_events({"price": True}, {"price": 40.0}, _MONITOR) == []


def test_back_in_stock_fires_on_false_to_true():
    events = detect_snapshot_events(
        {"available": False}, {"available": True}, _MONITOR
    )
    assert events == [DetectedEvent(EmailEventType.BACK_IN_STOCK, {})]


def test_back_in_stock_requires_previous_explicit_false():
    # A previously-True, previously-missing, or previously-None availability is not the
    # explicit false->true transition the rule requires.
    assert detect_snapshot_events({"available": True}, {"available": True}, _MONITOR) == []
    assert detect_snapshot_events({}, {"available": True}, _MONITOR) == []
    assert detect_snapshot_events({"available": None}, {"available": True}, _MONITOR) == []


def test_back_in_stock_requires_current_true():
    assert detect_snapshot_events(
        {"available": False}, {"available": False}, _MONITOR
    ) == []


def test_price_drop_and_back_in_stock_can_fire_together():
    events = detect_snapshot_events(
        {"price": 50.0, "available": False},
        {"price": 30.0, "available": True},
        _MONITOR,
    )
    assert set(e.event_type for e in events) == {
        EmailEventType.PRICE_DROP,
        EmailEventType.BACK_IN_STOCK,
    }


def test_snapshot_events_gated_by_permitted_set():
    # A tracker permits only DISCOVERED, so neither snapshot event fires even on a clear
    # price-drop-and-restock transition.
    events = detect_snapshot_events(
        {"price": 50.0, "available": False},
        {"price": 30.0, "available": True},
        _DISCOVERED_ONLY,
    )
    assert events == []
