from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.connection import connect, init_schema
from beehive.db.evidence_state import create_evidence_state_revision
from beehive.db.research_runs import (
    claim_research_run,
    enqueue_research_run,
    get_research_run,
)
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import (
    add_snapshot_items,
    create_snapshot,
    seal_snapshot,
)
from beehive.db.research_sources import create_research_source
from beehive.domain.research import (
    EvidenceQuality,
    ResearchRunStatus,
    ResearchSourceOrigin,
    SufficiencyState,
)
from beehive.localization import localizer_for
from beehive.research.orchestrator import RunOutcomeStatus
from beehive.research.synthesis_retry import run_synthesis_retry

T0 = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    init_schema(connection)
    yield connection
    connection.close()


@pytest.mark.asyncio
async def test_synthesis_retry_reuses_latest_revision_without_collection(
    conn,
    monkeypatch,
):
    session_id = create_research_session(conn, "What changed?", T0).id
    source_id = create_research_source(
        conn,
        session_id,
        "rbnz_news",
        {},
        ResearchSourceOrigin.OWNER,
        T0,
    ).id
    original_run = enqueue_research_run(conn, session_id, T0)
    item = upsert_evidence_item(
        conn,
        session_id,
        source_id,
        "item-1",
        "Source title",
        "https://example.com/source",
        EvidenceQuality.PRIMARY,
        T0,
        snippet="Evidence",
    )
    snapshot = create_snapshot(conn, session_id, original_run.id, T0)
    add_snapshot_items(conn, snapshot.id, [item.id], T0)
    seal_snapshot(conn, snapshot.id, T0)
    revision = create_evidence_state_revision(
        conn,
        session_id,
        snapshot.id,
        [item.id],
        T0,
    )
    conn.execute(
        "UPDATE research_runs SET status='completed', completed_at=? WHERE id=?",
        (T0.isoformat(), original_run.id),
    )
    conn.commit()

    retry = enqueue_research_run(conn, session_id, T0, run_kind="synthesis")
    lease = claim_research_run(
        conn,
        retry.id,
        T0,
        lease_seconds=600,
        deadline_seconds=1200,
    )
    captured = {}

    async def fake_generate(
        _conn,
        passed_session_id,
        passed_run_id,
        passed_claim_token,
        _question,
        revision_id,
        sufficiency,
        _localizer,
        _now,
        **_kwargs,
    ):
        captured.update(
            session_id=passed_session_id,
            run_id=passed_run_id,
            claim_token=passed_claim_token,
            revision_id=revision_id,
            sufficiency=sufficiency,
        )
        return SimpleNamespace(id=42)

    monkeypatch.setattr(
        "beehive.research.synthesis_retry.generate_synthesis",
        fake_generate,
    )

    outcome = await run_synthesis_retry(
        conn,
        retry.id,
        lease.run.claim_token,
        session_id,
        "What changed?",
        localizer_for("en"),
        model="gpt-5",
        now_fn=lambda: T0,
    )

    assert captured["revision_id"] == revision.id
    assert captured["sufficiency"] is SufficiencyState.PARTIAL
    assert outcome.status is RunOutcomeStatus.RESUMED_SEALED_SNAPSHOT
    assert outcome.synthesis_id == 42
    assert get_research_run(conn, retry.id).status is ResearchRunStatus.COMPLETED
