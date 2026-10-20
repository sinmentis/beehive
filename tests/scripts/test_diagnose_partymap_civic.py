from __future__ import annotations

import io
import json
import sqlite3
from datetime import timedelta

import pytest

from beehive.civic.events import CivicEventLogger
from beehive.civic.spool import CivicSpool
from beehive.civic.worker import CivicWorker
from beehive.db.civic_batches import get_civic_batch
from beehive.db.connection import connect, init_schema
from scripts.diagnose_partymap_civic import (
    MAX_ROWS,
    _connect_read_only,
    _safe_code,
    build_report,
    main,
)
from tests.civic.civic_worker_support import (
    SOURCE_ENTRY,
    T0,
    create_partymap_watch,
    housing_fetch_result,
    passing_green_gateway,
)


async def _delivered_state(tmp_path):
    db_path = tmp_path / "diagnostic.db"
    spool_root = tmp_path / "civic-spool"
    conn = connect(str(db_path))
    init_schema(conn)
    create_partymap_watch(conn)

    async def fetch(_entry, _now):
        return housing_fetch_result()

    logger = CivicEventLogger(io.StringIO(), now_fn=lambda: T0)
    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=lambda: T0,
        event_logger=logger,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    spool = CivicSpool(spool_root, event_logger=logger)
    spool.write_batch(conn, execution.batch_id, T0)
    batch = get_civic_batch(conn, execution.batch_id)
    spool.outcomes.mkdir(parents=True)
    (spool.outcomes / "accepted.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "idempotency_key": batch.idempotency_key,
                "payload_hash": batch.payload_hash,
                "outcome": "accepted",
                "recorded_at": T0.isoformat(),
                "proposal": {
                    "branch": "newsroom/test",
                    "reference": "Bearer provider-secret",
                    "pull_request_number": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    spool.process_outcomes(conn, T0, now_fn=lambda: T0)
    conn.execute("UPDATE civic_watches SET question = 'prompt=private-watch-question'")
    conn.execute(
        "UPDATE civic_candidates SET title = 'private-article-title', "
        "statement = 'private-evidence-statement'"
    )
    conn.execute(
        "UPDATE civic_ai_stages SET output_json = "
        '\'{"model_response":"private-model-output"}\''
    )
    conn.execute(
        "UPDATE civic_delivery_receipts SET provider_reference = "
        "'CLOUDFLARE_API_TOKEN=private-cloudflare-token'"
    )
    conn.commit()
    conn.close()
    return db_path, spool_root, batch


@pytest.mark.parametrize(
    "value",
    (
        "_leading-underscore-token",
        "-leading-hyphen-token",
        "_CivicWorkerClaimLost",
    ),
)
def test_safe_code_allows_token_urlsafe_prefixes_and_private_classes(value):
    assert _safe_code(value) == value


@pytest.mark.parametrize(
    "value",
    ("contains space", 'contains"quote', "contains=equals"),
)
def test_safe_code_rejects_unsafe_delimiters(value):
    assert _safe_code(value) == "[redacted]"


async def test_report_is_read_only_bounded_and_omits_civic_content(tmp_path):
    db_path, spool_root, batch = await _delivered_state(tmp_path)
    before = db_path.read_bytes()

    report = build_report(
        str(db_path),
        str(spool_root),
        since_hours=48,
        now=T0,
    )

    assert db_path.read_bytes() == before
    assert report["database"] == {
        "label": "diagnostic.db",
        "read_only": True,
        "query_only": True,
    }
    assert report["runs"][0]["run_id"] == batch.run_id
    assert report["batches"][0]["batch_id"] == batch.id
    assert report["watches"][0]["vocabularies"][0]["term_count"] == 1
    assert report["delivery_receipts"][0]["provider_reference"] == "[REDACTED]"
    processed = report["spool"]["directories"]["processed_accepted"]["files"][0]
    assert processed["batch_id"] == batch.id
    assert processed["schema_valid"]
    assert processed["hash_valid"]
    assert any(
        anomaly["code"] == "batch_content_hash_mismatch"
        for anomaly in report["anomalies"]
    )

    rendered = json.dumps(report, sort_keys=True)
    document = housing_fetch_result().documents[0]
    for forbidden in (
        str(tmp_path),
        document.title,
        document.excerpt,
        document.canonical_url,
        "private-watch-question",
        "private-article-title",
        "private-evidence-statement",
        "private-model-output",
        "private-cloudflare-token",
        "provider-secret",
        "github_pat_",
        "ghp_",
    ):
        assert forbidden not in rendered


async def test_read_only_connection_rejects_writes(tmp_path):
    db_path, _spool_root, _batch = await _delivered_state(tmp_path)
    conn = _connect_read_only(str(db_path))
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE civic_watches SET status = 'paused'")
    finally:
        conn.close()


async def test_report_flags_orphan_missing_and_expired_state(tmp_path):
    db_path, spool_root, batch = await _delivered_state(tmp_path)
    archived = spool_root / "processed" / "accepted" / f"{batch.idempotency_key}.json"
    archived.unlink()
    (spool_root / "outcomes" / "orphan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "idempotency_key": "orphan-outcome-key",
                "payload_hash": "a" * 64,
                "outcome": "accepted",
                "recorded_at": T0.isoformat(),
                "proposal": None,
            }
        ),
        encoding="utf-8",
    )

    first = build_report(str(db_path), str(spool_root), since_hours=48, now=T0)
    first_codes = {anomaly["code"] for anomaly in first["anomalies"]}
    assert "orphan_outcome" in first_codes
    assert "missing_processed_file" in first_codes

    conn = connect(str(db_path))
    old = (T0 - timedelta(hours=72)).isoformat()
    expired = (T0 - timedelta(minutes=1)).isoformat()
    conn.execute(
        "UPDATE civic_runs SET status = 'processing', phase = 'publishing', "
        "claim_token = 'test-claim', lease_expires_at = ?, completed_at = NULL "
        "WHERE id = ?",
        (expired, batch.run_id),
    )
    conn.execute(
        "UPDATE civic_batches SET status = 'delivering', claim_token = 'test-claim', "
        "lease_expires_at = ?, settled_at = NULL, created_at = ?, sealed_at = ? "
        "WHERE id = ?",
        (expired, old, old, batch.id),
    )
    conn.commit()
    conn.close()

    second = build_report(str(db_path), str(spool_root), since_hours=48, now=T0)
    second_codes = {anomaly["code"] for anomaly in second["anomalies"]}
    assert {
        "expired_run_claim",
        "expired_delivering_claim",
        "stuck_batch",
        "missing_inbox_file",
        "delivery_receipt_status_mismatch",
    } <= second_codes


