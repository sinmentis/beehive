import pytest
from starlette.requests import Request

from beehive.web.reading_prefs import ListingView, resolve_listing_prefs


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
