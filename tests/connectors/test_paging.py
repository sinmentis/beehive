from __future__ import annotations

import pytest

from beehive.connectors.base import TruncatedSnapshotError
from beehive.connectors.paging import Page, collect_pages


def _source(pages: dict[int, list[int]], *, page_size: int, past_end: str = "empty"):
    """A fake paginated listing. past_end="repeat" answers page numbers past the end with the last
    page again, the way some storefronts clamp the page number."""
    calls: list[int] = []
    last = max(pages) if pages else 0

    def fetch_page(number: int) -> Page[int]:
        calls.append(number)
        if number in pages:
            items = pages[number]
        elif past_end == "repeat" and last:
            items = pages[last]
        else:
            items = []
        return Page(items=list(items), has_more=len(items) >= page_size)

    return fetch_page, calls


def _collect(fetch_page, max_pages=5, key=None):
    return collect_pages(fetch_page, max_pages=max_pages, label="store", key=key)


def test_stops_at_the_first_page_that_reports_no_more():
    fetch_page, calls = _source({1: [1, 2], 2: [3]}, page_size=2)

    assert _collect(fetch_page) == [1, 2, 3]
    assert calls == [1, 2]


def test_stops_at_an_empty_page():
    fetch_page, calls = _source({1: [1, 2]}, page_size=2)

    assert _collect(fetch_page) == [1, 2]
    assert calls == [1, 2]


def test_accepts_a_collection_that_exactly_fills_the_cap_after_one_probe():
    fetch_page, calls = _source({1: [1, 2], 2: [3, 4]}, page_size=2)

    assert _collect(fetch_page, max_pages=2) == [1, 2, 3, 4]
    assert calls == [1, 2, 3]


def test_raises_when_the_probe_page_past_the_cap_has_items():
    fetch_page, calls = _source({1: [1, 2], 2: [3, 4], 3: [5]}, page_size=2)

    with pytest.raises(TruncatedSnapshotError, match="store exceeded the 2-page cap"):
        _collect(fetch_page, max_pages=2)
    assert calls == [1, 2, 3]


def test_with_a_key_a_repeated_last_page_ends_the_listing():
    fetch_page, calls = _source({1: [1, 2], 2: [3, 4]}, page_size=2, past_end="repeat")

    assert _collect(fetch_page, key=lambda item: item) == [1, 2, 3, 4]
    assert calls == [1, 2, 3]


def test_with_a_key_the_cap_probe_ignores_a_repeated_last_page():
    fetch_page, calls = _source({1: [1, 2], 2: [3, 4]}, page_size=2, past_end="repeat")

    assert _collect(fetch_page, max_pages=2, key=lambda item: item) == [1, 2, 3, 4]
    assert calls == [1, 2, 3]


def test_with_a_key_repeated_items_are_kept_once_in_first_seen_order():
    fetch_page, _ = _source({1: [1, 2, 2], 2: [3, 1]}, page_size=3)

    assert _collect(fetch_page, key=lambda item: item) == [1, 2, 3]


def test_without_a_key_a_repeating_listing_runs_into_the_cap():
    fetch_page, _ = _source({1: [1, 2], 2: [3, 4]}, page_size=2, past_end="repeat")

    with pytest.raises(TruncatedSnapshotError):
        _collect(fetch_page, max_pages=3)


def test_rejects_a_non_positive_cap():
    fetch_page, _ = _source({}, page_size=2)

    with pytest.raises(ValueError, match="max_pages"):
        _collect(fetch_page, max_pages=0)
