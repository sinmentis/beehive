# tests/db/test_civic_receipts.py
"""Civic Delivery Receipt persistence: the idempotency key is the delivery's identity, so a
replayed delivery is a no-op rather than a second publication."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_batches import (bind_batch_payload_hash, claim_batch_for_delivery,
                                       get_civic_batch, get_or_create_batch_if_claimed,
                                       quarantine_batch, release_batch_claim,
                                       seal_batch_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate,
                                          mark_candidate_verified, set_candidate_status)
from beehive.db.civic_receipts import (get_receipt_for_batch, get_receipt_for_key,
                                        is_already_delivered, list_receipts,
                                        record_delivery_receipt)
from beehive.db.civic_runs import claim_civic_run, enqueue_civic_run
from beehive.db.civic_verifications import record_verification
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicBatchStatus, CivicCandidateStatus, CivicDeliveryChannel,
                                   CivicEvidenceStance, CivicEvidenceTier,
                                   CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _sealed_batch(conn, subject="NZ election", slot_at=T0):
    watch_id = create_civic_watch(conn, subject, "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, slot_at, slot_at)
    lease = claim_civic_run(conn, run.id, slot_at, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, slot_at)
    # A seal only admits settled work, and only VERIFIED Candidates enter the envelope.
    candidate = create_candidate(conn, batch.id, "dk-1", "Title", "Statement", slot_at)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", "e1", "https://example.test/1", "Release",
        CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, slot_at,
        publisher="Beehive")
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, slot_at)
    record_verification(
        conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", slot_at)
    mark_candidate_verified(conn, candidate.id, slot_at)
    sealed = seal_batch_if_claimed(
        conn, batch.id, run.id, lease.run.claim_token, slot_at)
    return bind_batch_payload_hash(conn, sealed.id, "f" * 64, slot_at)


@pytest.fixture
def sealed(conn):
    return _sealed_batch(conn)


def _deliver(conn, batch, now=T0, **overrides):
    lease = claim_batch_for_delivery(conn, batch.id, now, 60)
    assert lease is not None
    kwargs = dict(channel=CivicDeliveryChannel.SPOOL, target="/var/spool/civic")
    kwargs.update(overrides)
    return record_delivery_receipt(conn, batch.id, lease.batch.claim_token, now=now, **kwargs)


# ============================================================================
# Recording
# ============================================================================


def test_recording_a_receipt_also_terminalizes_the_batch(conn, sealed):
    result = _deliver(conn, sealed)
    assert not result.replayed
    assert result.receipt.batch_id == sealed.id
    assert result.receipt.idempotency_key == sealed.idempotency_key
    assert result.receipt.content_hash == sealed.content_hash
    assert result.receipt.payload_hash == sealed.payload_hash
    assert result.receipt.channel is CivicDeliveryChannel.SPOOL
    assert result.receipt.delivered_at == T0

    delivered = get_civic_batch(conn, sealed.id)
    assert delivered.status is CivicBatchStatus.DELIVERED
    assert delivered.settled_at == T0


def test_the_receipt_key_and_hash_come_from_the_batch_not_the_caller(conn, sealed):
    result = _deliver(conn, sealed)
    assert result.receipt.idempotency_key == sealed.idempotency_key
    assert result.receipt.content_hash == sealed.content_hash


def test_recording_is_fenced_on_the_delivery_claim(conn, sealed):
    claim_batch_for_delivery(conn, sealed.id, T0, 60)
    assert record_delivery_receipt(
        conn, sealed.id, "stale", CivicDeliveryChannel.SPOOL, "/spool", T0) is None
    assert get_receipt_for_batch(conn, sealed.id) is None
    assert get_civic_batch(conn, sealed.id).status is CivicBatchStatus.DELIVERING


def test_an_unclaimed_batch_cannot_be_receipted(conn, sealed):
    assert record_delivery_receipt(
        conn, sealed.id, "anything", CivicDeliveryChannel.SPOOL, "/spool", T0) is None


def test_a_receipt_needs_a_target(conn, sealed):
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    with pytest.raises(ValueError, match="target"):
        record_delivery_receipt(
            conn, sealed.id, lease.batch.claim_token, CivicDeliveryChannel.SPOOL, "  ", T0)


def test_provider_reference_is_recorded(conn, sealed):
    result = _deliver(conn, sealed, channel=CivicDeliveryChannel.EMAIL,
                      target="owner@example.test", provider_reference="msg-42")
    assert result.receipt.provider_reference == "msg-42"
    assert result.receipt.channel is CivicDeliveryChannel.EMAIL


# ============================================================================
# Replay
# ============================================================================


def test_a_replayed_delivery_returns_the_original_receipt_and_writes_nothing(conn, sealed):
    first = _deliver(conn, sealed, now=T0)

    # Force the envelope back into a claimable state, as an operator recovering a downstream
    # system that lost its own record of the delivery would.
    conn.execute(
        "UPDATE civic_batches SET status = 'sealed', settled_at = NULL WHERE id = ?",
        (sealed.id,))
    conn.commit()

    replay = _deliver(conn, sealed, now=T1, provider_reference="second-attempt")
    assert replay.replayed
    assert replay.receipt.id == first.receipt.id
    assert replay.receipt.delivered_at == T0
    assert replay.receipt.provider_reference == ""
    assert len(list_receipts(conn)) == 1


def test_a_replay_converges_the_envelope_to_delivered_without_a_second_receipt(conn, sealed):
    """A replay must not leave the envelope holding a live claim: the receipt proves it was
    published, so the envelope's own status converges to DELIVERED and it stops being offered."""
    _deliver(conn, sealed, now=T0)
    conn.execute(
        "UPDATE civic_batches SET status = 'sealed', settled_at = NULL WHERE id = ?",
        (sealed.id,))
    conn.commit()
    replay = _deliver(conn, sealed, now=T1)
    assert replay.replayed
    converged = get_civic_batch(conn, sealed.id)
    assert converged.status is CivicBatchStatus.DELIVERED
    assert converged.claim_token is None
    assert len(list_receipts(conn)) == 1


