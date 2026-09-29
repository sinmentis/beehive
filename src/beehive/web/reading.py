"""The reading pages, set in the same datasheet as the admin and the workspace: the home page,
the channel pages, the archive, search and the deep-read briefs.

Their contents rail reads 1 Featured, then one chapter per Channel, then the Archive. Featured
carries a quiet number, the Owner's unread stories in the featured window, rather than a badge:
it is a tally, not a call to act. Every reading page renders through `render_reading` so the
rail and the clock are built in one place."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse

from beehive.db.channels import list_channels
from beehive.db.items import count_dashboard_signals
from beehive.featured import featured_utc_bounds, load_featured_window_days
from beehive.localization import Localizer
from beehive.web.formatting import format_count, host_local_time_label


def reading_nav(t: Localizer, *, channels: list[dict], featured_unread: int | None) -> dict:
    """`featured_unread` is None for an anonymous reader, who has no read state."""
    featured = {
        "key": "featured",
        "number": 1,
        "label": t.text("web.dashboard.heading"),
        "href": "/",
    }
    if featured_unread is not None:
        # A quiet count, not an alert: the Owner sees 0 once everything is read.
        featured["tally"] = format_count(featured_unread, t.code)
        featured["tally_label"] = t.text(
            "web.reading.shell.featured_unread",
            count=featured_unread,
            number=format_count(featured_unread, t.code),
        )
    chapters = [featured]
    for number, channel in enumerate(channels, start=2):
        chapters.append(
            {
                "key": f"channel-{channel['id']}",
                "number": number,
                "label": channel["name"],
                "href": f"/channels/{channel['id']}",
            }
        )
    chapters.append(
        {
            "key": "archive",
            "number": len(channels) + 2,
            "label": t.text("web.nav.archive"),
            "href": "/archive",
        }
    )
    return {
        "home_href": "/",
        "label": t.text("web.reading.shell.label"),
        "home_aria": t.text("web.nav.brand_aria", product=t.text("common.product_name")),
        "toc_aria": t.text("web.reading.shell.toc_aria"),
        "chapters": chapters,
        "clock": t.text(
            "web.admin.shell.clock",
            time=host_local_time_label(datetime.now(timezone.utc).isoformat()),
        ),
    }


def featured_unread_count(conn: sqlite3.Connection, now: datetime) -> int:
    """The Owner's unread stories in the featured window: the number beside 1 Featured."""
    published_from, published_to = featured_utc_bounds(now, load_featured_window_days(conn))
    return count_dashboard_signals(
        conn, read_state="unread", published_from=published_from, published_to=published_to
    )


def channel_chapter_number(channels: list[dict], channel_id: int) -> int:
    """A Channel's chapter number in the rail: Featured is 1, Channels follow in list order."""
    return next(
        number for number, channel in enumerate(channels, start=2) if channel["id"] == channel_id
    )


def number_sections(
    chapter_number: int, sections: list[tuple[str, str, str]]
) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """Number a page's present sections under its chapter, in order: (key, label, anchor) in,
    the rail's (number, label, href) list and a key -> number map out."""
    numbers = {key: f"{chapter_number}.{index}" for index, (key, _, _) in enumerate(sections, 1)}
    return [(numbers[key], label, anchor) for key, label, anchor in sections], numbers


def render_reading(
    request: Request,
    t: Localizer,
    template: str,
    context: dict,
    *,
    is_owner: bool,
    channels: list[dict] | None = None,
    featured_unread: int | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a reading page inside the reading shell. Callers that already hold the Channel
    list or the Owner's featured count pass them in; otherwise they are read here."""
    conn = request.state.db
    if channels is None:
        channels = list_channels(conn)
    if is_owner and featured_unread is None:
        featured_unread = featured_unread_count(conn, datetime.now(timezone.utc))
    return request.app.state.templates.TemplateResponse(
        request,
        template,
        {
            **context,
            "shell_nav": reading_nav(
                t, channels=channels, featured_unread=featured_unread if is_owner else None
            ),
        },
        status_code=status_code,
    )
