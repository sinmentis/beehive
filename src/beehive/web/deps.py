"""Per-request DB connection dependency. WAL mode (Task 3) allows concurrent readers, so
opening/closing one short-lived connection per request is a fine, simple default at this
app's traffic scale — no pooling needed for a single-operator tool.

require_admin_session (Slice 3) gates every /admin/* route and, later, Slice 2's vote/read-state
writes (ADR-0005) — both are owner-only mutations protected by the same session. It short-circuits
via a 303 redirect (not a bare 401) since this guards a browser-navigated admin UI, not an API.
Every Research route (ADR-0008: Research Sessions are owner-only) also depends on it -- there is
no public/optional Research read, unlike the public Dashboard/Archive/Channel pages below.

_get_valid_session stashes request.state.is_owner on every call (success or failure) so app.py's
template context processor can expose a single `is_owner` flag to every page -- this is what lets
the top-level Research nav link stay owner-only without each page duplicating its own session
check (see that module's docstring).

get_localizer (localization slice) loads the platform's saved language once per request through
the already-cached get_db connection and stashes it on request.state so the Jinja context
processor (app.py) and the custom 404 handler -- which never runs normal dependencies -- can both
read the same per-request Localizer without any process-global mutable cache."""
from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Generator
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse

from fastapi import Depends, HTTPException, Request

from beehive.auth.tokens import verify_signed_session_id
from beehive.db.connection import connect
from beehive.db.sessions import get_session
from beehive.localization import Localizer, load_localizer
from beehive.reading_access import reading_is_private

SESSION_COOKIE_NAME = "admin_session"


def get_db(request: Request) -> Generator[sqlite3.Connection, None, None]:
    conn = connect(request.app.state.db_path)
    # Stashed so app.py's template context processor can render over this same connection
    # instead of opening a second one per page (it runs inside the handler, while this is still
    # open). Cleared on the way out so nothing can reach a closed connection through it.
    request.state.db = conn
    try:
        yield conn
    finally:
        request.state.db = None
        conn.close()


def get_localizer(request: Request,
                   conn: sqlite3.Connection = Depends(get_db)) -> Localizer:
    localizer = load_localizer(conn)
    request.state.localizer = localizer
    return localizer


def _get_valid_session(request: Request, conn: sqlite3.Connection) -> dict | None:
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    session_id = (verify_signed_session_id(raw_cookie, request.app.state.session_secret)
                  if raw_cookie else None)
    session = get_session(conn, session_id) if session_id else None
    if session is None or session["expires_at"] <= datetime.now(timezone.utc).isoformat():
        request.state.is_owner = False
        request.state.csrf_token = None
        return None
    # Single choke point for "is this request from the authenticated Owner", stashed on
    # request.state so app.py's _localization_context can expose it to EVERY template (the
    # top-level Research nav link's owner-only visibility, ADR-0008) without any page needing
    # its own duplicate session check -- both require_admin_session and get_optional_session
    # below call this helper, so any route depending on either one populates it.
    request.state.is_owner = True
    request.state.csrf_token = session["csrf_token"]
    return session


def require_admin_session(request: Request,
                           conn: sqlite3.Connection = Depends(get_db)) -> dict:
    session = _get_valid_session(request, conn)
    if session is None:
        if request.method == "GET":
            return_path = request.url.path
            query = request.url.query
        else:
            referer = urlparse(request.headers.get("referer", ""))
            if referer.path.startswith("/") and (
                not referer.netloc or referer.netloc == request.url.netloc
            ):
                return_path = referer.path
                query = referer.query
            else:
                return_path = "/admin/"
                query = ""
        return_params = dict(parse_qsl(query, keep_blank_values=True))
        return_params["reauth"] = "1"
        return_query = urlencode(return_params)
        target = f"{return_path}?{return_query}" if return_query else return_path
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/admin/login?{urlencode({'next': target})}"},
        )
    return session


def get_optional_session(request: Request,
                          conn: sqlite3.Connection = Depends(get_db)) -> dict | None:
    return _get_valid_session(request, conn)


def require_reader(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> None:
    """The gate on every reading page (ADR-0011). Open to anyone unless the Owner made reading
    private; then a signed-out browser is sent to sign in and brought back to the same page. An
    htmx request gets 401 with HX-Redirect so the whole page moves to the sign-in form."""
    if not reading_is_private(conn) or _get_valid_session(request, conn) is not None:
        return
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    login = f"/admin/login?{urlencode({'next': target})}"
    if request.headers.get("hx-request") == "true":
        raise HTTPException(status_code=401, headers={"HX-Redirect": login})
    if request.method in ("GET", "HEAD"):
        raise HTTPException(status_code=303, headers={"Location": login})
    raise HTTPException(status_code=401)


def verify_csrf(session: dict, csrf_token: str) -> None:
    # Compare as bytes: hmac.compare_digest raises TypeError on non-ASCII str input (see tokens.py).
    if not hmac.compare_digest(session["csrf_token"].encode(), csrf_token.encode()):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")
