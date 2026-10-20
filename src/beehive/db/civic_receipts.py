"""Civic Delivery Receipt persistence: the immutable proof that one Civic Batch was accepted
downstream, and the mechanism that makes a replayed delivery a no-op instead of a second
publication.

**The idempotency key is the identity.** Not the row id, not the batch id: a redelivery
presenting the same key is a replay of the same delivery. `record_delivery_receipt` therefore
never inserts a second row for a key it has already seen -- it returns the original receipt with
`replayed=True`, so a caller cannot tell "I delivered it" and "someone already delivered it"
apart by accident, and cannot double-publish by retrying. A replay does still converge the
envelope's own status to DELIVERED, because the receipt proves the publication happened: leaving
it DELIVERING would strand a live claim that only a recovery sweep could clear, and would keep
offering an already-published envelope for redelivery.

**A replay whose envelope changed is refused, not silently accepted.** The receipt records both
the legacy Candidate content_hash and the canonical spool payload_hash. A replay presenting the
same key with either hash changed is an integrity problem. NULL payload_hash is tolerated only on
legacy receipt rows that predate the v1 spool.

**Receipts are write-once.** There is deliberately no update or delete path in this module. A
receipt is a historical fact; correcting a delivery means publishing a correction downstream, not
editing the record of what was sent.

Recording a receipt and terminalizing its Batch as DELIVERED happen in one transaction
(`record_delivery_receipt` with a claim token), so the two can never disagree: there is no window
in which a Batch is delivered with no receipt, or receipted while still looking deliverable."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from beehive.domain.civic import (CivicBatchStatus, CivicDeliveryChannel, CivicDeliveryReceipt,
                                   require_batch_transition)

MAX_PROVIDER_REFERENCE_CHARS = 2048


@dataclass(frozen=True)
class DeliveryReceiptResult:
    """The outcome of one `record_delivery_receipt` call.

    `replayed` is True when this exact idempotency key had already been receipted, in which case
    `receipt` is the ORIGINAL receipt -- its original delivered_at and provider_reference, never
    this attempt's -- and no receipt row was written. The Batch's own status is still converged to
    DELIVERED, because the receipt proves the publication happened. A caller must treat a replay
    as "already published", never as a fresh success."""
    receipt: CivicDeliveryReceipt
    replayed: bool


def _row_to_receipt(row: sqlite3.Row) -> CivicDeliveryReceipt:
    return CivicDeliveryReceipt(
        id=row["id"],
        batch_id=row["batch_id"],
        idempotency_key=row["idempotency_key"],
        content_hash=row["content_hash"],
        channel=CivicDeliveryChannel(row["channel"]),
        target=row["target"],
        delivered_at=datetime.fromisoformat(row["delivered_at"]),
        provider_reference=row["provider_reference"],
        payload_hash=row["payload_hash"])


def record_delivery_receipt(
    conn: sqlite3.Connection,
    batch_id: int,
    claim_token: str,
    channel: CivicDeliveryChannel,
    target: str,
    now: datetime,
    *,
    provider_reference: str = "",
    now_fn: Callable[[], datetime] | None = None,
) -> DeliveryReceiptResult | None:
    """Records that the Batch's envelope was accepted downstream, and terminalizes the Batch as
    DELIVERED, in one transaction.

    The idempotency key and content_hash are read from the Batch itself, never from the caller,
    so a receipt can only ever attest to bytes that actually exist. If that key already has a
    receipt this is a replay: the original receipt is returned unchanged and no receipt row is
    written, while the Batch is still converged to DELIVERED so an already-published envelope
    stops being offered for redelivery.

    Returns None -- writing nothing -- when the Batch is not this worker's live DELIVERING claim,
    which is the same fence every other progressive write in the civic_* family uses. Raises
    ValueError if a replay presents a content_hash that no longer matches the receipted one."""
    if not target.strip():
        raise ValueError("a Civic Delivery Receipt needs a target")
    if len(provider_reference) > MAX_PROVIDER_REFERENCE_CHARS:
        raise ValueError(
            f"provider_reference exceeds {MAX_PROVIDER_REFERENCE_CHARS} characters")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        batch = conn.execute(
            "SELECT status, idempotency_key, content_hash, payload_hash, lease_expires_at "
            "FROM civic_batches "
            "WHERE id = ? AND claim_token = ? AND status = 'delivering'",
            (batch_id, claim_token)).fetchone()
        if (batch is None or datetime.fromisoformat(batch["lease_expires_at"]) <= now):
            result = None
        elif batch["payload_hash"] is None:
            raise ValueError(
                f"Civic Batch {batch_id} has no payload_hash; writing an inbox file, not merely "
                "sealing the Batch, must bind the exact delivered bytes before a receipt")
        else:
            require_batch_transition(
                CivicBatchStatus(batch["status"]), CivicBatchStatus.DELIVERED)
            existing = conn.execute(
                "SELECT * FROM civic_delivery_receipts WHERE idempotency_key = ?",
                (batch["idempotency_key"],)).fetchone()
            if existing is not None:
                if existing["content_hash"] != batch["content_hash"]:
                    raise ValueError(
                        f"Civic Batch {batch_id} was already delivered under idempotency key "
                        f"{batch['idempotency_key']!r} with a different content_hash -- the "
                        "envelope changed between delivery attempts")
                # NULL is accepted only on rows written before payload_hash existed. New receipt
                # inserts below always bind it, and a non-NULL legacy replay must match exactly.
                if (existing["payload_hash"] is not None
                        and existing["payload_hash"] != batch["payload_hash"]):
                    raise ValueError(
                        f"Civic Batch {batch_id} was already delivered under idempotency key "
                        f"{batch['idempotency_key']!r} with a different payload_hash")
                receipt_row = existing
                replayed = True
            else:
                cur = conn.execute(
                    "INSERT INTO civic_delivery_receipts (batch_id, idempotency_key, "
                    "content_hash, payload_hash, channel, target, provider_reference, "
                    "delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (batch_id, batch["idempotency_key"], batch["content_hash"],
                     batch["payload_hash"], channel.value, target, provider_reference,
                     now.isoformat()))
                receipt_row = conn.execute(
                    "SELECT * FROM civic_delivery_receipts WHERE id = ?",
                    (cur.lastrowid,)).fetchone()
                replayed = False
            conn.execute(
                "UPDATE civic_batches SET status = 'delivered', claim_token = NULL, "
                "lease_expires_at = NULL, settled_at = ?, next_attempt_at = NULL, "
                "error_code = NULL, error_detail = NULL "
                "WHERE id = ? AND claim_token = ? AND status = 'delivering'",
                (now.isoformat(), batch_id, claim_token))
            result = DeliveryReceiptResult(
                receipt=_row_to_receipt(receipt_row), replayed=replayed)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def get_receipt_for_key(
    conn: sqlite3.Connection, idempotency_key: str,
) -> CivicDeliveryReceipt | None:
    """The receipt for one idempotency key, or None. A caller checks this BEFORE performing an
    expensive downstream delivery so a known-delivered envelope is never sent again."""
    row = conn.execute(
        "SELECT * FROM civic_delivery_receipts WHERE idempotency_key = ?",
        (idempotency_key,)).fetchone()
    return _row_to_receipt(row) if row else None


def get_receipt_for_batch(
    conn: sqlite3.Connection, batch_id: int,
) -> CivicDeliveryReceipt | None:
    row = conn.execute(
        "SELECT * FROM civic_delivery_receipts WHERE batch_id = ?", (batch_id,)).fetchone()
    return _row_to_receipt(row) if row else None


def list_receipts(conn: sqlite3.Connection, limit: int = 50) -> list[CivicDeliveryReceipt]:
    rows = conn.execute(
        "SELECT * FROM civic_delivery_receipts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_receipt(row) for row in rows]


def is_already_delivered(conn: sqlite3.Connection, idempotency_key: str) -> bool:
    return get_receipt_for_key(conn, idempotency_key) is not None


def require_receipt_matches_batch(
    conn: sqlite3.Connection, batch_id: int,
) -> CivicDeliveryReceipt | None:
    """Validate a receipt replay without claiming or writing.

    A NULL receipt payload_hash is tolerated only as legacy history. Every non-NULL hash is bound
    to the Batch, as are the legacy content hash and idempotency key.
    """
    row = conn.execute(
        "SELECT r.*, b.idempotency_key AS batch_key, b.content_hash AS batch_content_hash, "
        "b.payload_hash AS batch_payload_hash FROM civic_delivery_receipts r "
        "JOIN civic_batches b ON b.id = r.batch_id WHERE b.id = ?",
        (batch_id,)).fetchone()
    if row is None:
        return None
    if row["idempotency_key"] != row["batch_key"]:
        raise ValueError(f"Civic Batch {batch_id} receipt idempotency_key does not match")
    if row["content_hash"] != row["batch_content_hash"]:
        raise ValueError(f"Civic Batch {batch_id} receipt content_hash does not match")
    if (row["payload_hash"] is not None
            and row["payload_hash"] != row["batch_payload_hash"]):
        raise ValueError(f"Civic Batch {batch_id} receipt payload_hash does not match")
    return _row_to_receipt(row)