async def test_spool_correlation_uses_names_beyond_detailed_file_limit(tmp_path):
    db_path, spool_root, batch = await _delivered_state(tmp_path)
    processed = spool_root / "processed" / "accepted"
    archived_name = f"{batch.idempotency_key}.json"
    for index in range(MAX_ROWS + 1):
        (processed / f"!{index:04d}.json").touch()

    report = build_report(str(db_path), str(spool_root), since_hours=48, now=T0)

    directory = report["spool"]["directories"]["processed_accepted"]
    assert directory["truncated"]
    assert len(directory["files"]) == MAX_ROWS
    assert archived_name not in {entry["file_name"] for entry in directory["files"]}
    assert not any(
        anomaly["code"] == "missing_processed_file"
        and anomaly.get("batch_id") == batch.id
        for anomaly in report["anomalies"]
    )


async def test_unreadable_spool_directory_is_unknown_not_missing(tmp_path, monkeypatch):
    db_path, spool_root, batch = await _delivered_state(tmp_path)
    unreadable = spool_root / "processed" / "accepted"
    original_iterdir = type(unreadable).iterdir

    def guarded_iterdir(path):
        if path == unreadable:
            raise PermissionError("fixture directory is unreadable")
        return original_iterdir(path)

    monkeypatch.setattr(type(unreadable), "iterdir", guarded_iterdir)

    report = build_report(str(db_path), str(spool_root), since_hours=48, now=T0)

    directory = report["spool"]["directories"]["processed_accepted"]
    assert directory == {
        "exists": None,
        "files": [],
        "error_code": "spool_directory_unreadable",
    }
    unreadable_anomalies = [
        anomaly
        for anomaly in report["anomalies"]
        if anomaly["code"] == "spool_directory_unreadable"
    ]
    assert unreadable_anomalies == [
        {
            "code": "spool_directory_unreadable",
            "location": "processed_accepted",
        }
    ]
    assert not any(
        anomaly["code"] == "missing_processed_file"
        and anomaly.get("batch_id") == batch.id
        for anomaly in report["anomalies"]
    )


