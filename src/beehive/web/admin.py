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
from beehive.localization import SUPPORTED_LANGUAGES, Localizer, UnsupportedLanguageError, save_language
from beehive.web.deps import (
    SESSION_COOKIE_NAME,
    get_db,
    get_localizer,
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

# Every value here is a translations/web.py key, not display text -- the actual copy always
# comes from the request's Localizer, so the same English exception message renders correctly
# in any supported platform language rather than being hardcoded to one.
_EMAIL_ERROR_KEYS = {
    "Email address is required": "web.email_error.required",
    "Email address cannot contain whitespace": "web.email_error.no_whitespace",
    "Only one email address is supported": "web.email_error.single_address",
    "Email address must contain one @": "web.email_error.at_symbol",
    "Email address needs a local part and domain": "web.email_error.local_and_domain",
    "Email address contains an invalid dot": "web.email_error.invalid_dot",
    "Email domain must contain a valid dot": "web.email_error.invalid_domain_dot",
    _CLEAR_DEFAULT_WITHOUT_ENV_ERROR: "web.email_error.clear_without_env",
}


def _email_error_message(error: EmailConfigurationError, t: Localizer) -> str:
    message = str(error)
    key = _EMAIL_ERROR_KEYS.get(message)
    return t.text(key) if key is not None else message


def _client_ip(request: Request) -> str:
    return request.headers.get("CF-Connecting-IP") or (
        request.client.host if request.client else "unknown")


def _client_country(request: Request) -> str | None:
    return request.headers.get("CF-IPCountry")


def _render_login_page(request: Request, conn: sqlite3.Connection, t: Localizer,
                        error: str | None, status_code: int = 200) -> HTMLResponse:
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
    return templates.TemplateResponse(request, "admin_login.html", {
        "error": error, "last_login": last_login,
    }, status_code=status_code)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, session: dict | None = Depends(get_optional_session),
               conn: sqlite3.Connection = Depends(get_db),
               t: Localizer = Depends(get_localizer)):
    # The public header's admin link always points here (Slice 2 Task 10) -- an already-logged-in
    # owner following it must land on the settings home, not be shown the password form again
    # just because their session is still valid (that previously looked exactly like an
    # unexpectedly-short session).
    if session is not None:
        return RedirectResponse("/admin/", status_code=303)
    return _render_login_page(request, conn, t, error=None)


@router.post("/login")
def login_submit(request: Request, password: str = Form(...),
                  conn: sqlite3.Connection = Depends(get_db),
                  t: Localizer = Depends(get_localizer)):
    ip = _client_ip(request)
    country = _client_country(request)
    now = datetime.now(timezone.utc)

    if is_locked_out(conn, ip, now):
        return _render_login_page(request, conn, t,
                                   error=t.text("web.admin.login.rate_limited"),
                                   status_code=429)

    stored_hash = app_state.get(conn, _PASSWORD_HASH_KEY)
    success = stored_hash is not None and verify_password(stored_hash, password)
    record_attempt(conn, ip, country, success, now.isoformat())

    if not success:
        return _render_login_page(request, conn, t,
                                   error=t.text("web.admin.login.wrong_password"),
                                   status_code=401)

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


def _fetch_interval_label(hours: int, t: Localizer) -> str:
    return (t.text("web.fetch_interval.daily") if hours >= 24
            else t.text("web.fetch_interval.every_n_hours", hours=hours))


def _resolve_default_for_admin(
    conn: sqlite3.Connection, t: Localizer,
) -> tuple[ResolvedRecipient, str | None]:
    try:
        return (
            resolve_default_email(
                conn, os.environ.get("DIGEST_EMAIL_TO")),
            None,
        )
    except EmailConfigurationError as exc:
        return ResolvedRecipient(None, "missing"), _email_error_message(exc, t)


def _build_admin_channel_rows(
    conn: sqlite3.Connection,
    default_recipient: ResolvedRecipient,
    t: Localizer,
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
                channel["fetch_interval_hours"], t),
            "freshness_label": freshness_label(sources, t),
            "freshness_exact_label": freshness_exact_time(sources),
            "fetch_stats_label": fetch_stats_label(sources, t),
            "effective_email": recipient,
        })
    return channels


