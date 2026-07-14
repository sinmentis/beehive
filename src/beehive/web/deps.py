"""Per-request DB connection dependency. WAL mode (Task 3) allows concurrent readers, so
opening/closing one short-lived connection per request is a fine, simple default at this
app's traffic scale — no pooling needed for a single-operator tool.

require_admin_session (Slice 3) gates every /admin/* route and, later, Slice 2's vote/read-state
writes (ADR-0005) — both are owner-only mutations protected by the same session. It short-circuits
via a 303 redirect (not a bare 401) since this guards a browser-navigated admin UI, not an API."""
from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Generator
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request

from beehive.auth.tokens import verify_signed_session_id
from beehive.db.connection import connect
from beehive.db.sessions import get_session

SESSION_COOKIE_NAME = "admin_session"


def get_db(request: Request) -> Generator[sqlite3.Connection, None, None]:
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _get_valid_session(request: Request, conn: sqlite3.Connection) -> dict | None:
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    session_id = (verify_signed_session_id(raw_cookie, request.app.state.session_secret)
                  if raw_cookie else None)
    session = get_session(conn, session_id) if session_id else None
    if session is None or session["expires_at"] <= datetime.now(timezone.utc).isoformat():
        return None
    return session


def require_admin_session(request: Request,
                           conn: sqlite3.Connection = Depends(get_db)) -> dict:
    session = _get_valid_session(request, conn)
    if session is None:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return session


def get_optional_session(request: Request,
                          conn: sqlite3.Connection = Depends(get_db)) -> dict | None:
    return _get_valid_session(request, conn)


def verify_csrf(session: dict, csrf_token: str) -> None:
    # Compare as bytes: hmac.compare_digest raises TypeError on non-ASCII str input (see tokens.py).
    if not hmac.compare_digest(session["csrf_token"].encode(), csrf_token.encode()):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")
