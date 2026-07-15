"""Validated global model selection for every LLM-backed workflow."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from beehive.db import app_state

DEFAULT_MODEL = "claude-haiku-4.5"
LLM_MODEL_KEY = "llm_model"


class UnsupportedModelError(ValueError):
    pass


@dataclass(frozen=True)
class ModelOption:
    model_id: str
    display_name: str


# The web container intentionally has no Copilot token, so it cannot query models dynamically.
# Keep this allowlist aligned with CopilotClient.list_models() from the deployed SDK account.
SUPPORTED_MODELS = (
    ModelOption("auto", "Auto"),
    ModelOption("claude-sonnet-5", "Claude Sonnet 5"),
    ModelOption("claude-sonnet-4.6", "Claude Sonnet 4.6"),
    ModelOption("claude-sonnet-4.5", "Claude Sonnet 4.5"),
    ModelOption("claude-haiku-4.5", "Claude Haiku 4.5"),
    ModelOption("claude-opus-4.8", "Claude Opus 4.8"),
    ModelOption("claude-opus-4.7", "Claude Opus 4.7"),
    ModelOption("claude-opus-4.6", "Claude Opus 4.6"),
    ModelOption("claude-opus-4.5", "Claude Opus 4.5"),
    ModelOption("gpt-5.6-sol", "GPT-5.6 Sol"),
    ModelOption("gpt-5.6-terra", "GPT-5.6 Terra"),
    ModelOption("gpt-5.6-luna", "GPT-5.6 Luna"),
    ModelOption("gpt-5.5", "GPT-5.5"),
    ModelOption("gpt-5.4", "GPT-5.4"),
    ModelOption("gpt-5.3-codex", "GPT-5.3-Codex"),
    ModelOption("gpt-5.4-mini", "GPT-5.4 mini"),
    ModelOption("gpt-5-mini", "GPT-5 mini"),
    ModelOption("gemini-3.1-pro-preview", "Gemini 3.1 Pro"),
    ModelOption("gemini-3.5-flash", "Gemini 3.5 Flash"),
    ModelOption("mai-code-1-flash-picker", "MAI-Code-1-Flash"),
)
_MODELS_BY_ID = {model.model_id: model for model in SUPPORTED_MODELS}


def model_for(model_id: str) -> ModelOption:
    try:
        return _MODELS_BY_ID[model_id]
    except KeyError as exc:
        raise UnsupportedModelError(f"Unsupported LLM model: {model_id!r}") from exc


def load_model(conn: sqlite3.Connection) -> str:
    model_id = app_state.get(conn, LLM_MODEL_KEY, default=DEFAULT_MODEL)
    try:
        model_for(model_id)
    except UnsupportedModelError:
        print(
            f"[model-selection] stored model {model_id!r} is no longer supported; "
            f"using default {DEFAULT_MODEL!r}"
        )
        return DEFAULT_MODEL
    return model_id


def save_model(conn: sqlite3.Connection, model_id: str) -> None:
    model_for(model_id)
    app_state.set(conn, LLM_MODEL_KEY, model_id)
