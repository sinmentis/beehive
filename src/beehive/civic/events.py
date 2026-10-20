"""Secret-safe, line-oriented operational events for the Civic producer."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TextIO

EVENT_SCHEMA_VERSION = "partymap.civic.event.v1"
MAX_DETAIL_CHARS = 240

_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.:/@+-]{0,159}$")
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_CORRELATION_ID = re.compile(
    r"github_pat_|gh[pousr]_|bearer|cloudflare|private[_-]?key|"
    r"(?:^|[_-])(?:token|secret|credential|password)(?:[_-]|$)",
    re.I,
)
_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        re.I | re.S,
    ),
    re.compile(r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+)\b", re.I),
    re.compile(r"\bBearer\s+[^\s,;]+", re.I),
    re.compile(
        r"\b(?:CF|CLOUDFLARE)[_-]?(?:API[_-]?)?(?:TOKEN|KEY)"
        r"(?:\s*[:=]\s*|\s+)?[^\s,;]*",
        re.I,
    ),
    re.compile(
        r"\b(?:authorization|cookie|set-cookie|x-api-key|api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|password|passwd|secret|credential)"
        r"\s*[:=]\s*[^\s,;]+",
        re.I,
    ),
    re.compile(
        r"\b(?:article|evidence|excerpt|prompt|model[_ -]?response|statement|summary|title)"
        r"\s*[:=]\s*[^\n,;]+",
        re.I,
    ),
    re.compile(r"\b(?:https?|ftp)://[^\s]+", re.I),
)
_SAFE_FIELDS = frozenset(
    {
        "ai_calls",
        "attempt",
        "attempt_count",
        "batch_count",
        "batch_id",
        "candidate_count",
        "candidate_id",
        "completed_count",
        "content_hash",
        "count",
        "created_count",
        "deadline_at",
        "deferred_count",
        "detail",
        "document_count",
        "elapsed_ms",
        "evidence_count",
        "error_class",
        "error_code",
        "failed_count",
        "held_count",
        "file_name",
        "idempotency_key",
        "lease_expires_at",
        "model",
        "outcome_count",
        "pages_fetched",
        "payload_hash",
        "phase",
        "publication_date_hydrated",
        "reason",
        "released_count",
        "replayed",
        "replayed_count",
        "run_count",
        "run_id",
        "scheduled_count",
        "skipped_count",
        "source_count",
        "source_failed_count",
        "source_key",
        "status",
        "succeeded_count",
        "title_hydrated",
        "unprojected_count",
        "verification_count",
        "watch_id",
    }
)
_HASH_FIELDS = frozenset({"content_hash", "payload_hash"})
_INTEGER_FIELDS = frozenset(
    {
        "ai_calls",
        "attempt",
        "attempt_count",
        "batch_count",
        "batch_id",
        "candidate_count",
        "candidate_id",
        "completed_count",
        "count",
        "created_count",
        "deferred_count",
        "document_count",
        "elapsed_ms",
        "evidence_count",
        "failed_count",
        "held_count",
        "outcome_count",
        "pages_fetched",
        "released_count",
        "replayed_count",
        "run_count",
        "run_id",
        "scheduled_count",
        "skipped_count",
        "source_count",
        "source_failed_count",
        "succeeded_count",
        "unprojected_count",
        "verification_count",
        "watch_id",
    }
)


def sanitize_civic_detail(value: object, *, limit: int = MAX_DETAIL_CHARS) -> str:
    """Return bounded single-line text with credentials and URLs removed."""
    limit = max(4, int(limit))
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        return f"{text[: limit - 3]}..."
    return text


def _utc_stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _safe_correlation_id(value: object | None) -> str:
    if value is None:
        return uuid.uuid4().hex
    text = str(value)
    if _CORRELATION_ID.fullmatch(text) and not _SENSITIVE_CORRELATION_ID.search(text):
        return text
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_string(field: str, value: object) -> str:
    if isinstance(value, Enum):
        value = value.value
    text = sanitize_civic_detail(value)
    if field == "file_name":
        text = Path(text).name
    if field in _HASH_FIELDS:
        return text if _HASH.fullmatch(text) else "[invalid]"
    if field != "detail" and not _IDENTIFIER.fullmatch(text):
        return "[redacted]"
    return text


class CivicEventLogger:
    """Write one canonical NDJSON object per event and flush it before returning."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        correlation_id: str | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._now_fn = now_fn
        self._correlation_id = _safe_correlation_id(correlation_id)
        self._lock = threading.Lock()
        self._available = True

    def emit(self, event: str, **fields: object) -> None:
        if not _EVENT_NAME.fullmatch(event):
            raise ValueError(f"invalid Civic event name: {event!r}")
        record: dict[str, object] = {
            "correlation_id": self._correlation_id,
            "event": event,
            "schema_version": EVENT_SCHEMA_VERSION,
            "timestamp": _utc_stamp(self._now_fn()),
        }
        for field in sorted(fields):
            if field not in _SAFE_FIELDS:
                continue
            value = fields[field]
            if value is None:
                continue
            if field in _INTEGER_FIELDS:
                if isinstance(value, bool):
                    continue
                try:
                    record[field] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
            elif isinstance(value, bool):
                record[field] = value
            elif isinstance(value, datetime):
                record[field] = _utc_stamp(value)
            else:
                record[field] = _safe_string(field, value)
        line = json.dumps(
            record,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._lock:
            if not self._available:
                return
            try:
                self._stream.write(f"{line}\n")
                self._stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._available = False


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "MAX_DETAIL_CHARS",
    "CivicEventLogger",
    "sanitize_civic_detail",
]
