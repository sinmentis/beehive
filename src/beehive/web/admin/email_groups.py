"""Email group pages: create, edit, preview, test-send and delete a group."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from beehive.channels import require_channel_kind
from beehive.db.admin_actions import (
    delete_email_group_with_undo,
    record_admin_action,
)
from beehive.db.channels import (
    list_channels,
)
from beehive.db.email_groups import (
    assign_channel,
    create_email_group,
    get_channel_group,
    get_email_group,
    list_member_channels,
    unassign_channel,
    update_email_group,
)
from beehive.digest.send import build_email_group_digest_preview
from beehive.email_routing import (
    EmailConfigurationError,
    resolve_default_email,
    resolve_group_email,
    validate_email,
)
from beehive.localization import (
    Localizer,
)
from beehive.notify import build_notifier
from beehive.scheduling import (
    next_email_group_due_at,
)
from beehive.web.deps import (
    get_db,
    get_localizer,
    require_admin_session,
    verify_csrf,
)
from beehive.web.formatting import (
    short_time_label,
)
from beehive.web.admin.common import (
    _EMAIL_WEEKDAYS,
    _SCHEDULE_TIMEZONES,
    _channel_kind_label,
    _email_error_message,
    _email_group_schedule_label,
    _render_admin,
    _resolve_default_for_admin,
)

router = APIRouter()


def _email_weekday_rows(t: Localizer, selected: set[int]) -> list[dict]:
    return [
        {
            "value": value,
            "label": t.text(label_key),
            "short_label": t.text(f"web.admin.weekday_short.{label_key.rsplit('.', 1)[1]}"),
            "selected": value in selected,
        }
        for value, label_key in _EMAIL_WEEKDAYS
    ]


def _normalize_email_schedule(
    *,
    schedule_mode: str,
    schedule_timezone: str,
    schedule_time: str,
    schedule_weekdays: list[int] | None,
) -> tuple[str, str, str, str]:
    if schedule_mode not in {"interval", "calendar"}:
        raise ValueError("web.admin.email_group.schedule_error_mode")
    timezone_name = schedule_timezone.strip() or "Pacific/Auckland"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("web.admin.email_group.schedule_error_timezone") from exc
    try:
        parsed_time = datetime.strptime(schedule_time.strip(), "%H:%M")
    except ValueError as exc:
        raise ValueError("web.admin.email_group.schedule_error_time") from exc
    normalized_time = parsed_time.strftime("%H:%M")
    weekdays = sorted(set(schedule_weekdays or []))
    if schedule_mode == "calendar" and (
        not weekdays or any(day not in range(7) for day in weekdays)
    ):
        raise ValueError("web.admin.email_group.schedule_error_days")
    if not weekdays:
        weekdays = list(range(7))
    return (
        schedule_mode,
        timezone_name,
        normalized_time,
        ",".join(str(day) for day in weekdays),
    )


def _build_group_channel_rows(
    conn: sqlite3.Connection,
    t: Localizer,
    *,
    group_id: int | None,
    selected_ids: set[int] | None = None,
) -> list[dict]:
    """Powers the channel-assignment checklist on both the new and edit group pages. When
    selected_ids is given (re-rendering after a validation error) it reflects the user's
    just-submitted, not-yet-saved checkbox state instead of the DB's current membership --
    group_id is still used to decide whether a channel's *other* group membership should be
    flagged (a channel already in *this* group is never "other")."""
    if selected_ids is not None:
        member_ids = selected_ids
    elif group_id is not None:
        member_ids = {c["id"] for c in list_member_channels(conn, group_id)}
    else:
        member_ids = set()
    rows = []
    for channel in list_channels(conn):
        kind = require_channel_kind(channel["kind"])
        other_group = get_channel_group(conn, channel["id"])
        other_group_name = None
        if other_group is not None and other_group["id"] != group_id:
            other_group_name = other_group["name"]
        rows.append(
            {
                "id": channel["id"],
                "name": channel["name"],
                "kind": kind.value,
                "kind_label": _channel_kind_label(kind, t),
                "is_member": channel["id"] in member_ids,
                "other_group_name": other_group_name,
            }
        )
    return rows


def _render_new_email_group_page(
    request: Request,
    conn: sqlite3.Connection,
    session: dict,
    t: Localizer,
    *,
    name: str = "",
    subject_template: str = "",
    recipient_email: str = "",
    send_interval_hours: int = 24,
    schedule_mode: str = "interval",
    schedule_timezone: str = "Pacific/Auckland",
    schedule_time: str = "09:00",
    schedule_weekdays: set[int] | None = None,
    selected_channel_ids: set[int] | None = None,
    error: str | None = None,
    schedule_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    default_recipient, default_error = _resolve_default_for_admin(conn, t)
    return _render_admin(
        request,
        t,
        "admin_new_email_group.html",
        {
            "csrf_token": session["csrf_token"],
            "name": name,
            "subject_template": subject_template,
            "recipient_email": recipient_email,
            "send_interval_hours": send_interval_hours,
            "schedule_mode": schedule_mode,
            "schedule_timezone": schedule_timezone,
            "schedule_time": schedule_time,
            "schedule_timezones": _SCHEDULE_TIMEZONES,
            "schedule_weekdays": _email_weekday_rows(
                t,
                set(range(7)) if schedule_weekdays is None else schedule_weekdays,
            ),
            "effective_email": default_recipient.address,
            "default_error": default_error,
            "error": error,
            "schedule_error": schedule_error,
            "channel_rows": _build_group_channel_rows(
                conn, t, group_id=None, selected_ids=selected_channel_ids
            ),
        },
        status_code=status_code,
    )


@router.get("/email-groups/new", response_class=HTMLResponse)
def new_email_group_form(
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    return _render_new_email_group_page(request, conn, session, t)


@router.post("/email-groups/new")
def new_email_group_submit(
    request: Request,
    name: str = Form(...),
    subject_template: str = Form(...),
    recipient_email: str = Form(""),
    send_interval_hours: int = Form(24, ge=1),
    schedule_mode: str = Form("interval"),
    schedule_timezone: str = Form("Pacific/Auckland"),
    schedule_time: str = Form("09:00"),
    schedule_weekdays: list[int] | None = Form(None),
    channel_ids: list[int] | None = Form(None),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    submitted_email = recipient_email.strip()
    try:
        normalized_email = validate_email(submitted_email) if submitted_email else None
    except EmailConfigurationError as exc:
        return _render_new_email_group_page(
            request,
            conn,
            session,
            t,
            name=name,
            subject_template=subject_template,
            recipient_email=recipient_email,
            send_interval_hours=send_interval_hours,
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=set(schedule_weekdays or []),
            selected_channel_ids=set(channel_ids or []),
            error=_email_error_message(exc, t),
            status_code=400,
        )
    try:
        normalized_schedule = _normalize_email_schedule(
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=schedule_weekdays,
        )
    except ValueError as exc:
        return _render_new_email_group_page(
            request,
            conn,
            session,
            t,
            name=name,
            subject_template=subject_template,
            recipient_email=recipient_email,
            send_interval_hours=send_interval_hours,
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=set(schedule_weekdays or []),
            selected_channel_ids=set(channel_ids or []),
            schedule_error=t.text(str(exc)),
            status_code=400,
        )
    group_id = create_email_group(
        conn,
        name,
        subject_template,
        normalized_email,
        send_interval_hours,
        schedule_mode=normalized_schedule[0],
        schedule_timezone=normalized_schedule[1],
        schedule_time=normalized_schedule[2],
        schedule_weekdays=normalized_schedule[3],
    )
    for channel_id in dict.fromkeys(channel_ids or []):
        assign_channel(conn, group_id, channel_id)
    record_admin_action(
        conn,
        action_type="email_group_created",
        target_type="email_group",
        target_id=group_id,
        target_label=name,
        detail={"channels": len(set(channel_ids or []))},
    )
    return RedirectResponse(
        f"/admin/email-groups/{group_id}/edit?created=1",
        status_code=303,
    )


def _render_edit_email_group_page(
    request: Request,
    conn: sqlite3.Connection,
    session: dict,
    group: dict,
    t: Localizer,
    *,
    name: str | None = None,
    subject_template: str | None = None,
    recipient_email: str | None = None,
    send_interval_hours: int | None = None,
    schedule_mode: str | None = None,
    schedule_timezone: str | None = None,
    schedule_time: str | None = None,
    schedule_weekdays: set[int] | None = None,
    selected_channel_ids: set[int] | None = None,
    error: str | None = None,
    schedule_error: str | None = None,
    saved: bool = False,
    created: bool = False,
    test_sent: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    default_recipient, default_error = _resolve_default_for_admin(conn, t)
    channel_rows = _build_group_channel_rows(
        conn, t, group_id=group["id"], selected_ids=selected_channel_ids
    )
    return _render_admin(
        request,
        t,
        "admin_edit_email_group.html",
        {
            "group": group,
            "csrf_token": session["csrf_token"],
            "name": group["name"] if name is None else name,
            "subject_template": (
                group["subject_template"]
                if subject_template is None
                else subject_template
            ),
            "recipient_email": (
                (group["recipient_email"] or "")
                if recipient_email is None
                else recipient_email
            ),
            "send_interval_hours": (
                group["send_interval_hours"]
                if send_interval_hours is None
                else send_interval_hours
            ),
            "schedule_mode": (
                group["schedule_mode"] if schedule_mode is None else schedule_mode
            ),
            "schedule_timezone": (
                group["schedule_timezone"]
                if schedule_timezone is None
                else schedule_timezone
            ),
            "schedule_time": (
                group["schedule_time"] if schedule_time is None else schedule_time
            ),
            "schedule_timezones": _SCHEDULE_TIMEZONES,
            "schedule_weekdays": _email_weekday_rows(
                t,
                (
                    {
                        int(value)
                        for value in group["schedule_weekdays"].split(",")
                        if value
                    }
                    if schedule_weekdays is None
                    else schedule_weekdays
                ),
            ),
            "effective_email": default_recipient.address,
            "default_error": default_error,
            "error": error,
            "schedule_error": schedule_error,
            "saved": saved,
            "created": created,
            "test_sent": test_sent,
            "channel_rows": channel_rows,
            "member_count": sum(row["is_member"] for row in channel_rows),
            "schedule_label": _email_group_schedule_label(group, t),
            "schedule_short": _email_group_schedule_label(group, t, short=True),
            "last_sent_short": (
                short_time_label(group["last_sent_at"], datetime.now(timezone.utc), t)
                if group["last_sent_at"]
                else None
            ),
            "next_due_short": short_time_label(
                next_email_group_due_at(group, datetime.now(timezone.utc)),
                datetime.now(timezone.utc),
                t,
            ),
            "unsaved_submission": status_code >= 400,
        },
        status_code=status_code,
    )


@router.get("/email-groups/{group_id}/edit", response_class=HTMLResponse)
def edit_email_group_form(
    group_id: int,
    request: Request,
    saved: int | None = None,
    created: int | None = None,
    test_sent: int | None = None,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    return _render_edit_email_group_page(
        request,
        conn,
        session,
        group,
        t,
        saved=saved == 1,
        created=created == 1,
        test_sent=test_sent == 1,
    )


@router.post("/email-groups/{group_id}/edit")
def edit_email_group_submit(
    group_id: int,
    request: Request,
    name: str = Form(...),
    subject_template: str = Form(...),
    recipient_email: str = Form(""),
    send_interval_hours: int = Form(..., ge=1),
    schedule_mode: str = Form("interval"),
    schedule_timezone: str = Form("Pacific/Auckland"),
    schedule_time: str = Form("09:00"),
    schedule_weekdays: list[int] | None = Form(None),
    channel_ids: list[int] | None = Form(None),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    submitted_email = recipient_email.strip()
    try:
        normalized_email = validate_email(submitted_email) if submitted_email else None
    except EmailConfigurationError as exc:
        return _render_edit_email_group_page(
            request,
            conn,
            session,
            group,
            t,
            name=name,
            subject_template=subject_template,
            recipient_email=recipient_email,
            send_interval_hours=send_interval_hours,
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=set(schedule_weekdays or []),
            selected_channel_ids=set(channel_ids or []),
            error=_email_error_message(exc, t),
            status_code=400,
        )
    try:
        normalized_schedule = _normalize_email_schedule(
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=schedule_weekdays,
        )
    except ValueError as exc:
        return _render_edit_email_group_page(
            request,
            conn,
            session,
            group,
            t,
            name=name,
            subject_template=subject_template,
            recipient_email=recipient_email,
            send_interval_hours=send_interval_hours,
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=set(schedule_weekdays or []),
            selected_channel_ids=set(channel_ids or []),
            schedule_error=t.text(str(exc)),
            status_code=400,
        )
    update_email_group(
        conn,
        group_id,
        name,
        subject_template,
        normalized_email,
        send_interval_hours,
        schedule_mode=normalized_schedule[0],
        schedule_timezone=normalized_schedule[1],
        schedule_time=normalized_schedule[2],
        schedule_weekdays=normalized_schedule[3],
    )
    selected_ids = set(dict.fromkeys(channel_ids or []))
    current_member_ids = {c["id"] for c in list_member_channels(conn, group_id)}
    for channel_id in selected_ids - current_member_ids:
        assign_channel(conn, group_id, channel_id)
    for channel_id in current_member_ids - selected_ids:
        unassign_channel(conn, channel_id)
    record_admin_action(
        conn,
        action_type="email_group_updated",
        target_type="email_group",
        target_id=group_id,
        target_label=name,
        detail={"channels": len(selected_ids)},
    )
    return RedirectResponse(
        f"/admin/email-groups/{group_id}/edit?saved=1",
        status_code=303,
    )


@router.get("/email-groups/{group_id}/preview", response_class=HTMLResponse)
def preview_email_group(
    group_id: int,
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    preview = build_email_group_digest_preview(conn, group, t)
    default_recipient, default_error = _resolve_default_for_admin(conn, t)
    try:
        recipient = resolve_group_email(group, default_recipient).address
    except EmailConfigurationError as exc:
        recipient = None
        default_error = _email_error_message(exc, t)
    return _render_admin(
        request,
        t,
        "admin_email_group_preview.html",
        {
            "group": group,
            "preview": preview,
            "recipient": recipient,
            "recipient_error": default_error,
            "csrf_token": session["csrf_token"],
        },
    )


@router.post("/email-groups/{group_id}/test-send")
def test_send_email_group(
    group_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    preview = build_email_group_digest_preview(conn, group, t)
    if preview is None:
        raise HTTPException(status_code=409, detail="Email group has no content to preview")
    default_recipient = resolve_default_email(
        conn,
        os.environ.get("DIGEST_EMAIL_TO"),
    )
    recipient = resolve_group_email(group, default_recipient)
    if recipient.address is None:
        raise HTTPException(status_code=409, detail="Email recipient is not configured")
    notifier = build_notifier(os.environ, default_to_addr=default_recipient.address)
    notifier.send(
        f"[Test] {preview.subject}",
        preview.plain_text,
        preview.html,
        to_addr=recipient.address,
    )
    record_admin_action(
        conn,
        action_type="email_group_test_sent",
        target_type="email_group",
        target_id=group_id,
        target_label=group["name"],
        detail={
            "channels": preview.channel_count,
            "events": preview.event_count,
        },
    )
    return RedirectResponse(
        f"/admin/email-groups/{group_id}/edit?test_sent=1",
        status_code=303,
    )


@router.post("/email-groups/{group_id}/delete")
def delete_email_group_submit(
    group_id: int,
    csrf_token: str = Form(...),
    confirmation: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    if confirmation.strip() != group["name"]:
        raise HTTPException(status_code=409, detail="Email group confirmation did not match")
    action_id = delete_email_group_with_undo(
        conn,
        group_id,
        target_label=group["name"],
    )
    return RedirectResponse(
        f"/admin/?tab=system&action={action_id}",
        status_code=303,
    )
