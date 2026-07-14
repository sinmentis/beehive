# src/beehive/auth/tokens.py
"""Session-id generation and HMAC signing (stdlib only, no new dependency). The cookie value is
"<session_id>.<hmac_hex>" — the signature lets the app reject a tampered/forged cookie cheaply,
before ever touching the sessions table, and (via a persisted SESSION_SECRET)
keeps existing sessions valid across container restarts."""
from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_session_id() -> str:
    return secrets.token_urlsafe(32)


def sign_session_id(session_id: str, secret: str) -> str:
    signature = hmac.new(secret.encode(), session_id.encode(), hashlib.sha256).hexdigest()
    return f"{session_id}.{signature}"


def verify_signed_session_id(signed: str, secret: str) -> str | None:
    if "." not in signed:
        return None
    session_id, _, signature = signed.rpartition(".")
    if not session_id:
        return None
    expected = hmac.new(secret.encode(), session_id.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return None
    return session_id
