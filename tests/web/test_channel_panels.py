import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.item_events import record_or_coalesce_event
from beehive.db.items import insert_new, update_ai_ranking
from beehive.db.sessions import create_session
from beehive.db.sources import create_source
from beehive.db.tracker_watches import add_tracker_watch
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    connection = connect(path)
    init_schema(connection)
    return path, connection


@pytest.fixture
def client(conn):
    path, _ = conn
    return TestClient(create_app(path))


@pytest.fixture
def authed_client(conn):
    path, connection = conn
    set_admin_password(path, "correct-password")
    create_session(connection, "sess1", "csrf1", "2099-01-01T00:00:00")
    client = TestClient(
        create_app(path, session_secret="test-secret-at-least-32-characters-long"),
        follow_redirects=False,
    )
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret-at-least-32-characters-long"))
    return client


def _add_ranked_item(connection, source_id, external_id, title, metadata, *, score=90):
    insert_new(
        connection,
        source_id,
        RawItem(
            external_id=external_id,
            title=title,
            url=f"https://example.com/items/{external_id}",
            raw_metadata=metadata,
        ),
    )
    update_ai_ranking(
        connection,
        source_id,
        external_id,
        score=score,
        summary=f"{title} summary",
        rationale="Strong profile match",
    )
    return connection.execute(
        "SELECT id FROM items WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()["id"]


def _monitor_metadata(
    *,
    price,
    compare_at_price=None,
    on_sale=False,
    available=True,
    vendor="Teva",
    image_url="https://cdn.example.com/item.jpg",
):
    return {
        "price": price,
        "compare_at_price": compare_at_price,
        "on_sale": on_sale,
        "available": available,
        "vendor": vendor,
        "product_type": "Footwear",
        "image_url": image_url,
    }


def _tracker_metadata(closes_in_hours, *, image_url="https://cdn.example.com/lot.jpg"):
    return {
        "auction_title": "Weekly tools auction",
        "closing_at": (
            datetime.now(timezone.utc) + timedelta(hours=closes_in_hours)
        ).isoformat(),
        "currency_code": "NZD",
        "current_bid": 50.0,
        "buyer_premium_rate": 0.17,
        "estimated_cost": 58.5,
        "image_url": image_url,
    }


def _section(html, key):
    match = re.search(rf'<section class="chan-sec" id="{key}".*?</section>', html, re.DOTALL)
    assert match is not None, key
    return match.group(0)


def _row(html, row_id):
    """A listing or lot by id: a table row in list view, a plate in the gallery."""
    match = re.search(rf'<(tr|li)\b[^>]*\bid="{row_id}"[^>]*>.*?</\1>', html, re.DOTALL)
    assert match is not None, row_id
    return match.group(0)


def test_monitor_panel_filters_sorts_and_renders_safe_change_badges(conn, client):
    _, connection = conn
    channel_id = create_channel(
        connection,
        "Outdoor deals",
        "discounted outdoor gear",
        kind="monitor",
    )
    source_id = create_source(
        connection,
        channel_id,
        "shopify_collection",
        {"collection_url": "https://example.com/collections/sale"},
    )
    jacket_id = _add_ranked_item(
        connection,
        source_id,
        "jacket",
        "Beta Jacket",
        _monitor_metadata(
            price=80,
            compare_at_price=100,
            on_sale=True,
            vendor="Arc'teryx",
        ),
        score=95,
    )
    _add_ranked_item(
        connection,
        source_id,
        "shoe",
        "Trail Shoe",
        _monitor_metadata(
            price=120,
            vendor="Teva",
            image_url="javascript:alert(1)",
        ),
        score=80,
    )
    record_or_coalesce_event(
        connection,
        jacket_id,
        "price_drop",
        {"old_price": 100, "new_price": 80},
        datetime.now(timezone.utc).isoformat(),
    )

    response = client.get(
        f"/channels/{channel_id}",
        params={
            "q": "jacket",
            "source": "example.com",
            "vendor": "Arc'teryx",
            "on_sale": "true",
            "sort": "discount",
            "view": "list",
        },
    )

    assert response.status_code == 200
    assert response.template.name == "channel_monitor.html"
    # The Channel is its own chapter of the reading shell, labelled with its kind.
    assert '<body class="adm page-reading">' in response.text
    assert f'<a href="/channels/{channel_id}" aria-current="page">' in response.text
    assert '<span class="nw">Monitor</span>' in response.text
    assert "Beta Jacket" in response.text
    assert "Trail Shoe" not in response.text
    jacket = _row(_section(response.text, "available"), f"listing-{jacket_id}")
    assert '<small class="chg">Price drop · 100 → 80</small>' in jacket
    assert '<s class="price-was">100</s>' in jacket
    assert '<span class="price-off">−20%</span>' in jacket
    assert (
        '<span class="lot-where">Arc&#39;teryx · Footwear · example.com</span>'
        in jacket
    )
    assert "shopify_collection" not in response.text
    assert 'referrerpolicy="no-referrer"' in response.text
    assert "javascript:alert(1)" not in response.text
    # Anonymous readers get no feedback controls, and a listing has no deep read.
    assert '<form class="vote"' not in response.text
    assert "/relevance" not in response.text
    assert "deep-read" not in response.text


def test_monitor_listing_change_tags_name_what_changed(conn, client):
    _, connection = conn
    channel_id = create_channel(connection, "Gear", "outdoor gear", kind="monitor")
    source_id = create_source(
        connection,
        channel_id,
        "shopify_collection",
        {"collection_url": "https://example.com/collections/gear"},
    )
    dropped = _add_ranked_item(
        connection,
        source_id,
        "dropped",
        "Dropped Jacket",
        _monitor_metadata(price=80, compare_at_price=100, on_sale=True),
    )
    restocked = _add_ranked_item(
        connection, source_id, "restocked", "Restocked Boot", _monitor_metadata(price=150)
    )
    fresh = _add_ranked_item(
        connection, source_id, "fresh", "Fresh Hat", _monitor_metadata(price=30)
    )
    quiet = _add_ranked_item(
        connection, source_id, "quiet", "Quiet Sock", _monitor_metadata(price=10)
    )
    observed_at = datetime.now(timezone.utc).isoformat()
    record_or_coalesce_event(
        connection, dropped, "price_drop", {"old_price": 100, "new_price": 80}, observed_at
    )
    record_or_coalesce_event(connection, restocked, "back_in_stock", {}, observed_at)
    record_or_coalesce_event(connection, fresh, "discovered", {}, observed_at)

    response = client.get(f"/channels/{channel_id}", params={"view": "list"})

    assert response.status_code == 200
    available = _section(response.text, "available")
    # The tag sits in the price cell, under the price it explains.
    dropped_row = _row(available, f"listing-{dropped}")
    assert re.search(
        r'<td class="c-price-now" data-label="Price"><span class="price-now">80</span>.*?'
        r'<small class="chg">Price drop · 100 → 80</small></td>',
        dropped_row,
        re.DOTALL,
    )
    assert '<small class="chg">Back in stock</small>' in _row(available, f"listing-{restocked}")
    assert '<small class="chg">New</small>' in _row(available, f"listing-{fresh}")
    assert 'class="chg"' not in _row(available, f"listing-{quiet}")


def test_monitor_panel_accepts_multiple_vendor_and_source_filters(conn, client):
    _, connection = conn
    channel_id = create_channel(
        connection,
        "Multi-source deals",
        "discounted outdoor gear",
        kind="monitor",
    )
    first_source_id = create_source(
        connection,
        channel_id,
        "shopify_collection",
        {"collection_url": "https://first.example/collections/sale"},
    )
    second_source_id = create_source(
        connection,
        channel_id,
        "shopify_collection",
        {"collection_url": "https://second.example/collections/sale"},
    )
    third_source_id = create_source(
        connection,
        channel_id,
        "shopify_collection",
        {"collection_url": "https://third.example/collections/sale"},
    )
    _add_ranked_item(
        connection,
        first_source_id,
        "selected-arc",
        "Selected Arc Jacket",
        _monitor_metadata(price=80, vendor="Arc'teryx"),
    )
    _add_ranked_item(
        connection,
        second_source_id,
        "selected-patagonia",
        "Selected Patagonia Fleece",
        _monitor_metadata(price=90, vendor="Patagonia"),
    )
    _add_ranked_item(
        connection,
        second_source_id,
        "wrong-vendor",
        "Unselected Teva Shoe",
        _monitor_metadata(price=100, vendor="Teva"),
    )
    _add_ranked_item(
        connection,
        third_source_id,
        "wrong-source",
        "Unselected Source Arc Jacket",
        _monitor_metadata(price=110, vendor="Arc'teryx"),
    )

    response = client.get(
        f"/channels/{channel_id}",
        params=[
            ("vendor", "Arc'teryx"),
            ("vendor", "Patagonia"),
            ("source", "first.example"),
            ("source", "second.example"),
        ],
    )

    assert response.status_code == 200
    assert [item.title for item in response.context["page"].items] == [
        "Selected Arc Jacket",
        "Selected Patagonia Fleece",
    ]
    assert response.context["page"].vendors == ("Arc'teryx", "Patagonia")
    assert response.context["page"].sources == ("first.example", "second.example")
    assert 'type="checkbox" name="vendor"' in response.text
    assert 'type="checkbox" name="source"' in response.text
    assert "Unselected Teva Shoe" not in response.text
    assert "Unselected Source Arc Jacket" not in response.text


def test_monitor_panel_separates_out_of_stock_and_removed_history(conn, client):
    _, connection = conn
    channel_id = create_channel(connection, "Gear", "outdoor gear", kind="monitor")
    source_id = create_source(
        connection,
        channel_id,
        "shopify_collection",
        {"collection_url": "https://example.com/collections/gear"},
    )
    _add_ranked_item(
        connection,
        source_id,
        "available",
        "Available Jacket",
        _monitor_metadata(price=100),
    )
    _add_ranked_item(
        connection,
        source_id,
        "oos",
        "Out of Stock Jacket",
        _monitor_metadata(price=90, available=False),
    )
    removed_id = _add_ranked_item(
        connection,
        source_id,
        "removed",
        "Removed Jacket",
        _monitor_metadata(price=80),
    )
    connection.execute(
        "UPDATE items SET inactive_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), removed_id),
    )
    connection.commit()

    response = client.get(f"/channels/{channel_id}")

    assert response.status_code == 200
    assert "Available Jacket" in response.text
    assert "Unavailable history" in response.text
    assert "Out of Stock Jacket" in response.text
    assert "Out of stock" in response.text
    assert "Removed Jacket" in response.text
    assert "Removed" in response.text


def test_monitor_pagination_preserves_filters_in_navigation(conn, client):
    _, connection = conn
    channel_id = create_channel(connection, "Gear", "outdoor gear", kind="monitor")
    source_id = create_source(
        connection,
        channel_id,
        "shopify_collection",
        {"collection_url": "https://example.com/collections/gear"},
    )
    for index in range(25):
        _add_ranked_item(
            connection,
            source_id,
            f"item-{index}",
            f"Item {index}",
            _monitor_metadata(price=float(index), vendor="Teva"),
            score=100 - index,
        )

    first = client.get(
        f"/channels/{channel_id}",
        params={"q": "Item", "vendor": "Teva", "sort": "price_asc"},
    )
    second = client.get(
        f"/channels/{channel_id}",
        params={"page": 2, "q": "Item", "vendor": "Teva", "sort": "price_asc"},
    )

    assert first.status_code == 200
    assert "Page 1 of 2" in first.text
    assert (
        f"/channels/{channel_id}?sort=price_asc&amp;page=2&amp;vendor=Teva&amp;q=Item"
        in first.text
    )
    assert second.status_code == 200
    assert len(second.context["page"].items) == 1
    assert "Page 2 of 2" in second.text


@pytest.mark.parametrize(
    ("page_param", "section"), [("page", "available"), ("history_page", "history")]
)
def test_monitor_section_past_the_end_says_so_above_its_pager(conn, client, page_param, section):
    _, connection = conn
    channel_id = create_channel(connection, "Gear", "outdoor gear", kind="monitor")
    source_id = create_source(
        connection,
        channel_id,
        "shopify_collection",
        {"collection_url": "https://example.com/collections/gear"},
    )
    _add_ranked_item(connection, source_id, "live", "Live Jacket", _monitor_metadata(price=100))
    _add_ranked_item(
        connection,
        source_id,
        "sold-out",
        "Sold-out Jacket",
        _monitor_metadata(price=90, available=False),
    )

    response = client.get(f"/channels/{channel_id}", params={page_param: 5})

    assert response.status_code == 200
    past_end = _section(response.text, section)
    assert "Nothing is left on this page." in past_end
    assert 'id="listing-' not in past_end
    # "Previous" leads back to the last page with listings, the first, and the pager counts the
    # one page there is.
    assert (
        f'<a class="btn btn-sm" href="/channels/{channel_id}?sort=score">Previous</a>' in past_end
    )
    assert "<span>Page 5 of 1</span>" in past_end


@pytest.mark.parametrize(
    ("page_param", "section", "closes_in_hours"),
    [
        ("ending_page", "ending", 3),
        ("upcoming_page", "upcoming", 48),
        ("history_page", "history", -2),
    ],
)
def test_tracker_section_past_the_end_says_so_above_its_pager(
    conn, client, page_param, section, closes_in_hours
):
    _, connection = conn
    channel_id = create_channel(connection, "Auctions", "tools", kind="tracker")
    source_id = create_source(connection, channel_id, "all_about_auctions", {})
    _add_ranked_item(
        connection, source_id, "lot", "Cordless drill", _tracker_metadata(closes_in_hours)
    )

    response = client.get(f"/channels/{channel_id}", params={page_param: 5})

    assert response.status_code == 200
    past_end = _section(response.text, section)
    assert "Nothing is left on this page." in past_end
    assert "Cordless drill" not in response.text
    # "Previous" leads back to the last page with lots, the first, and the pager counts the one
    # page there is.
    assert f'<a class="btn btn-sm" href="/channels/{channel_id}">Previous</a>' in past_end
    assert "<span>Page 5 of 1</span>" in past_end


def test_tracker_panel_groups_watched_deadlines_and_history_without_duplicates(
    conn,
    authed_client,
):
    _, connection = conn
    channel_id = create_channel(
        connection,
        "Auction watch",
        "interesting tools",
        kind="tracker",
    )
    source_id = create_source(connection, channel_id, "all_about_auctions", {})
    watched_id = _add_ranked_item(
        connection,
        source_id,
        "watched",
        "Watched drill",
        _tracker_metadata(2),
    )
    _add_ranked_item(
        connection,
        source_id,
        "soon",
        "Ending saw",
        _tracker_metadata(3),
    )
    _add_ranked_item(
        connection,
        source_id,
        "upcoming",
        "Upcoming sander",
        _tracker_metadata(48),
    )
    _add_ranked_item(
        connection,
        source_id,
        "closed",
        "Closed grinder",
        _tracker_metadata(-2),
    )
    add_tracker_watch(connection, watched_id, datetime.now(timezone.utc))

    response = authed_client.get(f"/channels/{channel_id}")

    assert response.status_code == 200
    assert response.template.name == "channel_tracker.html"
    assert '<body class="adm page-reading">' in response.text
    assert '<span class="nw">Tracker</span>' in response.text
    # Each group is a numbered section, listed under the Channel's chapter in the rail.
    for number, key, heading in (
        ("2.1", "watched", "Watched"),
        ("2.2", "ending", "Ending within 24 hours"),
        ("2.3", "upcoming", "Upcoming"),
        ("2.4", "history", "Permanent history"),
    ):
        assert (
            f'<a href="#{key}"><span class="no">{number}</span><span>{heading}</span></a>'
            in response.text
        )
        assert f'<span class="no">{number}</span><span>{heading}</span>' in _section(
            response.text, key
        )
    page = response.context["page"]
    assert [item.id for item in page.watched] == [watched_id]
    assert watched_id not in {
        item.id for item in (*page.ending_soon, *page.upcoming, *page.history)
    }
    # The watched lot is listed once, in its own section, with its watch toggle pressed.
    assert response.text.count(f'id="lot-{watched_id}"') == 1
    watched = _row(_section(response.text, "watched"), f"lot-{watched_id}")
    assert "Current bid: NZD 50" in watched
    assert f'hx-post="/items/{watched_id}/watch"' in watched
    assert '<button class="tg tg-watch" type="submit" aria-pressed="true"' in watched
    history = _section(response.text, "history")
    assert "Closed grinder" in history
    assert 'id="lot-' in history and 'is-closed' in history
    assert 'referrerpolicy="no-referrer"' in response.text
    # Lots take relevance feedback, never the Editorial vote, and have no deep read.
    assert not re.search(r'action="/items/\d+/vote"', response.text)
    assert "deep-read" not in response.text


def test_tracker_watch_htmx_returns_generic_control_fragment(conn, authed_client):
    _, connection = conn
    channel_id = create_channel(connection, "Auctions", "tools", kind="tracker")
    source_id = create_source(connection, channel_id, "all_about_auctions", {})
    item_id = _add_ranked_item(
        connection,
        source_id,
        "lot",
        "Cordless drill",
        _tracker_metadata(4),
    )

    response = authed_client.post(
        f"/items/{item_id}/watch",
        data={"csrf_token": "csrf1", "origin": "channel"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.template.name == "_tracker_watch_control.html"
    assert f'hx-post="/items/{item_id}/watch"' in response.text
    assert '<button class="tg tg-watch" type="submit" aria-pressed="true"' in response.text
    # Just the control: no row around it.
    assert response.text.lstrip().startswith('<form class="watch"')
    assert response.text.count("<form") == 1
    assert "<tr" not in response.text


def test_tracker_large_sections_use_server_side_pagination(conn, client):
    _, connection = conn
    channel_id = create_channel(connection, "Auctions", "tools", kind="tracker")
    source_id = create_source(connection, channel_id, "all_about_auctions", {})
    for index in range(30):
        _add_ranked_item(
            connection,
            source_id,
            f"lot-{index}",
            f"Lot {index}",
            _tracker_metadata(48 + index),
        )

    first = client.get(f"/channels/{channel_id}")
    second = client.get(f"/channels/{channel_id}", params={"upcoming_page": 2})

    assert first.status_code == 200
    assert len(first.context["page"].upcoming) == 24
    assert first.context["page"].upcoming_pagination.total == 30
    assert f"/channels/{channel_id}?upcoming_page=2" in first.text
    assert "Page 1 of 2" in first.text
    assert second.status_code == 200
    assert len(second.context["page"].upcoming) == 6
    assert "Page 2 of 2" in second.text


@pytest.mark.parametrize(
    ("kind", "expected_template", "expected_copy"),
    [
        ("monitor", "channel_monitor.html", "Nothing available yet"),
        ("tracker", "channel_tracker.html", "No active tracked items"),
    ],
)
def test_non_editorial_panels_have_kind_specific_empty_states(
    conn,
    client,
    kind,
    expected_template,
    expected_copy,
):
    _, connection = conn
    channel_id = create_channel(connection, kind.title(), kind, kind=kind)
    source_type = "shopify_collection" if kind == "monitor" else "all_about_auctions"
    config = (
        {"collection_url": "https://example.com/collections/empty"}
        if kind == "monitor"
        else {}
    )
    create_source(connection, channel_id, source_type, config)

    response = client.get(f"/channels/{channel_id}")

    assert response.status_code == 200
    assert response.template.name == expected_template
    assert expected_copy in response.text
