"""The home page's channel desk: what each kind of Channel shows, in what order, for whom, and
that none of it builds a whole Channel page."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.channels.views import TrackerQuery, build_channel_page
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel, get_channel
from beehive.db.connection import connect, init_schema
from beehive.db.item_events import record_or_coalesce_event
from beehive.db.items import (
    count_active_monitor_listings_by_channel,
    count_dashboard_signals_by_channel,
    insert_new,
    update_ai_ranking,
)
from beehive.db.sessions import create_session
from beehive.db.sources import create_source
from beehive.db.tracker_watches import add_tracker_watch
from beehive.localization import load_localizer
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password

_SECRET = "test-secret-at-least-32-characters-long"


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    c = connect(path)
    init_schema(c)
    return path, c


@pytest.fixture
def client(conn):
    path, _ = conn
    return TestClient(create_app(path))


@pytest.fixture
def authed_client(conn):
    path, c = conn
    set_admin_password(path, "correct-password")
    create_session(c, "sess1", "csrf1", "2099-01-01T00:00:00")
    client = TestClient(create_app(path, session_secret=_SECRET), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", _SECRET))
    return client


def _item_id(c, external_id):
    return c.execute("SELECT id FROM items WHERE external_id = ?", (external_id,)).fetchone()[0]


def _story(c, source_id, external_id, score, *, metadata=None, read=False):
    insert_new(
        c,
        source_id,
        RawItem(
            external_id=external_id,
            title=f"Title {external_id}",
            url=f"https://example.com/{external_id}",
            raw_metadata=metadata or {},
        ),
    )
    update_ai_ranking(c, source_id, external_id, score=score, summary=f"Summary {external_id}", rationale="r")
    if read:
        c.execute("UPDATE items SET is_read = 1 WHERE external_id = ?", (external_id,))
        c.commit()


def _listing(c, source_id, external_id, score, *, available=True, price=199.0, compare_at=299.0):
    insert_new(
        c,
        source_id,
        RawItem(
            external_id=external_id,
            title=f"Jacket {external_id}",
            url=f"https://shop.example/{external_id}",
            raw_metadata={
                "price": price,
                "compare_at_price": compare_at,
                "on_sale": compare_at is not None,
                "available": available,
                "vendor": "Arc'teryx",
            },
        ),
    )
    if score is not None:
        update_ai_ranking(c, source_id, external_id, score=score, summary="s", rationale="r")


def _lot(c, source_id, external_id, score, *, closes_in_hours):
    closes_at = datetime.now(timezone.utc) + timedelta(hours=closes_in_hours)
    insert_new(
        c,
        source_id,
        RawItem(
            external_id=external_id,
            title=f"Lot {external_id}",
            url=f"https://auctions.example/{external_id}",
            raw_metadata={
                "auction_title": "Weekly auction",
                "closing_at": closes_at.isoformat(),
                "currency_code": "NZD",
                "current_bid": 50.0,
            },
        ),
    )
    update_ai_ranking(c, source_id, external_id, score=score, summary="s", rationale="r")


def _section(response, channel_id):
    return next(section for section in response.context["desk"] if section.channel_id == channel_id)


def test_owner_desk_puts_unread_stories_first_and_readers_see_the_best(conn, client, authed_client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    _story(c, source_id, "best-but-read", 95, read=True)
    for external_id, score in (("a", 90), ("b", 85), ("c", 80), ("d", 75)):
        _story(c, source_id, external_id, score)

    owner = _section(authed_client.get("/"), channel_id)
    reader = _section(client.get("/"), channel_id)

    assert [story.ai_score for story in owner.stories] == [90, 85, 80, 75]
    assert [story.ai_score for story in reader.stories] == [95, 90, 85, 80]
    assert (owner.story_total, owner.story_unread) == (5, 4)


def test_counts_by_channel_match_the_featured_rules(conn):
    _, c = conn
    first = create_channel(c, "First", "p", minimum_score=50)
    second = create_channel(c, "Second", "p")
    first_source = create_source(c, first, "reddit_subreddit", {"subreddit": "a"})
    second_source = create_source(c, second, "reddit_subreddit", {"subreddit": "b"})
    _story(c, first_source, "kept", 60)
    _story(c, first_source, "below-minimum", 40)
    _story(c, first_source, "read", 70, read=True)
    _story(c, second_source, "other", 90)

    counts = count_dashboard_signals_by_channel(c, high_score=65)

    assert counts == {
        first: {"all": 2, "unread": 1, "high": 1},
        second: {"all": 1, "unread": 1, "high": 1},
    }


def test_story_byline_names_the_outlet_and_only_real_engagement(conn, client):
    _, c = conn
    channel_id = create_channel(c, "News", "p")
    news = create_source(c, channel_id, "google_news_query", {"query": "nz economy"})
    forum = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "newzealand"})
    blogs_id = create_channel(c, "Blogs", "p")
    feed = create_source(c, blogs_id, "rss_feed", {"feed_url": "https://trail.example/feed"})
    _story(c, news, "outlet", 90, metadata={"source_name": "Reuters"})
    _story(c, feed, "blog", 88, metadata={"feed_title": "Trail Notes"})
    _story(c, feed, "untitled-feed", 87, metadata={})
    _story(c, forum, "quiet", 85, metadata={"score": 0, "num_comments": 0})
    _story(c, forum, "busy", 80, metadata={"score": 12, "num_comments": 3})

    stories = {story.id: story for story in _section(client.get("/"), channel_id).stories}

    assert stories[_item_id(c, "outlet")].byline == ("Reuters",)
    blog_stories = {story.id: story for story in _section(client.get("/"), blogs_id).stories}
    assert blog_stories[_item_id(c, "blog")].byline == ("Trail Notes",)
    assert blog_stories[_item_id(c, "untitled-feed")].byline == ("trail.example/feed",)
    assert stories[_item_id(c, "quiet")].byline == ("r/newzealand",)
    assert stories[_item_id(c, "busy")].byline == ("r/newzealand", "12 upvotes · 3 comments")


def test_monitor_section_lists_the_best_live_listings_like_the_channel_page(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Outdoor gear", "deals", kind="monitor")
    source_id = create_source(
        c, channel_id, "shopify_collection", {"collection_url": "https://shop.example/collections/sale"}
    )
    _listing(c, source_id, "good", 90)
    _listing(c, source_id, "best", 95, price=120.0, compare_at=None)
    _listing(c, source_id, "sold-out", 99, available=False)
    _listing(c, source_id, "gone", 98)
    _listing(c, source_id, "unranked", None)
    c.execute("UPDATE items SET inactive_at = '2026-09-01T00:00:00' WHERE external_id = 'gone'")
    c.commit()
    record_or_coalesce_event(
        c, _item_id(c, "good"), "price_drop", {"old_price": 249.0, "new_price": 199.0},
        "2026-09-02T00:00:00",
    )

    response = client.get("/")
    section = _section(response, channel_id)

    assert [listing.title for listing in section.listings] == [
        "Jacket best", "Jacket good", "Jacket unranked",
    ]
    assert section.listing_total == 3
    now = datetime.now(timezone.utc)
    page = build_channel_page(
        c, get_channel(c, channel_id), t=load_localizer(c), now=now
    )
    assert section.listing_total == page.pagination.total
    assert '<span class="desk-state nw">3 live listings</span>' in response.text
    assert '<span class="price-off">−33%</span>' in response.text
    assert '<s class="price-was">299</s>' in response.text
    assert '<small class="chg">Price drop</small>' in response.text


def test_tracker_section_shows_open_good_lots_soonest_first(conn, client, authed_client):
    _, c = conn
    channel_id = create_channel(c, "Auctions", "lots", kind="tracker")
    source_id = create_source(c, channel_id, "all_about_auctions", {})
    _lot(c, source_id, "later", 85, closes_in_hours=30)
    _lot(c, source_id, "sooner", 90, closes_in_hours=2)
    _lot(c, source_id, "weak", 70, closes_in_hours=5)
    _lot(c, source_id, "closed", 95, closes_in_hours=-3)
    _lot(c, source_id, "delisted", 88, closes_in_hours=10)
    c.execute("UPDATE items SET inactive_at = '2026-09-01T00:00:00' WHERE external_id = 'delisted'")
    c.commit()
    add_tracker_watch(c, _item_id(c, "later"), datetime.now(timezone.utc))

    owner_response = authed_client.get("/")
    owner = _section(owner_response, channel_id)
    reader = _section(client.get("/"), channel_id)

    assert [lot.title for lot in owner.lots] == ["Lot sooner", "Lot later"]
    assert (owner.lot_total, owner.lot_minimum_score, owner.watched_total) == (2, 80, 1)
    assert reader.watched_total is None
    assert not any(lot.is_watched for lot in reader.lots)
    assert '<span class="desk-state nw">2 open lots at 80+</span>' in owner_response.text
    assert '<span class="desk-state nw">1 watched</span>' in owner_response.text
    assert '<span class="st st-busy">Watching</span>' in owner_response.text


def test_monitor_counts_are_grouped_and_use_each_channels_minimum(conn):
    _, c = conn
    lenient = create_channel(c, "Lenient", "p", kind="monitor")
    strict = create_channel(c, "Strict", "p", kind="monitor", minimum_score=90)
    empty = create_channel(c, "Empty", "p", kind="monitor")
    lenient_source = create_source(c, lenient, "shopify_collection", {"collection_url": "https://a.example/c"})
    strict_source = create_source(c, strict, "shopify_collection", {"collection_url": "https://b.example/c"})
    for source_id, prefix in ((lenient_source, "l"), (strict_source, "s")):
        _listing(c, source_id, f"{prefix}-high", 95)
        _listing(c, source_id, f"{prefix}-low", 50)
        _listing(c, source_id, f"{prefix}-unranked", None)
        _listing(c, source_id, f"{prefix}-sold-out", 99, available=False)

    counts = count_active_monitor_listings_by_channel(c, [lenient, strict, empty])

    assert counts == {lenient: 3, strict: 2}
    assert count_active_monitor_listings_by_channel(c, []) == {}


def test_tracker_desk_builds_views_only_for_the_lots_it_shows(conn, client, monkeypatch):
    _, c = conn
    channel_id = create_channel(c, "Auctions", "lots", kind="tracker")
    source_id = create_source(c, channel_id, "all_about_auctions", {})
    for hours in (9, 3, 7, 1, 5, 8, 2):
        _lot(c, source_id, f"h{hours}", 90, closes_in_hours=hours)
    from beehive.channels import views

    built = []
    real_tracker_item = views._tracker_item

    def _counting_tracker_item(item, *args, **kwargs):
        built.append(item["external_id"])
        return real_tracker_item(item, *args, **kwargs)

    monkeypatch.setattr(views, "_tracker_item", _counting_tracker_item)

    section = _section(client.get("/"), channel_id)

    assert section.lot_total == 7
    assert [lot.title for lot in section.lots] == ["Lot h1", "Lot h2", "Lot h3", "Lot h5", "Lot h7"]
    assert built == ["h1", "h2", "h3", "h5", "h7"]


def test_monitor_desk_orders_by_the_displayed_score_like_the_channel_page(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Gear", "deals", kind="monitor")
    source_id = create_source(c, channel_id, "shopify_collection", {"collection_url": "https://shop.example/c"})
    # Displayed (rounded half to even): 90, 90, 90, 90, 92, 91, 90.
    for external_id, score in (("a", 90.1), ("b", 90.4), ("c", 90.5), ("d", 89.5), ("e", 91.5), ("f", 90.6), ("g", 90.4)):
        _listing(c, source_id, external_id, score)

    section = _section(client.get("/"), channel_id)
    page = build_channel_page(
        c, get_channel(c, channel_id), t=load_localizer(c), now=datetime.now(timezone.utc)
    )

    assert [listing.id for listing in section.listings] == [listing.id for listing in page.items[:5]]
    assert [listing.title for listing in section.listings] == [
        "Jacket e", "Jacket f", "Jacket a", "Jacket b", "Jacket c",
    ]


def test_tracker_desk_floor_reads_the_displayed_score_like_the_channel_filter(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Auctions", "lots", kind="tracker")
    source_id = create_source(c, channel_id, "all_about_auctions", {})
    _lot(c, source_id, "shows-80", 79.6, closes_in_hours=2)
    _lot(c, source_id, "shows-79", 79.4, closes_in_hours=1)
    _lot(c, source_id, "exactly-80", 80.0, closes_in_hours=3)

    section = _section(client.get("/"), channel_id)
    page = build_channel_page(
        c,
        get_channel(c, channel_id),
        t=load_localizer(c),
        now=datetime.now(timezone.utc),
        tracker_query=TrackerQuery(minimum_score=80),
    )

    assert [lot.title for lot in section.lots] == ["Lot shows-80", "Lot exactly-80"]
    assert {lot.id for lot in section.lots} == {lot.id for lot in (*page.ending_soon, *page.upcoming)}


def test_tracker_section_uses_a_stricter_channel_minimum(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Auctions", "lots", kind="tracker", minimum_score=88)
    source_id = create_source(c, channel_id, "all_about_auctions", {})
    _lot(c, source_id, "85", 85, closes_in_hours=4)
    _lot(c, source_id, "90", 90, closes_in_hours=6)
    # Shown as 88, but under the Channel's minimum, which the channel page applies to the raw score.
    _lot(c, source_id, "87.6", 87.6, closes_in_hours=5)

    section = _section(client.get("/"), channel_id)

    assert [lot.title for lot in section.lots] == ["Lot 90"]
    assert section.lot_minimum_score == 88


def test_empty_sections_say_what_is_missing(conn, client):
    _, c = conn
    create_channel(c, "News", "p")
    create_channel(c, "Gear", "p", kind="monitor")
    create_channel(c, "Auctions", "p", kind="tracker")

    response = client.get("/")

    assert "Nothing from this channel is featured right now." in response.text
    assert "No live listings right now." in response.text
    assert "No open lots at 80 or above right now." in response.text


def test_home_never_builds_a_whole_channel_page(conn, client, monkeypatch):
    _, c = conn
    news = create_channel(c, "News", "p")
    gear = create_channel(c, "Gear", "p", kind="monitor")
    auctions = create_channel(c, "Auctions", "p", kind="tracker")
    _story(c, create_source(c, news, "reddit_subreddit", {"subreddit": "x"}), "s", 90)
    _listing(c, create_source(c, gear, "shopify_collection", {"collection_url": "https://shop.example/c"}), "l", 90)
    _lot(c, create_source(c, auctions, "all_about_auctions", {}), "lot", 90, closes_in_hours=5)

    def _whole_channel(*args, **kwargs):
        raise AssertionError("the home page must not load a whole Channel")

    monkeypatch.setattr("beehive.channels.views.list_by_channel", _whole_channel)

    response = client.get("/")

    assert response.status_code == 200
    assert "Summary s" in response.text
    assert "Jacket l" in response.text
    assert "Lot lot" in response.text


def test_owner_rail_tally_counts_down_to_zero(conn, client, authed_client):
    _, c = conn
    channel_id = create_channel(c, "News", "p")
    _story(c, create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"}), "s", 90, read=True)

    owner = authed_client.get("/")
    reader = client.get("/")

    assert (
        '<span class="toc-n" title="0 unread featured stories">'
        '<span aria-hidden="true">0</span>'
    ) in owner.text
    assert 'class="toc-n"' not in reader.text


def test_read_toggle_re_renders_its_block_in_place(conn, authed_client):
    _, c = conn
    channel_id = create_channel(c, "News", "p")
    _story(c, create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"}), "s", 90)
    item_id = _item_id(c, "s")

    desk = authed_client.get("/")
    listed = authed_client.get("/?view=all")

    assert (
        f'hx-post="/items/{item_id}/read-state" hx-target="#desk-{channel_id}" '
        f'hx-select="#desk-{channel_id}" hx-swap="outerHTML"'
    ) in desk.text
    assert 'hx-select-oob="#home-meta,#toc-count-featured"' in desk.text
    assert 'hx-target="#home-list" hx-select="#home-list"' in listed.text
    assert 'hx-select-oob="#home-meta,#home-seg,#toc-count-featured"' in listed.text
    assert "/static/htmx.min.js" in desk.text

    marked = authed_client.post(
        f"/items/{item_id}/read-state",
        data={"csrf_token": "csrf1", "is_read": "1", "next_url": "/"},
    )
    assert marked.status_code == 303
    assert marked.headers["location"] == "/"
    assert c.execute("SELECT is_read FROM items WHERE id = ?", (item_id,)).fetchone()[0] == 1


def test_a_combined_filter_marks_the_score_tab_with_its_own_count(conn, authed_client):
    _, c = conn
    channel_id = create_channel(c, "News", "p")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    _story(c, source_id, "unread-high", 95)
    _story(c, source_id, "read-high", 92, read=True)
    _story(c, source_id, "unread-low", 70)

    response = authed_client.get("/?view=unread&minimum_score=90")

    assert (
        '<a href="/?view=unread&amp;minimum_score=90" aria-current="page">'
        'Score 90+ <span class="n">1</span></a>'
    ) in response.text
    assert "Summary unread-high" in response.text
    assert "Summary read-high" not in response.text
    assert "Summary unread-low" not in response.text


def test_an_emptied_ranked_list_keeps_a_focus_target(conn, authed_client):
    _, c = conn
    channel_id = create_channel(c, "News", "p")
    _story(c, create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"}), "s", 90, read=True)

    response = authed_client.get("/?view=unread")

    assert '<div id="home-list" data-refocus-slot>' in response.text
    assert '<div class="empty home-empty" tabindex="-1" data-refocus-fallback>' in response.text


def test_readers_get_no_owner_controls_or_read_state(conn, client):
    _, c = conn
    channel_id = create_channel(c, "News", "p")
    _story(c, create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"}), "s", 90, read=True)

    desk = client.get("/")
    unread_view = client.get("/?view=unread")

    assert 'class="rd' not in desk.text
    assert "is-read" not in desk.text
    assert "/static/htmx.min.js" not in desk.text
    assert "Mark all as read" not in desk.text
    assert '<a href="/admin/login">Log in</a>' in desk.text
    assert 'href="/watchlist"' not in desk.text
    # Read state belongs to the Owner, so a reader's unread view is every story.
    assert unread_view.context["view"] == "all"
    assert "Summary s" in unread_view.text


def test_reading_contract_ships_first_in_the_body(client):
    response = client.get("/")

    body = response.text.split('<body class="adm page-reading">', 1)[1]
    assert body.lstrip().startswith("<!-- Reading direction contract (impeccable seed ba4c87a4)")
