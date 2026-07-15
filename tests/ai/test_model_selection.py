import pytest

from beehive.ai.model_selection import (
    DEFAULT_MODEL,
    LLM_MODEL_KEY,
    SUPPORTED_MODELS,
    UnsupportedModelError,
    load_model,
    save_model,
)
from beehive.db import app_state
from beehive.db.connection import connect, init_schema


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "models.db"))
    init_schema(connection)
    return connection


def test_missing_setting_preserves_existing_default(conn):
    assert load_model(conn) == DEFAULT_MODEL == "claude-haiku-4.5"


def test_supported_models_are_unique_and_include_current_sdk_choices():
    model_ids = [model.model_id for model in SUPPORTED_MODELS]
    assert len(model_ids) == len(set(model_ids))
    assert DEFAULT_MODEL in model_ids
    assert {"auto", "claude-sonnet-5", "gpt-5.6-sol", "gemini-3.5-flash"} <= set(model_ids)


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
