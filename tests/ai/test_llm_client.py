# tests/ai/test_llm_client.py
import pytest

pytest.importorskip("copilot")

from unittest.mock import AsyncMock, MagicMock, patch

from beehive.ai.llm_client import (
    _deny_all_permissions,
    _reject_user_input,
    _require_tool_free_capability,
    run_data_only_prompt,
    run_prompt,
)


@pytest.mark.asyncio
async def test_run_prompt_returns_response_content():
    from copilot.session_events import AssistantMessageData

    fake_response = MagicMock()
    fake_response.data = AssistantMessageData(content="42", message_id="m1")
    fake_session = AsyncMock()
    fake_session.send_and_wait.return_value = fake_response
    fake_client = AsyncMock()
    fake_client.create_session.return_value = fake_session

    with patch("copilot.CopilotClient", return_value=fake_client):
        result = await run_prompt("what is 6*7", model="claude-haiku-4.5", timeout=30.0)

    assert result == "42"
    fake_client.start.assert_awaited_once()
    fake_client.stop.assert_awaited_once()
    fake_session.send_and_wait.assert_awaited_once_with("what is 6*7", timeout=30.0)


@pytest.mark.asyncio
async def test_run_prompt_raises_on_none_response():
    fake_session = AsyncMock()
    fake_session.send_and_wait.return_value = None
    fake_client = AsyncMock()
    fake_client.create_session.return_value = fake_session

    with patch("copilot.CopilotClient", return_value=fake_client):
        with pytest.raises(RuntimeError, match="no response"):
            await run_prompt("x", model="claude-haiku-4.5")
    fake_client.stop.assert_awaited_once()


# ============================================================================
# run_data_only_prompt: the tool-free entry point for prompts holding untrusted text
# ============================================================================

class _FakeCopilotClient:
    """Stands in for `copilot.CopilotClient` in tests, with a real `create_session` method
    (not a bare MagicMock) so `_require_tool_free_capability`'s `inspect.signature` check
    exercises the actual parameter list -- including `available_tools` -- instead of being
    bypassed by a Mock's auto-generated attributes. `next_session` is set by each test before
    the client is constructed; `create_session` returns it once instantiated."""
    next_session: object = None
    last_instance: "_FakeCopilotClient | None" = None

    def __init__(self):
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.create_session = AsyncMock(return_value=_FakeCopilotClient.next_session)
        _FakeCopilotClient.last_instance = self

    async def create_session(self, *, model=None, available_tools=None,
                              on_permission_request=None, on_user_input_request=None):
        raise NotImplementedError  # shadowed per-instance in __init__


@pytest.mark.asyncio
async def test_run_data_only_prompt_returns_response_content():
    from copilot.session_events import AssistantMessageData

    fake_response = MagicMock()
    fake_response.data = AssistantMessageData(content="ok", message_id="m1")
    fake_session = AsyncMock()
    fake_session.send_and_wait.return_value = fake_response
    _FakeCopilotClient.next_session = fake_session

    with patch("copilot.CopilotClient", _FakeCopilotClient) as client_cls:
        result = await run_data_only_prompt("summarize this", model="claude-haiku-4.5",
                                             timeout=30.0)

    assert result == "ok"
    client_cls.last_instance.start.assert_awaited_once()
    client_cls.last_instance.stop.assert_awaited_once()
    fake_session.send_and_wait.assert_awaited_once_with("summarize this", timeout=30.0)


@pytest.mark.asyncio
async def test_run_data_only_prompt_creates_session_with_empty_tool_allowlist():
    """available_tools=[] is what the SDK docs say takes precedence over every other tool
    source (built-in, MCP, custom) and, empty, leaves nothing available -- this is the actual
    tool-free guarantee, not just a permission-handler convention."""
    from copilot.session_events import AssistantMessageData

    fake_response = MagicMock()
    fake_response.data = AssistantMessageData(content="ok", message_id="m1")
    fake_session = AsyncMock()
    fake_session.send_and_wait.return_value = fake_response
    _FakeCopilotClient.next_session = fake_session

    with patch("copilot.CopilotClient", _FakeCopilotClient) as client_cls:
        await run_data_only_prompt("x", model="claude-haiku-4.5")

    _, kwargs = client_cls.last_instance.create_session.call_args
    assert kwargs["available_tools"] == []
    assert kwargs["on_permission_request"] is not None
    assert kwargs["on_user_input_request"] is not None


@pytest.mark.asyncio
async def test_run_data_only_prompt_raises_on_none_response():
    fake_session = AsyncMock()
    fake_session.send_and_wait.return_value = None
    _FakeCopilotClient.next_session = fake_session

    with patch("copilot.CopilotClient", _FakeCopilotClient):
        with pytest.raises(RuntimeError, match="no response"):
            await run_data_only_prompt("x", model="claude-haiku-4.5")


def test_deny_all_permissions_rejects_rather_than_approves():
    from copilot.rpc import PermissionDecisionReject

    result = _deny_all_permissions(object(), {"session_id": "s1"})

    assert isinstance(result, PermissionDecisionReject)
    assert result.kind == "reject"


def test_reject_user_input_raises_instead_of_answering():
    """Belt-and-braces: with available_tools=[], the ask_user tool (a builtin tool per the
    SDK's BUILTIN_TOOLS_ISOLATED list) should never be reachable at all, so this handler
    should never fire in practice -- but if it ever does, it must fail loudly rather than
    quietly answering on the user's behalf like run_prompt's `_decline_user_input` does."""
    with pytest.raises(RuntimeError, match="tool-free"):
        _reject_user_input({"question": "proceed?"}, {"session_id": "s1"})


def test_require_tool_free_capability_accepts_the_installed_sdk():
    """Regression guard: confirms the verified mechanism (available_tools=...) still exists
    on the installed github-copilot-sdk's CopilotClient.create_session."""
    from copilot import CopilotClient

    _require_tool_free_capability(CopilotClient)  # must not raise


def test_require_tool_free_capability_fails_fast_when_sdk_lacks_available_tools():
    class _SDKWithoutToolFilter:
        def create_session(self, *, model=None, on_permission_request=None):
            raise AssertionError("should never be called")

    with pytest.raises(RuntimeError, match="available_tools"):
        _require_tool_free_capability(_SDKWithoutToolFilter)


@pytest.mark.asyncio
async def test_run_data_only_prompt_never_falls_back_to_approve_all_when_sdk_is_incapable():
    """If the installed SDK can't express a tool-free session, run_data_only_prompt must fail
    explicitly instead of silently starting a tool-permissive session (e.g. approve_all)."""
    class _SDKWithoutToolFilter:
        def __init__(self):
            raise AssertionError(
                "CopilotClient must never be instantiated once the tool-free capability "
                "check has failed")

        def create_session(self, *, model=None, on_permission_request=None):
            raise AssertionError("should never be called")

    with patch("copilot.CopilotClient", _SDKWithoutToolFilter):
        with pytest.raises(RuntimeError, match="available_tools"):
            await run_data_only_prompt("x", model="claude-haiku-4.5")
