from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from beehive.db import app_state

DEFAULT_DIGEST_EMAIL_KEY = "default_digest_email"


class EmailConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedRecipient:
    address: str | None
    source: Literal["database", "environment", "channel", "missing"]


def validate_email(value: str) -> str:
    address = value.strip()
    if not address:
        raise EmailConfigurationError("Email address is required")
    if any(character.isspace() for character in address):
        raise EmailConfigurationError("Email address cannot contain whitespace")
    if "," in address or ";" in address:
        raise EmailConfigurationError("Only one email address is supported")
    if address.count("@") != 1:
        raise EmailConfigurationError("Email address must contain one @")

    local, domain = address.split("@", 1)
    if not local or not domain:
        raise EmailConfigurationError("Email address needs a local part and domain")
    if local.startswith(".") or local.endswith(".") or ".." in address:
        raise EmailConfigurationError("Email address contains an invalid dot")
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise EmailConfigurationError("Email domain must contain a valid dot")
    return address


def get_stored_default_email(conn: sqlite3.Connection) -> str | None:
    return app_state.get(conn, DEFAULT_DIGEST_EMAIL_KEY)


def set_stored_default_email(conn: sqlite3.Connection, value: str | None) -> None:
    if value is None:
        app_state.delete(conn, DEFAULT_DIGEST_EMAIL_KEY)
        return
    app_state.set(conn, DEFAULT_DIGEST_EMAIL_KEY, validate_email(value))


def resolve_default_email(conn: sqlite3.Connection,
                          env_fallback: str | None) -> ResolvedRecipient:
    stored = get_stored_default_email(conn)
    if stored:
        return ResolvedRecipient(validate_email(stored), "database")
    if env_fallback:
        return ResolvedRecipient(validate_email(env_fallback), "environment")
    return ResolvedRecipient(None, "missing")


def resolve_channel_email(channel: dict,
                          default: ResolvedRecipient) -> ResolvedRecipient:
    override = channel.get("digest_email")
    if override:
        return ResolvedRecipient(validate_email(override), "channel")
    return default
