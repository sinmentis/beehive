from datetime import date

import pytest

from beehive.civic.novelty import CivicPriorEvent, find_duplicate_event, identify_event, source_newness_issue

BILL = "Maritime Transport (Lifejackets on Recreational Craft) Amendment Bill"


def item(summary, *, title=None, party="national", day="2026-09-02"):
    return {
        "subject": {"id": party}, "event_date": day,
        "locales": {"en": {"title": title or f"National reports on the {BILL}", "summary": summary}},
    }


@pytest.mark.parametrize("text,stage", [
    (f"The {BILL} passed its first reading.", "first-reading"),
    (f"The {BILL} passed its second reading and will face its final reading later.", "second-reading"),
    (f"The {BILL} passed its third reading.", "final-passage"),
    (f"The {BILL} passed its final reading.", "final-passage"),
    (f"The {BILL} had passed and would take effect 28 days after Royal Assent.", "final-passage"),
    (f"The {BILL} received Royal Assent.", "assent"),
    (f"The {BILL} came into force.", "commencement"),
    (f"The {BILL} was introduced in Parliament.", "introduction"),
    (f"Parliament passed the {BILL} today.", "final-passage"),
    (f"Parliament has passed the {BILL}.", "final-passage"),
    (f"MPs voted to pass the {BILL}.", "final-passage"),
    (f"The House gave the {BILL} its third reading.", "final-passage"),
])
def test_legislative_event_stage_is_not_the_article_or_a_future_condition(text, stage):
    assert identify_event(item(text)).stage == stage


def test_real_legislative_stages_are_distinct():
    first = identify_event(item(f"The {BILL} passed its first reading."))
    for text in (
        f"The {BILL} passed its second reading.",
        f"The {BILL} passed its final reading.",
        f"The {BILL} received Royal Assent.",
        f"The {BILL} came into force.",
    ):
        assert find_duplicate_event(identify_event(item(text)), [CivicPriorEvent(1, 1, first)]) is None


@pytest.mark.parametrize("summary", [
    f"The {BILL} will receive Royal Assent.",
    f"The {BILL} would take effect 28 days after Royal Assent.",
    f"The {BILL} is expected to pass its final reading.",
])
def test_future_milestone_is_not_a_completed_event(summary):
    assert identify_event(item(summary)) is None


def test_conditional_commencement_does_not_override_actual_final_passage():
    event = identify_event(item(
        f"The {BILL} passed its third reading. It takes effect 28 days after Royal Assent.",
        title="Boating safety law completed"))
    assert event.stage == "final-passage"


@pytest.mark.parametrize("text", [
    f"Labour will oppose the {BILL} when it passes its third reading next month.",
    f"Unless the House intervenes, the {BILL} passes its third reading on Thursday.",
])
def test_distant_future_or_condition_does_not_create_a_completed_event(text):
    assert identify_event(item(text)) is None


def test_background_introduction_is_not_a_new_introduction_event():
    assert identify_event(item(
        f"The {BILL}, introduced in September, will cost more to administer.",
        title="Government defends lifejacket costs")) is None


def test_future_calendar_commencement_is_not_the_current_event():
    event = identify_event(item(
        f"The {BILL} passed its final reading. It takes effect on 2026-10-01."))
    assert event.stage == "final-passage"


def test_background_relative_clause_is_not_a_new_reading():
    assert identify_event(item(
        f"The {BILL}, which passed its third reading earlier, now faces a legal challenge.",
        title="New legal challenge over boating safety")) is None


def test_leading_capitalized_context_does_not_change_formal_bill_identity():
    first = identify_event(item(f"The {BILL} passed its final reading."))
    second = identify_event(item(
        f"Since the {BILL} passed its final reading, agencies have prepared implementation details.",
        title=f"{BILL} passes final reading"))
    assert first is not None and second is not None
    assert second.entity == first.entity


def test_explicit_announcement_is_not_negated_by_routine_commitment_boilerplate():
    assert source_newness_issue(
        title="National announces classroom plan",
        summary="National announced a plan to build 18 new classrooms in Northland.",
        source_texts=(
            "National announced a plan to build 18 new classrooms in Northland. "
            "National remains committed to lifting classroom capacity in Northland.",
        ),
    ) is None


