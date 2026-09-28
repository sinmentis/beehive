from datetime import datetime, timezone

import pytest

from beehive.ai.model_catalog import (
    BUILTIN_MODELS,
    ListedModel,
    claim_refresh,
    complete_refresh,
    normalize_models,
)
from beehive.ai.model_selection import (
    DEFAULT_MODEL,
    LLM_MODEL_KEY,
    UnsupportedModelError,
    choose_model,
    load_model,
    save_model,
)
from beehive.db import app_state
from beehive.db.connection import connect, init_schema

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "models.db"))
    init_schema(connection)
    return connection


def _refresh(conn, *models):
    claim = claim_refresh(conn, NOW)
    assert claim is not None
    complete_refresh(conn, claim, NOW, [ListedModel(model_id, name) for model_id, name in models])


def test_missing_setting_preserves_existing_default(conn):
    assert load_model(conn) == DEFAULT_MODEL == "claude-haiku-4.5"


def test_builtin_models_are_unique_sorted_and_include_the_default():
    model_ids = [model.model_id for model in BUILTIN_MODELS]
    assert len(model_ids) == len(set(model_ids))
    assert DEFAULT_MODEL in model_ids
    assert model_ids[0] == "auto"
    assert BUILTIN_MODELS == normalize_models(
        ListedModel(model.model_id, model.display_name) for model in BUILTIN_MODELS)


def test_save_model_roundtrips_through_app_state(conn):
    save_model(conn, "gpt-5.6-sol")
    assert app_state.get(conn, LLM_MODEL_KEY) == "gpt-5.6-sol"
    assert load_model(conn) == "gpt-5.6-sol"


def test_unsupported_model_is_rejected_without_writing(conn):
    with pytest.raises(UnsupportedModelError, match="Unsupported LLM model"):
        save_model(conn, "unknown-model")
    assert app_state.get(conn, LLM_MODEL_KEY) is None


def test_invalid_stored_model_logs_and_falls_back(conn, capsys):
    app_state.set(conn, LLM_MODEL_KEY, "retired-model")
    assert load_model(conn) == DEFAULT_MODEL
    warning = capsys.readouterr().out
    assert "retired-model" in warning
    assert DEFAULT_MODEL in warning


def test_the_refreshed_list_decides_what_can_be_saved(conn):
    _refresh(conn, ("claude-haiku-4.5", "Claude Haiku 4.5"), ("brand-new-9", "Brand New 9"))

    save_model(conn, "brand-new-9")
    assert load_model(conn) == "brand-new-9"
    with pytest.raises(UnsupportedModelError):
        save_model(conn, "gpt-5.6-sol")  # built in, but the account no longer offers it


def test_a_saved_model_that_left_the_list_falls_back_to_the_default(conn, capsys):
    save_model(conn, "gpt-5.6-sol")
    _refresh(conn, ("claude-haiku-4.5", "Claude Haiku 4.5"))

    choice = choose_model(conn)
    assert (choice.saved, choice.model_id, choice.unavailable) == (
        "gpt-5.6-sol", DEFAULT_MODEL, "gpt-5.6-sol")
    assert load_model(conn) == DEFAULT_MODEL
    assert "no longer available" in capsys.readouterr().out


def test_without_the_default_on_the_list_auto_stands_in(conn, capsys):
    _refresh(conn, ("auto", "Auto"), ("gpt-6-sol", "GPT-6 Sol"))

    assert load_model(conn) == "auto"
    app_state.set(conn, LLM_MODEL_KEY, "claude-haiku-4.5")
    assert load_model(conn) == "auto"
    assert "'claude-haiku-4.5' is no longer available; using 'auto'" in capsys.readouterr().out


def test_without_the_default_or_auto_the_first_listed_model_stands_in(conn):
    _refresh(conn, ("grok-4.7", "Grok 4.7"), ("gpt-6-sol", "GPT-6 Sol"))

    choice = choose_model(conn)
    assert (choice.model_id, choice.display_name, choice.unavailable) == (
        "gpt-6-sol", "GPT-6 Sol", None)


def test_save_model_writes_in_its_own_transaction(conn):
    conn.execute("INSERT INTO app_state (key, value) VALUES ('pending', 'x')")
    assert conn.in_transaction

    save_model(conn, "gpt-5.6-sol")

    assert not conn.in_transaction
    assert app_state.get(conn, "pending") == "x"
