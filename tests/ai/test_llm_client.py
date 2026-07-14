# tests/ai/test_llm_client.py
import pytest

pytest.importorskip("copilot")

from unittest.mock import AsyncMock, MagicMock, patch

from beehive.ai.llm_client import run_prompt


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
