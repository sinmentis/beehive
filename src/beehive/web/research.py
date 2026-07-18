# src/beehive/web/research.py
"""Owner-only Research workspace routes (ADR-0008: Research Sessions are owner-only -- there is
no public/optional Research read, unlike public.py's Dashboard/Archive/Channel pages). Every
route below -- including every GET -- depends on require_admin_session, and every POST verifies
CSRF before any database write, exactly like admin.py's own convention.

This module stays thin: every view-model, safe-string, and citation-rendering decision lives in
web/research_view.py; every write this module performs goes through an existing repository/
domain entry point (db/research_sessions.py's atomic create-with-sources, db/research_runs.py's
enqueue/cancel, beehive.research.synthesis's exclude/restore, beehive.research.conversation's
submit_owner_message) -- this module never runs a raw AI/network call itself, only ever enqueues
durable work for the separate research worker (ADR-0009) to pick up."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from beehive.connectors import (  # noqa: F401 (import side effect: registers the connectors)
    google_news,
    hackernews,
    official_feeds,
    reddit,
)
from beehive.db.research_runs import (list_research_runs, enqueue_research_run,
                                       request_cancel_research_run)
from beehive.db.research_sessions import (archive_research_session,
                                           create_research_session_with_sources,
                                           get_research_session, hard_delete_research_session,
                                           list_research_sessions, touch_research_session_activity,
                                           unarchive_research_session)
from beehive.db.research_sources import list_research_sources
from beehive.domain.research import ResearchRunStatus, ResearchSessionStatus
from beehive.localization import Localizer
from beehive.research.connector_policy import ConnectorPolicyError, normalize_and_validate_sources
from beehive.research.conversation import ConversationError, submit_owner_message
from beehive.research.synthesis import SynthesisError, exclude_evidence_item, restore_evidence_item
from beehive.web import research_view
from beehive.web.deps import get_db, get_localizer, require_admin_session, verify_csrf

router = APIRouter(prefix="/research")

_TABS = frozenset({"synthesis", "plan", "evidence"})

# Connectors an Owner may pick directly from the create-session form. Reddit is deliberately not
# in this list -- it is offered separately as its own "seed" text field (see module docstring
# item 4 in the task brief) because it needs a specific subreddit value a generic search keyword
# would not correctly fill.
_QUERY_CONNECTORS = ("google_news_query", "hackernews_query")
_FIXED_CONNECTORS = ("hackernews_stories", "rbnz_news", "nz_government_news",
                     "federal_reserve_news")
_SELECTABLE_CONNECTORS = (*_QUERY_CONNECTORS, *_FIXED_CONNECTORS)

_ACTION_ERROR_KEYS = {
    "refresh": "web.research.error.refresh_failed",
    "cancel": "web.research.error.cancel_failed",
    "archive": "web.research.error.archive_failed",
    "unarchive": "web.research.error.unarchive_failed",
    "delete": "web.research.error.delete_failed",
    "evidence": "web.research.error.evidence_failed",
    "message": "web.research.error.message_failed",
}


def _connector_option_label(connector_type: str, t: Localizer) -> str:
    return t.text(f"web.research.new.connector.{connector_type}")


def _require_session(conn: sqlite3.Connection, session_id: int):
    research_session = get_research_session(conn, session_id)
    if research_session is None:
        raise HTTPException(status_code=404, detail="Research Session not found")
    return research_session


def _latest_run(conn: sqlite3.Connection, session_id: int):
    runs = list_research_runs(conn, session_id)
    return runs[-1] if runs else None


# ============================================================================
# List
# ============================================================================

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def research_list(
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    active = list_research_sessions(conn, ResearchSessionStatus.ACTIVE)
    archived = list_research_sessions(conn, ResearchSessionStatus.ARCHIVED)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "research_list.html", {
        "active_rows": [research_view.build_session_row(s, t) for s in active],
        "archived_rows": [research_view.build_session_row(s, t) for s in archived],
    })


# ============================================================================
# Create
# ============================================================================

def _render_new_form(request: Request, session: dict, t: Localizer, *, question: str = "",
                      keyword: str = "", reddit_subreddit: str = "",
                      selected_connectors: list[str] | None = None, error: str | None = None,
                      status_code: int = 200) -> HTMLResponse:
    templates = request.app.state.templates
    selected = set(selected_connectors or [])
    connector_options = [
        {
            "connector_type": connector_type,
            "label": _connector_option_label(connector_type, t),
            "needs_keyword": connector_type in _QUERY_CONNECTORS,
            "checked": connector_type in selected,
        }
        for connector_type in _SELECTABLE_CONNECTORS
    ]
    return templates.TemplateResponse(request, "research_new.html", {
        "csrf_token": session["csrf_token"],
        "question": question,
        "keyword": keyword,
        "reddit_subreddit": reddit_subreddit,
        "connector_options": connector_options,
        "error": error,
        "max_question_length": research_view.MAX_QUESTION_LENGTH,
    }, status_code=status_code)


@router.get("/new", response_class=HTMLResponse)
def new_session_form(
    request: Request,
    session: dict = Depends(require_admin_session),
    t: Localizer = Depends(get_localizer),
):
    return _render_new_form(request, session, t)


@router.post("/new")
def new_session_submit(
    request: Request,
    question: str = Form(...),
    keyword: str = Form(""),
    connectors: list[str] = Form([]),
    reddit_subreddit: str = Form(""),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)

    question_clean = question.strip()
    keyword_clean = keyword.strip() or question_clean
    reddit_clean = reddit_subreddit.strip()
    selected = [c for c in connectors if c in _SELECTABLE_CONNECTORS]

    error = None
    if not question_clean:
        error = t.text("web.research.new.error_question_required")
    elif len(question_clean) > research_view.MAX_QUESTION_LENGTH:
        error = t.text("web.research.new.error_question_too_long")
    elif reddit_clean and len(reddit_clean) > research_view.MAX_SUBREDDIT_LENGTH:
        error = t.text("web.research.new.error_reddit_invalid")

    proposed: list[tuple[str, dict]] = []
    if error is None:
        for connector_type in selected:
            if connector_type == "google_news_query":
                proposed.append((connector_type, {"query": keyword_clean}))
            elif connector_type == "hackernews_query":
                proposed.append((connector_type, {"query": keyword_clean, "sort": "relevance"}))
            elif connector_type == "hackernews_stories":
                proposed.append((connector_type, {"feed": "top"}))
            else:
                proposed.append((connector_type, {}))
        if reddit_clean:
            proposed.append(("reddit_subreddit", {"subreddit": reddit_clean}))
        if not proposed:
            error = t.text("web.research.new.error_no_source")

    normalized: list[tuple[str, dict]] = []
    if error is None:
        try:
            normalized = normalize_and_validate_sources(proposed)
        except ConnectorPolicyError:
            error = t.text("web.research.new.error_source_invalid")

    if error is not None:
        return _render_new_form(
            request, session, t, question=question, keyword=keyword,
            reddit_subreddit=reddit_subreddit, selected_connectors=selected, error=error,
            status_code=400)

    session_id, _run_id = create_research_session_with_sources(
        conn, question_clean, normalized, research_view.utcnow())
    return RedirectResponse(f"/research/{session_id}", status_code=303)


# ============================================================================
# Detail
# ============================================================================

@router.get("/{session_id}", response_class=HTMLResponse)
def research_detail(
    session_id: int,
    request: Request,
    tab: str = "synthesis",
    action_error: str | None = None,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    research_session = _require_session(conn, session_id)
    active_tab = tab if tab in _TABS else "synthesis"
    run = _latest_run(conn, session_id)
    sources = list_research_sources(conn, session_id)

    synthesis_document = research_view.load_synthesis_document(conn, session_id)
    synthesis_view = research_view.build_synthesis_tab_view(synthesis_document, t)
    evidence_view = research_view.build_evidence_tab_view(conn, session_id, t)
    plan_views = research_view.build_plan_views(conn, run.id, t) if run is not None else ()
    conversation_view = research_view.build_conversation_view(
        conn, research_session, synthesis_document, evidence_view.all_excluded, t)
    run_status_view = research_view.build_run_status_view(run, t)

    is_archived = research_session.status is ResearchSessionStatus.ARCHIVED
    has_active_run = run_status_view.is_pending

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "research_detail.html", {
        "session_id": session_id,
        "csrf_token": session["csrf_token"],
        "research_session": research_session,
        "question": research_session.question,
        "is_archived": is_archived,
        "active_tab": active_tab,
        "sources": research_view.build_source_rows(sources, t),
        "run_status": run_status_view,
        "status_url": f"/research/{session_id}/status",
        "messages_status_url": f"/research/{session_id}/messages/status",
        "synthesis": synthesis_view,
        "plan_revisions": plan_views,
        "evidence": evidence_view,
        "conversation": conversation_view,
        "can_refresh": not is_archived and not has_active_run,
        "can_archive": not is_archived and not has_active_run and not conversation_view.has_pending_request,
        "can_unarchive": is_archived,
        "can_mutate_evidence": not is_archived,
        "action_error_message": (
            t.text(_ACTION_ERROR_KEYS[action_error])
            if action_error in _ACTION_ERROR_KEYS else None
        ),
    })


@router.get("/{session_id}/status", response_class=HTMLResponse)
def research_status(
    session_id: int,
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    research_session = _require_session(conn, session_id)
    run = _latest_run(conn, session_id)
    run_status_view = research_view.build_run_status_view(run, t)
    templates = request.app.state.templates
    response = templates.TemplateResponse(request, "_research_status.html", {
        "status_fragment_only": True,
        "run_status": run_status_view,
        "status_url": f"/research/{session_id}/status",
        "session_id": session_id,
        "csrf_token": session["csrf_token"],
        "is_archived": research_session.status is ResearchSessionStatus.ARCHIVED,
        "can_refresh": (
            research_session.status is ResearchSessionStatus.ACTIVE
            and not run_status_view.is_pending
        ),
    })
    if run_status_view.has_run and not run_status_view.is_pending:
        response.headers["HX-Refresh"] = "true"
    return response


@router.get("/{session_id}/messages/status", response_class=HTMLResponse)
def research_messages_status(
    session_id: int,
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    research_session = _require_session(conn, session_id)
    synthesis_document = research_view.load_synthesis_document(conn, session_id)
    evidence_view = research_view.build_evidence_tab_view(conn, session_id, t)
    conversation_view = research_view.build_conversation_view(
        conn, research_session, synthesis_document, evidence_view.all_excluded, t)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "_research_conversation.html", {
        "conversation_fragment_only": True,
        "conversation": conversation_view,
        "session_id": session_id,
        "csrf_token": session["csrf_token"],
        "messages_status_url": f"/research/{session_id}/messages/status",
    })


# ============================================================================
# Run lifecycle: refresh / cancel
# ============================================================================

@router.post("/{session_id}/refresh")
def research_refresh(
    session_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    research_session = _require_session(conn, session_id)
    run = _latest_run(conn, session_id)
    if (research_session.status is not ResearchSessionStatus.ACTIVE
            or (run is not None and run.status in (
                ResearchRunStatus.PENDING, ResearchRunStatus.PROCESSING))):
        return RedirectResponse(
            f"/research/{session_id}?action_error=refresh", status_code=303)
    try:
        enqueue_research_run(conn, session_id, research_view.utcnow())
    except ValueError:
        return RedirectResponse(
            f"/research/{session_id}?action_error=refresh", status_code=303)
    touch_research_session_activity(conn, session_id, research_view.utcnow())
    return RedirectResponse(f"/research/{session_id}", status_code=303)


@router.post("/{session_id}/cancel")
def research_cancel(
    session_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    _require_session(conn, session_id)
    run = _latest_run(conn, session_id)
    if run is None or run.status not in (
            ResearchRunStatus.PENDING, ResearchRunStatus.PROCESSING):
        return RedirectResponse(
            f"/research/{session_id}?action_error=cancel", status_code=303)
    if not request_cancel_research_run(conn, run.id):
        return RedirectResponse(
            f"/research/{session_id}?action_error=cancel", status_code=303)
    return RedirectResponse(f"/research/{session_id}", status_code=303)


# ============================================================================
# Evidence exclude / restore
# ============================================================================

@router.post("/{session_id}/evidence/{item_id}/exclude")
def research_evidence_exclude(
    session_id: int,
    item_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    research_session = _require_session(conn, session_id)
    if research_session.status is not ResearchSessionStatus.ACTIVE:
        return RedirectResponse(
            f"/research/{session_id}?tab=evidence&action_error=evidence", status_code=303)
    try:
        exclude_evidence_item(conn, session_id, item_id, research_view.utcnow())
    except (SynthesisError, ValueError):
        return RedirectResponse(
            f"/research/{session_id}?tab=evidence&action_error=evidence", status_code=303)
    return RedirectResponse(f"/research/{session_id}?tab=evidence", status_code=303)


@router.post("/{session_id}/evidence/{item_id}/restore")
def research_evidence_restore(
    session_id: int,
    item_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    research_session = _require_session(conn, session_id)
    if research_session.status is not ResearchSessionStatus.ACTIVE:
        return RedirectResponse(
            f"/research/{session_id}?tab=evidence&action_error=evidence", status_code=303)
    try:
        restore_evidence_item(conn, session_id, item_id, research_view.utcnow())
    except (SynthesisError, ValueError):
        return RedirectResponse(
            f"/research/{session_id}?tab=evidence&action_error=evidence", status_code=303)
    return RedirectResponse(f"/research/{session_id}?tab=evidence", status_code=303)


# ============================================================================
# Conversation
# ============================================================================

@router.post("/{session_id}/messages")
def research_messages_submit(
    session_id: int,
    content: str = Form(...),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    _require_session(conn, session_id)
    try:
        submit_owner_message(conn, session_id, content, research_view.utcnow())
    except ConversationError:
        return RedirectResponse(
            f"/research/{session_id}?action_error=message", status_code=303)
    touch_research_session_activity(conn, session_id, research_view.utcnow())
    return RedirectResponse(f"/research/{session_id}", status_code=303)


# ============================================================================
# Archive / unarchive / delete
# ============================================================================

@router.post("/{session_id}/archive")
def research_archive(
    session_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    _require_session(conn, session_id)
    try:
        archive_research_session(conn, session_id, research_view.utcnow())
    except ValueError:
        return RedirectResponse(
            f"/research/{session_id}?action_error=archive", status_code=303)
    return RedirectResponse(f"/research/{session_id}", status_code=303)


@router.post("/{session_id}/unarchive")
def research_unarchive(
    session_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    _require_session(conn, session_id)
    try:
        unarchive_research_session(conn, session_id, research_view.utcnow())
    except ValueError:
        return RedirectResponse(
            f"/research/{session_id}?action_error=unarchive", status_code=303)
    return RedirectResponse(f"/research/{session_id}", status_code=303)


@router.post("/{session_id}/delete")
def research_delete(
    session_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    _require_session(conn, session_id)
    if not hard_delete_research_session(conn, session_id):
        return RedirectResponse(
            f"/research/{session_id}?action_error=delete", status_code=303)
    return RedirectResponse("/research", status_code=303)
