"""Display choices on the reading pages that outlast a visit: whether a store or auction Channel
shows its listings as a list or a gallery, and how many rows a catalogue page holds.

A link makes a choice with a query parameter (`?view=gallery`, `?per_page=48`) and the response
remembers it in a cookie, so the Channel opens the same way next time. A choice missing from
both falls back to the Channel kind's default."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from starlette.requests import Request
from starlette.responses import Response

VIEW_COOKIE = "reading_view"
PER_PAGE_COOKIE = "reading_per_page"
PER_PAGE_CHOICES = (24, 48, 96)
DEFAULT_PER_PAGE = PER_PAGE_CHOICES[0]
_MAX_REMEMBERED_VIEWS = 32
_COOKIE_MAX_AGE_SECONDS = 365 * 86400
_VIEW_CODES = {"g": "gallery", "l": "list"}
# One remembered view: a Channel id in ASCII digits, then g or l. A cookie is the browser's to
# keep, so anything else in it is skipped rather than trusted.
_VIEW_ENTRY_RE = re.compile(r"([0-9]{1,18})([gl])")


class ListingView(str, Enum):
    LIST = "list"
    GALLERY = "gallery"


def default_view(kind: str) -> ListingView:
    """Stores open as a gallery, since their photos are the point. Auctions open as a list,
    where closing times line up to scan."""
    return ListingView.GALLERY if kind == "monitor" else ListingView.LIST


def _parse_views(raw: str | None) -> dict[int, ListingView]:
    """The remembered views, stored compactly as "6g.7l": Channel id, then g or l."""
    views: dict[int, ListingView] = {}
    for entry in (raw or "").split("."):
        match = _VIEW_ENTRY_RE.fullmatch(entry)
        if match is not None:
            views[int(match.group(1))] = ListingView(_VIEW_CODES[match.group(2)])
    return views


def _format_views(views: dict[int, ListingView]) -> str:
    entries = list(views.items())[-_MAX_REMEMBERED_VIEWS:]
    return ".".join(f"{channel}{view.value[0]}" for channel, view in entries)


@dataclass(frozen=True, slots=True)
class ListingPrefs:
    """The view and page size to render, and the cookies that remember a new choice."""

    view: ListingView
    per_page: int
    default_view: ListingView
    _cookies: tuple[tuple[str, str], ...] = ()

    def remember(self, response: Response) -> Response:
        for name, value in self._cookies:
            response.set_cookie(
                name,
                value,
                max_age=_COOKIE_MAX_AGE_SECONDS,
                httponly=True,
                secure=True,
                samesite="lax",
            )
        return response


def resolve_listing_prefs(
    request: Request,
    *,
    channel_id: int,
    kind: str,
    view: str | None,
    per_page: int | None,
) -> ListingPrefs:
    """The Channel's view and page size: a valid query value wins and is remembered, then the
    remembered one, then the default. An unknown value is ignored, never an error."""
    cookies: list[tuple[str, str]] = []
    remembered_views = _parse_views(request.cookies.get(VIEW_COOKIE))
    fallback_view = default_view(kind)
    chosen_view = remembered_views.get(channel_id, fallback_view)
    if view in {choice.value for choice in ListingView}:
        chosen_view = ListingView(view)
        remembered_views.pop(channel_id, None)
        remembered_views[channel_id] = chosen_view
        cookies.append((VIEW_COOKIE, _format_views(remembered_views)))

    remembered_size = request.cookies.get(PER_PAGE_COOKIE, "")
    chosen_size = next(
        (size for size in PER_PAGE_CHOICES if str(size) == remembered_size), DEFAULT_PER_PAGE
    )
    if per_page in PER_PAGE_CHOICES:
        chosen_size = per_page
        cookies.append((PER_PAGE_COOKIE, str(per_page)))

    return ListingPrefs(
        view=chosen_view,
        per_page=chosen_size,
        default_view=fallback_view,
        _cookies=tuple(cookies),
    )
