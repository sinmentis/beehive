"""The Owner's workspace: Research and the Watch List, set in the same datasheet as the admin.

The workspace has its own contents rail with two chapters. Research carries a blue count of
finished runs the Owner has not opened yet; the Watch List carries an amber count of reminders
that failed to send. Every workspace page renders through `render_workspace` so the rail, the
running head and the clock are built in one place."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse

from beehive.db.research_sessions import count_unread_completed_research_sessions
from beehive.db.tracker_watches import count_failed_tracker_reminders
from beehive.localization import Localizer
from beehive.web.formatting import host_local_time_label


def workspace_nav(request: Request, t: Localizer) -> dict:
    conn = request.state.db
    return {
        "home_href": "/research",
        "label": t.text("web.workspace.shell.label"),
        "home_aria": t.text("web.workspace.shell.home_aria", product=t.text("common.product_name")),
        "toc_aria": t.text("web.workspace.shell.toc_aria"),
        "chapters": [
            {
                "key": "research",
                "number": 1,
                "label": t.text("web.nav.research"),
                "href": "/research",
                "unread": count_unread_completed_research_sessions(conn),
            },
            {
                "key": "watchlist",
                "number": 2,
                "label": t.text("web.nav.watchlist"),
                "href": "/watchlist",
                "attention": count_failed_tracker_reminders(conn),
            },
        ],
        "clock": t.text(
            "web.admin.shell.clock",
            time=host_local_time_label(datetime.now(timezone.utc).isoformat()),
        ),
    }


def render_workspace(
    request: Request,
    t: Localizer,
    template: str,
    context: dict,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a workspace page inside the workspace shell."""
    return request.app.state.templates.TemplateResponse(
        request,
        template,
        {**context, "shell_nav": workspace_nav(request, t)},
        status_code=status_code,
    )
