# tests/ai/test_structured_response_reexport.py
"""The strict structured-JSON primitives now live in beehive.ai.structured_response, with
beehive.research.structured_response as a re-export shim.

tests/research/test_structured_response.py already covers the parsing behavior itself through the
Research import path. What matters here is that the move did not fork the two paths: they must
resolve to the SAME objects, so `except StructuredResponseError` written against either import
still catches an error raised through the other.
"""
import pytest

from beehive.ai import structured_response as ai_module
from beehive.research import structured_response as research_module

_SHARED_NAMES = [
    "StructuredResponseError",
    "bounded_string_list",
    "extract_fenced_json_object",
    "require_bool",
    "require_dict",
    "require_exact_keys",
    "require_list",
    "require_string",
]


@pytest.mark.parametrize("name", _SHARED_NAMES)
def test_research_reexports_the_same_object_not_a_copy(name):
    assert getattr(research_module, name) is getattr(ai_module, name)


def test_error_raised_through_ai_path_is_caught_through_research_path():
    with pytest.raises(research_module.StructuredResponseError):
        ai_module.extract_fenced_json_object("no fence here", context="test")


def test_error_raised_through_research_path_is_caught_through_ai_path():
    with pytest.raises(ai_module.StructuredResponseError):
        research_module.extract_fenced_json_object("no fence here", context="test")


def test_structured_response_error_is_still_a_valueerror():
    """Callers that only catch ValueError (the base every other repository error uses) must keep
    working across the move."""
    assert issubclass(ai_module.StructuredResponseError, ValueError)


def test_ai_module_parses_a_fenced_object_identically():
    text = 'prose\n```json\n{"a": 1}\n```\nmore prose'
    assert ai_module.extract_fenced_json_object(text, context="test") == {"a": 1}
    assert research_module.extract_fenced_json_object(text, context="test") == {"a": 1}


def test_research_shim_exports_exactly_the_shared_names():
    assert sorted(research_module.__all__) == sorted(_SHARED_NAMES)
