#!/usr/bin/env python
"""Report safe PartyMap Civic state from SQLite and the filesystem spool."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from jsonschema import Draft202012Validator, FormatChecker

from beehive.civic.events import sanitize_civic_detail
from beehive.civic.source_outages import OUTAGE_ESCALATION_AFTER
from beehive.db.civic_source_outages import OUTAGE_OBSERVATION_GAP

REPORT_SCHEMA_VERSION = "partymap.civic.diagnostic.v1"
MAX_ROWS = 500
MAX_SPOOL_FILE_BYTES = 8 * 1024 * 1024
MAX_PROVIDER_REFERENCE_CHARS = 160

_ROOT = Path(__file__).parents[1]
_SCHEMA_ROOT = _ROOT / "src" / "beehive" / "civic" / "schemas"
_PUBLICATION_VALIDATOR = Draft202012Validator(
    json.loads(
        (_SCHEMA_ROOT / "civic-publication-v1.schema.json").read_text(encoding="utf-8")
    ),
    format_checker=FormatChecker(),
)
_OUTCOME_VALIDATOR = Draft202012Validator(
    json.loads(
        (_SCHEMA_ROOT / "civic-publication-outcome-v1.schema.json").read_text(
            encoding="utf-8"
        )
    ),
    format_checker=FormatChecker(),
)
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.:/@+-]{0,159}$")
_SAFE_HASH = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_TABLES = frozenset(
    {
        "civic_ai_stages",
        "civic_batches",
        "civic_candidates",
        "civic_delivery_receipts",
        "civic_notifications",
        "civic_qualifications",
        "civic_runs",
        "civic_vocabulary_terms",
        "civic_vocabulary_versions",
        "civic_watches",
    }
)


class CivicDiagnosticError(RuntimeError):
    pass


def _parse_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_stamp(value: datetime | str | None) -> str | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_code(value: object | None) -> str | None:
    if value is None:
        return None
    text = sanitize_civic_detail(value, limit=160)
    return text if _SAFE_CODE.fullmatch(text) else "[redacted]"


def _safe_hash(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if _SAFE_HASH.fullmatch(text) else "[invalid]"


def _safe_schema_version(value: object | None) -> int | str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _safe_code(value)


def _safe_file_name(path: Path) -> str:
    return sanitize_civic_detail(path.name, limit=160)


def _safe_provider_reference(value: object | None) -> str | None:
    if value is None or not str(value):
        return None
    return sanitize_civic_detail(value, limit=MAX_PROVIDER_REFERENCE_CHARS)


def _database_label(path: str) -> str:
    name = Path(path).name
    safe = _safe_code(name)
    return safe if safe not in {None, "[redacted]"} else "sqlite-database"


def _connect_read_only(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve(strict=True)
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        conn.close()
        raise CivicDiagnosticError("SQLite query-only mode could not be enabled")
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    }
    missing = sorted(_REQUIRED_TABLES - tables)
    if missing:
        conn.close()
        raise CivicDiagnosticError(
            f"Civic schema is incomplete ({len(missing)} required tables missing)"
        )
    return conn


def _watch_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT w.id, w.subject, w.status, w.schedule_timezone, w.schedule_time, "
        "w.created_at, w.paused_at, w.retired_at, v.id AS vocabulary_id, "
        "v.version AS vocabulary_version, v.status AS vocabulary_status, "
        "v.content_hash AS vocabulary_hash, v.created_at AS vocabulary_created_at, "
        "v.published_at, v.superseded_at, COUNT(t.id) AS term_count "
        "FROM civic_watches w "
        "LEFT JOIN civic_vocabulary_versions v ON v.watch_id = w.id "
        "LEFT JOIN civic_vocabulary_terms t ON t.vocabulary_version_id = v.id "
        "GROUP BY w.id, v.id ORDER BY w.id, v.version"
    ).fetchall()
    watches: dict[int, dict] = {}
    for row in rows:
        watch = watches.setdefault(
            row["id"],
            {
                "watch_id": row["id"],
                "subject": _safe_code(row["subject"]),
                "status": _safe_code(row["status"]),
                "schedule": {
                    "timezone": _safe_code(row["schedule_timezone"]),
                    "time": _safe_code(row["schedule_time"]),
                },
                "created_at": _utc_stamp(row["created_at"]),
                "paused_at": _utc_stamp(row["paused_at"]),
                "retired_at": _utc_stamp(row["retired_at"]),
                "vocabularies": [],
            },
        )
        if row["vocabulary_id"] is not None:
            watch["vocabularies"].append(
                {
                    "vocabulary_id": row["vocabulary_id"],
                    "version": row["vocabulary_version"],
                    "status": _safe_code(row["vocabulary_status"]),
                    "content_hash": _safe_hash(row["vocabulary_hash"]),
                    "term_count": row["term_count"],
                    "created_at": _utc_stamp(row["vocabulary_created_at"]),
                    "published_at": _utc_stamp(row["published_at"]),
                    "superseded_at": _utc_stamp(row["superseded_at"]),
                }
            )
    return list(watches.values())


def _recent_runs(
    conn: sqlite3.Connection,
    cutoff: datetime,
) -> list[dict]:
    rows = conn.execute(
        "SELECT id, watch_id, vocabulary_version_id, slot_at, run_mode, status, phase, "
        "attempt_count, cancel_requested, requested_at, started_at, lease_expires_at, "
        "deadline_at, completed_at, error_code "
        "FROM civic_runs "
        "WHERE status IN ('pending', 'processing') "
        "OR julianday(COALESCE(completed_at, started_at, requested_at)) >= julianday(?) "
        "ORDER BY CASE WHEN status IN ('pending', 'processing') THEN 0 ELSE 1 END, "
        "id DESC LIMIT ?",
        (cutoff.isoformat(), MAX_ROWS),
    ).fetchall()
    return [
        {
            "run_id": row["id"],
            "watch_id": row["watch_id"],
            "vocabulary_version_id": row["vocabulary_version_id"],
            "slot_at": _utc_stamp(row["slot_at"]),
            "run_mode": _safe_code(row["run_mode"]),
            "status": _safe_code(row["status"]),
            "phase": _safe_code(row["phase"]),
            "attempt_count": row["attempt_count"],
            "cancel_requested": bool(row["cancel_requested"]),
            "requested_at": _utc_stamp(row["requested_at"]),
            "started_at": _utc_stamp(row["started_at"]),
            "lease_expires_at": _utc_stamp(row["lease_expires_at"]),
            "deadline_at": _utc_stamp(row["deadline_at"]),
            "completed_at": _utc_stamp(row["completed_at"]),
            "error_code": _safe_code(row["error_code"]),
        }
        for row in rows
    ]


def _recent_batches(conn: sqlite3.Connection, cutoff: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT id, run_id, watch_id, status, idempotency_key, content_hash, "
        "payload_hash, candidate_count, attempt_count, max_attempts, created_at, "
        "sealed_at, lease_expires_at, settled_at, next_attempt_at, error_code "
        "FROM civic_batches WHERE status IN ('building', 'sealed', 'delivering') "
        "OR julianday(created_at) >= julianday(?) "
        "ORDER BY CASE WHEN status IN ('building', 'sealed', 'delivering') "
        "THEN 0 ELSE 1 END, id DESC LIMIT ?",
        (cutoff.isoformat(), MAX_ROWS),
    ).fetchall()
    return [
        {
            "batch_id": row["id"],
            "run_id": row["run_id"],
            "watch_id": row["watch_id"],
            "status": _safe_code(row["status"]),
            "idempotency_key": _safe_code(row["idempotency_key"]),
            "content_hash": _safe_hash(row["content_hash"]),
            "payload_hash": _safe_hash(row["payload_hash"]),
            "candidate_count": row["candidate_count"],
            "attempt_count": row["attempt_count"],
            "max_attempts": row["max_attempts"],
            "created_at": _utc_stamp(row["created_at"]),
            "sealed_at": _utc_stamp(row["sealed_at"]),
            "lease_expires_at": _utc_stamp(row["lease_expires_at"]),
            "settled_at": _utc_stamp(row["settled_at"]),
            "next_attempt_at": _utc_stamp(row["next_attempt_at"]),
            "error_code": _safe_code(row["error_code"]),
        }
        for row in rows
    ]


def _qualification_counts(conn: sqlite3.Connection, cutoff: datetime) -> dict:
    rows = conn.execute(
        "SELECT q.outcome, q.reasons, COUNT(*) AS count "
        "FROM civic_qualifications q "
        "JOIN civic_runs r ON r.id = q.run_id "
        "WHERE r.status IN ('pending', 'processing') "
        "OR julianday(COALESCE(r.completed_at, r.started_at, r.requested_at)) "
        ">= julianday(?) GROUP BY q.outcome, q.reasons",
        (cutoff.isoformat(),),
    ).fetchall()
    outcomes = Counter()
    reasons = Counter()
    for row in rows:
        outcomes[_safe_code(row["outcome"])] += row["count"]
        for reason in str(row["reasons"]).split(","):
            safe = _safe_code(reason)
            if safe:
                reasons[safe] += row["count"]
    return {
        "by_outcome": [
            {"outcome": outcome, "count": count}
            for outcome, count in sorted(outcomes.items())
        ],
        "by_reason": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reasons.items())
        ],
    }


def _candidate_counts(conn: sqlite3.Connection, cutoff: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT c.status, c.rejection_code, COUNT(*) AS count "
        "FROM civic_candidates c JOIN civic_runs r ON r.id = c.run_id "
        "WHERE r.status IN ('pending', 'processing') "
        "OR julianday(COALESCE(r.completed_at, r.started_at, r.requested_at)) "
        ">= julianday(?) "
        "GROUP BY c.status, c.rejection_code ORDER BY c.status, c.rejection_code",
        (cutoff.isoformat(),),
    ).fetchall()
    return [
        {
            "status": _safe_code(row["status"]),
            "rejection_code": _safe_code(row["rejection_code"]),
            "count": row["count"],
        }
        for row in rows
    ]


def _ai_counts(conn: sqlite3.Connection, cutoff: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT a.stage, a.status, a.model, a.attempt, a.error_code, COUNT(*) AS count "
        "FROM civic_ai_stages a JOIN civic_runs r ON r.id = a.run_id "
        "WHERE r.status IN ('pending', 'processing') "
        "OR julianday(COALESCE(r.completed_at, r.started_at, r.requested_at)) "
        ">= julianday(?) "
        "GROUP BY a.stage, a.status, a.model, a.attempt, a.error_code "
        "ORDER BY a.stage, a.status, a.model, a.attempt, a.error_code",
        (cutoff.isoformat(),),
    ).fetchall()
    return [
        {
            "stage": _safe_code(row["stage"]),
            "status": _safe_code(row["status"]),
            "model": _safe_code(row["model"]),
            "attempt": row["attempt"],
            "error_code": _safe_code(row["error_code"]),
            "count": row["count"],
        }
        for row in rows
    ]


def _notification_counts(conn: sqlite3.Connection, cutoff: datetime) -> dict:
    raw_rows = conn.execute(
        "SELECT kind, status, error_code, COUNT(*) AS count "
        "FROM civic_notifications WHERE status IN ('pending', 'sending') "
        "OR julianday(queued_at) >= julianday(?) "
        "GROUP BY kind, status, error_code ORDER BY kind, status, error_code",
        (cutoff.isoformat(),),
    ).fetchall()
    rows = [
        {
            "kind": _safe_code(row["kind"]),
            "status": _safe_code(row["status"]),
            "error_code": _safe_code(row["error_code"]),
            "count": row["count"],
        }
        for row in raw_rows
    ]
    by_status = Counter()
    source_by_status = Counter()
    for row in rows:
        by_status[row["status"]] += row["count"]
        if str(row["kind"]).startswith("civic_source"):
            source_by_status[row["status"]] += row["count"]
    return {
        "by_status": [
            {"status": status, "count": count}
            for status, count in sorted(by_status.items())
        ],
        "by_kind_status_error": rows,
        "source_by_status": [
            {"status": status, "count": count}
            for status, count in sorted(source_by_status.items())
        ],
    }


def _receipts(conn: sqlite3.Connection, cutoff: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT id, batch_id, idempotency_key, content_hash, payload_hash, channel, "
        "target, provider_reference, delivered_at FROM civic_delivery_receipts "
        "WHERE julianday(delivered_at) >= julianday(?) ORDER BY id DESC LIMIT ?",
        (cutoff.isoformat(), MAX_ROWS),
    ).fetchall()
    return [
        {
            "receipt_id": row["id"],
            "batch_id": row["batch_id"],
            "idempotency_key": _safe_code(row["idempotency_key"]),
            "content_hash": _safe_hash(row["content_hash"]),
            "payload_hash": _safe_hash(row["payload_hash"]),
            "channel": _safe_code(row["channel"]),
            "provider_reference": _safe_provider_reference(row["provider_reference"]),
            "delivered_at": _utc_stamp(row["delivered_at"]),
        }
        for row in rows
    ]


def _read_regular_file(path: Path) -> tuple[bytes | None, os.stat_result]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        return None, metadata
    if metadata.st_size > MAX_SPOOL_FILE_BYTES:
        return None, metadata
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return None, metadata
        chunks: list[bytes] = []
        remaining = MAX_SPOOL_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        return (data if len(data) <= MAX_SPOOL_FILE_BYTES else None), metadata
    finally:
        os.close(descriptor)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _publication_hash(payload: dict) -> str | None:
    batch = payload.get("batch")
    if not isinstance(batch, dict):
        return None
    projected = dict(payload)
    projected_batch = dict(batch)
    projected_batch.pop("payload_hash", None)
    projected["batch"] = projected_batch
    return hashlib.sha256(_canonical_json_bytes(projected)).hexdigest()


def _inspect_spool_file(
    conn: sqlite3.Connection,
    path: Path,
    location: str,
) -> tuple[dict, list[dict]]:
    anomalies: list[dict] = []
    record = {
        "location": location,
        "file_name": _safe_file_name(path),
        "size": None,
        "mtime": None,
        "kind": "outcome" if location == "outcomes" else "publication",
        "schema_version": None,
        "schema_valid": False,
        "canonical_json": False,
        "hash_valid": False,
        "correlation_valid": False,
        "batch_id": None,
        "idempotency_key": None,
        "payload_hash": None,
        "outcome": None,
        "item_count": None,
        "error_code": None,
    }
    try:
        data, metadata = _read_regular_file(path)
        record["size"] = metadata.st_size
        record["mtime"] = _utc_stamp(
            datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc)
        )
        if data is None:
            record["error_code"] = (
                "spool_file_too_large"
                if stat.S_ISREG(metadata.st_mode)
                else "spool_file_not_regular"
            )
            anomalies.append(
                {
                    "code": record["error_code"],
                    "file_name": record["file_name"],
                }
            )
            return record, anomalies
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("spool document must be an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        record["error_code"] = "spool_file_unreadable"
        anomalies.append(
            {
                "code": record["error_code"],
                "file_name": record["file_name"],
            }
        )
        return record, anomalies

    record["schema_version"] = _safe_schema_version(payload.get("schema_version"))
    record["canonical_json"] = _canonical_json_bytes(payload) == data
    if location == "outcomes":
        record["schema_valid"] = not any(_OUTCOME_VALIDATOR.iter_errors(payload))
        key = payload.get("idempotency_key")
        declared_hash = payload.get("payload_hash")
        record["idempotency_key"] = _safe_code(key)
        record["payload_hash"] = _safe_hash(declared_hash)
        record["outcome"] = _safe_code(payload.get("outcome"))
        record["hash_valid"] = _SAFE_HASH.fullmatch(str(declared_hash)) is not None
    else:
        record["schema_valid"] = not any(_PUBLICATION_VALIDATOR.iter_errors(payload))
        batch_payload = payload.get("batch")
        if not isinstance(batch_payload, dict):
            record["error_code"] = "invalid_publication_shape"
            anomalies.append(
                {
                    "code": "spool_schema_invalid",
                    "file_name": record["file_name"],
                }
            )
            return record, anomalies
        key = batch_payload.get("idempotency_key")
        declared_hash = batch_payload.get("payload_hash")
        record["idempotency_key"] = _safe_code(key)
        record["payload_hash"] = _safe_hash(declared_hash)
        items = payload.get("items")
        record["item_count"] = len(items) if isinstance(items, list) else None
        computed_hash = _publication_hash(payload)
        record["hash_valid"] = computed_hash == declared_hash

    batch = None
    if isinstance(key, str):
        batch = conn.execute(
            "SELECT id, status, content_hash, payload_hash FROM civic_batches "
            "WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
    record["batch_id"] = batch["id"] if batch is not None else None
    if batch is not None:
        record["correlation_valid"] = (
            record["hash_valid"]
            and declared_hash == batch["payload_hash"]
            and (
                location == "outcomes"
                or (
                    payload["batch"].get("batch_content_hash") == batch["content_hash"]
                    and path.name == f"{key}.json"
                )
            )
        )

    if batch is None and location == "outcomes":
        anomalies.append(
            {
                "code": "orphan_outcome",
                "file_name": record["file_name"],
            }
        )
    elif not record["hash_valid"]:
        anomalies.append(
            {
                "code": (
                    "outcome_payload_hash_invalid"
                    if location == "outcomes"
                    else "spool_payload_hash_mismatch"
                ),
                "batch_id": record["batch_id"],
                "file_name": record["file_name"],
            }
        )
    elif not record["correlation_valid"]:
        anomalies.append(
            {
                "code": (
                    "outcome_payload_hash_mismatch"
                    if location == "outcomes"
                    else "spool_batch_correlation_mismatch"
                ),
                "batch_id": record["batch_id"],
                "file_name": record["file_name"],
            }
        )
    if (
        batch is not None
        and location == "outcomes"
        and payload.get("outcome") == "rejected"
        and batch["status"] == "delivered"
    ):
        anomalies.append(
            {
                "code": "outcome_status_mismatch",
                "batch_id": record["batch_id"],
                "file_name": record["file_name"],
            }
        )
    if not record["schema_valid"]:
        anomalies.append(
            {
                "code": "spool_schema_invalid",
                "batch_id": record["batch_id"],
                "file_name": record["file_name"],
            }
        )
    if location != "outcomes" and not record["canonical_json"]:
        anomalies.append(
            {
                "code": "spool_not_canonical",
                "batch_id": record["batch_id"],
                "file_name": record["file_name"],
            }
        )
    return record, anomalies


def _spool_report(
    conn: sqlite3.Connection,
    spool_root: str,
) -> tuple[dict, list[dict], dict[str, set[str] | None]]:
    root = Path(spool_root).expanduser()
    locations = {
        "inbox": root / "inbox",
        "outcomes": root / "outcomes",
        "processed_accepted": root / "processed" / "accepted",
        "processed_rejected": root / "processed" / "rejected",
    }
    report = {"label": "civic-spool", "directories": {}}
    anomalies: list[dict] = []
    names: dict[str, set[str] | None] = {}
    for location, directory in locations.items():
        files = []
        try:
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except FileNotFoundError:
            names[location] = set()
            report["directories"][location] = {"exists": False, "files": []}
            continue
        except OSError:
            names[location] = None
            report["directories"][location] = {
                "exists": None,
                "files": [],
                "error_code": "spool_directory_unreadable",
            }
            anomalies.append(
                {
                    "code": "spool_directory_unreadable",
                    "location": location,
                }
            )
            continue
        names[location] = {path.name for path in entries}
        for path in entries[:MAX_ROWS]:
            record, file_anomalies = _inspect_spool_file(conn, path, location)
            files.append(record)
            anomalies.extend(file_anomalies)
        report["directories"][location] = {
            "exists": True,
            "files": files,
            "truncated": len(entries) > MAX_ROWS,
        }
    return report, anomalies, names


def _current_content_hash(conn: sqlite3.Connection, batch_id: int) -> tuple[str, int]:
    rows = conn.execute(
        "SELECT dedupe_key, title, statement FROM civic_candidates "
        "WHERE batch_id = ? AND sealed_into_batch_at IS NOT NULL ORDER BY id",
        (batch_id,),
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            f"{row['dedupe_key']}\x1f{row['title']}\x1f{row['statement']}\x1e".encode()
        )
    return digest.hexdigest(), len(rows)


def _database_anomalies(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    cutoff: datetime,
    spool_names: dict[str, set[str] | None],
) -> list[dict]:
    anomalies: list[dict] = []
    for row in conn.execute(
        "SELECT id, status, lease_expires_at FROM civic_runs "
        "WHERE status = 'processing' ORDER BY id",
    ):
        lease_expires_at = _parse_datetime(row["lease_expires_at"])
        if lease_expires_at is None:
            anomalies.append(
                {
                    "code": "invalid_run_lease_timestamp",
                    "run_id": row["id"],
                }
            )
        elif lease_expires_at <= now:
            anomalies.append(
                {
                    "code": "expired_run_claim",
                    "run_id": row["id"],
                    "status": _safe_code(row["status"]),
                    "lease_expires_at": _utc_stamp(lease_expires_at),
                }
            )
    for table, identifier, status in (
        ("civic_batches", "batch_id", "delivering"),
        ("civic_notifications", "notification_id", "sending"),
    ):
        rows = conn.execute(
            f"SELECT id, status, lease_expires_at FROM {table} "
            "WHERE status = ? ORDER BY id",
            (status,),
        )
        for row in rows:
            lease_expires_at = _parse_datetime(row["lease_expires_at"])
            if lease_expires_at is None:
                anomalies.append(
                    {
                        "code": f"invalid_{status}_lease_timestamp",
                        identifier: row["id"],
                    }
                )
            elif lease_expires_at <= now:
                anomalies.append(
                    {
                        "code": f"expired_{status}_claim",
                        identifier: row["id"],
                        "status": _safe_code(row["status"]),
                        "lease_expires_at": _utc_stamp(lease_expires_at),
                    }
                )

    batch_rows = conn.execute(
        "SELECT id, status, idempotency_key, content_hash, payload_hash, candidate_count, "
        "created_at, sealed_at FROM civic_batches ORDER BY id"
    ).fetchall()
    for row in batch_rows:
        if row["status"] in {"building", "sealed", "delivering"}:
            activity_at = _parse_datetime(row["sealed_at"] or row["created_at"])
            if activity_at is None:
                anomalies.append(
                    {
                        "code": "invalid_batch_timestamp",
                        "batch_id": row["id"],
                        "status": _safe_code(row["status"]),
                    }
                )
            elif activity_at < cutoff:
                anomalies.append(
                    {
                        "code": "stuck_batch",
                        "batch_id": row["id"],
                        "status": _safe_code(row["status"]),
                    }
                )
        if row["status"] != "building":
            actual_hash, actual_count = _current_content_hash(conn, row["id"])
            if actual_hash != row["content_hash"]:
                anomalies.append(
                    {
                        "code": "batch_content_hash_mismatch",
                        "batch_id": row["id"],
                    }
                )
            if actual_count != row["candidate_count"]:
                anomalies.append(
                    {
                        "code": "batch_candidate_count_mismatch",
                        "batch_id": row["id"],
                    }
                )
        if row["payload_hash"] is None:
            continue
        filename = f"{row['idempotency_key']}.json"
        inbox_names = spool_names.get("inbox")
        if (
            row["status"] in {"sealed", "delivering"}
            and inbox_names is not None
            and filename not in inbox_names
        ):
            anomalies.append(
                {
                    "code": "missing_inbox_file",
                    "batch_id": row["id"],
                }
            )
        expected_processed = {
            "delivered": "processed_accepted",
            "quarantined": "processed_rejected",
        }.get(row["status"])
        processed_names = (
            spool_names.get(expected_processed)
            if expected_processed is not None
            else None
        )
        if (
            expected_processed is not None
            and processed_names is not None
            and filename not in processed_names
        ):
            anomalies.append(
                {
                    "code": "missing_processed_file",
                    "batch_id": row["id"],
                    "status": _safe_code(row["status"]),
                }
            )

    receipt_rows = conn.execute(
        "SELECT r.id, r.batch_id, r.idempotency_key, r.content_hash, r.payload_hash, "
        "b.idempotency_key AS batch_key, b.content_hash AS batch_content_hash, "
        "b.payload_hash AS batch_payload_hash, b.status AS batch_status "
        "FROM civic_delivery_receipts r "
        "LEFT JOIN civic_batches b ON b.id = r.batch_id ORDER BY r.id"
    ).fetchall()
    for row in receipt_rows:
        if (
            row["batch_key"] is None
            or row["idempotency_key"] != row["batch_key"]
            or row["content_hash"] != row["batch_content_hash"]
            or (
                row["payload_hash"] is not None
                and row["payload_hash"] != row["batch_payload_hash"]
            )
        ):
            anomalies.append(
                {
                    "code": "delivery_receipt_hash_mismatch",
                    "receipt_id": row["id"],
                    "batch_id": row["batch_id"],
                }
            )
        if row["batch_status"] is not None and row["batch_status"] != "delivered":
            anomalies.append(
                {
                    "code": "delivery_receipt_status_mismatch",
                    "receipt_id": row["id"],
                    "batch_id": row["batch_id"],
                    "status": _safe_code(row["batch_status"]),
                }
            )
    return anomalies


def _source_outages(conn: sqlite3.Connection, now: datetime) -> tuple[list[dict], list[dict]]:
    """Open whole-site outage episodes, and an anomaly when the table cannot be read.

    Only a database that predates the table has none; any other read failure is reported."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'civic_source_outages'"
    ).fetchone()
    if present is None:
        return [], []
    try:
        rows = conn.execute(
            "SELECT source_key, unavailable_since, last_unavailable_at, observed_at "
            "FROM civic_source_outages ORDER BY source_key"
        ).fetchall()
    except sqlite3.OperationalError:
        return [], [{"code": "source_outage_schema_invalid"}]
    outages = []
    for row in rows:
        since = _parse_datetime(row["unavailable_since"])
        last = _parse_datetime(row["last_unavailable_at"])
        observed = _parse_datetime(row["observed_at"])
        if since is None or last is None or observed is None:
            state = "invalid"
            escalates_at = None
        else:
            escalates_at = since + OUTAGE_ESCALATION_AFTER
            if now - observed > OUTAGE_OBSERVATION_GAP:
                state = "stale"
            elif last < observed:
                state = "recovering"
            elif last >= escalates_at:
                state = "escalated"
            else:
                state = "held"
        outages.append(
            {
                "source_key": _safe_code(row["source_key"]),
                "state": state,
                "unavailable_since": _utc_stamp(since),
                "last_unavailable_at": _utc_stamp(last),
                "observed_at": _utc_stamp(observed),
                "escalates_at": _utc_stamp(escalates_at),
            }
        )
    return outages, [
        {"code": f"source_outage_{outage['state']}", "source_key": outage["source_key"]}
        for outage in outages
        if outage["state"] in {"escalated", "invalid"}
    ]


