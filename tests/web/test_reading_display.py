"""The reading pages' display choices and the Owner's triage around them: a store or auction
Channel's list or gallery and its page size, remembered between visits; long lists split into
two lanes; a story judged not relevant is read and stays in place for its reason; "mark all as
read" can be taken back."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new, update_ai_ranking
from beehive.db.sessions import create_session
from beehive.db.sources import create_source
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME

_SECRET = "test-secret-at-least-32-characters-long"


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    connection = connect(path)
    init_schema(connection)
    create_session(connection, "sess1", "csrf1", "2099-01-01T00:00:00")
    return path, connection


def _client(path, *, owner=False):
    # The choices are remembered in Secure cookies, which a client only sends back over https.
    client = TestClient(
        create_app(path, session_secret=_SECRET),
        base_url="https://testserver",
        follow_redirects=False,
    )
    if owner:
        client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", _SECRET))
    return client


def _add(connection, source_id, external_id, title, metadata=None, *, score=90):
    insert_new(
        connection,
        source_id,
        RawItem(
            external_id=external_id,
            title=title,
            url=f"https://example.com/items/{external_id}",
            raw_metadata=metadata or {},
        ),
    )
    update_ai_ranking(
        connection, source_id, external_id, score=score, summary=f"{title} summary", rationale="r"
    )
    return connection.execute(
        "SELECT id FROM items WHERE source_id = ? AND external_id = ?", (source_id, external_id)
    ).fetchone()["id"]


def _listing(price, image_url="https://cdn.shopify.com/s/files/1/gear.jpg?v=3"):
    return {
        "price": price,
        "available": True,
        "vendor": "Arc'teryx",
        "product_type": "Jackets",
        "image_url": image_url,
    }


def _store(connection, name="Gear", *, listings=1, minimum_score=0):
    channel_id = create_channel(
        connection, name, "outdoor gear", kind="monitor", minimum_score=minimum_score
    )
    source_id = create_source(
        connection,
        channel_id,
        "shopify_collection",
        {"collection_url": f"https://www.{name.lower()}.example/collections/sale"},
    )
    ids = [_add(connection, source_id, f"p{n}", f"Jacket {n:02d}", _listing(100 + n)) for n in range(listings)]
    return channel_id, ids


def _auctions(connection):
    channel_id = create_channel(connection, "Auctions", "tools", kind="tracker")
    source_id = create_source(connection, channel_id, "all_about_auctions", {})
    closes = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    lot_id = _add(
        connection, source_id, "lot", "Cordless drill", {"auction_title": "Tools", "closing_at": closes}
    )
    return channel_id, lot_id


def _cookie(response, name):
    header = "; ".join(response.headers.get_list("set-cookie"))
    match = re.search(rf"{name}=([^;]*)", header)
    return match.group(1) if match else None


def test_a_store_opens_as_a_gallery_and_an_auction_as_a_list(conn):
    path, connection = conn
    store_id, (listing_id,) = _store(connection)
    auction_id, lot_id = _auctions(connection)
    client = _client(path)

    store = client.get(f"/channels/{store_id}").text
    assert '<ul class="plates" role="list">' in store
    assert f'<li class="plate kb-row" id="listing-{listing_id}"' in store
    assert "tbl-stack" not in store
    assert re.search(r'aria-current="page">Gallery</a>', store)

    auction = client.get(f"/channels/{auction_id}").text
    assert f'<tr class="kb-row" id="lot-{lot_id}"' in auction
    assert '<ul class="plates"' not in auction
    assert re.search(r'aria-current="page">List</a>', auction)


def test_each_channel_remembers_its_own_view(conn):
    path, connection = conn
    first_id, _ = _store(connection, "Gear")
    second_id, _ = _store(connection, "Boots")
    client = _client(path)

    chosen = client.get(f"/channels/{first_id}", params={"view": "list"})
    assert '<table class="tbl tbl-stack tbl-lanes">' in chosen.text
    assert _cookie(chosen, "reading_view") == f"{first_id}l"

    # The next visit opens the list again, and another store keeps its own default.
    assert '<table class="tbl tbl-stack tbl-lanes">' in client.get(f"/channels/{first_id}").text
    assert '<ul class="plates"' in client.get(f"/channels/{second_id}").text
    back = client.get(f"/channels/{second_id}", params={"view": "gallery"})
    assert _cookie(back, "reading_view") == f"{first_id}l.{second_id}g"

    # An unknown view changes nothing.
    odd = client.get(f"/channels/{first_id}", params={"view": "grid"})
    assert _cookie(odd, "reading_view") is None
    assert '<table class="tbl tbl-stack tbl-lanes">' in odd.text


def test_page_size_is_a_remembered_choice_that_starts_lists_over(conn):
    path, connection = conn
    channel_id, _ = _store(connection, listings=30)
    client = _client(path)

    first = client.get(f"/channels/{channel_id}", params={"page": 2})
    assert first.text.count('class="plate kb-row"') == 6
    # Each size is a plain link that keeps the filters and starts the list from its first page.
    assert f'href="/channels/{channel_id}?per_page=48"' in first.text
    assert re.search(r'aria-current="page">24</a>', first.text)

    bigger = client.get(f"/channels/{channel_id}", params={"per_page": 48})
    assert bigger.text.count('class="plate kb-row"') == 30
    assert _cookie(bigger, "reading_per_page") == "48"
    assert client.get(f"/channels/{channel_id}").text.count('class="plate kb-row"') == 30

    # A size that is not offered is ignored.
    odd = client.get(f"/channels/{channel_id}", params={"per_page": 50})
    assert _cookie(odd, "reading_per_page") is None
    assert odd.text.count('class="plate kb-row"') == 30


def test_pager_keeps_a_chosen_view(conn):
    path, connection = conn
    channel_id, _ = _store(connection, listings=30)

    page = _client(path).get(f"/channels/{channel_id}", params={"view": "list"}).text

    assert (
        f'<a class="btn btn-sm" href="/channels/{channel_id}?sort=score&amp;page=2&amp;view=list">'
        "Next</a>"
    ) in page


def test_a_long_list_renders_two_lanes_of_rows_in_order(conn):
    path, connection = conn
    channel_id, ids = _store(connection, listings=5)

    response = _client(path).get(f"/channels/{channel_id}", params={"view": "list"})

    lanes = re.findall(r"<tbody data-lane>(.*?)</tbody>", response.text, re.DOTALL)
    by_lane = [[int(item) for item in re.findall(r'id="listing-(\d+)"', lane)] for lane in lanes]
    assert [len(lane) for lane in by_lane] == [3, 2]
    # Down the first lane, then the second: reading and keyboard order stay as listed.
    listed = [item.id for item in response.context["page"].items]
    assert by_lane[0] + by_lane[1] == listed and sorted(listed) == sorted(ids)


def test_photos_are_asked_for_at_the_size_they_show(conn):
    path, connection = conn
    channel_id, _ = _store(connection)
    client = _client(path)

    gallery = client.get(f"/channels/{channel_id}").text
    assert 'src="https://cdn.shopify.com/s/files/1/gear.jpg?v=3&amp;width=600"' in gallery
    assert "gear.jpg?v=3&amp;width=300 300w" in gallery
    listing = client.get(f"/channels/{channel_id}", params={"view": "list"}).text
    assert 'src="https://cdn.shopify.com/s/files/1/gear.jpg?v=3&amp;width=240"' in listing
    assert 'width="80" height="80"' in listing


def test_the_criteria_note_shows_only_when_a_threshold_hides_rows(conn):
    path, connection = conn
    open_id, _ = _store(connection, "Gear")
    strict_id, _ = _store(connection, "Boots", minimum_score=50)
    owner = _client(path, owner=True)

    assert "chan-criteria" not in owner.get(f"/channels/{open_id}").text
    assert "chan-criteria" in owner.get(f"/channels/{strict_id}").text


def test_a_store_collection_reads_as_its_store_and_an_owner_name_wins(conn):
    path, connection = conn
    channel_id = create_channel(connection, "Gear", "outdoor gear", kind="monitor")
    for url in (
        "https://www.shop.example/collections/sale",
        "https://www.shop.example/collections/outlet",
        "https://other.example/collections/sale",
    ):
        create_source(connection, channel_id, "shopify_collection", {"collection_url": url})
    named = create_source(
        connection, channel_id, "shopify_collection", {"collection_url": "https://x.example/c/y"}
    )
    connection.execute("UPDATE sources SET name = 'Named Store' WHERE id = ?", (named,))
    connection.commit()

    page = _client(path).get(f"/channels/{channel_id}").text

    assert "Sources: shop.example, other.example, Named Store</p>" in page


def _news(connection):
    channel_id = create_channel(connection, "News", "news")
    source_id = create_source(connection, channel_id, "reddit_subreddit", {"subreddit": "x"})
    first = _add(connection, source_id, "s1", "Rates fall")
    second = _add(connection, source_id, "s2", "Rates rise", score=80)
    return channel_id, first, second


def _is_read(connection, item_id):
    return connection.execute("SELECT is_read FROM items WHERE id = ?", (item_id,)).fetchone()[0]


def test_a_story_judged_not_relevant_is_read_and_stays_for_its_reason(conn):
    path, connection = conn
    channel_id, story_id, _ = _news(connection)
    owner = _client(path, owner=True)

    voted = owner.post(
        f"/items/{story_id}/vote",
        data={"value": "-1", "csrf_token": "csrf1", "next_url": f"/channels/{channel_id}"},
    )

    assert voted.headers["location"] == f"/channels/{channel_id}?keep={story_id}"
    assert _is_read(connection, story_id) == 1
    kept = owner.get(voted.headers["location"]).text
    row = re.search(rf'<tr class="kb-row is-read" id="story-{story_id}".*?</tr>', kept, re.DOTALL)
    assert row is not None
    assert f'id="reason-{story_id}"' in row.group(0)
    # The unread list drops it once the page loads afresh.
    assert f'id="story-{story_id}"' not in owner.get(f"/channels/{channel_id}").text

    # Taking the judgement back makes the story unread again.
    owner.post(f"/items/{story_id}/vote", data={"value": "-1", "csrf_token": "csrf1"})
    assert _is_read(connection, story_id) == 0


def test_the_ranked_list_rates_in_place_and_keeps_a_dismissed_story(conn):
    path, connection = conn
    _, story_id, other_id = _news(connection)
    owner = _client(path, owner=True)

    unread = owner.get("/", params={"view": "unread"}).text
    assert f'action="/items/{story_id}/vote"' in unread

    voted = owner.post(
        f"/items/{story_id}/vote",
        data={"value": "-1", "csrf_token": "csrf1", "next_url": "/?view=unread"},
    )

    assert voted.headers["location"] == f"/?view=unread&keep={story_id}"
    kept = owner.get(voted.headers["location"]).text
    assert f'id="story-{story_id}"' in kept and f'id="story-{other_id}"' in kept
    assert f'id="story-{story_id}"' not in owner.get("/", params={"view": "unread"}).text


def test_marking_the_featured_list_read_can_be_taken_back(conn):
    path, connection = conn
    _, first, second = _news(connection)
    owner = _client(path, owner=True)

    marked = owner.post(
        "/dashboard/mark-all-read", data={"csrf_token": "csrf1", "next_url": "/?view=unread"}
    )

    location = marked.headers["location"]
    assert location.startswith("/?view=unread&read_batch=")
    assert _is_read(connection, first) == _is_read(connection, second) == 1
    page = owner.get(location).text
    assert "Marked 2 stories as read." in page
    assert 'name="next_url" value="/?view=unread"' in page
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)

    # Only the Owner can take it back.
    anonymous = _client(path).post(
        "/read-batches/undo", data={"csrf_token": "csrf1", "token": token}
    )
    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/admin/login?")
    assert _is_read(connection, first) == 1

    undone = owner.post(
        "/read-batches/undo",
        data={"csrf_token": "csrf1", "token": token, "next_url": "/?view=unread"},
    )

    assert undone.headers["location"] == "/?view=unread"
    assert _is_read(connection, first) == _is_read(connection, second) == 0


def test_a_story_link_carries_the_words_for_reading_it_in_place(conn):
    path, connection = conn
    channel_id, story_id, _ = _news(connection)

    page = _client(path, owner=True).get(f"/channels/{channel_id}").text

    row = re.search(rf'<tr class="kb-row" id="story-{story_id}".*?</tr>', page, re.DOTALL).group(0)
    assert 'data-read-title="Mark unread"' in row
    assert 'data-read-aria="Mark Rates fall as unread"' in row
    assert "data-kb-open" in row


def test_a_relevant_vote_keeps_nothing_and_switching_from_not_relevant_unreads(conn):
    path, connection = conn
    channel_id, story_id, _ = _news(connection)
    owner = _client(path, owner=True)

    liked = owner.post(f"/items/{story_id}/vote", data={"value": "1", "csrf_token": "csrf1"})
    assert liked.headers["location"] == f"/channels/{channel_id}"
    assert _is_read(connection, story_id) == 0

    owner.post(f"/items/{story_id}/vote", data={"value": "-1", "csrf_token": "csrf1"})
    assert _is_read(connection, story_id) == 1
    # Changing the judgement to relevant takes the "not relevant" back, so the story is unread.
    switched = owner.post(f"/items/{story_id}/vote", data={"value": "1", "csrf_token": "csrf1"})
    assert switched.headers["location"] == f"/channels/{channel_id}"
    assert _is_read(connection, story_id) == 0


def test_a_kept_story_is_not_carried_into_the_pages_own_links(conn):
    path, connection = conn
    channel_id, story_id, other_id = _news(connection)
    owner = _client(path, owner=True)

    voted = owner.post(
        f"/items/{story_id}/vote",
        data={"value": "-1", "csrf_token": "csrf1", "next_url": f"/channels/{channel_id}"},
    )
    page = owner.get(voted.headers["location"]).text

    # The kept row is there, but every form sends the Owner back to the plain page, so the next
    # toggle drops it from the unread list.
    assert f'id="story-{story_id}"' in page
    assert "keep=" not in page
    assert f'name="next_url" value="/channels/{channel_id}"' in page
    marked = owner.post(
        f"/items/{other_id}/read-state",
        data={"csrf_token": "csrf1", "is_read": "1", "next_url": f"/channels/{channel_id}"},
    )
    assert f'id="story-{story_id}"' not in owner.get(marked.headers["location"]).text


def test_the_ranked_list_keeps_a_story_on_the_page_it_sat_on(conn):
    path, connection = conn
    channel_id = create_channel(connection, "News", "news")
    source_id = create_source(connection, channel_id, "reddit_subreddit", {"subreddit": "x"})
    # 25 unread stories: the last one is alone on page 2.
    ids = [_add(connection, source_id, f"s{n:02d}", f"Story {n:02d}", score=99 - n) for n in range(25)]
    last = ids[-1]
    owner = _client(path, owner=True)

    voted = owner.post(
        f"/items/{last}/vote",
        data={"value": "-1", "csrf_token": "csrf1", "next_url": "/?view=unread&page=2"},
    )

    assert voted.headers["location"] == f"/?view=unread&page=2&keep={last}"
    page = owner.get(voted.headers["location"]).text
    row = re.search(rf'<tr class="kb-row is-read" id="story-{last}".*?</tr>', page, re.DOTALL)
    assert row is not None and f'id="reason-{last}"' in row.group(0)
    assert "Page 2 of 2" in page
    # Without the kept story the unread list has one page again.
    assert "Page 2 of 2" not in owner.get("/", params={"view": "unread", "page": 2}).text


def test_the_archive_keeps_a_judged_story_under_its_unread_filter(conn):
    path, connection = conn
    _, story_id, other_id = _news(connection)
    owner = _client(path, owner=True)
    archive_url = "/archive?read_state=unread"

    voted = owner.post(
        f"/items/{story_id}/vote",
        data={"value": "-1", "csrf_token": "csrf1", "next_url": archive_url},
    )

    assert voted.headers["location"] == f"{archive_url}&keep={story_id}"
    kept = owner.get(voted.headers["location"]).text
    assert f'id="reason-{story_id}"' in kept and f'id="story-{other_id}"' in kept
    assert f'id="story-{story_id}"' not in owner.get(archive_url).text


def test_a_malformed_preference_cookie_falls_back_to_the_defaults(conn):
    path, connection = conn
    channel_id, _ = _store(connection, listings=30)

    response = _client(path).get(
        f"/channels/{channel_id}", headers={"cookie": "reading_view=x9g.7; reading_per_page=all"}
    )

    assert response.status_code == 200
    assert response.text.count('class="plate kb-row"') == 24


def test_a_malformed_batch_token_is_ignored(conn):
    path, connection = conn
    channel_id, _, _ = _news(connection)
    owner = _client(path, owner=True)
    owner.post(f"/channels/{channel_id}/mark-all-read", data={"csrf_token": "csrf1"})

    page = owner.get(f"/channels/{channel_id}", params={"read_batch": "\u2603"})
    undo = owner.post("/read-batches/undo", data={"csrf_token": "csrf1", "token": "\u2603"})

    assert page.status_code == 200 and "read-undo" not in page.text
    assert undo.status_code == 303
    assert connection.execute("SELECT COUNT(*) FROM items WHERE is_read = 0").fetchone()[0] == 0
