"""Page-number pagination for snapshot connectors, with the completeness rule kept in one place.

A monitor or tracker Source's fetch is treated as the complete current catalogue: ingest marks
every listing it does not contain inactive. A paginating connector must therefore never stop
quietly at its page cap and return the pages it has. It returns everything or raises
TruncatedSnapshotError. collect_pages owns that rule, so a connector only describes how to fetch
page N and whether more pages may follow."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from beehive.connectors.base import TruncatedSnapshotError

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    """One fetched page. has_more is the connector's own judgment (a full page, a next link, a
    page number below the reported total). total_pages, when the source reports it on page 1,
    lets a collection that is already too large fail before any further request."""

    items: list[T]
    has_more: bool
    total_pages: int | None = None


def collect_pages(
    fetch_page: Callable[[int], Page[T]],
    *,
    max_pages: int,
    label: str,
) -> list[T]:
    """Fetch pages 1..max_pages until one reports no more, and return every item in order.

    Raises TruncatedSnapshotError when the source reports more than max_pages pages, or when every
    page up to the cap still had more and one probe page past the cap is not empty. The probe
    tells a collection that exactly fills the cap apart from a larger one."""
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    collected: list[T] = []
    for number in range(1, max_pages + 1):
        page = fetch_page(number)
        if number == 1 and page.total_pages is not None and page.total_pages > max_pages:
            raise TruncatedSnapshotError(
                f"{label} has {page.total_pages} pages, above the {max_pages}-page cap"
            )
        collected.extend(page.items)
        if not page.has_more:
            return collected
    if fetch_page(max_pages + 1).items:
        raise TruncatedSnapshotError(f"{label} exceeded the {max_pages}-page cap")
    return collected
