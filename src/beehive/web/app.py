from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from beehive.db.connection import connect, init_schema
from beehive.db.research_sessions import count_unread_completed_research_sessions
from beehive.localization import load_localizer
from beehive.web import admin, public, research
from beehive.web.client_ip import parse_trusted_proxies

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_LOGGER = logging.getLogger(__name__)

_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "script-src 'self'; frame-ancestors 'none'")
# `deploy/README.md` tells the operator to generate this with `openssl rand -hex 32` (64 chars),
# so 32 is a floor well under any legitimate value.
MIN_SESSION_SECRET_LENGTH = 32


class InsecureSessionSecretError(RuntimeError):
    """`SESSION_SECRET` is missing or too short to sign a session cookie with.

    Raised at startup rather than tolerated, because the failure is otherwise silent and total:
    `sign_session_id` HMACs with whatever it is given, so an empty secret means anybody can mint
    `<any-session-id>.<hmac-with-empty-key>` and `require_admin_session` accepts it. The app came
    up looking healthy, served `/admin/*` to the world, and nothing in the logs said why.
    """


def _resolve_session_secret(explicit: str | None) -> str:
    secret = explicit if explicit is not None else os.environ.get("SESSION_SECRET", "")
    if len(secret.strip()) < MIN_SESSION_SECRET_LENGTH:
        raise InsecureSessionSecretError(
            f"SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters; "
            "generate one with `openssl rand -hex 32` (see deploy/README.md)")
    return secret


def _static_asset_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(_STATIC_DIR.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _localization_context(request: Request) -> dict:
    """Exposes t()/localizer/locale to every template via Jinja2Templates' context_processors.
    Matched routes populate request.state.localizer through the get_localizer dependency (over
    the same cached get_db connection, see deps.py); the custom 404 handler below never runs
    normal dependencies, so it loads the Localizer itself before rendering.

    is_owner mirrors request.state.is_owner, stashed by deps.py's _get_valid_session on every
    request that depends on require_admin_session or get_optional_session (every page in the
    app except the unmatched-route 404 handler, which never runs dependencies at all -- hence
    the getattr default of False there). base.html's top-level Research nav link is gated on
    this single flag so it never appears for an anonymous visitor on any page."""
    localizer = request.state.localizer
    is_owner = getattr(request.state, "is_owner", False)
    research_unread_count = 0
    if is_owner:
        with _request_db(request) as conn:
            research_unread_count = count_unread_completed_research_sessions(conn)
    return {
        "t": localizer.text, "localizer": localizer, "locale": localizer.code,
        "is_owner": is_owner,
        "global_csrf_token": getattr(request.state, "csrf_token", None),
        "research_unread_count": research_unread_count,
    }


@contextmanager
def _request_db(request: Request) -> Iterator[sqlite3.Connection]:
    """The request's existing connection, or a short-lived one when there is none.

    Rendering happens inside the route handler, so get_db's connection (deps.py stashes it on
    request.state) is still open and can be reused. Opening a second one per render meant every
    owner page paid an extra sqlite3.connect plus the WAL/foreign-key PRAGMAs just to read one
    integer. The fallback matters for the error handlers, which render without ever running a
    dependency."""
    existing = getattr(request.state, "db", None)
    if existing is not None:
        yield existing
        return
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _apply_security_headers(request: Request, response: Response) -> Response:
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "private, no-store"
        vary = response.headers.get("Vary")
        if not vary:
            response.headers["Vary"] = "Cookie"
        elif "cookie" not in {value.strip().lower() for value in vary.split(",")}:
            response.headers["Vary"] = f"{vary}, Cookie"
    return response


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """App-wide, not admin-only: a same-origin XSS on the *public* Dashboard/drill-down could
    otherwise pivot into stealing the *admin* session cookie now that one exists (Slice 3).

    This covers every response the routing layer produces, including the 403/404/422 handlers --
    but NOT an unhandled exception. Starlette serves those from ServerErrorMiddleware, which wraps
    the whole middleware stack from the outside, so a 500 response never travels back through
    here. The 500 handler therefore calls _apply_security_headers itself."""

    async def dispatch(self, request: Request, call_next) -> Response:
        return _apply_security_headers(request, await call_next(request))


def create_app(db_path: str, session_secret: str | None = None) -> FastAPI:
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()

    app = FastAPI()
    app.add_middleware(_SecurityHeadersMiddleware)
    app.state.db_path = db_path
    app.state.session_secret = _resolve_session_secret(session_secret)
    app.state.trusted_proxies = parse_trusted_proxies(os.environ.get("TRUSTED_PROXY_IPS"))
    app.state.templates = Jinja2Templates(
        directory=str(_TEMPLATES_DIR),
        context_processors=[_localization_context],
    )
    app.state.templates.env.globals["asset_version"] = _static_asset_version()
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    def _load_request_localizer(request: Request) -> None:
        if hasattr(request.state, "localizer"):
            return
        with _request_db(request) as request_conn:
            request.state.localizer = load_localizer(request_conn)

    def _wants_html(request: Request) -> bool:
        return (
            "text/html" in request.headers.get("accept", "").lower()
            or request.headers.get("HX-Request") == "true"
        )

    def _error_page(
        request: Request,
        *,
        status_code: int,
        title_key: str,
        message_key: str,
    ) -> Response:
        _load_request_localizer(request)
        return app.state.templates.TemplateResponse(
            request,
            "error.html",
            {
                "status_code": status_code,
                "error_title": request.state.localizer.text(title_key),
                "error_message": request.state.localizer.text(message_key),
            },
            status_code=status_code,
        )

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> Response:
        if isinstance(exc, StarletteHTTPException) and exc.detail != "Not Found":
            if _wants_html(request):
                return _error_page(
                    request,
                    status_code=404,
                    title_key="web.error.not_found_title",
                    message_key="web.error.not_found_message",
                )
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
        # Unmatched routes never run get_localizer (no route means no dependencies), so the
        # context processor above would find nothing on request.state -- load it directly here.
        _load_request_localizer(request)
        return app.state.templates.TemplateResponse(
            request,
            "not_found.html",
            status_code=404,
        )

    @app.exception_handler(403)
    async def forbidden(request: Request, exc: Exception) -> Response:
        if not _wants_html(request) and isinstance(exc, StarletteHTTPException):
            return JSONResponse(
                {"detail": exc.detail},
                status_code=403,
                headers=exc.headers,
            )
        return _error_page(
            request,
            status_code=403,
            title_key="web.error.forbidden_title",
            message_key="web.error.forbidden_message",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if not _wants_html(request):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        return _error_page(
            request,
            status_code=422,
            title_key="web.error.invalid_request_title",
            message_key="web.error.invalid_request_message",
        )

    @app.exception_handler(500)
    async def internal_error(request: Request, exc: Exception) -> Response:
        _LOGGER.error(
            "Unhandled web request error",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        # Starlette serves this from ServerErrorMiddleware, outside _SecurityHeadersMiddleware,
        # so the response has to pick up the headers here or it ships with none at all -- no CSP,
        # no nosniff, and no `Cache-Control: private, no-store` on an HTML page that a shared
        # cache would then be free to store.
        if not _wants_html(request):
            return _apply_security_headers(
                request,
                JSONResponse({"detail": "Internal server error"}, status_code=500),
            )
        return _apply_security_headers(
            request,
            _error_page(
                request,
                status_code=500,
                title_key="web.error.internal_title",
                message_key="web.error.internal_message",
            ),
        )

    app.include_router(public.router)
    app.include_router(admin.router)
    app.include_router(research.router)
    return app
