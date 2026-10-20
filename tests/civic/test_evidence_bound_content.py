from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from beehive.civic.candidate_parser import CivicDraftClaim, CivicPolicyStatus
from beehive.civic.candidate_policy import (
    CivicContentError, check_candidate_content, check_locale_content,
)
from beehive.civic.generation import (
    _source_event_date,
    build_candidate_snapshot,
    build_generation_prompt,
    build_verification_prompt,
    is_citable,
)
from beehive.civic.limits import CivicLocale
from tests.civic.test_civic_ai_contracts import _evidence_row, alias, content, snapshot


def test_uncited_document_cannot_license_a_number_in_a_claim():
    first = replace(alias(), excerpt="The party announced 4 new clinics.")
    second = replace(alias("e2", "src-2"), excerpt="The newsletter mentions 97 unrelated offices.")
    evidence = snapshot(aliases=(first, second), numeric_tokens=("4", "97"))
    invented = content(
        title="New clinics",
        summary="The party announced 97 clinics.",
        claims=(CivicDraftClaim("The party announced 97 clinics.", ("e1",)),),
    )
    with pytest.raises(CivicContentError, match="number_not_in_evidence"):
        check_locale_content(invented, evidence)


def test_another_claims_citation_cannot_license_this_claims_number():
    first = replace(alias(), excerpt="The party announced 4 clinics.")
    second = replace(alias("e2", "src-2"), excerpt="The party announced 97 offices.")
    evidence = snapshot(aliases=(first, second), numeric_tokens=("4", "97"))
    invented = content(
        title="New facilities",
        summary="The party announced 4 clinics and 97 offices.",
        claims=(
            CivicDraftClaim("The party announced 97 clinics.", ("e1",)),
            CivicDraftClaim("The party announced 97 offices.", ("e2",)),
        ),
    )
    with pytest.raises(CivicContentError, match="number_not_in_evidence"):
        check_locale_content(invented, evidence)


def test_earlier_cluster_date_needs_a_cited_date_bearing_source():
    early = replace(alias(), event_date=date(2026, 8, 1))
    later = replace(alias("e2", "src-2"), event_date=date(2026, 8, 7))
    evidence = snapshot(aliases=(early, later))
    unsupported_date = content(
        claims=(CivicDraftClaim("The bill passed its second reading.", ("e2",)),),
    )
    with pytest.raises(CivicContentError, match="event_date_drift"):
        check_locale_content(unsupported_date, evidence)


def test_a_cited_date_bearing_source_preserves_valid_multi_source_records():
    early = replace(alias(), event_date=date(2026, 8, 1))
    later = replace(alias("e2", "src-2"), event_date=date(2026, 8, 7))
    evidence = snapshot(aliases=(early, later))
    supported = content(
        claims=(CivicDraftClaim("The bill passed its second reading.", ("e1", "e2")),),
    )
    check_locale_content(supported, evidence)


def test_both_prompts_require_visible_copy_to_preserve_source_semantics():
    evidence = snapshot()
    texts = {locale: content(locale) for locale in CivicLocale}
    for prompt in (build_generation_prompt(evidence), build_verification_prompt(evidence, texts)):
        assert "total versus annual" in prompt
        assert "South Island" in prompt
        assert "publication date is not proof" in prompt
        assert "title and summary" in prompt


def test_party_alias_keeps_its_own_date_instead_of_an_earlier_cluster_date():
    source = _evidence_row(
        event_date=date(2026, 8, 17),
        published_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
    )
    assert _source_event_date(source) == date(2026, 8, 23)


def test_source_publication_date_is_interpreted_in_new_zealand_time():
    source = _evidence_row(
        published_at=datetime(2026, 8, 26, 22, 32, tzinfo=timezone.utc),
    )
    assert _source_event_date(source) == date(2026, 8, 27)


def test_snapshot_hash_covers_the_publication_date_and_publisher_shown_to_the_model():
    base = snapshot(aliases=(alias(),))
    assert base.snapshot_hash != snapshot(
        aliases=(replace(alias(), published_at="2026-08-07"),),
    ).snapshot_hash
    assert base.snapshot_hash != snapshot(
        aliases=(replace(alias(), publisher="Different source"),),
    ).snapshot_hash