def build_report(
    db_path: str,
    spool_root: str,
    *,
    since_hours: float = 48,
    now: datetime | None = None,
) -> dict:
    if since_hours <= 0:
        raise CivicDiagnosticError("since-hours must be positive")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise CivicDiagnosticError("diagnostic time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=since_hours)
    conn = _connect_read_only(db_path)
    try:
        conn.execute("BEGIN")
        watches = _watch_rows(conn)
        runs = _recent_runs(conn, cutoff)
        batches = _recent_batches(conn, cutoff)
        candidates = _candidate_counts(conn, cutoff)
        qualifications = _qualification_counts(conn, cutoff)
        ai_stages = _ai_counts(conn, cutoff)
        notifications = _notification_counts(conn, cutoff)
        receipts = _receipts(conn, cutoff)
        spool, spool_anomalies, spool_names = _spool_report(conn, spool_root)
        source_outages, outage_anomalies = _source_outages(conn, now)
        anomalies = spool_anomalies + _database_anomalies(
            conn, now=now, cutoff=cutoff, spool_names=spool_names
        ) + outage_anomalies
        anomalies.sort(
            key=lambda item: (
                str(item.get("code")),
                int(item.get("run_id") or item.get("batch_id") or 0),
                str(item.get("file_name") or ""),
            )
        )
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": _utc_stamp(now),
            "status": "attention" if anomalies else "ok",
            "database": {
                "label": _database_label(db_path),
                "read_only": True,
                "query_only": True,
            },
            "window": {
                "since_hours": since_hours,
                "since_at": _utc_stamp(cutoff),
            },
            "watches": watches,
            "runs": runs,
            "batches": batches,
            "candidate_counts": candidates,
            "qualification_counts": qualifications,
            "ai_stage_counts": ai_stages,
            "notification_counts": notifications,
            "delivery_receipts": receipts,
            "source_outages": source_outages,
            "spool": spool,
            "summary": {
                "watch_count": len(watches),
                "recent_run_count": len(runs),
                "recent_batch_count": len(batches),
                "source_outage_count": len(source_outages),
                "spool_file_count": sum(
                    len(directory["files"])
                    for directory in spool["directories"].values()
                ),
                "anomaly_count": len(anomalies),
            },
            "anomalies": anomalies,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="/data/beehive.db")
    parser.add_argument("--spool-root", default="/spool")
    parser.add_argument("--since-hours", type=float, default=48)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = build_report(
            args.db_path,
            args.spool_root,
            since_hours=args.since_hours,
        )
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 -- diagnostics must still return one JSON document
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": _utc_stamp(datetime.now(timezone.utc)),
            "status": "error",
            "database": {"label": _database_label(args.db_path), "read_only": True},
            "error": {
                "code": "diagnostic_failed",
                "class": _safe_code(type(exc).__name__),
            },
        }
        exit_code = 1
    print(
        json.dumps(
            report,
            ensure_ascii=True,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
