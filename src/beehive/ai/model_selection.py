"""Validated global model selection for every LLM-backed workflow.

Which models exist comes from ai/model_catalog.py: the list last fetched from the Copilot
account, or the built-in list before the first refresh."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from beehive.ai.model_catalog import ModelOption, available_models
from beehive.db import app_state
from beehive.db.connection import write_transaction

DEFAULT_MODEL = "claude-haiku-4.5"
LLM_MODEL_KEY = "llm_model"
# Copilot picks the model itself. The stand-in when the account stops offering DEFAULT_MODEL.
AUTO_MODEL = "auto"


class UnsupportedModelError(ValueError):
    pass


@dataclass(frozen=True)
class ModelChoice:
    """The model AI work uses now, and the one the Owner saved. They differ when the saved
    model has left the list."""
    model_id: str
    saved: str | None
    models: tuple[ModelOption, ...]

    @property
    def unavailable(self) -> str | None:
        """The saved model when it is no longer offered, so AI work uses model_id instead."""
        return self.saved if self.saved is not None and self.saved != self.model_id else None

    @property
    def display_name(self) -> str:
        return next(
            (model.display_name for model in self.models if model.model_id == self.model_id),
            self.model_id,
        )


def fallback_model(models: Sequence[ModelOption]) -> str:
    """What AI work uses when no model is saved, or the saved one left the list: the usual
    default while the account offers it, then Copilot's "auto", then the first listed model."""
    offered = [model.model_id for model in models]
    for candidate in (DEFAULT_MODEL, AUTO_MODEL):
        if candidate in offered:
            return candidate
    return offered[0] if offered else DEFAULT_MODEL


def choose_model(
    conn: sqlite3.Connection, models: Sequence[ModelOption] | None = None,
) -> ModelChoice:
    offered = tuple(available_models(conn) if models is None else models)
    saved = app_state.get(conn, LLM_MODEL_KEY)
    in_list = any(model.model_id == saved for model in offered)
    return ModelChoice(saved if in_list else fallback_model(offered), saved, offered)


def model_for(conn: sqlite3.Connection, model_id: str) -> ModelOption:
    for model in available_models(conn):
        if model.model_id == model_id:
            return model
    raise UnsupportedModelError(f"Unsupported LLM model: {model_id!r}")


def load_model(conn: sqlite3.Connection) -> str:
    choice = choose_model(conn)
    if choice.unavailable is not None:
        print(
            f"[model-selection] stored model {choice.unavailable!r} is no longer available; "
            f"using {choice.model_id!r}"
        )
    return choice.model_id


def save_model(conn: sqlite3.Connection, model_id: str) -> None:
    # One transaction, so a model-list refresh cannot replace the list between the check and
    # the write.
    with write_transaction(conn):
        model_for(conn, model_id)
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (LLM_MODEL_KEY, model_id),
        )
