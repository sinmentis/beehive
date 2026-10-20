"""Versioned, deterministic two-phase Civic publication spool.

Beehive writes a validated v1 inbox envelope and stops. The write neither claims delivery nor
records a receipt. PartyMap later writes a v1 outcome; only an accepted outcome consumes a
delivery attempt and records the SPOOL receipt.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Callable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from beehive.civic.events import CivicEventLogger
from beehive.db import app_state
from beehive.civic.publication import (
    SCHEMA_VERSION as SCHEMA_VERSION,
    CivicPublicationIntegrityError,
    canonical_json_bytes as canonical_json_bytes,
    payload_hash_for as payload_hash_for,
    read_publication_envelope,
    validate_publication_envelope as _validate_publication_envelope,
)
from beehive.db.civic_batches import (
    claim_batch_for_delivery,
    get_batch_by_idempotency_key,
    get_civic_batch,
    get_frozen_publication,
    iter_deliverable_batches,
    quarantine_batch,
    recompute_content_hash,
)
from beehive.db.civic_receipts import (
    record_delivery_receipt,
    require_receipt_matches_batch,
)
from beehive.domain.civic import CivicBatchStatus, CivicDeliveryChannel

SPOOL_TARGET = "civic-publication-v1"
PROJECTION_WRITE_LIMIT = 50
PROJECTION_VERIFY_LIMIT = 20
PROJECTION_WRITE_CURSOR_KEY = "civic_spool_projected_through_batch_id"
PROJECTION_VERIFY_CURSOR_KEY = "civic_spool_verified_through_batch_id"
PENDING_EVENT_LIMIT = 20
_LOCAL_APPLY_WITHOUT_PROPOSAL_DETAIL = (
    "an accepted outcome without a proposal is valid only for an empty envelope")
_SAFE_FILE_KEY = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
_SCHEMA_DIRECTORY = Path(__file__).with_name("schemas")
_OUTCOME_SCHEMA_PATH = _SCHEMA_DIRECTORY / "civic-publication-outcome-v1.schema.json"


class CivicSpoolError(RuntimeError):
    pass


class CivicSpoolIntegrityError(CivicSpoolError):
    pass


class CivicSpoolCandidateError(CivicSpoolIntegrityError):
    """Compatibility name; new Candidate failures are settled before seal, never in spool."""


class CivicSpoolLegacyProjectionError(CivicSpoolError):
    """A legacy Batch has no bound artifact to replay without reconstructing historical data."""


class _CivicSpoolEnvelopeMissingError(CivicSpoolIntegrityError):
    pass


class CivicOutcomeDisposition(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REPLAYED = "replayed"
    VALIDATED_SHADOW = "validated_shadow"
    PENDING_CLAIM = "pending_claim"
    ERROR = "error"


@dataclass(frozen=True)
class CivicSpoolWriteResult:
    batch_id: int
    path: Path
    payload_hash: str
    replayed: bool


@dataclass(frozen=True)
class CivicOutcomeResult:
    path: Path
    disposition: CivicOutcomeDisposition
    batch_id: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class CivicSpoolProjectionPlan:
    """One bounded projection pass over every deliverable Batch.

    Batches without a plain inbox file are written first, so an unsettled prefix can never
    starve newer Batches. Existing files are re-verified against their frozen bytes. Each
    kind of work is bounded per pass and continues after its own persisted cursor, so every
    Batch gets its turn within a bounded number of passes, whatever the timer cadence and
    however many attempts fail permanently.
    """

    unprojected_batch_ids: tuple[int, ...]
    verification_batch_ids: tuple[int, ...]
    deliverable_count: int
    deferred_count: int
    write_cursor: str | None = None
    verify_cursor: str | None = None

    @property
    def batch_ids(self) -> tuple[int, ...]:
        return self.unprojected_batch_ids + self.verification_batch_ids


@dataclass(frozen=True)
class CivicSpoolProjectionFailure:
    batch_id: int
    error: Exception


@dataclass(frozen=True)
class CivicSpoolProjection:
    writes: tuple[CivicSpoolWriteResult, ...]
    failures: tuple[CivicSpoolProjectionFailure, ...]


def _window_after(batch_ids: list[int], cursor: str | None, limit: int) -> tuple[int, ...]:
    """Up to ``limit`` ascending ids after ``cursor``, wrapping to the lowest ids."""
    try:
        after = max(0, int(cursor)) if cursor is not None else 0
    except ValueError:
        after = 0
    later = [batch_id for batch_id in batch_ids if batch_id > after]
    earlier = [batch_id for batch_id in batch_ids if batch_id <= after]
    return tuple((later + earlier)[:limit])


def _advance_cursor(
    conn: sqlite3.Connection, key: str, observed: str | None, window: tuple[int, ...],
) -> None:
    # A pass that planned from a cursor another pass has since moved must not move it back.
    if window:
        app_state.compare_and_set(conn, key, observed, str(window[-1]))


def _schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


_OUTCOME_VALIDATOR = Draft202012Validator(
    _schema(_OUTCOME_SCHEMA_PATH), format_checker=FormatChecker())


def _validate(validator: Draft202012Validator, value: object, label: str) -> None:
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    if not errors:
        return
    error = errors[0]
    path = ".".join(str(part) for part in error.absolute_path) or "<root>"
    raise CivicSpoolIntegrityError(f"{label} {path}: {error.message}")


def validate_publication_envelope(envelope: object) -> None:
    """Validate an envelope against the packaged authoritative v1 schema."""
    try:
        _validate_publication_envelope(envelope)
    except CivicPublicationIntegrityError as exc:
        raise CivicSpoolIntegrityError(str(exc)) from exc


def _frozen_publication_bytes(conn: sqlite3.Connection, batch_id: int) -> bytes:
    batch = get_civic_batch(conn, batch_id)
    if batch is None:
        raise CivicSpoolIntegrityError(f"no Civic Batch with id={batch_id}")
    if batch.status is not CivicBatchStatus.SEALED:
        raise CivicSpoolIntegrityError(
            f"Civic Batch {batch_id} is {batch.status.value}, not sealed")
    try:
        data = get_frozen_publication(conn, batch_id)
    except CivicPublicationIntegrityError as exc:
        raise CivicSpoolIntegrityError(str(exc)) from exc
    if data is None:
        raise CivicSpoolLegacyProjectionError(
            f"Civic Batch {batch_id} predates frozen publication; only an existing "
            "bound canonical spool artifact may be replayed")
    return data


def build_publication_envelope(conn: sqlite3.Connection, batch_id: int) -> dict:
    """Return the sealed projection, without reinterpreting generated content or evidence."""
    return json.loads(_frozen_publication_bytes(conn, batch_id))


def _safe_filename(idempotency_key: str) -> str:
    if (len(idempotency_key) < 16
            or any(char not in _SAFE_FILE_KEY for char in idempotency_key)):
        raise CivicSpoolIntegrityError(
            "idempotency_key is not safe for a same-directory spool filename")
    return f"{idempotency_key}.json"


def _read_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CivicSpoolIntegrityError(
            f"cannot safely open spool file {path.name!r}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CivicSpoolIntegrityError(
                f"spool path {path.name!r} is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_identical_or_fail(path: Path, data: bytes) -> bool:
    """Write in ``path.parent`` via fsync + replace. Return True for an identical replay."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = _read_file(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing == data:
            return True
        raise CivicSpoolIntegrityError(
            f"existing inbox file {path.name!r} differs from the sealed Batch")

    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if _read_file(path) != data:
        raise CivicSpoolIntegrityError(
            f"inbox file {path.name!r} changed during atomic publication")
    return False