@pytest.mark.parametrize("url", [
    "https://", "https://user:password@example.test/release",
    "https://example.test:8080/release", "https://[",
])
def test_a_https_prefix_alone_does_not_make_evidence_citable(url):
    assert not is_citable(_evidence_row(canonical_url=url), admissible_ids=frozenset({"src-1"}))


def test_party_source_alone_cannot_establish_a_completed_official_outcome():
    source = replace(alias(), source_kind="party-primary")
    evidence = snapshot(aliases=(source,))
    with pytest.raises(CivicContentError, match="policy_status_mismatch"):
        check_locale_content(content(policy_status=CivicPolicyStatus.COMPLETED), evidence)


def test_party_announcement_is_valid_without_independent_outcome_evidence():
    source = replace(alias(), source_kind="party-primary")
    check_locale_content(
        content(policy_status=CivicPolicyStatus.ANNOUNCED),
        snapshot(aliases=(source,)),
    )


def test_ambiguous_duration_and_amount_is_not_accepted_as_numeric_parity():
    source = replace(alias(), excerpt="A four-year funding boost of $9.7 million was announced.")
    evidence = snapshot(aliases=(source,), numeric_tokens=("4", "9.7"))
    ambiguous = content(
        locale=CivicLocale.EN,
        title="Funding announced",
        summary="The minister announced 4 years of $9.7 million in funding.",
        claims=(CivicDraftClaim("The minister announced funding.", ("e1",)),),
    )
    with pytest.raises(CivicContentError, match="quantity_scope_ambiguous"):
        check_locale_content(ambiguous, evidence)


def test_explicit_total_over_duration_preserves_numeric_scope():
    source = replace(alias(), excerpt="A four-year funding boost of $9.7 million was announced.")
    evidence = snapshot(aliases=(source,), numeric_tokens=("4", "9.7"))
    clear = content(
        locale=CivicLocale.EN,
        title="Funding announced",
        summary="The minister announced $9.7 million in total over 4 years.",
        claims=(CivicDraftClaim("The minister announced funding.", ("e1",)),),
    )
    check_locale_content(clear, evidence)


def test_nz_campaign_mainland_is_not_translated_as_chinese_mainland():
    source = replace(alias(), title="Speech at ACT Mainland Campaign Launch")
    evidence = snapshot(aliases=(source,))
    wrong = content(
        locale=CivicLocale.ZH,
        title="在ACT大陆竞选启动活动上发表演讲",
        summary="ACT发布演讲。",
        claims=(CivicDraftClaim("ACT发布演讲。", ("e1",)),),
    )
    with pytest.raises(CivicContentError, match="locale_term_drift"):
        check_locale_content(wrong, evidence)
    check_locale_content(replace(wrong, title="在ACT南岛竞选启动活动上发表演讲"), evidence)


def test_chinese_action_title_cannot_drop_an_english_proposal_qualifier():
    evidence = snapshot()
    texts = {locale: content(locale) for locale in CivicLocale}
    texts[CivicLocale.EN] = replace(
        texts[CivicLocale.EN], title="Party announces plan to abolish a commission")
    texts[CivicLocale.ZH] = replace(
        texts[CivicLocale.ZH], title="政党宣布废除委员会")
    with pytest.raises(CivicContentError, match="locale_status_drift"):
        check_candidate_content(evidence, texts)
    texts[CivicLocale.ZH] = replace(
        texts[CivicLocale.ZH], title="政党宣布计划废除委员会")
    check_candidate_content(evidence, texts)


