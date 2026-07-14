from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from beehive.db.connection import connect, init_schema
from beehive.web import admin, public

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "script-src 'self'; frame-ancestors 'none'")


def _static_asset_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(_STATIC_DIR.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """App-wide, not admin-only: a same-origin XSS on the *public* Dashboard/drill-down could
    otherwise pivot into stealing the *admin* session cookie now that one exists (Slice 3)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response


def create_app(db_path: str, session_secret: str | None = None) -> FastAPI:
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()

    app = FastAPI()
    app.add_middleware(_SecurityHeadersMiddleware)
    app.state.db_path = db_path
    app.state.session_secret = (session_secret if session_secret is not None
                                 else os.environ.get("SESSION_SECRET", ""))
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates.env.globals["asset_version"] = _static_asset_version()
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> Response:
        if isinstance(exc, StarletteHTTPException) and exc.detail != "Not Found":
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
        return app.state.templates.TemplateResponse(
            request,
            "not_found.html",
            status_code=404,
        )

    app.include_router(public.router)
    app.include_router(admin.router)
    return app