def _render_admin_home_page(
    request: Request,
    conn: sqlite3.Connection,
    session: dict,
    t: Localizer,
    *,
    submitted_email: str | None = None,
    error: str | None = None,
    saved: bool = False,
    triggered: int | None = None,
    language_saved: bool = False,
    language_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    effective, default_error = _resolve_default_for_admin(conn, t)
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
            "channels": _build_admin_channel_rows(conn, effective, t),
            "languages": SUPPORTED_LANGUAGES,
            "current_language": t.code,
            "language_saved": language_saved,
            "language_error": language_error,
        },
        status_code=status_code,
    )


@router.get("/", response_class=HTMLResponse)
def admin_settings(
    request: Request,
    saved: int | None = None,
    triggered: int | None = None,
    language_saved: int | None = None,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    return _render_admin_home_page(
        request,
        conn,
        session,
        t,
        saved=saved == 1,
        triggered=triggered,
        language_saved=language_saved == 1,
    )


@router.post("/", response_class=HTMLResponse)
def admin_settings_submit(
    request: Request,
    default_digest_email: str = Form(""),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
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
            request, conn, session, t,
            submitted_email=default_digest_email,
            error=_email_error_message(exc, t),
            status_code=400,
        )
    return RedirectResponse("/admin/?saved=1", status_code=303)


@router.post("/language", response_class=HTMLResponse)
def save_language_submit(
    request: Request,
    language: str = Form(...),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    """A deliberately separate form/route from the digest-email settings form above: saving the
    platform language must never run (or be blocked by) email validation, and vice versa. Existing
    per-Item AI summaries/rationale were generated in whatever language was active when the AI
    ranked them -- switching the platform language only changes the web UI's own copy going
    forward; it never retroactively re-summarizes already-stored content."""
    verify_csrf(session, csrf_token)
    try:
        save_language(conn, language)
    except UnsupportedLanguageError:
        return _render_admin_home_page(
            request, conn, session, t,
            language_error=t.text("web.admin.language.invalid"),
            status_code=400,
        )
    return RedirectResponse("/admin/?language_saved=1", status_code=303)


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
def new_channel_form(
    request: Request,
    session: dict = Depends(require_admin_session),
    t: Localizer = Depends(get_localizer),
):
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


_SOURCE_TYPE_ICONS = {
    "reddit_subreddit": "📍",
    "google_news_query": "📰",
    "hackernews_stories": "🟧",
    "hackernews_query": "🟧",
    "rbnz_news": official_feed_icon("rbnz_news"),
    "nz_government_news": official_feed_icon("nz_government_news"),
    "federal_reserve_news": official_feed_icon("federal_reserve_news"),
}


def _source_type_options(t: Localizer) -> tuple[dict, ...]:
    """Built per-request (not module-level) since every label is a translated string --
    Reddit/Google News/Hacker News/RBNZ/Federal Reserve stay in their own proper names in every
    language; only the descriptor after the em dash (e.g. "Subreddit", "Keyword query") changes
    per locale. Mirrors the input_id/icon convention _admin_source_icon relies on below."""
    return (
        {
            "type_key": "reddit_subreddit",
            "input_id": "type-reddit",
            "icon": _SOURCE_TYPE_ICONS["reddit_subreddit"],
            "label": t.text("web.source_type.reddit_subreddit"),
        },
        {
            "type_key": "google_news_query",
            "input_id": "type-google",
            "icon": _SOURCE_TYPE_ICONS["google_news_query"],
            "label": t.text("web.source_type.google_news_query"),
        },
        {
            "type_key": "hackernews_stories",
            "input_id": "type-hn-stories",
            "icon": _SOURCE_TYPE_ICONS["hackernews_stories"],
            "label": t.text("web.source_type.hackernews_stories"),
        },
        {
            "type_key": "hackernews_query",
            "input_id": "type-hn-query",
            "icon": _SOURCE_TYPE_ICONS["hackernews_query"],
            "label": t.text("web.source_type.hackernews_query"),
        },
        {
            "type_key": "rbnz_news",
            "input_id": "type-rbnz",
            "icon": _SOURCE_TYPE_ICONS["rbnz_news"],
            "label": t.text("web.source_type.rbnz_news"),
        },
        {
            "type_key": "nz_government_news",
            "input_id": "type-nz-gov",
            "icon": _SOURCE_TYPE_ICONS["nz_government_news"],
            "label": t.text("web.source_type.nz_government_news"),
        },
        {
            "type_key": "federal_reserve_news",
            "input_id": "type-fed",
            "icon": _SOURCE_TYPE_ICONS["federal_reserve_news"],
            "label": t.text("web.source_type.federal_reserve_news"),
        },
    )


def _admin_source_label(source: dict, t: Localizer) -> str:
    config = json.loads(source["config"])
    if source["type"] == "reddit_subreddit":
        return f"r/{config['subreddit']}"
    if source["type"] == "google_news_query":
        return f'"{config["query"]}"'
    official_label = official_feed_label(source["type"])
    if official_label is not None:
        return official_label
    hackernews_label = hackernews_source_label(source["type"], config, t)
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
    t: Localizer,
    *,
    effective_channel: dict | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    sources = [
        {
            "id": source["id"],
            "label": _admin_source_label(source, t),
            "icon": _admin_source_icon(source),
        }
        for source in list_sources(conn, channel["id"])
    ]
    default_recipient, default_error = _resolve_default_for_admin(conn, t)
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
                       conn: sqlite3.Connection = Depends(get_db),
                       t: Localizer = Depends(get_localizer)):
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _render_edit_channel_page(request, conn, session, channel, t)


@router.post("/channels/{channel_id}/edit")
def edit_channel_submit(channel_id: int, request: Request,
                         name: str = Form(...), profile: str = Form(...),
                         fetch_interval_hours: int = Form(...),
                         digest_email: str = Form(""), csrf_token: str = Form(...),
                         session: dict = Depends(require_admin_session),
                         conn: sqlite3.Connection = Depends(get_db),
                         t: Localizer = Depends(get_localizer)):
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
            request, conn, session, channel, t,
            effective_channel=existing,
            error=_email_error_message(exc, t), status_code=400)
    update_channel(conn, channel_id, name, profile, fetch_interval_hours, normalized_email)
    return RedirectResponse(f"/admin/channels/{channel_id}/edit", status_code=303)