def test_committee_followup_is_not_a_repeated_historical_reading():
    assert identify_event(item(
        f"The committee released its report on the {BILL}, which passed its first reading earlier.",
        title="New committee report on boating safety")) is None


def test_ambiguous_bill_reference_is_not_guessed_from_shared_topic():
    current = item("The maritime transport bill passed.", title="Transport legislation update")
    sources = (
        f"The {BILL} was discussed.",
        "The Maritime Transport (Commercial Crew Licensing) Amendment Bill was also discussed.",
    )
    assert identify_event(current, source_texts=sources) is None


def test_similar_topic_and_numbers_do_not_identify_another_named_bill():
    earlier = identify_event(item(f"The {BILL} passed its final reading."))
    other_bill = "Maritime Transport (Commercial Crew Licensing) Amendment Bill"
    current = identify_event(item(
        f"The {other_bill} passed its final reading.", title=f"{other_bill} passes"))
    assert find_duplicate_event(current, [CivicPriorEvent(1, 1, earlier)]) is None


def test_bill_number_is_part_of_identity():
    first = identify_event(item(
        f"The {BILL} (No. 1) passed its final reading.", title=f"{BILL} (No. 1) passes"))
    second = identify_event(item(
        f"The {BILL} (No. 2) passed its final reading.", title=f"{BILL} (No. 2) passes"))
    assert first is not None and second is not None
    assert first.entity != second.entity
    assert find_duplicate_event(second, [CivicPriorEvent(1, 1, first)]) is None


def policy(*, party="act", ministers=18, departments=19, extra="", title=None):
    return item(
        f"ACT proposed a smaller government with {ministers} ministers and {departments} departments. {extra}",
        title=title or f"ACT proposes {ministers} ministers and {departments} departments",
        party=party)


def test_policy_identity_needs_targets_relationship_party_and_scope():
    earlier = identify_event(policy())
    assert earlier is not None
    assert earlier.event_date == date(2026, 9, 2)
    history = [CivicPriorEvent(1, 1, earlier)]
    assert find_duplicate_event(identify_event(policy()), history) is not None
    assert find_duplicate_event(identify_event(policy(party="national")), history) is None
    assert find_duplicate_event(identify_event(policy(ministers=20)), history) is None
    assert find_duplicate_event(identify_event(policy(extra="The target applies by 2027.")), history) is None


def test_single_shared_number_or_broad_political_topic_is_not_an_identity():
    assert identify_event(item(
        "ACT proposed cutting government costs by 18 percent.",
        title="ACT government reform policy", party="act")) is None


def test_same_targets_with_a_real_policy_revision_are_not_blindly_merged():
    current = policy(extra="ACT revised the policy with new implementation details.")
    assert identify_event(current) is None


def test_reiteration_is_not_supported_as_a_new_announcement():
    assert source_newness_issue(
        title="Party announces a government reform policy",
        summary="The party announced a policy for a smaller government.",
        source_texts=("The party reiterated its existing policy for a smaller government.",),
    ) is not None


def test_explicit_new_policy_or_honest_reiteration_is_not_a_false_positive():
    assert source_newness_issue(
        title="Party announces a new policy",
        summary="The party announced a new policy.",
        source_texts=("The party reaffirmed an existing position and announced a new policy today.",),
    ) is None


def test_repeat_offenders_are_not_a_policy_restatement_cue():
    assert source_newness_issue(
        title="Party announces sentencing policy",
        summary="The party announced a sentencing policy for repeat offenders.",
        source_texts=("The party announced a sentencing policy for repeat offenders.",),
    ) is None


def test_unrelated_existing_policy_does_not_negate_a_new_development():
    assert source_newness_issue(
        title="Party announces a hospital building policy",
        summary="The party announced a hospital building policy.",
        source_texts=(
            "The party reiterated its existing transport policy. "
            "It announced a hospital building policy.",
        ),
    ) is None
    assert source_newness_issue(
        title="Party reiterates its existing policy",
        summary="The party reiterated its existing policy.",
        source_texts=("The party reiterated its existing policy.",),
    ) is None
