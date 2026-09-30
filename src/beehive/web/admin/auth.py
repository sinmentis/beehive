"""Admin sign-in and sign-out. Every other admin route needs the session these create."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from beehive.auth.passwords import verify_password
from beehive.auth.rate_limit import is_locked_out
from beehive.auth.tokens import generate_session_id, sign_session_id
from beehive.db import app_state
from beehive.db.admin_login_attempts import get_most_recent_attempt, record_attempt
from beehive.db.sessions import create_session, delete_session
from beehive.localization import (
    Localizer,
)
from beehive.web.deps import (
    SESSION_COOKIE_NAME,
    get_db,
    get_localizer,
    get_optional_session,
    require_admin_session,
    verify_csrf,
)
from beehive.web.formatting import (
    host_local_time_label,
)
from beehive.web.client_ip import resolve_client_ip
from beehive.web.admin.common import (
    _safe_return_path,
)

router = APIRouter()


_PASSWORD_HASH_KEY = "admin_password_hash"


_SESSION_LIFETIME_DAYS = 30


def _client_ip(request: Request) -> str:
    return resolve_client_ip(
        request.client.host if request.client else None,
        request.headers,
        getattr(request.app.state, "trusted_proxies", ()),
    )


def _client_country(request: Request) -> str | None:
    if not getattr(request.app.state, "trusted_proxies", ()):
        return None
    return request.headers.get("CF-IPCountry")


def _render_login_page(
    request: Request,
    conn: sqlite3.Connection,
    t: Localizer,
    error: str | None,
    next_path: str = "/admin/",
    expired: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    latest = get_most_recent_attempt(conn)
    last_login = None
    if latest:
        last_login = {
            "time": host_local_time_label(latest["attempted_at"]),
            "ip": latest["ip"] or "unknown",
            "country": latest["country"] or t.text("web.admin.login.unknown_region"),
            "success": bool(latest["success"]),
        }
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {
            "error": error,
            "last_login": last_login,
            "next_path": _safe_return_path(next_path),
            "expired": expired,
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    next: str = "/admin/",
    expired: int | None = None,
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    # The public header's admin link always points here (Slice 2 Task 10) -- an already-logged-in
    # owner following it must land on the settings home, not be shown the password form again
    # just because their session is still valid (that previously looked exactly like an
    # unexpectedly-short session).
    if session is not None:
        return RedirectResponse(_safe_return_path(next), status_code=303)
    return _render_login_page(
        request,
        conn,
        t,
        error=None,
        next_path=next,
        expired=expired == 1 or "reauth=1" in next,
    )


@router.post("/login")
def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/admin/"),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    ip = _client_ip(request)
    country = _client_country(request)
    now = datetime.now(timezone.utc)

    if is_locked_out(conn, ip, now):
        return _render_login_page(
            request,
            conn,
            t,
            error=t.text("web.admin.login.rate_limited"),
            next_path=next,
            status_code=429,
        )

    stored_hash = app_state.get(conn, _PASSWORD_HASH_KEY)
    success = stored_hash is not None and verify_password(stored_hash, password)
    record_attempt(conn, ip, country, success, now.isoformat())

    if not success:
        return _render_login_page(
            request,
            conn,
            t,
            error=t.text("web.admin.login.wrong_password"),
            next_path=next,
            status_code=401,
        )

    session_id = generate_session_id()
    csrf_token = (
        generate_session_id()
    )  # same high-entropy generator; distinct value/purpose
    expires_at = (now + timedelta(days=_SESSION_LIFETIME_DAYS)).isoformat()
    create_session(conn, session_id, csrf_token, expires_at)

    response = RedirectResponse(_safe_return_path(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        sign_session_id(session_id, request.app.state.session_secret),
        max_age=_SESSION_LIFETIME_DAYS * 86400,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout(
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    delete_session(conn, session["session_id"])
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE_NAME, httponly=True, secure=True, samesite="lax"
    )
    return response
