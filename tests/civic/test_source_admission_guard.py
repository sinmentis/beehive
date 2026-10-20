import pytest

from beehive.civic import source_extraction
from beehive.civic.ai_pipeline import generate_civic_candidate
from beehive.civic.destination import (
    DESTINATION_V1_ENGINE_VERSION, constrain_qualification_to_destination_v1,
)
from beehive.civic.pipeline import qualify_civic_candidate
from beehive.civic.production import build_ownership_registry, construct_candidate_claim
from beehive.civic.publication import preflight_candidate
from beehive.db.civic_batches import list_sealed_candidate_ids, seal_batch_if_claimed
from beehive.db.civic_candidates import get_candidate
from beehive.db.civic_evidence_captures import CivicSourceRefreshBlocked
from beehive.db.connection import connect, init_schema
from tests.civic.civic_worker_support import create_partymap_watch
from tests.civic.test_publication_novelty import (
    T0, claimed_batch, gateway_for, generated_candidate,
)


async def prepared_candidate(conn, watch, run, batch, key):
    return await generated_candidate(
        conn, watch, run, batch, key=key,
        source_title="National announces a school plan",
        source_text="National announced a school plan.",
        title="National announces a school plan",
        summary="National announced a school plan.")


@pytest.mark.parametrize("entrypoint", ["qualification", "ai", "preflight"])
async def test_stale_source_proof_cannot_be_reused_by_direct_entrypoints(
    tmp_path, monkeypatch, entrypoint,
):
    conn = connect(str(tmp_path / f"source-guard-{entrypoint}.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="policy")
    run, batch = claimed_batch(conn, watch)
    candidate = await prepared_candidate(conn, watch, run, batch, "old-capture")
    if entrypoint == "ai":
        conn.execute(
            "UPDATE civic_candidates SET status='verifying', settled_at=NULL WHERE id=?",
            (candidate.id,))
        conn.commit()
    monkeypatch.setattr(source_extraction, "SOURCE_CAPTURE_VERSION", 2)
    gateway = gateway_for("National announces a school plan", "National announced a school plan.")
    with pytest.raises(CivicSourceRefreshBlocked):
        if entrypoint == "qualification":
            qualify_civic_candidate(
                conn, run_id=run.id, claim_token=run.claim_token,
                claim=construct_candidate_claim(conn, candidate),
                registry=build_ownership_registry(), now=T0,
                engine_version=DESTINATION_V1_ENGINE_VERSION,
                qualification_scope=constrain_qualification_to_destination_v1)
        elif entrypoint == "ai":
            await generate_civic_candidate(
                conn, run_id=run.id, claim_token=run.claim_token,
                candidate_id=candidate.id, gateway=gateway, now=T0)
        else:
            preflight_candidate(conn, candidate.id)
    assert gateway.calls == []
    conn.close()


async def test_final_seal_holds_stale_source_without_spending_ai_revision_or_blocking_neighbour(
    tmp_path, monkeypatch,
):
    conn = connect(str(tmp_path / "source-seal.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="policy")
    run, batch = claimed_batch(conn, watch)
    stale = await prepared_candidate(conn, watch, run, batch, "old-capture")
    monkeypatch.setattr(source_extraction, "SOURCE_CAPTURE_VERSION", 2)
    healthy = await prepared_candidate(conn, watch, run, batch, "current-capture")
    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)
    assert sealed is not None
    assert list_sealed_candidate_ids(conn, batch.id) == [healthy.id]
    held = get_candidate(conn, stale.id)
    assert held.status.value == "deferred_ai"
    assert held.rejection_code is None
    assert held.ai_revision == stale.ai_revision
    conn.close()
