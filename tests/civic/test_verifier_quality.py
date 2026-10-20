import pytest

from beehive.ai.model_selection import UnsupportedModelError
from beehive.civic.ai_gateway import require_supported_model, verifier_model_for


@pytest.mark.parametrize(("draft", "verifier"), [
    ("gpt-5.6-terra", "claude-sonnet-5.5"),
    ("gpt-6-astra", "claude-sonnet-5.5"),
    ("claude-opus-5", "gpt-6-astra"),
    ("claude-haiku-4.5", "gpt-6-astra"),
    ("grok-4.7", "claude-sonnet-5.5"),
])
def test_verification_uses_an_explicit_strong_independent_model(draft, verifier):
    assert verifier_model_for(draft) == verifier
    assert require_supported_model(verifier) == verifier
    assert draft.split("-", 1)[0] != verifier.split("-", 1)[0]


def test_unpinned_auto_draft_cannot_claim_a_different_verifier_family():
    with pytest.raises(UnsupportedModelError, match="concrete"):
        verifier_model_for("auto")


@pytest.mark.parametrize(("draft", "override"), [
    ("auto", None),
    ("gpt-5.6-terra", "claude-haiku-4.5"),
    ("gpt-5.6-terra", "gpt-6-astra"),
])
def test_worker_refuses_invalid_verifier_configuration_before_claiming_work(tmp_path, draft, override):
    from beehive.civic.worker import CivicWorker
    from beehive.db.connection import connect, init_schema
    from tests.civic.civic_worker_support import passing_green_gateway

    conn = connect(str(tmp_path / "worker-model.db"))
    init_schema(conn)
    with pytest.raises(UnsupportedModelError):
        CivicWorker(conn, passing_green_gateway(), model=draft, verifier_model=override)
    assert conn.execute("SELECT COUNT(*) FROM civic_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM civic_consumed_items").fetchone()[0] == 0
    conn.close()
