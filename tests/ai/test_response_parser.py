# tests/ai/test_response_parser.py
import pytest

from beehive.ai.response_parser import ResponseParseError, parse_ranking_response

_GOOD_RESPONSE = """Here is my analysis.

```json
{
  "ranked": [
    {"id": "1", "score": 91, "summary": "RBNZ 暗示降息", "rationale": "匹配利率"},
    {"id": "2", "score": 22, "summary": "银行app问题", "rationale": "日常问答"}
  ]
}
```
"""


def test_parses_well_formed_response():
    result = parse_ranking_response(_GOOD_RESPONSE, candidate_item_keys=["t1", "t2"])
    assert len(result) == 2
    assert result[0].item_key == "t1"
    assert result[0].score == 91
    assert result[0].summary == "RBNZ 暗示降息"
    assert result[0].rationale == "匹配利率"


def test_id_maps_back_to_the_correct_item_key_by_position():
    # item_keys deliberately don't look like their own position numbers, so a bug that
    # passed the raw "id" through unchanged (instead of resolving it via position) would
    # fail this test.
    result = parse_ranking_response(_GOOD_RESPONSE, candidate_item_keys=["item-a", "item-b"])
    assert result[0].item_key == "item-a"
    assert result[1].item_key == "item-b"


def test_missing_fenced_block_raises():
    with pytest.raises(ResponseParseError, match="no fenced"):
        parse_ranking_response("just prose, no json", candidate_item_keys=["t1"])


def test_missing_id_raises():
    with pytest.raises(ResponseParseError, match="missing"):
        parse_ranking_response(_GOOD_RESPONSE, candidate_item_keys=["t1", "t2", "t3"])


def test_extra_id_raises():
    with pytest.raises(ResponseParseError, match="unexpected"):
        parse_ranking_response(_GOOD_RESPONSE, candidate_item_keys=["t1"])


def test_score_out_of_range_raises():
    bad = _GOOD_RESPONSE.replace('"score": 91', '"score": 150')
    with pytest.raises(ResponseParseError, match="score"):
        parse_ranking_response(bad, candidate_item_keys=["t1", "t2"])


def test_overlong_summary_is_truncated_not_failed():
    bad = _GOOD_RESPONSE.replace('"summary": "RBNZ 暗示降息"',
                                  '"summary": "' + "x" * 400 + '"')
    result = parse_ranking_response(bad, candidate_item_keys=["t1", "t2"])
    assert len(result[0].summary) == 300