async def test_cli_prints_one_json_document_without_paths(tmp_path, capsys):
    db_path, spool_root, _batch = await _delivered_state(tmp_path)

    result = main(
        [
            "--db-path",
            str(db_path),
            "--spool-root",
            str(spool_root),
            "--since-hours",
            "48",
            "--pretty",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["schema_version"] == "partymap.civic.diagnostic.v1"
    assert str(tmp_path) not in output



def test_report_classifies_source_outage_episodes_and_flags_escalations(tmp_path):
    db_path = tmp_path / "outages.db"
    conn = connect(str(db_path))
    init_schema(conn)
    day = timedelta(days=1)
    for source_key, since, last, observed in [
        ("act", T0 - 2 * day, T0, T0),
        ("conservative", T0 - 8 * day, T0, T0),
        ("labour", T0 - 5 * day, T0 - 2 * day, T0),
        ("vision-nz", T0 - 9 * day, T0 - 4 * day, T0 - 4 * day),
    ]:
        conn.execute(
            "INSERT INTO civic_source_outages (source_key, unavailable_since, "
            "last_unavailable_at, observed_at) VALUES (?, ?, ?, ?)",
            (source_key, since.isoformat(), last.isoformat(), observed.isoformat()))
    conn.commit()
    conn.close()

    report = build_report(str(db_path), str(tmp_path / "spool"), since_hours=48, now=T0)

    assert [(outage["source_key"], outage["state"]) for outage in report["source_outages"]] == [
        ("act", "held"), ("conservative", "escalated"),
        ("labour", "recovering"), ("vision-nz", "stale")]
    assert report["source_outages"][0]["escalates_at"] == "2026-08-05T18:30:00Z"
    assert report["summary"]["source_outage_count"] == 4
    outage_anomalies = [
        anomaly for anomaly in report["anomalies"] if anomaly["code"].startswith("source_")]
    assert outage_anomalies == [
        {"code": "source_outage_escalated", "source_key": "conservative"}]


def test_report_tolerates_a_database_without_the_outage_table(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = connect(str(db_path))
    init_schema(conn)
    conn.execute("DROP TABLE civic_source_outages")
    conn.commit()
    conn.close()

    report = build_report(str(db_path), str(tmp_path / "spool"), since_hours=48, now=T0)

    assert report["source_outages"] == []


def test_report_flags_an_outage_table_it_cannot_read(tmp_path):
    db_path = tmp_path / "broken.db"
    conn = connect(str(db_path))
    init_schema(conn)
    conn.execute("DROP TABLE civic_source_outages")
    conn.execute(
        "CREATE TABLE civic_source_outages (source_key TEXT PRIMARY KEY, "
        "unavailable_since TEXT NOT NULL, last_seen_at TEXT NOT NULL)")
    conn.commit()
    conn.close()

    report = build_report(str(db_path), str(tmp_path / "spool"), since_hours=48, now=T0)

    assert report["status"] == "attention"
    assert report["source_outages"] == []
    assert {"code": "source_outage_schema_invalid"} in report["anomalies"]
