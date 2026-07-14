"""Admin routes (Slice 3): password login/logout, plus Channel/Source CRUD added in later
tasks — all gated by require_admin_session except /admin/login itself (that's how a session gets
created in the first place)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from beehive.auth.passwords import verify_password
from beehive.auth.rate_limit import is_locked_out
from beehive.auth.tokens import generate_session_id, sign_session_id
from beehive.collector.manual_trigger import request_channel_fetch
from beehive.connectors import (  # noqa: F401 (registers the connectors)
    google_news,
    hackernews,
    official_feeds,
    reddit,
)
from beehive.connectors.registry import get as get_connector
from beehive.db import app_state
from beehive.db.admin_login_attempts import get_most_recent_attempt, record_attempt
from beehive.db.channels import (
    create_channel,
    delete_channel,
    get_channel,
    list_channels,
    update_channel,
)
from beehive.db.sessions import create_session, delete_session
from beehive.db.sources import (
    create_source,
    delete_source,
    get_source,
    list_by_channel as list_sources,
)
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    get_stored_default_email,
    resolve_channel_email,
    resolve_default_email,
    set_stored_default_email,
    validate_email,
)
from beehive.web.deps import (
    SESSION_COOKIE_NAME,
    get_db,
    get_optional_session,
    require_admin_session,
    verify_csrf,
)
from beehive.web.formatting import fetch_stats_label, freshness_exact_time, freshness_label, host_local_time_label
from beehive.web.hackernews_labels import hackernews_source_label
from beehive.web.official_feed_labels import official_feed_icon, official_feed_label

router = APIRouter(prefix="/admin")

_PASSWORD_HASH_KEY = "admin_password_hash"
_SESSION_LIFETIME_DAYS = 90

_CLEAR_DEFAULT_WITHOUT_ENV_ERROR = (
    "Cannot clear default recipient because DIGEST_EMAIL_TO is not configured")

_EMAIL_ERROR_MESSAGES = {
    "Email address is required": "请输入邮箱地址",
    "Email address cannot contain whitespace": "邮箱地址不能包含空格",
    "Only one email address is supported": "仅支持一个邮箱地址",
    "Email address must contain one @": "邮箱地址必须包含一个 @",
    "Email address needs a local part and domain": "邮箱地址缺少用户名或域名",
    "Email address contains an invalid dot": "邮箱地址中的点号位置无效",
    "Email domain must contain a valid dot": "邮箱域名格式无效",
    _CLEAR_DEFAULT_WITHOUT_ENV_ERROR: "无法清除：DIGEST_EMAIL_TO 环境变量未配置",
}


def _email_error_message(error: EmailConfigurationError) -> str:
    message = str(error)
    return _EMAIL_ERROR_MESSAGES.get(message, message)


def _client_ip(request: Request) -> str:
    return request.headers.get("CF-Connecting-IP") or (
        request.client.host if request.client else "unknown")


def _client_country(request: Request) -> str | None:
    return request.headers.get("CF-IPCountry")


def _render_login_page(request: Request, conn: sqlite3.Connection, error: str | None,
                        status_code: int = 200) -> HTMLResponse:
    latest = get_most_recent_attempt(conn)
    last_login = None
    if latest:
        last_login = {
            "time": host_local_time_label(latest["attempted_at"]),
            "ip": latest["ip"] or "unknown",
            "country": latest["country"] or "未知地区",
            "success": bool(latest["success"]),
        }
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin_login.html", {
        "error": error, "last_login": last_login,
    }, status_code=status_code)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, session: dict | None = Depends(get_optional_session),
               conn: sqlite3.Connection = Depends(get_db)):
    # The public header's "⚙ 管理后台" link always points here (Slice 2 Task 10) -- an
    # already-logged-in owner following it must land on the settings home, not be shown the
    # password form again just because their session is still valid (that previously looked
    # exactly like an unexpectedly-short session).
    if session is not None:
        return RedirectResponse("/admin/", status_code=303)
    return _render_login_page(request, conn, error=None)


@router.post("/login")
def login_submit(request: Request, password: str = Form(...),
                  conn: sqlite3.Connection = Depends(get_db)):
    ip = _client_ip(request)
    country = _client_country(request)
    now = datetime.now(timezone.utc)

    if is_locked_out(conn, ip, now):
        return _render_login_page(request, conn, error="登录尝试过多，请 15 分钟后再试",
                                   status_code=429)

    stored_hash = app_state.get(conn, _PASSWORD_HASH_KEY)
    success = stored_hash is not None and verify_password(stored_hash, password)
    record_attempt(conn, ip, country, success, now.isoformat())

    if not success:
        return _render_login_page(request, conn, error="密码错误", status_code=401)

    session_id = generate_session_id()
    csrf_token = generate_session_id()  # same high-entropy generator; distinct value/purpose
    expires_at = (now + timedelta(days=_SESSION_LIFETIME_DAYS)).isoformat()
    create_session(conn, session_id, csrf_token, expires_at)

    response = RedirectResponse("/admin/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME, sign_session_id(session_id, request.app.state.session_secret),
        max_age=_SESSION_LIFETIME_DAYS * 86400, httponly=True, secure=True, samesite="strict")
    return response


@router.post("/logout")
def logout(session: dict = Depends(require_admin_session),
           conn: sqlite3.Connection = Depends(get_db)):
    delete_session(conn, session["session_id"])
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, httponly=True, secure=True, samesite="strict")
    return response


def _fetch_interval_label(hours: int) -> str:
    return "每天一次" if hours >= 24 else f"每 {hours} 小时抓取一次"


def _resolve_default_for_admin(
    conn: sqlite3.Connection,
) -> tuple[ResolvedRecipient, str | None]:
    try:
        return (
            resolve_default_email(
                conn, os.environ.get("DIGEST_EMAIL_TO")),
            None,
        )
    except EmailConfigurationError as exc:
        return ResolvedRecipient(None, "missing"), _email_error_message(exc)


def _build_admin_channel_rows(
    conn: sqlite3.Connection,
    default_recipient: ResolvedRecipient,
) -> list[dict]:
    channels = []
    for channel in list_channels(conn):
        sources = list_sources(conn, channel["id"])
        try:
            recipient = resolve_channel_email(channel, default_recipient).address
        except EmailConfigurationError:
            recipient = None
        channels.append({
            "id": channel["id"],
            "name": channel["name"],
            "source_count": len(sources),
            "fetch_interval_label": _fetch_interval_label(
                channel["fetch_interval_hours"]),
            "freshness_label": freshness_label(sources),
            "freshness_exact_label": freshness_exact_time(sources),
            "fetch_stats_label": fetch_stats_label(sources),
            "effective_email": recipient,
        })
    return channels


def _render_admin_home_page(
    request: Request,
    conn: sqlite3.Connection,
    session: dict,
    *,
    submitted_email: str | None = None,
    error: str | None = None,
    saved: bool = False,
    triggered: int | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    effective, default_error = _resolve_default_for_admin(conn)
    stored = get_stored_default_email(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            "csrf_token": session["csrf_token"],
            "submitted_email": (
                stored or "") if submitted_email is None else submitted_email,
            "effective_email": effective.address,
            "effective_source": effective.source,
            "environment_email": os.environ.get("DIGEST_EMAIL_TO"),
            "error": error,
            "default_error": default_error,
            "saved": saved,
            "triggered": triggered,
            "channels": _build_admin_channel_rows(conn, effective),
        },
        status_code=status_code,
    )


@router.get("/", response_class=HTMLResponse)
def admin_settings(
    request: Request,
    saved: int | None = None,
    triggered: int | None = None,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    return _render_admin_home_page(
        request,
        conn,
        session,
        saved=saved == 1,
        triggered=triggered,
    )


@router.post("/", response_class=HTMLResponse)
def admin_settings_submit(
    request: Request,
    default_digest_email: str = Form(""),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    submitted = default_digest_email.strip()
    try:
        if submitted:
            set_stored_default_email(conn, submitted)
        else:
            environment_email = os.environ.get("DIGEST_EMAIL_TO")
            if not environment_email:
                raise EmailConfigurationError(_CLEAR_DEFAULT_WITHOUT_ENV_ERROR)
            validate_email(environment_email)
            set_stored_default_email(conn, None)
    except EmailConfigurationError as exc:
        return _render_admin_home_page(
            request, conn, session,
            submitted_email=default_digest_email,
            error=_email_error_message(exc),
            status_code=400,
        )
    return RedirectResponse("/admin/?saved=1", status_code=303)


@router.post("/channels/{channel_id}/trigger-fetch")
def trigger_channel_fetch(channel_id: int, request: Request, csrf_token: str = Form(...),
                           session: dict = Depends(require_admin_session),
                           conn: sqlite3.Connection = Depends(get_db)):
    verify_csrf(session, csrf_token)
    if get_channel(conn, channel_id) is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    data_dir = os.path.dirname(request.app.state.db_path)
    request_channel_fetch(data_dir, channel_id)
    return RedirectResponse(f"/admin/?triggered={channel_id}", status_code=303)


@router.get("/channels/new", response_class=HTMLResponse)
def new_channel_form(request: Request, session: dict = Depends(require_admin_session)):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin_new_channel.html", {
        "csrf_token": session["csrf_token"],
    })


@router.post("/channels/new")
def new_channel_submit(name: str = Form(...), profile: str = Form(...),
                        fetch_interval_hours: int = Form(...), csrf_token: str = Form(...),
                        session: dict = Depends(require_admin_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    verify_csrf(session, csrf_token)
    channel_id = create_channel(conn, name, profile, fetch_interval_hours=fetch_interval_hours)
    return RedirectResponse(f"/admin/channels/{channel_id}/edit", status_code=303)


_SOURCE_TYPE_OPTIONS = (
    {
        "type_key": "reddit_subreddit",
        "input_id": "type-reddit",
        "icon": "📍",
        "label": "Reddit — 子版块（Subreddit）",
    },
    {
        "type_key": "google_news_query",
        "input_id": "type-google",
        "icon": "📰",
        "label": "Google News — 关键词查询",
    },
    {
        "type_key": "hackernews_stories",
        "input_id": "type-hn-stories",
        "icon": "🟧",
        "label": "Hacker News — 榜单",
    },
    {
        "type_key": "hackernews_query",
        "input_id": "type-hn-query",
        "icon": "🟧",
        "label": "Hacker News — 关键词搜索",
    },
    {
        "type_key": "rbnz_news",
        "input_id": "type-rbnz",
        "icon": official_feed_icon("rbnz_news"),
        "label": "RBNZ — 新闻发布",
    },
    {
        "type_key": "nz_government_news",
        "input_id": "type-nz-gov",
        "icon": official_feed_icon("nz_government_news"),
        "label": "NZ Government — 政府公告",
    },
    {
        "type_key": "federal_reserve_news",
        "input_id": "type-fed",
        "icon": official_feed_icon("federal_reserve_news"),
        "label": "Federal Reserve — 新闻发布",
    },
)
_SOURCE_TYPE_ICONS = {
    option["type_key"]: option["icon"]
    for option in _SOURCE_TYPE_OPTIONS
}
def _admin_source_label(source: dict) -> str:
    config = json.loads(source["config"])
    if source["type"] == "reddit_subreddit":
        return f"r/{config['subreddit']}"
    if source["type"] == "google_news_query":
        return f'"{config["query"]}"'
    official_label = official_feed_label(source["type"])
    if official_label is not None:
        return official_label
    hackernews_label = hackernews_source_label(source["type"], config)
    return hackernews_label if hackernews_label is not None else source["type"]


def _admin_source_icon(source: dict) -> str:
    """Mirrors the icons admin_add_source.html uses for each type, so a source's icon in the
    Channel edit page's source list always matches the icon the admin picked it by."""
    return _SOURCE_TYPE_ICONS.get(source["type"], "🔗")