def test_persisted_later_citation_does_not_inherit_the_earlier_clusters_date(tmp_path):
    from beehive.db.civic_candidates import add_candidate_evidence
    from beehive.db.civic_qualifications import get_latest_qualification
    from beehive.db.connection import connect, init_schema
    from beehive.domain.civic import CivicEvidenceStance, CivicEvidenceTier
    from tests.civic.test_civic_ai_pipeline import T0, _make_run, qualified_candidate

    conn = connect(str(tmp_path / "citation-date.db"))
    init_schema(conn)
    run = _make_run(conn)
    candidate = qualified_candidate(conn, run)
    add_candidate_evidence(
        conn, candidate.id, "party_feed", "later-release",
        "https://party.example/release", "Later policy release",
        CivicEvidenceTier.PARTY_PRIMARY, CivicEvidenceStance.CORROBORATING, T0,
        publisher="Example party", publisher_id="party", source_kind="party-primary",
        subject_id="green-party", claim_category="legislation",
        event_date=date(2026, 8, 1),
        published_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        canonical_url="https://party.example/release",
        excerpt="The party announced a proposal.",
        accessible_full_text=True,
    )
    qualification = get_latest_qualification(conn, candidate.id)
    assert qualification is not None
    qualification = replace(
        qualification, evidence_used=(*qualification.evidence_used, "later-release"))
    evidence = build_candidate_snapshot(
        conn, candidate_id=candidate.id, qualification=qualification)
    later = next(item for item in evidence.aliases if item.evidence_id == "later-release")
    assert later.event_date == date(2026, 8, 7)
    assert evidence.event_date == date(2026, 8, 1)
    with pytest.raises(CivicContentError, match="event_date_drift"):
        check_locale_content(content(
            claims=(CivicDraftClaim("The party announced a proposal.", (later.alias,)),),
        ), evidence)
    add_candidate_evidence(
        conn, candidate.id, "party_feed", "undated-release",
        "https://party.example/undated", "Undated corroboration",
        CivicEvidenceTier.PARTY_PRIMARY, CivicEvidenceStance.CORROBORATING, T0,
        publisher_id="party", source_kind="party-primary",
        subject_id="green-party", claim_category="legislation",
        event_date=date(2026, 8, 1),
        canonical_url="https://party.example/undated",
        excerpt="The party announced a proposal.", accessible_full_text=True,
    )
    usable = build_candidate_snapshot(
        conn, candidate_id=candidate.id,
        qualification=replace(
            qualification, evidence_used=("later-release", "undated-release")))
    assert usable.event_date == date(2026, 8, 7)
    assert [item.evidence_id for item in usable.aliases] == ["later-release"]
    check_locale_content(content(
        title="Party announces proposal", summary="The party announced a proposal.",
        event_date=usable.event_date, policy_status=CivicPolicyStatus.ANNOUNCED,
        claims=(CivicDraftClaim("The party announced a proposal.", ("e1",)),),
    ), usable)
    conn.close()


def test_a_total_cannot_be_recast_as_an_annual_amount_with_the_same_digits():
    source = replace(alias(), excerpt="A four-year funding boost of $9.7 million was announced.")
    evidence = snapshot(aliases=(source,), numeric_tokens=("9.7", "4"))
    wrong = content(
        locale=CivicLocale.EN,
        title="Funding announced",
        summary="The minister announced $9.7 million per year for 4 years.",
        claims=(CivicDraftClaim("Funding was announced.", ("e1",)),),
    )
    with pytest.raises(CivicContentError, match="quantity_scope_ambiguous"):
        check_locale_content(wrong, evidence)
    annual_source = replace(
        source, excerpt="A four-year programme will receive $9.7 million per year.")
    check_locale_content(wrong, replace(evidence, aliases=(annual_source,)))


@pytest.mark.parametrize(("locale", "summary"), [
    (CivicLocale.EN, "$9.7 million a year was announced."),
    (CivicLocale.EN, "9.7 million dollars per annum was announced."),
    (CivicLocale.EN, "$9.7 million yearly was announced."),
    (CivicLocale.EN, "four years of $9.7 million was announced."),
    (CivicLocale.ZH, "部长宣布每年9.7百万美元。"),
    (CivicLocale.HI, "प्रति वर्ष $9.7 मिलियन की घोषणा की गई।"),
    (CivicLocale.TL, "Inanunsyo ang $9.7 milyon kada taon."),
])
def test_amount_scope_guards_cover_common_phrasings_and_all_locales(locale, summary):
    source = replace(alias(), excerpt="A four-year funding boost of $9.7 million was announced.")
    evidence = snapshot(aliases=(source,), numeric_tokens=("9.7", "4"))
    wrong = content(
        locale=locale, title="Funding announced", summary=summary,
        claims=(CivicDraftClaim("Funding was announced.", ("e1",)),),
    )
    with pytest.raises(CivicContentError, match="quantity_scope_ambiguous"):
        check_locale_content(wrong, evidence)


