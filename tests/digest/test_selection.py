from __future__ import annotations

from datetime import datetime, timedelta, timezone

from beehive.digest.selection import select_channel_events

NOW = datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc)
_FRESH_FETCH = (NOW - timedelta(hours=4)).isoformat()


def _channel(kind="editorial", *, highlight_count=8):
    return {
        "id": 1,
        "kind": kind,
        "highlight_count": highlight_count,
        "fetch_schedule_mode": "calendar",
        "fetch_interval_hours": 24,
    }


def _sources(last_fetch_at=_FRESH_FETCH):
    return {10: {"id": 10, "last_fetch_at": last_fetch_at}}


def _event(event_id, *, age_hours=1, event_type="discovered", kind="editorial", score=80,
           created_at=None, title=None, publisher=None):
    metadata = {"source_name": publisher} if publisher else {}
    return {
        "id": event_id,
        "event_type": event_type,
        "observed_at": (NOW - timedelta(hours=age_hours)).isoformat(),
        "item_created_at": created_at,
        "item_ai_score": score,
        "item_title": title or f"Story {event_id}",
        "item_raw_metadata": metadata,
        "channel_kind": kind,
        "source_id": 10,
    }


def _select(events, channel, sources=None, seen=frozenset()):
    return select_channel_events(
        events, channel, sources or _sources(), now=NOW, seen_fingerprints=seen)


def test_news_older_than_three_days_expires_instead_of_lingering():
    selection = _select([_event(1, age_hours=80), _event(2, age_hours=2)], _channel())

    assert [event["id"] for event in selection.delivered] == [2]
    assert selection.expired_ids == [1]


def test_news_ages_from_its_publication_time_when_the_feed_gives_one():
    old_article = _event(1, age_hours=1, created_at=(NOW - timedelta(days=5)).isoformat())

    assert _select([old_article], _channel()).expired_ids == [1]


def test_a_fresh_price_drop_on_an_old_listing_is_not_expired():
    # For a listing, created_at is the product's listing date and says nothing about the drop.
    drop = _event(1, event_type="price_drop", kind="monitor",
                  created_at=(NOW - timedelta(days=365)).isoformat())

    assert [event["id"] for event in _select([drop], _channel("monitor")).delivered] == [1]


def test_listing_events_expire_on_their_own_clock():
    events = [
        _event(1, event_type="price_drop", kind="monitor", age_hours=4 * 24),
        _event(2, event_type="discovered", kind="monitor", age_hours=4 * 24),
        _event(3, event_type="discovered", kind="monitor", age_hours=8 * 24),
    ]

    selection = _select(events, _channel("monitor"))

    assert [event["id"] for event in selection.delivered] == [2]
    assert sorted(selection.expired_ids) == [1, 3]


def test_tracker_lots_never_expire_by_age():
    lot = _event(1, kind="tracker", age_hours=30 * 24)

    assert [event["id"] for event in _select([lot], _channel("tracker")).delivered] == [1]


def test_events_from_a_stale_snapshot_source_are_held_not_delivered_or_expired():
    stale = _sources(last_fetch_at=(NOW - timedelta(days=3)).isoformat())

    selection = _select([_event(1, kind="monitor")], _channel("monitor"), stale)

    assert selection.delivered == []
    assert selection.expired_ids == []
    assert selection.held_count == 1


def test_a_stale_editorial_source_still_delivers_the_news_it_already_fetched():
    stale = _sources(last_fetch_at=(NOW - timedelta(days=3)).isoformat())

    selection = _select([_event(1)], _channel(), stale)

    assert [event["id"] for event in selection.delivered] == [1]


def test_highest_score_wins_newest_first_on_ties_and_the_rest_are_counted():
    events = [
        _event(1, score=70, age_hours=1),
        _event(2, score=90, age_hours=5),
        _event(3, score=90, age_hours=2),
        _event(4, score=60, age_hours=1),
    ]

    selection = _select(events, _channel(highlight_count=2))

    assert [event["id"] for event in selection.delivered] == [3, 2]
    assert selection.remaining_count == 2


def test_an_already_delivered_headline_is_closed_as_a_duplicate():
    seen = frozenset({("rnz", "rates held")})
    events = [
        _event(1, title="Rates held", publisher="RNZ"),
        _event(2, title="Rates cut", publisher="RNZ"),
    ]

    selection = _select(events, _channel(), seen=seen)

    assert [event["id"] for event in selection.delivered] == [2]
    assert selection.duplicate_ids == [1]
    assert selection.fingerprints == frozenset({("rnz", "rates cut")})