def _render_edit_channel_page(
    request: Request,
    conn: sqlite3.Connection,
    session: dict,
    channel: dict,
    *,
    effective_channel: dict | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    sources = [
        {
            "id": source["id"],
            "label": _admin_source_label(source),
            "icon": _admin_source_icon(source),
        }
        for source in list_sources(conn, channel["id"])
    ]
    default_recipient, default_error = _resolve_default_for_admin(conn)
    # The effective hint reflects what a *saved* value would resolve to, so a rejected
    # override (kept only in the display `channel` for the field) must not poison it --
    # resolve it from the unmodified existing row instead.
    source_channel = effective_channel if effective_channel is not None else channel
    try:
        effective = resolve_channel_email(source_channel, default_recipient).address
    except EmailConfigurationError:
        effective = None
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_edit_channel.html",
        {
            "channel": channel,
            "sources": sources,
            "csrf_token": session["csrf_token"],
            "effective_email": effective,
            "error": error,
            "default_error": default_error,
        },
        status_code=status_code,
    )


@router.get("/channels/{channel_id}/edit", response_class=HTMLResponse)
def edit_channel_form(channel_id: int, request: Request,
                       session: dict = Depends(require_admin_session),
                       conn: sqlite3.Connection = Depends(get_db)):
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _render_edit_channel_page(request, conn, session, channel)


