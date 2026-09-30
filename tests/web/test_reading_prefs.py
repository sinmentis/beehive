import pytest
from starlette.requests import Request

from beehive.web.reading_prefs import ListingView, resolve_hidden_sections, resolve_listing_prefs


def _request(cookie: bytes) -> Request:
    # Raw header bytes, as a browser sends them: the test client would re-encode them as UTF-8.
    return Request(
        {"type": "http", "method": "GET", "path": "/", "query_string": b"",
         "headers": [(b"cookie", cookie)]}
    )


def _prefs(cookie: bytes, **choice):
    return resolve_listing_prefs(
        _request(cookie),
        channel_id=6,
        kind="monitor",
        view=choice.get("view"),
        per_page=choice.get("per_page"),
    )


@pytest.mark.parametrize(
    "cookie",
    [
        # Characters Python counts as digits that int() refuses, or that are not ASCII.
        b"reading_view=\xb26g; reading_per_page=\xb2",
        b"reading_view=6\xb2g",
        b"reading_view=\xd9\xa6g",
        b"reading_view=9999999999999999999999999g.x.gg.6; reading_per_page=048",
        b"reading_view=; reading_per_page=",
    ],
)
def test_a_malformed_cookie_is_skipped_never_trusted(cookie):
    prefs = _prefs(cookie)

    assert prefs.view is ListingView.GALLERY
    assert prefs.per_page == 24


def test_remembered_choices_are_read_back():
    prefs = _prefs(b"reading_view=7g.6l; reading_per_page=96")

    assert prefs.view is ListingView.LIST
    assert prefs.per_page == 96


def test_a_choice_in_the_link_wins_and_is_remembered_last():
    response_cookies = []

    class _Response:
        def set_cookie(self, name, value, **options):
            response_cookies.append((name, value, options["secure"], options["httponly"]))

    prefs = _prefs(b"reading_view=6l.7g", view="gallery", per_page=48)
    prefs.remember(_Response())

    assert prefs.view is ListingView.GALLERY and prefs.per_page == 48
    assert response_cookies == [
        ("reading_view", "7g.6g", True, True),
        ("reading_per_page", "48", True, True),
    ]


def _hidden(cookie: bytes, present=("top", "more"), page="6", hide=None, show=None):
    return resolve_hidden_sections(_request(cookie), page=page, present=present, hide=hide, show=show)


class _Jar:
    def __init__(self):
        self.cookies = {}

    def set_cookie(self, name, value, **options):
        self.cookies[name] = value


def test_hidden_sections_are_read_per_page_and_in_page_order():
    hidden = _hidden(b"reading_hidden=7:ending.6:more.6:top.search:channel-3")

    # Everything on this page hidden would leave it empty, so it shows everything.
    assert hidden.hidden == ()
    assert _hidden(b"reading_hidden=7:ending.6:more", present=("top", "more")).hidden == ("more",)


def test_hiding_and_showing_keep_the_other_pages_choices():
    jar = _Jar()
    _hidden(b"reading_hidden=7:ending", hide="top").remember(jar)
    assert jar.cookies["reading_hidden"] == "7:ending.6:top"

    jar = _Jar()
    _hidden(b"reading_hidden=6:top.7:ending", show="top").remember(jar)
    assert jar.cookies["reading_hidden"] == "7:ending"

    deleted = []

    class _Deleting(_Jar):
        def delete_cookie(self, name, **options):
            deleted.append((name, options["secure"], options["httponly"]))

    _hidden(b"reading_hidden=6:top", show="top").remember(_Deleting())
    assert deleted == [("reading_hidden", True, True)]


def test_a_choice_for_a_section_this_render_lacks_is_kept():
    # Past its first page a news Channel shows only "more"; its hidden "top" waits for page 1.
    hidden = _hidden(b"reading_hidden=6:top", present=("more",))

    assert hidden.hidden == ()
    jar = _Jar()
    hidden.remember(jar)
    assert jar.cookies == {}


def test_a_page_that_showed_everything_hides_just_the_section_chosen_next():
    # Channels 1 and 2 were hidden on a search that also matched channel 3. This search matches
    # only 1 and 2, so it shows both, each with its Hide switch; channel 9 waits for its search.
    cookie = b"reading_hidden=7:ending.search:channel-1.search:channel-2.search:channel-9"
    both = ("channel-1", "channel-2")
    assert _hidden(cookie, present=both, page="search").hidden == ()

    jar = _Jar()
    hidden = _hidden(cookie, present=both, page="search", hide="channel-1")
    hidden.remember(jar)
    assert hidden.hidden == ("channel-1",)
    assert jar.cookies["reading_hidden"] == "7:ending.search:channel-1.search:channel-9"

    # A stale Show link on that page changes nothing, rather than hiding the other section.
    jar = _Jar()
    stale = _hidden(cookie, present=both, page="search", show="channel-1")
    stale.remember(jar)
    assert stale.hidden == () and jar.cookies == {}


@pytest.mark.parametrize(
    "cookie",
    [b"reading_hidden=6:\xb2", b"reading_hidden=6:TOP.x:top.6:", b"reading_hidden=" + b"6:" + b"a" * 60],
)
def test_a_malformed_hidden_cookie_is_skipped(cookie):
    assert _hidden(cookie).hidden == ()


def test_an_unknown_section_is_never_hidden_or_remembered():
    jar = _Jar()
    hidden = _hidden(b"", hide="history")
    hidden.remember(jar)

    assert hidden.hidden == () and jar.cookies == {}
