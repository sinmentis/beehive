# tests/ai/test_summary_rewrite.py
from unittest.mock import AsyncMock, patch

import pytest

from beehive.ai.summary_rewrite import (RewriteItemContext, RewrittenSummary,
                                         SummaryRewriteParseError,
                                         build_summary_rewrite_prompt,
                                         parse_summary_rewrite_response, rewrite_item_summary)
from beehive.localization import localizer_for

_EN = localizer_for("en").language
_CONTEXT = RewriteItemContext(title="Rates fall", body="The central bank cut rates by 25bps.",
                               source_type="reddit_subreddit", source_name=None)


def _good_response(item_id: int = 42, summary: str = "The central bank cut rates by 25bps.") -> str:
    return f"""```json
{{
  "item_id": {item_id},
  "summary": "{summary}"
}}
```"""


# ============================================================================
# build_summary_rewrite_prompt
# ============================================================================

def test_prompt_includes_title_body_and_source():
    context = RewriteItemContext(title="Rates fall", body="body text",
                                  source_type="reddit_subreddit", source_name=None)
    prompt = build_summary_rewrite_prompt(1, context, _EN)
    assert "Rates fall" in prompt
    assert "body text" in prompt
    assert "reddit_subreddit" in prompt


def test_prompt_includes_source_name_when_present():
    context = RewriteItemContext(title="t", body="b", source_type="google_news_query",
                                  source_name="Example Wire")
    prompt = build_summary_rewrite_prompt(1, context, _EN)
    assert "Example Wire (google_news_query)" in prompt


def test_prompt_includes_the_selected_language():
    japanese = localizer_for("ja").language
    prompt = build_summary_rewrite_prompt(1, _CONTEXT, japanese)
    assert japanese.llm_name in prompt


def test_prompt_echoes_the_exact_item_id():
    prompt = build_summary_rewrite_prompt(123, _CONTEXT, _EN)
    assert "123" in prompt


def test_prompt_output_schema_only_requests_item_id_and_summary():
    """The prompt may explicitly tell the model NOT to reproduce score/rationale, but the
    OUTPUT json shape itself must only ask for item_id + summary -- never a score/rationale
    field the model could invent a new value for."""
    prompt = build_summary_rewrite_prompt(1, _CONTEXT, _EN)
    output_block = prompt.split("=== OUTPUT ===")[1]
    fenced_json = output_block.split("```json")[1].split("```")[0]
    assert '"item_id"' in fenced_json
    assert '"summary"' in fenced_json
    assert "score" not in fenced_json.lower()
    assert "rationale" not in fenced_json.lower()


def test_prompt_states_the_injection_guard():
    prompt = build_summary_rewrite_prompt(1, _CONTEXT, _EN)
    assert "untrusted" in prompt.lower()
    assert "ignore all previous instructions" in prompt.lower()


# ============================================================================
# parse_summary_rewrite_response
# ============================================================================

def test_parse_returns_the_matching_item_id_and_summary():
    result = parse_summary_rewrite_response(_good_response(42, "Rates cut 25bps."), 42)
    assert result == RewrittenSummary(item_id=42, summary="Rates cut 25bps.")


def test_parse_raises_on_missing_fenced_block():
    with pytest.raises(SummaryRewriteParseError, match="no fenced"):
        parse_summary_rewrite_response("no json here", 42)


def test_parse_raises_on_invalid_json():
    with pytest.raises(SummaryRewriteParseError, match="not valid JSON"):
        parse_summary_rewrite_response("```json\n{not json\n```", 42)


def test_parse_raises_when_item_id_does_not_match():
    with pytest.raises(SummaryRewriteParseError, match="does not match"):
        parse_summary_rewrite_response(_good_response(99), 42)


def test_parse_raises_on_missing_summary():
    response = """```json
{"item_id": 42}
```"""
    with pytest.raises(SummaryRewriteParseError, match="summary"):
        parse_summary_rewrite_response(response, 42)


def test_parse_raises_on_empty_summary():
    response = """```json
{"item_id": 42, "summary": "   "}
```"""
    with pytest.raises(SummaryRewriteParseError, match="summary"):
        parse_summary_rewrite_response(response, 42)


def test_parse_truncates_overlong_summary_rather_than_failing():
    long_summary = "x" * 400
    response = f'```json\n{{"item_id": 42, "summary": "{long_summary}"}}\n```'
    result = parse_summary_rewrite_response(response, 42)
    assert len(result.summary) == 300


# ============================================================================
# rewrite_item_summary: orchestration, tool-free entry point
# ============================================================================

@pytest.mark.asyncio
async def test_rewrite_item_summary_calls_run_data_only_prompt_and_parses():
    with patch("beehive.ai.summary_rewrite.run_data_only_prompt",
               new=AsyncMock(return_value=_good_response(7, "New finding stated first."))
               ) as mock_run:
        result = await rewrite_item_summary(7, _CONTEXT, _EN)

    assert result == RewrittenSummary(item_id=7, summary="New finding stated first.")
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_rewrite_item_summary_uses_the_tool_free_entry_point_not_run_prompt():
    """This module must never call the tool-permissive run_prompt -- title/body are
    machine-fetched, untrusted content, same trust boundary as deep_read/summarize.py."""
    with patch("beehive.ai.summary_rewrite.run_data_only_prompt",
               new=AsyncMock(return_value=_good_response(1))) as mock_run:
        with patch("beehive.ai.summary_rewrite.run_prompt", create=True) as mock_run_prompt:
            await rewrite_item_summary(1, _CONTEXT, _EN)
    mock_run.assert_awaited_once()
    mock_run_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_rewrite_item_summary_passes_model_through():
    with patch("beehive.ai.summary_rewrite.run_data_only_prompt",
               new=AsyncMock(return_value=_good_response(1))) as mock_run:
        await rewrite_item_summary(1, _CONTEXT, _EN, model="claude-opus-4.8")
    assert mock_run.await_args.kwargs["model"] == "claude-opus-4.8"


@pytest.mark.asyncio
async def test_rewrite_item_summary_raises_parse_error_on_mismatched_id():
    with patch("beehive.ai.summary_rewrite.run_data_only_prompt",
               new=AsyncMock(return_value=_good_response(999))):
        with pytest.raises(SummaryRewriteParseError):
            await rewrite_item_summary(1, _CONTEXT, _EN)
