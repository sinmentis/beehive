"""A summary must tell a reader something its title does not.

The pairs below are real published entries: restatements that reached the site, and the
substantive entries the guard must keep passing."""
import pytest

from beehive.civic.candidate_policy import MIN_SUMMARY_NEW_TERMS, summary_new_terms
from beehive.civic.generation import CIVIC_PROMPT_VERSION, build_generation_prompt
from tests.civic.test_civic_ai_contracts import snapshot


@pytest.mark.parametrize("title, summary", [
    ("Alliance Party announces first Trade Policy installment",
     "The Alliance Party announced the first installment of its Trade Policy on 2026-09-28."),
    ("ACT announces classroom discipline policy",
     "On 2026-08-02, ACT announced a classroom discipline policy."),
    ("ACT announces team", "On 2026-09-13, ACT published a statement presenting its team."),
    ("ACT welcomes RSE scheme changes",
     "On 2026-07-28, ACT said it welcomed Government-announced changes to the RSE scheme."),
    ("ACT announces digital finance policy agenda",
     "On 2026-08-27, ACT announced a policy agenda to modernise digital finance rules."),
    ("ACT calls to end reliance on corporate welfare",
     "On 2026-08-10, ACT Leader David Seymour said New Zealand should end its reliance on "
     "corporate welfare."),
    ("ACT proposes a government with 18 ministers and 19 departments",
     "On 2026-09-02, ACT New Zealand announced a policy proposal for a smaller government with "
     "18 ministers and 19 departments."),
])
def test_a_summary_that_restates_its_title_adds_too_little(title, summary):
    assert len(summary_new_terms(title, summary)) < MIN_SUMMARY_NEW_TERMS


@pytest.mark.parametrize("title, summary", [
    ("ACT announces plan to increase supermarket competition",
     "On 2026-09-28, ACT said it would focus on barriers that make it difficult for new "
     "competitors to enter the New Zealand market."),
    ("Alliance Party releases constitutional policy",
     "On 2026-09-26, the Alliance Party released a constitutional policy it said centres Te "
     "Tiriti o Waitangi and supports lowering the threshold for entering Parliament and "
     "processes to recall elected candidates."),
    ("Labour statement on student loan repayment rate",
     "On 2026-08-30, Labour issued a statement about what it said was National's announcement "
     "to reverse an increase in the student loan repayment rate."),
    ("Announced Māori Development Fund investments for 3 Māori enterprises",
     "On 2026-09-04, the New Zealand National Party announced more than $3.4 million in "
     "Government investment through the Māori Development Fund for 3 Māori enterprises."),
])
def test_a_summary_that_adds_substance_passes(title, summary):
    assert len(summary_new_terms(title, summary)) >= MIN_SUMMARY_NEW_TERMS


def test_new_amounts_count_but_dates_and_years_do_not():
    title = "New Zealand National Party publishes Aviation Action Plan update"
    assert summary_new_terms(
        title, "On 2026-09-07, it said 15 of the plan's 25 priorities were done.") >= {"15", "25"}
    assert not summary_new_terms(title, "The update was published on 2026-09-07 in 2026.")
    assert not summary_new_terms(
        "ACT announces team", "On 2026-09-13 ACT announced its team, according to the release.")


def test_the_generation_contract_asks_for_substance_beyond_the_title():
    prompt = build_generation_prompt(snapshot())
    assert "must add what the title does not" in prompt
    assert CIVIC_PROMPT_VERSION == "civic-ai-prompts/6"
    assert "fiscal cost or funding source" in prompt
