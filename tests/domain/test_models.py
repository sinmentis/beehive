from datetime import datetime, timezone

from beehive.domain.models import Channel, Item, Source


def test_channel_construction():
    c = Channel(id=1, name="NZ Finance", profile="economic news", fetch_interval_hours=3)
    assert c.name == "NZ Finance"
    assert c.fetch_interval_hours == 3


def test_source_construction():
    s = Source(id=1, channel_id=1, type="reddit_subreddit", config={"subreddit": "PersonalFinanceNZ"})
    assert s.type == "reddit_subreddit"
    assert s.config["subreddit"] == "PersonalFinanceNZ"


def test_item_construction_and_defaults():
    it = Item(
        id=None, source_id=1, external_id="t3_abc123", title="Rates fall",
        url="https://reddit.com/r/x/comments/abc123", body="",
        created_at=datetime(2026, 7, 8, tzinfo=timezone.utc), fetched_at=datetime.now(timezone.utc),
        ai_score=None, ai_summary=None, ai_rationale=None, is_read=False, raw_metadata={},
    )
    assert it.ai_score is None
    assert it.is_read is False


def test_models_are_frozen():
    c = Channel(id=1, name="X", profile="", fetch_interval_hours=3)
    try:
        c.name = "Y"  # type: ignore[misc]
        assert False, "expected FrozenInstanceError"
    except Exception as exc:
        assert type(exc).__name__ == "FrozenInstanceError"
