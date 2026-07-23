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
        create_app(path, session_secret="test-secret"),
        follow_redirects=False,
    )
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
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
            "source": "example.com/collections/sale",
            "vendor": "Arc'teryx",
            "on_sale": "true",
            "sort": "discount",
        },
    )

    assert response.status_code == 200
    assert response.template.name == "channel_monitor.html"
    assert "page-channel-monitor" in response.text
    assert "Beta Jacket" in response.text
    assert "Trail Shoe" not in response.text
    assert "Price drop" in response.text
    assert "100 → 80" in response.text
    assert "20% off" in response.text
    assert "example.com/collections/sale" in response.text
    assert "shopify_collection" not in response.text
    assert 'referrerpolicy="no-referrer"' in response.text
    assert "javascript:alert(1)" not in response.text
    assert 'class="votes"' not in response.text
    assert "deep-read" not in response.text


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
    assert "page-channel-tracker" in response.text
    assert "Watched" in response.text
    assert "Ending within 24 hours" in response.text
    assert "Upcoming" in response.text
    assert "Permanent history" in response.text
    page = response.context["page"]
    assert [item.id for item in page.watched] == [watched_id]
    assert watched_id not in {
        item.id for item in (*page.ending_soon, *page.upcoming, *page.history)
    }
    assert "Current bid: NZD 50" in response.text
    assert f'hx-post="/items/{watched_id}/watch"' in response.text
    assert 'aria-pressed="true"' in response.text
    assert 'referrerpolicy="no-referrer"' in response.text
    assert 'class="votes"' not in response.text
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
    assert 'aria-pressed="true"' in response.text
    assert 'class="item' not in response.text
    assert 'class="folded-item"' not in response.text


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
