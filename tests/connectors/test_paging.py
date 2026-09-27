from __future__ import annotations

import pytest

from beehive.connectors.base import TruncatedSnapshotError
from beehive.connectors.paging import Page, collect_pages


def _source(pages: dict[int, list[int]], *, page_size: int, total_pages: int | None = None):
    calls: list[int] = []

    def fetch_page(number: int) -> Page[int]:
        calls.append(number)
        items = pages.get(number, [])
        has_more = len(items) == page_size and (total_pages is None or number < total_pages)
        return Page(items=items, has_more=has_more, total_pages=total_pages)

    return fetch_page, calls


def test_stops_at_the_first_page_that_reports_no_more():
    fetch_page, calls = _source({1: [1, 2], 2: [3]}, page_size=2)

    assert collect_pages(fetch_page, max_pages=5, label="store") == [1, 2, 3]
    assert calls == [1, 2]


def test_stops_at_an_empty_page():
    fetch_page, calls = _source({1: [1, 2]}, page_size=2)

    assert collect_pages(fetch_page, max_pages=5, label="store") == [1, 2]
    assert calls == [1, 2]


def test_accepts_a_collection_that_exactly_fills_the_cap_after_one_probe():
    fetch_page, calls = _source({1: [1, 2], 2: [3, 4]}, page_size=2)

    assert collect_pages(fetch_page, max_pages=2, label="store") == [1, 2, 3, 4]
    assert calls == [1, 2, 3]


def test_raises_when_the_probe_page_past_the_cap_has_items():
    fetch_page, calls = _source({1: [1, 2], 2: [3, 4], 3: [5]}, page_size=2)

    with pytest.raises(TruncatedSnapshotError, match="store exceeded the 2-page cap"):
        collect_pages(fetch_page, max_pages=2, label="store")
    assert calls == [1, 2, 3]


def test_raises_before_fetching_more_when_the_reported_total_is_over_the_cap():
    fetch_page, calls = _source(
        {n: [n, n] for n in range(1, 10)}, page_size=2, total_pages=9)

    with pytest.raises(TruncatedSnapshotError, match="has 9 pages, above the 3-page cap"):
        collect_pages(fetch_page, max_pages=3, label="store")
    assert calls == [1]


def test_follows_a_reported_total_without_probing():
    fetch_page, calls = _source({1: [1, 2], 2: [3, 4]}, page_size=2, total_pages=2)

    assert collect_pages(fetch_page, max_pages=2, label="store") == [1, 2, 3, 4]
    assert calls == [1, 2]


def test_rejects_a_non_positive_cap():
    fetch_page, _ = _source({}, page_size=2)

    with pytest.raises(ValueError, match="max_pages"):
        collect_pages(fetch_page, max_pages=0, label="store")