def test_unrelated_annual_amount_in_same_newsletter_does_not_support_a_total():
    source = replace(alias(), excerpt=(
        "The programme receives $9.7 million over four years. "
        "Another programme receives $1 million per year."))
    evidence = snapshot(aliases=(source,), numeric_tokens=("9.7", "4", "1"))
    wrong = content(
        locale=CivicLocale.EN, title="Funding announced",
        summary="The programme receives $9.7 million per year.",
        claims=(CivicDraftClaim("Funding was announced.", ("e1",)),),
    )
    with pytest.raises(CivicContentError, match="quantity_scope_ambiguous"):
        check_locale_content(wrong, evidence)


def test_the_model_sees_every_captured_character_of_a_release(tmp_path):
    from beehive.civic.documents import MAX_EXCERPT_CHARS
    from beehive.civic.limits import MAX_EVIDENCE_TEXT_CHARS
    from beehive.db.civic_candidates import add_candidate_evidence
    from beehive.db.civic_qualifications import get_latest_qualification
    from beehive.db.connection import connect, init_schema
    from beehive.domain.civic import CivicEvidenceStance, CivicEvidenceTier
    from tests.civic.test_civic_ai_pipeline import T0, _make_run, qualified_candidate

    assert MAX_EVIDENCE_TEXT_CHARS >= MAX_EXCERPT_CHARS
    preamble = "The party set out its supermarket competition plan in detail today. " * 30
    late = "It would open a one-stop consenting shop for 12 new grocery entrants."
    excerpt = f"{preamble}{late}"[:MAX_EXCERPT_CHARS]
    assert excerpt.index(late) > 1500 and excerpt.endswith(late)

    conn = connect(str(tmp_path / "evidence-window.db"))
    init_schema(conn)
    run = _make_run(conn)
    candidate = qualified_candidate(conn, run)
    add_candidate_evidence(
        conn, candidate.id, "party_feed", "long-release",
        "https://party.example/long-release", "Party sets out supermarket plan",
        CivicEvidenceTier.PARTY_PRIMARY, CivicEvidenceStance.CORROBORATING, T0,
        publisher="Example party", publisher_id="party", source_kind="party-primary",
        subject_id="green-party", claim_category="legislation",
        event_date=date(2026, 8, 7),
        published_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        canonical_url="https://party.example/long-release",
        excerpt=excerpt, accessible_full_text=True,
    )
    qualification = get_latest_qualification(conn, candidate.id)
    evidence = build_candidate_snapshot(
        conn, candidate_id=candidate.id,
        qualification=replace(qualification, evidence_used=("long-release",)))
    conn.close()

    assert [item.excerpt for item in evidence.aliases] == [excerpt]
    assert late in build_generation_prompt(evidence)
    check_locale_content(content(
        title="Party sets out supermarket plan",
        summary="The party would open a one-stop consenting shop for 12 new grocery entrants.",
        event_date=evidence.event_date, policy_status=CivicPolicyStatus.ANNOUNCED,
        claims=(CivicDraftClaim(
            "The party would open a one-stop consenting shop for 12 new grocery entrants.",
            ("e1",)),),
    ), evidence)


_PUBLISHED = date(2026, 8, 29)


@pytest.mark.parametrize(("source", "expected"), [
    ("Graduates receive the cut from 1 April 2027.", {date(2027, 4, 1)}),
    ("Graduates receive the cut from April 1, 2027.", {date(2027, 4, 1)}),
    ("It starts on Monday 3rd March 2027.", {date(2027, 3, 3)}),
    ("It starts on the 1st of July 2029.", {date(2029, 7, 1)}),
    ("Enrolments close 30/6/2027.", {date(2027, 6, 30)}),
    ("Registrations run September 14-16, 2026.", {date(2026, 9, 14), date(2026, 9, 16)}),
    ("The retreat runs 3\u20135 September 2026.", {date(2026, 9, 3), date(2026, 9, 5)}),
    ("The retreat runs from 3 to 5 September 2026.", {date(2026, 9, 3), date(2026, 9, 5)}),
])
def test_evidence_states_whole_dates(source, expected):
    from beehive.civic.calendar_text import evidence_dates

    assert expected <= evidence_dates([source])


