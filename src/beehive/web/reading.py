"""The reading pages, set in the same datasheet as the admin and the workspace.

The home page is the first of them; the channel pages, the archive and search are to follow.
Their contents rail reads 1 Featured, then one chapter per Channel, then the Archive. Featured
carries a quiet number, the Owner's unread stories in the featured window, rather than a badge:
it is a tally, not a call to act. Every reading page renders through `render_reading` so the
rail and the clock are built in one place."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse

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


def render_reading(
    request: Request,
    t: Localizer,
    template: str,
    context: dict,
    *,
    channels: list[dict],
    featured_unread: int | None,
) -> HTMLResponse:
    """Render a reading page inside the reading shell."""
    return request.app.state.templates.TemplateResponse(
        request,
        template,
        {
            **context,
            "shell_nav": reading_nav(t, channels=channels, featured_unread=featured_unread),
        },
    )
