"""The production Civic model gateway: tool-free, credential-free, and the only one there is.

These tests are about the seam rather than the content. The gateway is the single place a Civic
stage can reach a model, so what has to hold is that it routes through the repository's one
tool-free entry point, that it cannot be handed an endpoint or a credential, and that this
package has not quietly grown a second LLM stack next to `beehive.ai`.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from beehive.civic.ai_gateway import (CIVIC_MODEL_PROVIDER, CivicAiStage, CivicModelError,
                                      CivicModelRequest, CopilotCivicGateway,
                                      require_supported_model, verifier_model_for)

CIVIC_PACKAGE = pathlib.Path("src/beehive/civic")


def _request(**overrides) -> CivicModelRequest:
    kwargs = dict(
        stage=CivicAiStage.GENERATE, prompt="write the entry", model="claude-haiku-4.5")
    kwargs.update(overrides)
    return CivicModelRequest(**kwargs)


def test_the_gateway_calls_the_repository_tool_free_entry_point(monkeypatch):
    """`run_data_only_prompt` is where `available_tools=[]`, permission denial and the SDK
    capability check live. The gateway must not reimplement any of that."""
    seen: dict = {}

    async def fake(prompt, *, model, timeout, **kwargs):
        seen.update({"prompt": prompt, "model": model, "timeout": timeout, "extra": kwargs})
        return "```json\n{}\n```"

    import beehive.ai.llm_client as llm_client

    monkeypatch.setattr(llm_client, "run_data_only_prompt", fake)
    response = asyncio.run(CopilotCivicGateway().complete(_request(timeout=42.0)))

    assert seen["prompt"] == "write the entry"
    assert seen["model"] == "claude-haiku-4.5"
    assert seen["timeout"] == 42.0
    assert seen["extra"] == {}
    assert response.provider == CIVIC_MODEL_PROVIDER
    assert response.model == "claude-haiku-4.5"


def test_an_empty_reply_is_a_model_error(monkeypatch):
    async def fake(prompt, *, model, timeout, **kwargs):
        return "   "

    import beehive.ai.llm_client as llm_client

    monkeypatch.setattr(llm_client, "run_data_only_prompt", fake)
    with pytest.raises(CivicModelError) as exc:
        asyncio.run(CopilotCivicGateway().complete(_request()))
    assert exc.value.error_code == "model_empty_response"


def test_a_timeout_is_a_typed_model_error(monkeypatch):
    async def fake(prompt, *, model, timeout, **kwargs):
        raise TimeoutError("no response")

    import beehive.ai.llm_client as llm_client

    monkeypatch.setattr(llm_client, "run_data_only_prompt", fake)
    with pytest.raises(CivicModelError) as exc:
        asyncio.run(CopilotCivicGateway().complete(_request()))
    assert exc.value.error_code == "model_timeout"


def test_a_provider_failure_is_a_typed_model_error(monkeypatch):
    async def fake(prompt, *, model, timeout, **kwargs):
        raise RuntimeError("Copilot SDK returned no response before going idle")

    import beehive.ai.llm_client as llm_client

    monkeypatch.setattr(llm_client, "run_data_only_prompt", fake)
    with pytest.raises(CivicModelError) as exc:
        asyncio.run(CopilotCivicGateway().complete(_request()))
    assert exc.value.error_code == "model_provider_error"


def test_a_reused_client_is_forwarded_but_changes_nothing_else(monkeypatch):
    seen: dict = {}

    async def fake(prompt, *, model, timeout, client=None):
        seen["client"] = client
        return "ok"

    import beehive.ai.llm_client as llm_client

    monkeypatch.setattr(llm_client, "run_data_only_prompt", fake)
    sentinel = object()
    asyncio.run(CopilotCivicGateway(client=sentinel).complete(_request()))
    assert seen["client"] is sentinel


def test_the_civic_package_never_imports_the_sdk_directly():
    """One file in this repository may import `copilot`, and it is `ai/llm_client.py`. A civic
    module importing it would be a second, ungated LLM stack."""
    offenders = [
        path.name for path in CIVIC_PACKAGE.rglob("*.py")
        if "import copilot" in path.read_text() or "from copilot" in path.read_text()]
    assert offenders == []


def test_the_civic_package_offers_no_tool_permissive_call():
    """`run_prompt` was removed from the repository precisely because there is no trusted-prompt
    tier. Civic prompts embed publisher-controlled article text, so there must be no path here
    that could reintroduce one."""
    for path in CIVIC_PACKAGE.rglob("*.py"):
        source = path.read_text()
        assert "run_prompt(" not in source, path.name
        assert "available_tools" not in source or path.name == "ai_gateway.py", path.name


def test_a_model_id_that_looks_like_an_endpoint_is_refused():
    for candidate in ("https://attacker.test/v1", "file:///etc/passwd", "claude-haiku-4.5 "):
        with pytest.raises(ValueError):
            require_supported_model(candidate)


def test_the_verifier_model_is_deterministic_and_supported():
    verifier = verifier_model_for("claude-haiku-4.5")
    assert require_supported_model(verifier) == verifier
    assert verifier_model_for("claude-haiku-4.5") == verifier


def test_a_request_needs_a_prompt_and_a_positive_timeout():
    with pytest.raises(ValueError, match="prompt"):
        _request(prompt="   ")
    with pytest.raises(ValueError, match="timeout"):
        _request(timeout=0)