@pytest.mark.parametrize("source", [
    "The levy changes from 1 July.",
    "Mr Luxon will travel from 3-5 September to attend.",
    "The scheme begins on 1 April.",
])
def test_a_date_without_a_year_states_no_full_date(source):
    from beehive.civic.calendar_text import evidence_dates

    assert evidence_dates([source], event_dates=(date(2026, 12, 31),)) == {date(2026, 12, 31)}


def test_a_publication_date_never_licenses_a_date_in_copy():
    source = replace(alias(), published_at="2027-04-01")
    evidence = snapshot(aliases=(source,))
    stated = content(
        summary="The record shows the bill passed its second reading on 2026-08-01 and takes "
                "effect on 2027-04-01.")
    with pytest.raises(CivicContentError, match="number_not_in_evidence"):
        check_locale_content(stated, evidence)


def test_text_without_a_day_states_no_date():
    from beehive.civic.calendar_text import evidence_dates
    from beehive.civic.generation import numeric_tokens

    assert evidence_dates([
        "The minister may act. March for jobs. It will open in Sept 2027. An august "
        "institution. May the best party win. The election is in November."]) == frozenset()
    assert numeric_tokens("It starts on 1 April 2027.", include_english_words=True) == (
        "1", "2027")


def test_the_generation_contract_names_months_without_digits():
    prompt = build_generation_prompt(snapshot())
    assert "never 11月" in prompt
    assert "months without a day in words" in prompt


def test_an_iso_date_the_prompt_requires_is_licensed_by_the_same_date_in_words(tmp_path):
    from beehive.db.civic_candidates import add_candidate_evidence
    from beehive.db.civic_qualifications import get_latest_qualification
    from beehive.db.connection import connect, init_schema
    from beehive.domain.civic import CivicEvidenceStance, CivicEvidenceTier
    from tests.civic.test_civic_ai_pipeline import T0, _make_run, qualified_candidate

    conn = connect(str(tmp_path / "iso-dates.db"))
    init_schema(conn)
    run = _make_run(conn)
    candidate = qualified_candidate(conn, run)
    add_candidate_evidence(
        conn, candidate.id, "party_feed", "student-debt",
        "https://party.example/student-debt", "Party to cut student debt",
        CivicEvidenceTier.PARTY_PRIMARY, CivicEvidenceStance.CORROBORATING, T0,
        publisher="Example party", publisher_id="party", source_kind="party-primary",
        subject_id="green-party", claim_category="legislation",
        event_date=date(2026, 9, 27),
        published_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
        canonical_url="https://party.example/student-debt",
        excerpt=("The party will cut eligible student loan debt by 10% from 1 April 2027. "
                 "Consultation on the details closes on 2 May 2027."),
        accessible_full_text=True,
    )
    qualification = get_latest_qualification(conn, candidate.id)
    evidence = build_candidate_snapshot(
        conn, candidate_id=candidate.id,
        qualification=replace(qualification, evidence_used=("student-debt",)))
    conn.close()

    def entry(day):
        text = f"The party said it would cut eligible student loan debt by 10% from {day}."
        return content(
            title="Party plans student loan cut", summary=text,
            event_date=evidence.event_date, policy_status=CivicPolicyStatus.ANNOUNCED,
            claims=(CivicDraftClaim(text, ("e1",)),))

    check_locale_content(entry("2027-04-01"), evidence)
    # Every part of 2027-04-02 is in the evidence, but no date the evidence states.
    for recombined in ("2027-04-02", "2027-05-01", "2027年4月2日"):
        with pytest.raises(CivicContentError, match="number_not_in_evidence"):
            check_locale_content(entry(recombined), evidence)