@router.post("/channels/{channel_id}/edit")
def edit_channel_submit(channel_id: int, request: Request,
                         name: str = Form(...), profile: str = Form(...),
                         fetch_interval_hours: int = Form(...),
                         digest_email: str = Form(""), csrf_token: str = Form(...),
                         session: dict = Depends(require_admin_session),
                         conn: sqlite3.Connection = Depends(get_db)):
    verify_csrf(session, csrf_token)
    existing = get_channel(conn, channel_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    submitted_email = digest_email.strip()
    try:
        normalized_email = validate_email(submitted_email) if submitted_email else None
    except EmailConfigurationError as exc:
        channel = {
            **existing,
            "name": name,
            "profile": profile,
            "fetch_interval_hours": fetch_interval_hours,
            "digest_email": digest_email,
        }
        return _render_edit_channel_page(
            request, conn, session, channel,
            effective_channel=existing,
            error=_email_error_message(exc), status_code=400)
    update_channel(conn, channel_id, name, profile, fetch_interval_hours, normalized_email)
    return RedirectResponse(f"/admin/channels/{channel_id}/edit", status_code=303)


@router.post("/channels/{channel_id}/delete")
def delete_channel_submit(channel_id: int, csrf_token: str = Form(...),
                           session: dict = Depends(require_admin_session),
                           conn: sqlite3.Connection = Depends(get_db)):
    verify_csrf(session, csrf_token)
    delete_channel(conn, channel_id)
    return RedirectResponse("/admin/", status_code=303)


_SOURCE_ERROR_MESSAGES = {
    "hackernews_query config needs a non-empty 'query' key": "请输入 Hacker News 搜索关键词",
}


def _source_error_message(error: ValueError) -> str:
    message = str(error)
    if message.startswith("hackernews_stories config needs 'feed'"):
        return "请选择有效的 Hacker News 榜单"
    if message.startswith("hackernews_query config needs 'sort'"):
        return "请选择有效的 Hacker News 排序方式"
    return _SOURCE_ERROR_MESSAGES.get(message, message)


def _source_config_from_form(
    source_type: str,
    *,
    subreddit: str,
    query: str,
    hn_feed: str,
    hn_query: str,
    hn_sort: str,
) -> dict:
    if source_type == "reddit_subreddit":
        return {"subreddit": subreddit}
    if source_type == "google_news_query":
        return {"query": query}
    if source_type == "hackernews_stories":
        return {"feed": hn_feed}
    if source_type == "hackernews_query":
        return {"query": hn_query, "sort": hn_sort}
    if source_type in {"rbnz_news", "nz_government_news", "federal_reserve_news"}:
        return {}
    raise ValueError(f"unknown Source type: {source_type!r}")


def _render_new_source_page(
    request: Request,
    channel: dict,
    session: dict,
    *,
    error: str | None = None,
    selected_type: str = "reddit_subreddit",
    form_values: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    values = {
        "subreddit": "",
        "query": "",
        "hn_feed": "top",
        "hn_query": "",
        "hn_sort": "relevance",
        **(form_values or {}),
    }
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_add_source.html",
        {
            "channel": channel,
            "csrf_token": session["csrf_token"],
            "error": error,
            "source_type_options": _SOURCE_TYPE_OPTIONS,
            "selected_type": selected_type,
            **values,
        },
        status_code=status_code,
    )


@router.get("/channels/{channel_id}/sources/new", response_class=HTMLResponse)
def new_source_form(channel_id: int, request: Request,
                     session: dict = Depends(require_admin_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _render_new_source_page(request, channel, session)


@router.post("/channels/{channel_id}/sources/new")
def new_source_submit(channel_id: int, request: Request, type: str = Form(...),
                       subreddit: str = Form(""), query: str = Form(""),
                       hn_feed: str = Form("top"), hn_query: str = Form(""),
                       hn_sort: str = Form("relevance"),
                       csrf_token: str = Form(...),
                       session: dict = Depends(require_admin_session),
                       conn: sqlite3.Connection = Depends(get_db)):
    verify_csrf(session, csrf_token)
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    form_values = {
        "subreddit": subreddit,
        "query": query,
        "hn_feed": hn_feed,
        "hn_query": hn_query,
        "hn_sort": hn_sort,
    }
    try:
        config = _source_config_from_form(type, **form_values)
        connector = get_connector(type)
        connector.validate_config(config)
    except ValueError as exc:
        return _render_new_source_page(
            request,
            channel,
            session,
            error=_source_error_message(exc),
            selected_type=type,
            form_values=form_values,
            status_code=400,
        )
    create_source(conn, channel_id, type, config)
    return RedirectResponse(f"/admin/channels/{channel_id}/edit", status_code=303)


@router.post("/sources/{source_id}/delete")
def delete_source_submit(source_id: int, csrf_token: str = Form(...),
                          session: dict = Depends(require_admin_session),
                          conn: sqlite3.Connection = Depends(get_db)):
    verify_csrf(session, csrf_token)
    source = get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    channel_id = source["channel_id"]
    delete_source(conn, source_id)
    return RedirectResponse(f"/admin/channels/{channel_id}/edit", status_code=303)
