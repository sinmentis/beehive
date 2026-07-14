# tests/ai/test_ranker.py
from unittest.mock import AsyncMock, patch

import pytest

from beehive.ai.prompt_builder import ItemCandidate
from beehive.ai.ranker import rank_channel

_FAKE_RESPONSE = """```json
{"ranked": [{"id": "1", "score": 91, "summary": "s", "rationale": "r"}]}
```"""


@pytest.mark.asyncio
async def test_rank_channel_builds_prompt_calls_llm_and_parses():
    candidates = [ItemCandidate(item_key="t1", title="Rates fall", body="",
                                 score=100, num_comments=20)]
    with patch("beehive.ai.ranker.run_prompt", new=AsyncMock(return_value=_FAKE_RESPONSE)) as mock_run:
        result = await rank_channel(profile="economic news", votes=[], candidates=candidates)

    assert len(result) == 1
    assert result[0].item_key == "t1"
    assert result[0].score == 91
    mock_run.assert_awaited_once()
    called_prompt = mock_run.await_args.args[0]
    assert "economic news" in called_prompt
    assert "Rates fall" in called_prompt


@pytest.mark.asyncio
async def test_rank_channel_passes_model_through():
    candidates = [ItemCandidate(item_key="t1", title="x", body="", score=1, num_comments=0)]
    with patch("beehive.ai.ranker.run_prompt", new=AsyncMock(return_value=_FAKE_RESPONSE)) as mock_run:
        await rank_channel(profile="p", votes=[], candidates=candidates, model="claude-opus-4.8")
    assert mock_run.await_args.kwargs["model"] == "claude-opus-4.8"