def _outcome_correlation(
    conn: sqlite3.Connection,
    path: Path,
):
    try:
        payload = json.loads(_read_file(path).decode("utf-8"))
    except (CivicSpoolError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    idempotency_key = payload.get("idempotency_key")
    payload_hash = payload.get("payload_hash")
    if not isinstance(idempotency_key, str):
        return None, None, payload_hash
    return (
        get_batch_by_idempotency_key(conn, idempotency_key),
        idempotency_key,
        payload_hash,
    )


class CivicSpool:
    def __init__(
        self,
        root: str | Path,
        *,
        shadow: bool = False,
        event_logger: CivicEventLogger | None = None,
    ) -> None:
        root_text = str(root).strip()
        if not root_text or "://" in root_text:
            raise ValueError("CIVIC_SPOOL_ROOT must be a filesystem path")
        self.root = Path(root_text)
        self.inbox = self.root / "inbox"
        self.outcomes = self.root / "outcomes"
        self.processed = self.root / "processed"
        self.processed_accepted = self.processed / "accepted"
        self.processed_rejected = self.processed / "rejected"
        self.shadow = shadow
        self.event_logger = event_logger or CivicEventLogger()

    def write_batch(
        self,
        conn: sqlite3.Connection,
        batch_id: int,
        now: datetime,
    ) -> CivicSpoolWriteResult:
        """Validate and atomically write frozen bytes without recording delivery."""
        started_at = monotonic()
        batch = get_civic_batch(conn, batch_id)
        self.event_logger.emit(
            "civic.spool.write.started",
            watch_id=batch.watch_id if batch is not None else None,
            run_id=batch.run_id if batch is not None else None,
            batch_id=batch_id,
            idempotency_key=batch.idempotency_key if batch is not None else None,
            payload_hash=batch.payload_hash if batch is not None else None,
            phase="spool_write",
            status="started",
        )
        try:
            result = self._write_batch(conn, batch_id, now)
        except Exception as exc:
            self.event_logger.emit(
                "civic.spool.write.failed",
                watch_id=batch.watch_id if batch is not None else None,
                run_id=batch.run_id if batch is not None else None,
                batch_id=batch_id,
                idempotency_key=batch.idempotency_key if batch is not None else None,
                phase="spool_write",
                status="failed",
                error_code="spool_write_failed",
                error_class=type(exc).__name__,
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
            raise
        self.event_logger.emit(
            "civic.spool.write.finished",
            watch_id=batch.watch_id if batch is not None else None,
            run_id=batch.run_id if batch is not None else None,
            batch_id=result.batch_id,
            idempotency_key=batch.idempotency_key if batch is not None else None,
            payload_hash=result.payload_hash,
            file_name=result.path.name,
            phase="spool_write",
            status="replayed" if result.replayed else "written",
            replayed=result.replayed,
            elapsed_ms=(monotonic() - started_at) * 1000,
        )
        return result

    def _write_batch(
        self,
        conn: sqlite3.Connection,
        batch_id: int,
        now: datetime,
    ) -> CivicSpoolWriteResult:
        try:
            try:
                data = _frozen_publication_bytes(conn, batch_id)
            except CivicSpoolLegacyProjectionError:
                batch = get_civic_batch(conn, batch_id)
                if batch.payload_hash is None:
                    raise CivicSpoolLegacyProjectionError(
                        f"Civic Batch {batch_id} has no bound legacy payload hash") from None
                try:
                    envelope = self._require_matching_inbox(conn, batch)
                except _CivicSpoolEnvelopeMissingError as exc:
                    raise CivicSpoolLegacyProjectionError(
                        f"Civic Batch {batch_id} has no legacy artifact to replay; "
                        "historical Candidates will not be reconstructed") from exc
                data = canonical_json_bytes(envelope)
        except CivicSpoolIntegrityError as exc:
            batch = get_civic_batch(conn, batch_id)
            if batch is not None and batch.status in {
                    CivicBatchStatus.SEALED, CivicBatchStatus.DELIVERING}:
                quarantine_batch(
                    conn, batch_id, now, "invalid_envelope", str(exc)[:500])
            raise
        envelope = json.loads(data)
        payload_hash = envelope["batch"]["payload_hash"]
        path = self.inbox / _safe_filename(envelope["batch"]["idempotency_key"])
        try:
            replayed = atomic_write_identical_or_fail(path, data)
        except CivicSpoolIntegrityError as exc:
            batch = get_civic_batch(conn, batch_id)
            if batch is not None and batch.status in {
                    CivicBatchStatus.SEALED, CivicBatchStatus.DELIVERING}:
                quarantine_batch(
                    conn, batch_id, now, "spool_integrity_mismatch", str(exc)[:500])
            raise
        return CivicSpoolWriteResult(
            batch_id=batch_id,
            path=path,
            payload_hash=payload_hash,
            replayed=replayed,
        )

    def emit_sealed_batches(
        self,
        conn: sqlite3.Connection,
        now: datetime,
    ) -> tuple[CivicSpoolWriteResult, ...]:
        """Project every planned Batch, then raise the first failure, if any."""
        projection = self.project_deliverable_batches(conn, now)
        if projection.failures:
            raise projection.failures[0].error
        return projection.writes

    def project_deliverable_batches(
        self,
        conn: sqlite3.Connection,
        now: datetime,
        *,
        write_limit: int = PROJECTION_WRITE_LIMIT,
        verify_limit: int = PROJECTION_VERIFY_LIMIT,
    ) -> CivicSpoolProjection:
        """Write or re-verify one planned pass; one Batch's failure never stops the others."""
        plan = self.plan_projection(
            conn, now, write_limit=write_limit, verify_limit=verify_limit)
        return self.execute_projection(conn, plan, now)

    def execute_projection(
        self,
        conn: sqlite3.Connection,
        plan: CivicSpoolProjectionPlan,
        now: datetime,
    ) -> CivicSpoolProjection:
        writes = []
        failures = []
        for batch_id in plan.batch_ids:
            try:
                writes.append(self.write_batch(conn, batch_id, now))
            except Exception as exc:  # noqa: BLE001 -- continue other immutable Batches
                failures.append(CivicSpoolProjectionFailure(batch_id=batch_id, error=exc))
        _advance_cursor(
            conn, PROJECTION_WRITE_CURSOR_KEY, plan.write_cursor, plan.unprojected_batch_ids)
        _advance_cursor(
            conn, PROJECTION_VERIFY_CURSOR_KEY, plan.verify_cursor, plan.verification_batch_ids)
        return CivicSpoolProjection(writes=tuple(writes), failures=tuple(failures))

    def plan_projection(
        self,
        conn: sqlite3.Connection,
        now: datetime,
        *,
        write_limit: int = PROJECTION_WRITE_LIMIT,
        verify_limit: int = PROJECTION_VERIFY_LIMIT,
    ) -> CivicSpoolProjectionPlan:
        """Choose this pass's work from all deliverable Batches, never from a fixed prefix."""
        unprojected: list[int] = []
        projected: list[int] = []
        for batch in iter_deliverable_batches(conn, now):
            if self._has_regular_inbox_file(batch):
                projected.append(batch.id)
            else:
                unprojected.append(batch.id)
        write_cursor = app_state.get(conn, PROJECTION_WRITE_CURSOR_KEY)
        verify_cursor = app_state.get(conn, PROJECTION_VERIFY_CURSOR_KEY)
        writes = _window_after(unprojected, write_cursor, write_limit)
        plan = CivicSpoolProjectionPlan(
            unprojected_batch_ids=writes,
            verification_batch_ids=_window_after(projected, verify_cursor, verify_limit),
            deliverable_count=len(unprojected) + len(projected),
            deferred_count=len(unprojected) - len(writes),
            write_cursor=write_cursor,
            verify_cursor=verify_cursor,
        )
        self.event_logger.emit(
            "civic.spool.projection.planned",
            phase="spool_write",
            status="planned",
            batch_count=plan.deliverable_count,
            scheduled_count=len(plan.batch_ids),
            unprojected_count=len(unprojected),
            verification_count=len(plan.verification_batch_ids),
            deferred_count=plan.deferred_count,
        )
        return plan

    def _has_regular_inbox_file(self, batch) -> bool:
        """Anything but an existing regular file is rechecked every pass, not by rotation."""
        try:
            path = self.inbox / _safe_filename(batch.idempotency_key)
        except CivicSpoolIntegrityError:
            return False
        try:
            return stat.S_ISREG(os.lstat(path).st_mode)
        except OSError:
            return False

    def process_outcomes(
        self,
        conn: sqlite3.Connection,
        now: datetime,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> tuple[CivicOutcomeResult, ...]:
        started_at = monotonic()
        self.event_logger.emit(
            "civic.outcome.scan.started",
            phase="outcome",
            status="started",
        )
        try:
            return self._process_outcomes(
                conn, now, now_fn=now_fn, started_at=started_at)
        except BaseException as exc:
            self.event_logger.emit(
                "civic.outcome.scan.failed",
                phase="outcome",
                status="failed",
                error_code="outcome_scan_failed",
                error_class=type(exc).__name__,
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
            raise

    def _process_outcomes(
        self,
        conn: sqlite3.Connection,
        now: datetime,
        *,
        now_fn: Callable[[], datetime] | None,
        started_at: float,
    ) -> tuple[CivicOutcomeResult, ...]:
        self.outcomes.mkdir(parents=True, exist_ok=True)
        results = []
        for path in sorted(self.outcomes.glob("*.json")):
            item_started_at = monotonic()
            correlated_batch, idempotency_key, payload_hash = _outcome_correlation(
                conn, path)
            self.event_logger.emit(
                "civic.outcome.started",
                watch_id=(
                    correlated_batch.watch_id if correlated_batch is not None else None),
                run_id=correlated_batch.run_id if correlated_batch is not None else None,
                batch_id=correlated_batch.id if correlated_batch is not None else None,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                file_name=path.name,
                phase="outcome",
                status="started",
            )
            try:
                result = self._process_outcome_file(
                    conn, path, now, now_fn=now_fn)
            except (CivicSpoolError, ValidationError, ValueError, OSError, json.JSONDecodeError) as exc:
                result = CivicOutcomeResult(
                    path=path,
                    disposition=CivicOutcomeDisposition.ERROR,
                    detail=f"{type(exc).__name__}: {exc}"[:500],
                )
                self.event_logger.emit(
                    "civic.outcome.finished",
                    watch_id=(
                        correlated_batch.watch_id
                        if correlated_batch is not None else None),
                    run_id=(
                        correlated_batch.run_id
                        if correlated_batch is not None else None),
                    batch_id=(
                        correlated_batch.id
                        if correlated_batch is not None else None),
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    file_name=path.name,
                    phase="outcome",
                    status=CivicOutcomeDisposition.ERROR,
                    error_code="outcome_processing_failed",
                    error_class=type(exc).__name__,
                    elapsed_ms=(monotonic() - item_started_at) * 1000,
                )
            else:
                batch = (
                    get_civic_batch(conn, result.batch_id)
                    if result.batch_id is not None else None
                )
                self.event_logger.emit(
                    "civic.outcome.finished",
                    watch_id=batch.watch_id if batch is not None else None,
                    run_id=batch.run_id if batch is not None else None,
                    batch_id=result.batch_id,
                    idempotency_key=batch.idempotency_key if batch is not None else None,
                    payload_hash=batch.payload_hash if batch is not None else None,
                    file_name=path.name,
                    phase="outcome",
                    status=result.disposition,
                    reason=(
                        batch.error_code
                        if batch is not None
                        and result.disposition is CivicOutcomeDisposition.REJECTED
                        else None
                    ),
                    elapsed_ms=(monotonic() - item_started_at) * 1000,
                )
            results.append(result)
        seen_batch_ids = {
            result.batch_id for result in results if result.batch_id is not None
        }
        pending = [
            batch for batch in iter_deliverable_batches(conn, now)
            if batch.id not in seen_batch_ids
        ]
        # A shadow ring never settles, so per-Batch detail is bounded to the newest Batches;
        # deferred_count below stays exact.
        for batch in pending[-PENDING_EVENT_LIMIT:]:
            self.event_logger.emit(
                "civic.outcome.pending",
                watch_id=batch.watch_id,
                run_id=batch.run_id,
                batch_id=batch.id,
                idempotency_key=batch.idempotency_key,
                payload_hash=batch.payload_hash,
                phase="outcome",
                status="pending",
            )
        error_count = sum(
            result.disposition is CivicOutcomeDisposition.ERROR for result in results
        )
        self.event_logger.emit(
            "civic.outcome.scan.finished",
            phase="outcome",
            status="failed" if error_count else "completed",
            outcome_count=len(results),
            failed_count=error_count,
            deferred_count=len(pending),
            elapsed_ms=(monotonic() - started_at) * 1000,
        )
        return tuple(results)

    def _require_matching_envelope(
        self,
        conn: sqlite3.Connection,
        batch,
        path: Path,
        *,
        label: str,
    ) -> dict:
        try:
            data = _read_file(path)
        except FileNotFoundError:
            raise _CivicSpoolEnvelopeMissingError(
                f"Civic Batch {batch.id} has no {label} envelope") from None
        try:
            envelope = read_publication_envelope(data)
            frozen = get_frozen_publication(conn, batch.id)
        except CivicPublicationIntegrityError as exc:
            raise CivicSpoolIntegrityError(str(exc)) from exc
        if frozen is not None and data != frozen:
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} {label} envelope differs from its frozen publication")
        if envelope["batch"]["idempotency_key"] != batch.idempotency_key:
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} {label} idempotency_key changed")
        if envelope["batch"]["batch_content_hash"] != batch.content_hash:
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} {label} batch_content_hash changed")
        if envelope["batch"]["payload_hash"] != batch.payload_hash:
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} {label} payload_hash changed")
        if payload_hash_for(envelope) != batch.payload_hash:
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} {label} bytes do not produce its payload_hash")
        if recompute_content_hash(conn, batch.id) != batch.content_hash:
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} Candidate content changed after sealing")
        return envelope

    def _require_matching_inbox(self, conn: sqlite3.Connection, batch) -> dict:
        return self._require_matching_envelope(
            conn,
            batch,
            self.inbox / _safe_filename(batch.idempotency_key),
            label="inbox",
        )

    def _archive_settled_envelope(
        self,
        conn: sqlite3.Connection,
        batch,
        *,
        accepted: bool,
    ) -> Path:
        """Move or recover one settled inbox envelope, then fsync both rename directories."""
        started_at = monotonic()
        status = "accepted" if accepted else "rejected"
        self.event_logger.emit(
            "civic.spool.archive.started",
            watch_id=batch.watch_id,
            run_id=batch.run_id,
            batch_id=batch.id,
            idempotency_key=batch.idempotency_key,
            payload_hash=batch.payload_hash,
            phase="archive",
            status=status,
        )
        try:
            destination = self._archive_settled_envelope_unlogged(
                conn, batch, accepted=accepted)
        except Exception as exc:
            self.event_logger.emit(
                "civic.spool.archive.failed",
                watch_id=batch.watch_id,
                run_id=batch.run_id,
                batch_id=batch.id,
                idempotency_key=batch.idempotency_key,
                payload_hash=batch.payload_hash,
                phase="archive",
                status="failed",
                error_code="spool_archive_failed",
                error_class=type(exc).__name__,
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
            raise
        self.event_logger.emit(
            "civic.spool.archive.finished",
            watch_id=batch.watch_id,
            run_id=batch.run_id,
            batch_id=batch.id,
            idempotency_key=batch.idempotency_key,
            payload_hash=batch.payload_hash,
            file_name=destination.name,
            phase="archive",
            status=status,
            elapsed_ms=(monotonic() - started_at) * 1000,
        )
        return destination

    def _archive_settled_envelope_unlogged(
        self,
        conn: sqlite3.Connection,
        batch,
        *,
        accepted: bool,
    ) -> Path:
        destination_directory = (
            self.processed_accepted if accepted else self.processed_rejected)
        self.processed.mkdir(parents=True, exist_ok=True)
        destination_directory.mkdir(parents=True, exist_ok=True)
        _fsync_directory(self.root)
        _fsync_directory(self.processed)

        filename = _safe_filename(batch.idempotency_key)
        source = self.inbox / filename
        destination = destination_directory / filename
        try:
            self._require_matching_envelope(
                conn, batch, source, label="inbox")
            source_exists = True
        except _CivicSpoolEnvelopeMissingError:
            source_exists = False

        try:
            self._require_matching_envelope(
                conn, batch, destination, label="processed")
            destination_exists = True
        except _CivicSpoolEnvelopeMissingError:
            destination_exists = False

        if not source_exists and not destination_exists:
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} is settled but has no inbox or processed envelope")
        if source_exists:
            os.replace(source, destination)
        _fsync_directory(self.inbox)
        _fsync_directory(destination_directory)
        return destination

    @staticmethod
    def _matches_terminal_quarantine(batch, payload: dict) -> bool:
        if batch.status is not CivicBatchStatus.QUARANTINED:
            return False
        if payload["outcome"] == "rejected":
            return (
                batch.error_code == payload["reason"]
                and batch.error_detail == payload["detail"]
            )
        return (
            payload["proposal"] is None
            and batch.error_code == "destination_validation_failed"
            and batch.error_detail == _LOCAL_APPLY_WITHOUT_PROPOSAL_DETAIL
        )

    def _process_outcome_file(
        self,
        conn: sqlite3.Connection,
        path: Path,
        now: datetime,
        *,
        now_fn: Callable[[], datetime] | None,
    ) -> CivicOutcomeResult:
        payload = json.loads(_read_file(path).decode("utf-8"))
        _validate(_OUTCOME_VALIDATOR, payload, "publication outcome")
        batch = get_batch_by_idempotency_key(conn, payload["idempotency_key"])
        if batch is None:
            raise CivicSpoolIntegrityError(
                f"outcome names unknown idempotency_key {payload['idempotency_key']!r}")
        if batch.payload_hash is None:
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} has no bound payload_hash")
        if payload["payload_hash"] != batch.payload_hash:
            detail = (
                f"outcome payload_hash {payload['payload_hash']} does not match "
                f"{batch.payload_hash}")
            if not self.shadow and batch.status in {
                    CivicBatchStatus.SEALED, CivicBatchStatus.DELIVERING}:
                quarantine_batch(conn, batch.id, now, "invalid_payload_hash", detail[:500])
            raise CivicSpoolIntegrityError(detail)

        receipt = require_receipt_matches_batch(conn, batch.id)
        if receipt is not None:
            if payload["outcome"] != "accepted":
                raise CivicSpoolIntegrityError(
                    f"Civic Batch {batch.id} is delivered but outcome now says rejected")
            if not self.shadow:
                self._archive_settled_envelope(conn, batch, accepted=True)
            return CivicOutcomeResult(
                path=path,
                disposition=(
                    CivicOutcomeDisposition.VALIDATED_SHADOW
                    if self.shadow else CivicOutcomeDisposition.REPLAYED
                ),
                batch_id=batch.id,
            )

        if self._matches_terminal_quarantine(batch, payload):
            if not self.shadow:
                self._archive_settled_envelope(conn, batch, accepted=False)
            return CivicOutcomeResult(
                path=path,
                disposition=(
                    CivicOutcomeDisposition.VALIDATED_SHADOW
                    if self.shadow else CivicOutcomeDisposition.REPLAYED
                ),
                batch_id=batch.id,
                detail=batch.error_code or "",
            )

        if batch.status in {
                CivicBatchStatus.DELIVERED,
                CivicBatchStatus.QUARANTINED,
                CivicBatchStatus.FAILED}:
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} is terminal without a matching receipt/quarantine")

        try:
            envelope = self._require_matching_inbox(conn, batch)
        except (CivicSpoolIntegrityError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            if not self.shadow and batch.status in {
                    CivicBatchStatus.SEALED, CivicBatchStatus.DELIVERING}:
                quarantine_batch(
                    conn, batch.id, now, "spool_integrity_mismatch", str(exc)[:500])
            if isinstance(exc, CivicSpoolIntegrityError):
                raise
            raise CivicSpoolIntegrityError(
                f"Civic Batch {batch.id} inbox is unreadable: {exc}") from exc

        if self.shadow:
            return CivicOutcomeResult(
                path=path,
                disposition=CivicOutcomeDisposition.VALIDATED_SHADOW,
                batch_id=batch.id,
            )
        if (payload["outcome"] == "accepted"
                and payload["proposal"] is None
                and envelope["items"]):
            quarantined = quarantine_batch(
                conn,
                batch.id,
                now,
                "destination_validation_failed",
                _LOCAL_APPLY_WITHOUT_PROPOSAL_DETAIL,
            )
            if quarantined is None:
                raise CivicSpoolIntegrityError(
                    f"Civic Batch {batch.id} disappeared during local-apply rejection")
            self._archive_settled_envelope(conn, batch, accepted=False)
            return CivicOutcomeResult(
                path=path,
                disposition=CivicOutcomeDisposition.REJECTED,
                batch_id=batch.id,
                detail=_LOCAL_APPLY_WITHOUT_PROPOSAL_DETAIL,
            )
        if payload["outcome"] == "rejected":
            if batch.status not in {
                    CivicBatchStatus.SEALED, CivicBatchStatus.DELIVERING}:
                raise CivicSpoolIntegrityError(
                    f"cannot reject Civic Batch {batch.id} in {batch.status.value}")
            quarantined = quarantine_batch(
                conn,
                batch.id,
                now,
                payload["reason"],
                payload["detail"],
            )
            if quarantined is None:
                raise CivicSpoolIntegrityError(
                    f"Civic Batch {batch.id} disappeared during rejection")
            self._archive_settled_envelope(conn, batch, accepted=False)
            return CivicOutcomeResult(
                path=path,
                disposition=CivicOutcomeDisposition.REJECTED,
                batch_id=batch.id,
                detail=payload["reason"],
            )

        if batch.status is not CivicBatchStatus.SEALED:
            return CivicOutcomeResult(
                path=path,
                disposition=CivicOutcomeDisposition.PENDING_CLAIM,
                batch_id=batch.id,
                detail=f"batch is {batch.status.value}",
            )
        lease = claim_batch_for_delivery(
            conn, batch.id, now, 60, now_fn=now_fn)
        if lease is None:
            return CivicOutcomeResult(
                path=path,
                disposition=CivicOutcomeDisposition.PENDING_CLAIM,
                batch_id=batch.id,
            )
        proposal = payload["proposal"]
        provider_reference = proposal["reference"] if proposal is not None else ""
        recorded = record_delivery_receipt(
            conn,
            batch.id,
            lease.batch.claim_token,
            CivicDeliveryChannel.SPOOL,
            SPOOL_TARGET,
            now,
            provider_reference=provider_reference,
            now_fn=now_fn,
        )
        if recorded is None:
            return CivicOutcomeResult(
                path=path,
                disposition=CivicOutcomeDisposition.PENDING_CLAIM,
                batch_id=batch.id,
                detail="delivery claim moved before receipt",
            )
        self._archive_settled_envelope(conn, batch, accepted=True)
        return CivicOutcomeResult(
            path=path,
            disposition=(
                CivicOutcomeDisposition.REPLAYED
                if recorded.replayed else CivicOutcomeDisposition.ACCEPTED
            ),
            batch_id=batch.id,
        )


__all__ = [
    "SCHEMA_VERSION",
    "SPOOL_TARGET",
    "CivicOutcomeDisposition",
    "CivicOutcomeResult",
    "CivicSpool",
    "CivicSpoolError",
    "CivicSpoolCandidateError",
    "CivicSpoolIntegrityError",
    "CivicSpoolLegacyProjectionError",
    "CivicSpoolProjection",
    "CivicSpoolProjectionFailure",
    "CivicSpoolProjectionPlan",
    "CivicSpoolWriteResult",
    "PENDING_EVENT_LIMIT",
    "PROJECTION_VERIFY_CURSOR_KEY",
    "PROJECTION_VERIFY_LIMIT",
    "PROJECTION_WRITE_CURSOR_KEY",
    "PROJECTION_WRITE_LIMIT",
    "atomic_write_identical_or_fail",
    "build_publication_envelope",
    "canonical_json_bytes",
    "payload_hash_for",
    "validate_publication_envelope",
]
