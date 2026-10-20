import pytest

from beehive.civic.novelty import (
    CivicPriorEvent, find_duplicate_event, identify_event, source_newness_issue,
)
from tests.civic.test_novelty import BILL, item


@pytest.mark.parametrize("next_stage", [
    "and now moves to its third reading",
    "and heads to its third reading",
    "ahead of its third reading",
    ", with the third reading set down for October",
])
def test_a_passage_verb_cannot_skip_the_reading_it_actually_describes(next_stage):
    earlier = identify_event(item(
        f"The {BILL} passed its second reading {next_stage}.",
        title="Lifejacket bill progresses through Parliament"))
    assert earlier is not None and earlier.stage == "second-reading"
    later = identify_event(item(
        f"The {BILL} passed its third reading.",
        title="Lifejacket bill passes final reading", day="2026-09-20"))
    assert later is not None
    assert find_duplicate_event(later, [CivicPriorEvent(11, 7, earlier)]) is None


def test_unrelated_new_policy_cannot_license_reannouncement_of_an_existing_policy():
    assert source_newness_issue(
        title="ACT announces smaller government policy",
        summary="ACT announced a policy for a smaller government with 18 ministers and 19 departments.",
        subject_id="act",
        source_texts=(
            "ACT reiterated its existing policy for a smaller government with "
            "18 ministers and 19 departments. "
            "Separately, ACT launched a new policy on school truancy.",
        ),
    ) is not None