@pytest.mark.parametrize(("zh", "en"), [
    ("工党表示，如果赢得十一月选举，将在未来3年不提高燃油税。",
     "Labour said that if it wins the November election it would not raise fuel tax for 3 years."),
    ("出口在截至2026年六月的一年中接近$5 billion。",
     "Exports were near $5 billion in the year to June 2026."),
    ("援引截至2026年6月30日的3个月数据。", "Citing data for the 3 months ended 2026-06-30."),
    ("该银行推出6月期定存。", "The bank launched a 6-month fixed deposit."),
    ("为6月龄儿童提供补贴。", "A subsidy for 6-month-old children."),
])
def test_a_chinese_date_or_month_name_keeps_locale_parity(zh, en):
    from beehive.civic.candidate_policy import check_locale_parity

    check_locale_parity(
        content(CivicLocale.ZH, summary=zh), content(CivicLocale.EN, summary=en),
        stated_dates_for=lambda cited: frozenset({date(2026, 6, 30)}))


@pytest.mark.parametrize(("zh", "en"), [
    ("该计划为期3个月。", "The plan runs for 4 months."),
    ("工党将在11月选举后削减5项税收。", "Labour would cut 6 taxes after the November election."),
    ("援引截至2026年6月30日的数据。", "Citing data for the period ended 2026-06-29."),
    ("援引截至2025年6月30日的数据。", "Citing data for the period ended 2026-06-30."),
    ("该措施将于12月生效。", "The measure takes effect in November."),
    ("数据显示2026-03-09的结果。", "Data show the result on 2026-09-03."),
])
def test_quantities_and_dates_still_drift(zh, en):
    from beehive.civic.candidate_policy import check_locale_parity

    stated = frozenset({date(2026, 6, 30), date(2026, 3, 9), date(2026, 9, 3)})
    with pytest.raises(CivicContentError, match="locale_number_drift"):
        check_locale_parity(
            content(CivicLocale.ZH, summary=zh), content(CivicLocale.EN, summary=en),
            stated_dates_for=lambda cited: stated)



def test_an_unstated_date_keeps_the_digit_parity_it_had_before():
    """Only a date the evidence states is compared as a date; any other stays digit by digit."""
    from beehive.civic.candidate_policy import check_candidate_content
    from beehive.civic.generation import numeric_tokens

    source = replace(alias(), excerpt=(
        "Grants open 2027-04-01 and audits close 2027-05-02. The agency approved 4 grants and "
        "2 audits."))
    evidence = snapshot(aliases=(source,), numeric_tokens=numeric_tokens(
        f"{source.title} {source.excerpt} {source.event_date.isoformat()}",
        include_english_words=True))

    def entries(zh, other):
        return {
            locale: content(locale, summary=zh if locale is CivicLocale.ZH else other)
            for locale in CivicLocale
        }

    check_candidate_content(evidence, entries(
        "该机构的拨款于2027年4月1日开放。", "The agency's grants open on 2027-04-01."))
    with pytest.raises(CivicContentError, match="locale_number_drift"):
        check_candidate_content(evidence, entries(
            "该机构的拨款于2027年4月2日开放。", "The agency's grants open on 2027-04-02."))



def test_a_date_only_an_uncited_source_states_keeps_digit_parity():
    from beehive.civic.candidate_policy import check_candidate_content
    from beehive.civic.generation import numeric_tokens

    uncited = replace(alias(), excerpt="Grants opened on 2027-04-02.")
    cited = replace(alias("e2", "src-2"), excerpt=(
        "Grants open 2027-04-01 and audits close 2027-05-02. The agency approved 4 grants and "
        "2 audits."))
    evidence = snapshot(aliases=(uncited, cited), numeric_tokens=numeric_tokens(
        " ".join(f"{entry.title} {entry.excerpt} {entry.event_date.isoformat()}"
                 for entry in (uncited, cited)), include_english_words=True))

    def entries(zh, other):
        result = {}
        for locale in CivicLocale:
            default = content(locale)
            result[locale] = replace(
                default, summary=zh if locale is CivicLocale.ZH else other,
                claims=tuple(replace(claim, citations=("e2",)) for claim in default.claims))
        return result

    check_candidate_content(evidence, entries(
        "该机构的拨款于2027年4月1日开放。", "The agency's grants open on 2027-04-01."))
    with pytest.raises(CivicContentError, match="locale_number_drift"):
        check_candidate_content(evidence, entries(
            "该机构的拨款于2027年4月2日开放。", "The agency's grants open on 2027-04-02."))