def test_a_replay_whose_envelope_changed_is_refused(conn, sealed):
    _deliver(conn, sealed, now=T0)
    conn.execute(
        "UPDATE civic_batches SET status = 'sealed', settled_at = NULL, "
        "content_hash = 'a-different-hash' WHERE id = ?", (sealed.id,))
    conn.commit()
    lease = claim_batch_for_delivery(conn, sealed.id, T1, 60)
    with pytest.raises(ValueError, match="different content_hash"):
        record_delivery_receipt(
            conn, sealed.id, lease.batch.claim_token, CivicDeliveryChannel.SPOOL, "/spool", T1)
    assert len(list_receipts(conn)) == 1


def test_a_replay_whose_payload_changed_is_refused(conn, sealed):
    _deliver(conn, sealed, now=T0)
    conn.execute(
        "UPDATE civic_batches SET status = 'sealed', settled_at = NULL, "
        "payload_hash = ? WHERE id = ?",
        ("e" * 64, sealed.id))
    conn.commit()
    lease = claim_batch_for_delivery(conn, sealed.id, T1, 60)
    with pytest.raises(ValueError, match="different payload_hash"):
        record_delivery_receipt(
            conn, sealed.id, lease.batch.claim_token,
            CivicDeliveryChannel.SPOOL, "/spool", T1)


def test_a_null_payload_hash_is_tolerated_only_on_a_legacy_receipt(conn, sealed):
    conn.execute(
        "INSERT INTO civic_delivery_receipts (batch_id, idempotency_key, content_hash, "
        "payload_hash, channel, target, delivered_at) VALUES (?, ?, ?, ?, 'spool', "
        "'/legacy', ?)",
        (sealed.id, sealed.idempotency_key, sealed.content_hash, sealed.payload_hash,
         T0.isoformat()))
    # Simulate the row as it existed before the additive migration. Repository code has no
    # receipt UPDATE path; this direct write is only the upgrade fixture.
    conn.execute(
        "UPDATE civic_delivery_receipts SET payload_hash = NULL WHERE batch_id = ?",
        (sealed.id,))
    conn.commit()
    lease = claim_batch_for_delivery(conn, sealed.id, T1, 60)
    replay = record_delivery_receipt(
        conn, sealed.id, lease.batch.claim_token,
        CivicDeliveryChannel.SPOOL, "/spool", T1)
    assert replay.replayed
    assert replay.receipt.payload_hash is None


def test_a_caller_can_check_for_a_prior_delivery_before_doing_any_work(conn, sealed):
    assert not is_already_delivered(conn, sealed.idempotency_key)
    assert get_receipt_for_key(conn, sealed.idempotency_key) is None
    _deliver(conn, sealed)
    assert is_already_delivered(conn, sealed.idempotency_key)
    assert get_receipt_for_key(conn, sealed.idempotency_key).batch_id == sealed.id


def test_two_batches_hold_independent_receipts(conn):
    first = _sealed_batch(conn, subject="A")
    second = _sealed_batch(conn, subject="B")
    assert first.idempotency_key != second.idempotency_key
    _deliver(conn, first)
    _deliver(conn, second)
    assert len(list_receipts(conn)) == 2
    assert get_receipt_for_batch(conn, first.id).batch_id == first.id
    assert get_receipt_for_batch(conn, second.id).batch_id == second.id


def test_a_retried_delivery_after_a_failure_delivers_the_same_bytes_exactly_once(conn, sealed):
    """The realistic retry path: the first attempt fails before any receipt is written, the
    envelope goes back to the queue unchanged, and the second attempt writes the one receipt."""
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    released = release_batch_claim(
        conn, sealed.id, lease.batch.claim_token, T0, retry_after_seconds=0,
        error_code="upstream_500")
    assert released.content_hash == sealed.content_hash
    assert get_receipt_for_batch(conn, sealed.id) is None

    result = _deliver(conn, sealed, now=T1)
    assert not result.replayed
    assert result.receipt.content_hash == sealed.content_hash
    assert len(list_receipts(conn)) == 1
    assert get_civic_batch(conn, sealed.id).attempt_count == 2


def test_a_quarantined_envelope_is_never_receipted(conn, sealed):
    quarantine_batch(conn, sealed.id, T0, "integrity")
    assert claim_batch_for_delivery(conn, sealed.id, T0, 60) is None
    assert get_receipt_for_batch(conn, sealed.id) is None


def test_the_schema_itself_refuses_a_duplicate_idempotency_key(conn, sealed):
    import sqlite3
    _deliver(conn, sealed)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_delivery_receipts (batch_id, idempotency_key, content_hash, "
            "channel, target, delivered_at) VALUES (?, ?, 'h', 'spool', '/spool', ?)",
            (sealed.id, sealed.idempotency_key, T1.isoformat()))


def test_the_schema_refuses_a_new_receipt_without_payload_hash(conn, sealed):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="require payload_hash"):
        conn.execute(
            "INSERT INTO civic_delivery_receipts (batch_id, idempotency_key, content_hash, "
            "channel, target, delivered_at) VALUES (?, 'new-key', ?, 'spool', '/spool', ?)",
            (sealed.id, sealed.content_hash, T1.isoformat()))


def test_receipts_survive_as_long_as_their_batch(conn, sealed):
    _deliver(conn, sealed)
    conn.execute("DELETE FROM civic_batches WHERE id = ?", (sealed.id,))
    conn.commit()
    assert list_receipts(conn) == []
