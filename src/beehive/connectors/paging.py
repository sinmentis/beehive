"""Page-number pagination for snapshot connectors, with the completeness rule kept in one place.

A monitor or tracker Source's fetch is treated as the complete current catalogue: ingest marks
every listing it does not contain inactive. A paginating connector must therefore never stop
quietly at its page cap and return the pages it has. It returns everything or raises
TruncatedSnapshotError. collect_pages owns that rule, so a connector only describes how to fetch
page N and whether more pages may follow.

Some storefronts answer a page number past the end with the last page again instead of an empty
page, and their reported page counts can be wrong (Land & Sea reported 83 pages on page 1 and 8 on
the others while serving 8). So the end of a listing is decided by content: given an item key,
collect_pages drops repeated items and stops at the first page that adds nothing new."""
from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

from beehive.connectors.base import TruncatedSnapshotError

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    """One fetched page. has_more is the connector's own judgment (a full page, a next link)."""

    items: list[T]
    has_more: bool


def collect_pages(
    fetch_page: Callable[[int], Page[T]],
    *,
    max_pages: int,
    label: str,
    key: Callable[[T], Hashable] | None = None,
) -> list[T]:
    """Fetch pages 1..max_pages until one reports no more, and return every item in order.

    With a key, an item already collected is dropped, and a non-empty page that adds no new item
    ends the listing (a repeated last page). Raises TruncatedSnapshotError when every page up to
    the cap still had more and one probe page past the cap still adds new items. The probe tells a
    collection that exactly fills the cap apart from a larger one."""
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    collected: list[T] = []
    seen: set[Hashable] = set()

    def new_items(page: Page[T]) -> list[T]:
        if key is None:
            return list(page.items)
        fresh = []
        for item in page.items:
            item_key = key(item)
            if item_key not in seen:
                seen.add(item_key)
                fresh.append(item)
        return fresh

    for number in range(1, max_pages + 1):
        page = fetch_page(number)
        fresh = new_items(page)
        collected.extend(fresh)
        if not page.has_more or (key is not None and page.items and not fresh):
            return collected
    if new_items(fetch_page(max_pages + 1)):
        raise TruncatedSnapshotError(f"{label} exceeded the {max_pages}-page cap")
    return collected