@router.post("/channels/{channel_id}/delete")
def delete_channel_submit(channel_id: int, csrf_token: str = Form(...),
                           session: dict = Depends(require_admin_session),
                           conn: sqlite3.Connection = Depends(get_db)):
    verify_csrf(session, csrf_token)
    delete_channel(conn, channel_id)
    return RedirectResponse("/admin/", status_code=303)


# Every value here is a translations/web.py key, not display text -- see _EMAIL_ERROR_KEYS above.
_SOURCE_ERROR_KEYS = {
    "hackernews_query config needs a non-empty 'query' key": "web.source_error.hn_query_required",
    "reddit_subreddit config needs a non-empty 'subreddit' key": "web.source_error.reddit_subreddit_required",
    "google_news_query config needs a non-empty 'query' key": "web.source_error.google_query_required",
}


def _source_error_message(error: ValueError, t: Localizer) -> str:
    message = str(error)
    if message.startswith("hackernews_stories config needs 'feed'"):
        return t.text("web.source_error.hn_feed_invalid")
    if message.startswith("hackernews_query config needs 'sort'"):
        return t.text("web.source_error.hn_sort_invalid")
    key = _SOURCE_ERROR_KEYS.get(message)
    return t.text(key) if key is not None else message


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
    t: Localizer,
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
            "source_type_options": _source_type_options(t),
            "selected_type": selected_type,
            **values,
        },
        status_code=status_code,
    )


@router.get("/channels/{channel_id}/sources/new", response_class=HTMLResponse)
def new_source_form(channel_id: int, request: Request,
                     session: dict = Depends(require_admin_session),
                     conn: sqlite3.Connection = Depends(get_db),
                     t: Localizer = Depends(get_localizer)):
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _render_new_source_page(request, channel, session, t)


@router.post("/channels/{channel_id}/sources/new")
def new_source_submit(channel_id: int, request: Request, type: str = Form(...),
                       subreddit: str = Form(""), query: str = Form(""),
                       hn_feed: str = Form("top"), hn_query: str = Form(""),
                       hn_sort: str = Form("relevance"),
                       csrf_token: str = Form(...),
                       session: dict = Depends(require_admin_session),
                       conn: sqlite3.Connection = Depends(get_db),
                       t: Localizer = Depends(get_localizer)):
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
            t,
            error=_source_error_message(exc, t),
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
