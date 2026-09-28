"""The LLM models the Owner can pick from, refreshed from the Copilot account.

The web container has no Copilot token (deploy/README.md), so it cannot ask the SDK which models
exist. It records a refresh request instead. The always-on Research worker holds the token: it
claims the request on its next poll (every few seconds), lists the account's models through
llm_client.list_models() and stores them. It also refreshes once a day on its own, so the list
stays current without anyone asking. Until a refresh has succeeded, BUILTIN_MODELS stands in.

Both halves live in app_state as JSON, so no schema change is involved:

  llm_model_catalog          the last list that was fetched successfully, and when
  llm_model_catalog_refresh  the latest refresh: queued -> running -> done | failed

Every read-modify-write runs in write_transaction(), and a running refresh carries a claim id.
Two workers (ADR-0009 tolerates an accidental second one) therefore never run the same refresh,
and a refresh that was superseded cannot overwrite the newer result.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from beehive.db.connection import write_transaction

CATALOG_KEY = "llm_model_catalog"
REFRESH_KEY = "llm_model_catalog_refresh"

# How long the Research worker waits for the SDK to start and list models (about 7s in practice).
LIST_TIMEOUT_SECONDS = 60.0
# A refresh still marked running after this long belonged to a worker that died mid-refresh.
RUNNING_STALE_AFTER = timedelta(minutes=3)
# The worker polls every few seconds, so a request still queued after this long means it is down.
QUEUED_WAIT_WARNING = timedelta(minutes=1)
# The worker refreshes on its own once the last attempt, successful or not, is this old.
AUTO_REFRESH_INTERVAL = timedelta(hours=24)

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
_NAME_MAX = 80
_ERROR_MAX = 300
# "disabled" and "unconfigured" models need a change on the Copilot account before a session
# can use them. Models without a policy (for example "auto") are usable.
_USABLE_POLICY_STATES = {None, "enabled"}


@dataclass(frozen=True)
class ModelOption:
    model_id: str
    display_name: str


@dataclass(frozen=True)
class ListedModel:
    """One model as the Copilot account lists it. Plain data, so callers never need the SDK."""
    model_id: str
    name: str
    policy_state: str | None = None


# Stands in until the Research worker's first successful refresh. Matches what
# CopilotClient.list_models() returned for the deployed account on 2026-09-29.
BUILTIN_MODELS = (
    ModelOption("auto", "Auto"),
    ModelOption("claude-haiku-4.5", "Claude Haiku 4.5"),
    ModelOption("claude-opus-4.7", "Claude Opus 4.7"),
    ModelOption("claude-opus-4.8", "Claude Opus 4.8"),
    ModelOption("claude-opus-5", "Claude Opus 5"),
    ModelOption("claude-opus-5.5", "Claude Opus 5.5"),
    ModelOption("claude-sonnet-5", "Claude Sonnet 5"),
    ModelOption("claude-sonnet-5.5", "Claude Sonnet 5.5"),
    ModelOption("gpt-5-mini", "GPT-5 mini"),
    ModelOption("gpt-5.3-codex", "GPT-5.3-Codex"),
    ModelOption("gpt-5.4", "GPT-5.4"),
    ModelOption("gpt-5.4-mini", "GPT-5.4 mini"),
    ModelOption("gpt-5.5", "GPT-5.5"),
    ModelOption("gpt-5.6-luna", "GPT-5.6 Luna"),
    ModelOption("gpt-5.6-sol", "GPT-5.6 Sol"),
    ModelOption("gpt-5.6-terra", "GPT-5.6 Terra"),
    ModelOption("gpt-6-astra", "GPT-6 Astra"),
    ModelOption("gpt-6-luna", "GPT-6 Luna"),
    ModelOption("gpt-6-sol", "GPT-6 Sol"),
    ModelOption("grok-4.5", "Grok 4.5"),
    ModelOption("grok-4.6", "Grok 4.6"),
    ModelOption("grok-4.7", "Grok 4.7"),
    ModelOption("mai-code-1.1-flash", "MAI-Code-1.1-Flash"),
)


@dataclass(frozen=True)
class ModelCatalog:
    refreshed_at: datetime
    models: tuple[ModelOption, ...]


class RefreshStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class RefreshError(StrEnum):
    TIMEOUT = "timeout"
    EMPTY = "empty"
    FAILED = "failed"


@dataclass(frozen=True)
class RefreshState:
    status: RefreshStatus
    # When the Owner asked. None for the worker's own daily refresh.
    requested_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    claim: str | None = None
    error: RefreshError | None = None
    error_detail: str | None = None
    model_count: int | None = None
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def is_stale(self, now: datetime) -> bool:
        """Still marked running long after any real refresh would have finished."""
        return self.status is RefreshStatus.RUNNING and (
            self.started_at is None or now - self.started_at > RUNNING_STALE_AFTER)

    def is_pending(self, now: datetime) -> bool:
        return self.status is RefreshStatus.QUEUED or (
            self.status is RefreshStatus.RUNNING and not self.is_stale(now))

    def is_waiting(self, now: datetime) -> bool:
        """Queued for longer than the worker takes to notice, so the worker is probably down."""
        return (self.status is RefreshStatus.QUEUED and self.requested_at is not None
                and now - self.requested_at > QUEUED_WAIT_WARNING)


@dataclass(frozen=True)
class RefreshClaim:
    claim: str
    # True when the Owner asked for this refresh, False for the daily one.
    requested: bool


def normalize_models(listed: Iterable[ListedModel]) -> tuple[ModelOption, ...]:
    """The usable, well-formed models: "Auto" first, then the rest in natural name order."""
    options: dict[str, ModelOption] = {}
    for model in listed:
        if model.policy_state not in _USABLE_POLICY_STATES:
            continue
        if not _MODEL_ID.fullmatch(model.model_id) or model.model_id in options:
            continue
        name = " ".join("".join(ch if ch.isprintable() else " " for ch in model.name).split())
        options[model.model_id] = ModelOption(model.model_id, name[:_NAME_MAX] or model.model_id)
    return tuple(sorted(options.values(), key=_sort_key))


def _sort_key(option: ModelOption) -> tuple[bool, list[int | str]]:
    # re.split with a capturing group alternates text and digits, so equal positions compare
    # like with like: "GPT-5.4" sorts before "GPT-5.10".
    parts = re.split(r"(\d+)", option.display_name.casefold())
    return (option.model_id != "auto",
            [int(part) if index % 2 else part for index, part in enumerate(parts)])


def load_catalog(conn: sqlite3.Connection) -> ModelCatalog | None:
    """The last successfully fetched list, or None before the first refresh."""
    raw = _read(conn, CATALOG_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        refreshed_at = _parse_time(data["refreshed_at"])
        models = normalize_models(
            ListedModel(str(item["id"]), str(item["name"])) for item in data["models"])
    except (ValueError, KeyError, TypeError):
        print("[model-catalog] the stored model list is unreadable; using the built-in list")
        return None
    if refreshed_at is None or not models:
        return None
    return ModelCatalog(refreshed_at, models)


def available_models(conn: sqlite3.Connection) -> tuple[ModelOption, ...]:
    catalog = load_catalog(conn)
    return catalog.models if catalog is not None else BUILTIN_MODELS


def load_refresh_state(conn: sqlite3.Connection) -> RefreshState | None:
    return _parse_state(_read(conn, REFRESH_KEY))


def request_refresh(conn: sqlite3.Connection, now: datetime) -> RefreshState:
    """Queue a refresh for the Research worker, unless one is already queued or running."""
    with write_transaction(conn):
        current = _parse_state(_read(conn, REFRESH_KEY))
        if current is not None and current.is_pending(now):
            return current
        state = RefreshState(RefreshStatus.QUEUED, requested_at=now)
        _write_state(conn, state)
    return state


def claim_refresh(
    conn: sqlite3.Connection, now: datetime, *,
    auto_interval: timedelta = AUTO_REFRESH_INTERVAL,
) -> RefreshClaim | None:
    """Claim the refresh that is due now, if any: one the Owner queued, one a dead worker left
    running, or the daily one. The worker calls this every poll, so the common "nothing due"
    answer is a plain read that never takes the write lock."""
    if not _refresh_due(_parse_state(_read(conn, REFRESH_KEY)), now, auto_interval):
        return None
    with write_transaction(conn):
        current = _parse_state(_read(conn, REFRESH_KEY))
        if not _refresh_due(current, now, auto_interval):
            return None
        requested_at = (
            current.requested_at
            if current is not None
            and current.status in (RefreshStatus.QUEUED, RefreshStatus.RUNNING)
            else None)
        claim = RefreshClaim(uuid.uuid4().hex, requested=requested_at is not None)
        _write_state(conn, RefreshState(
            RefreshStatus.RUNNING, requested_at=requested_at, started_at=now, claim=claim.claim))
    return claim


def complete_refresh(
    conn: sqlite3.Connection, claim: RefreshClaim, now: datetime, listed: Iterable[ListedModel],
) -> RefreshState | None:
    """Store the listed models as the new list. Returns None when this claim was superseded."""
    models = normalize_models(listed)
    if not models:
        return fail_refresh(
            conn, claim, now, RefreshError.EMPTY, "Copilot listed no usable models")
    with write_transaction(conn):
        current = _parse_state(_read(conn, REFRESH_KEY))
        if not _holds(current, claim):
            return None
        previous = {model.model_id for model in available_models(conn)}
        model_ids = [model.model_id for model in models]
        state = replace(
            current, status=RefreshStatus.DONE, finished_at=now, claim=None,
            error=None, error_detail=None, model_count=len(models),
            added=tuple(model_id for model_id in model_ids if model_id not in previous),
            removed=tuple(sorted(previous.difference(model_ids))))
        _write(conn, CATALOG_KEY, json.dumps(
            {"refreshed_at": now.isoformat(),
             "models": [{"id": model.model_id, "name": model.display_name} for model in models]},
            ensure_ascii=False, separators=(",", ":")))
        _write_state(conn, state)
    return state


def fail_refresh(
    conn: sqlite3.Connection, claim: RefreshClaim, now: datetime,
    error: RefreshError, detail: str,
) -> RefreshState | None:
    """Record why the refresh failed. The previous list stays in use."""
    with write_transaction(conn):
        current = _parse_state(_read(conn, REFRESH_KEY))
        if not _holds(current, claim):
            return None
        state = replace(
            current, status=RefreshStatus.FAILED, finished_at=now, claim=None, error=error,
            error_detail=" ".join(detail.split())[:_ERROR_MAX] or None,
            model_count=None, added=(), removed=())
        _write_state(conn, state)
    return state


def requeue_refresh(conn: sqlite3.Connection, claim: RefreshClaim, now: datetime) -> None:
    """Hand a claimed refresh back, for example when the worker shuts down mid-refresh, so the
    next poll runs it again."""
    with write_transaction(conn):
        current = _parse_state(_read(conn, REFRESH_KEY))
        if _holds(current, claim):
            _write_state(conn, RefreshState(
                RefreshStatus.QUEUED, requested_at=current.requested_at or now))


def _refresh_due(state: RefreshState | None, now: datetime, auto_interval: timedelta) -> bool:
    if state is None or state.status is RefreshStatus.QUEUED:
        return True
    if state.status is RefreshStatus.RUNNING:
        return state.is_stale(now)
    last_attempt = state.finished_at or state.started_at or state.requested_at
    return last_attempt is None or now - last_attempt >= auto_interval


def _holds(state: RefreshState | None, claim: RefreshClaim) -> bool:
    return (state is not None and state.status is RefreshStatus.RUNNING
            and state.claim == claim.claim)


def _read(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _write(conn: sqlite3.Connection, key: str, value: str) -> None:
    # No commit here: every caller is inside write_transaction(), which commits once.
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value))


def _write_state(conn: sqlite3.Connection, state: RefreshState) -> None:
    _write(conn, REFRESH_KEY, json.dumps({
        "status": state.status.value,
        "requested_at": _format_time(state.requested_at),
        "started_at": _format_time(state.started_at),
        "finished_at": _format_time(state.finished_at),
        "claim": state.claim,
        "error": state.error.value if state.error else None,
        "error_detail": state.error_detail,
        "model_count": state.model_count,
        "added": list(state.added),
        "removed": list(state.removed),
    }, ensure_ascii=False, separators=(",", ":")))


def _parse_state(raw: str | None) -> RefreshState | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return RefreshState(
            status=RefreshStatus(data["status"]),
            requested_at=_parse_time(data.get("requested_at")),
            started_at=_parse_time(data.get("started_at")),
            finished_at=_parse_time(data.get("finished_at")),
            claim=data.get("claim"),
            error=RefreshError(data["error"]) if data.get("error") else None,
            error_detail=data.get("error_detail"),
            model_count=data.get("model_count"),
            added=tuple(data.get("added") or ()),
            removed=tuple(data.get("removed") or ()),
        )
    except (ValueError, KeyError, TypeError):
        return None


def _format_time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("model catalog times are stored with their UTC offset")
    return parsed
