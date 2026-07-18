# tests/research/test_limits.py
"""Limits are simple constants, but every value the rest of the Research package depends on is
asserted here once so a future edit that accidentally weakens a safety ceiling (e.g. raising
MAX_SOURCES_PER_PLAN to an unbounded number, or dropping the run-duration cap entirely) fails a
test instead of silently shipping."""
from datetime import timedelta

from beehive.research import limits


def test_max_sources_per_plan_is_a_small_positive_bound():
    assert isinstance(limits.MAX_SOURCES_PER_PLAN, int)
    assert 0 < limits.MAX_SOURCES_PER_PLAN <= 20


def test_config_and_text_length_bounds_are_positive_and_reasonable():
    assert 0 < limits.MAX_CONFIG_STRING_LENGTH <= 1000
    assert 0 < limits.MAX_RATIONALE_LENGTH <= 1000
    assert 0 < limits.MAX_PLAN_SUMMARY_LENGTH <= 2000
    assert 0 < limits.MAX_GAP_LENGTH <= 1000


def test_prior_sources_in_prompt_never_exceeds_a_plan_size():
    # A revision prompt must never be asked to echo back more prior sources than a Research
    # Plan is itself allowed to contain.
    assert limits.MAX_PRIOR_SOURCES_IN_PROMPT <= limits.MAX_SOURCES_PER_PLAN


def test_gaps_in_prompt_is_bounded():
    assert 0 < limits.MAX_GAPS_IN_PROMPT <= 50


def test_run_duration_ceiling_is_twenty_minutes():
    assert limits.MAX_RUN_DURATION == timedelta(minutes=20)


def test_deep_fetch_ceiling_is_thirty():
    assert limits.MAX_DEEP_FETCHES_PER_RUN == 30


def test_structured_error_bounds_are_positive():
    assert 0 < limits.MAX_STRUCTURED_ERRORS <= 100
    assert 0 < limits.MAX_ERROR_MESSAGE_LENGTH <= 1000


def test_candidates_and_deep_fetches_per_round_are_bounded():
    assert 0 < limits.MAX_CANDIDATES_PER_SOURCE <= 100
    assert 0 < limits.MAX_DEEP_FETCHES_PER_ROUND <= limits.MAX_DEEP_FETCHES_PER_RUN


def test_evidence_prompt_projection_bounds_are_positive():
    assert 0 < limits.MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT <= 10_000
    assert 0 < limits.MAX_EVIDENCE_ITEMS_IN_SUFFICIENCY_PROMPT <= 200


def test_sufficiency_response_bounds_are_positive():
    assert 0 < limits.MAX_SUB_QUESTIONS_IN_PROMPT <= 50
    assert 0 < limits.MAX_SUB_QUESTION_LENGTH <= 1000
    assert 0 < limits.MAX_CONTRADICTIONS_IN_PROMPT <= 50
    assert 0 < limits.MAX_CONTRADICTION_LENGTH <= 1000


def test_revision_loop_ceilings_are_small_positive_bounds():
    assert 0 < limits.MAX_REVISION_ROUNDS <= 50
    assert 0 < limits.NOVELTY_STOP_ROUNDS <= limits.MAX_REVISION_ROUNDS


def test_synthesis_evidence_prompt_projection_bounds_are_positive():
    assert 0 < limits.MAX_EVIDENCE_ITEMS_IN_SYNTHESIS_PROMPT <= 200
    assert 0 < limits.MAX_EVIDENCE_TEXT_CHARS_IN_SYNTHESIS_PROMPT <= 10_000


def test_synthesis_claim_and_section_bounds_are_small_positive():
    assert 0 < limits.MAX_CLAIMS_PER_SYNTHESIS_SECTION <= 50
    assert 0 < limits.MAX_SYNTHESIS_CLAIM_TEXT_LENGTH <= 2000
    assert 0 < limits.MAX_CITATIONS_PER_SYNTHESIS_CLAIM <= 50


def test_synthesis_reuses_sufficiency_prompt_ceilings_for_gaps_and_contradictions():
    # A synthesis prompt must never be asked to echo back more prior gaps/contradictions than
    # Evidence Sufficiency itself is allowed to produce.
    assert limits.MAX_PRIOR_GAPS_IN_SYNTHESIS_PROMPT == limits.MAX_GAPS_IN_PROMPT
    assert (limits.MAX_PRIOR_CONTRADICTIONS_IN_SYNTHESIS_PROMPT
            == limits.MAX_CONTRADICTIONS_IN_PROMPT)


def test_model_knowledge_bounds_are_small_positive():
    assert 0 < limits.MAX_MODEL_KNOWLEDGE_NOTES <= 20
    assert 0 < limits.MAX_MODEL_KNOWLEDGE_NOTE_LENGTH <= 2000
