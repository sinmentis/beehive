from dataclasses import replace
from datetime import date

import pytest

from beehive.civic.candidate_parser import CivicDraftClaim, CivicPolicyStatus
from beehive.civic.candidate_policy import CivicContentError, check_locale_content
from beehive.civic.generation import numeric_tokens
from beehive.civic.limits import CivicLocale
from tests.civic.test_civic_ai_contracts import alias, content, snapshot


def example(locale, source_text, summary, *, day=date(2026, 9, 4)):
    source = replace(
        alias(), title="Policy announcement", excerpt=source_text,
        event_date=day, published_at=day.isoformat())
    evidence = snapshot(
        aliases=(source,), event_date=day,
        numeric_tokens=numeric_tokens(
            source_text + " " + day.isoformat(), include_english_words=True))
    draft = content(
        locale, title="Policy announcement", summary=summary, event_date=day,
        policy_status=CivicPolicyStatus.ANNOUNCED,
        claims=(CivicDraftClaim(summary, ("e1",)),))
    return draft, evidence


@pytest.mark.parametrize(("locale", "source", "wrong", "correct"), [
    (
        CivicLocale.TL,
        "New Zealand has joined the Troika ahead of hosting the Forum in Auckland in 2027.",
        "Sumali ang New Zealand bago nito i-host ang Forum noong 2027.",
        "Sumali ang New Zealand bago nito i-host ang Forum sa 2027.",
    ),
    (
        CivicLocale.HI,
        "Labour plans 3 free GP visits and free prescriptions.",
        "लेबर ने 3 मुफ्त जीपी यात्राओं की योजना घोषित की।",
        "लेबर ने जीपी से 3 मुफ्त परामर्श की योजना घोषित की।",
    ),
    (
        CivicLocale.TL,
        "Labour plans to return the books to surplus by 2029/30 and reduce net debt.",
        "Kasama ang pagbabalik ng mga aklat sa surplus pagsapit ng 2029/30.",
        "Kasama ang pagbabalik sa surplus ng mga account ng pamahalaan sa 2029/30.",
    ),
    (
        CivicLocale.HI,
        "The dental sedation service offers an alternative to general anaesthetic treatment.",
        "दंत बेहोशी सेवा की घोषणा की गई।",
        "दंत सिडेशन सेवा की घोषणा की गई।",
    ),
])
def test_actual_domain_and_tense_failures_are_rejected_without_rejecting_clear_translations(
    locale, source, wrong, correct,
):
    draft, evidence = example(locale, source, wrong)
    with pytest.raises(CivicContentError, match=r"locale_(term|date)_drift"):
        check_locale_content(draft, evidence)
    fixed = replace(
        draft, summary=correct, claims=(CivicDraftClaim(correct, ("e1",)),))
    check_locale_content(fixed, evidence)


@pytest.mark.parametrize(("locale", "source", "text"), [
    (CivicLocale.TL, "The Forum met in Auckland in 2025.", "Nagpulong ang Forum noong 2025."),
    (CivicLocale.TL, "Libraries will return surplus books.", "Ibabalik ang mga aklat."),
    (CivicLocale.TL, "Financial accounts will return to surplus.",
     "Babalik sa surplus ang mga aklat ng pagtutuos."),
    (CivicLocale.HI, "Transport to GP visits will be subsidised.",
     "जीपी तक की यात्रा के लिए सहायता मिलेगी।"),
    (CivicLocale.HI, "The general anaesthetic dental service was announced.",
     "दंत बेहोशी सेवा की घोषणा की गई।"),
    (CivicLocale.HI, "The dental sedation service avoids general anaesthesia.",
     "दंत सिडेशन सेवा सामान्य बेहोशी की आवश्यकता को घटाएगी।"),
])
def test_domain_guards_preserve_legitimate_other_senses_and_historical_dates(locale, source, text):
    check_locale_content(*example(locale, source, text))


def test_past_marker_cannot_describe_a_later_date_within_the_same_year():
    draft, evidence = example(
        CivicLocale.TL, "The Forum will meet on 2026-12-01.",
        "Magpupulong ang Forum noong 2026-12-01.")
    with pytest.raises(CivicContentError, match="locale_date_drift"):
        check_locale_content(draft, evidence)


@pytest.mark.parametrize("when", ["Enero 2027", "ika-3 ng Enero 2027", "Enero 3, 2027"])
def test_named_future_dates_cannot_use_a_past_marker(when):
    draft, evidence = example(
        CivicLocale.TL, "The Forum will meet on 3 January 2027.",
        f"Magpupulong ang Forum noong {when}.")
    with pytest.raises(CivicContentError, match="locale_date_drift"):
        check_locale_content(draft, evidence)


@pytest.mark.parametrize(("locale", "source", "text"), [
    (CivicLocale.HI, "There will be 3 free GP visits.",
     "जीपी की 3 मुफ्त यात्राओं की योजना घोषित की गई।"),
    (CivicLocale.TL, "The government will return the books to surplus.",
     "Ibabalik ang mga aklat patungo sa surplus."),
    (CivicLocale.HI, "The dental sedation services avoid general anaesthetic treatment.",
     "दंत बेहोशी सेवा की घोषणा की गई।"),
])
def test_common_grammatical_variants_do_not_bypass_concept_checks(locale, source, text):
    with pytest.raises(CivicContentError, match="locale_term_drift"):
        check_locale_content(*example(locale, source, text))


def test_explicitly_supported_transport_is_not_relabelled_as_a_consultation():
    check_locale_content(*example(
        CivicLocale.HI, "Transport to GP visits will be subsidised.",
        "जीपी यात्रा के लिए परिवहन सहायता मिलेगी।"))


def test_unrelated_transport_wording_cannot_hide_a_medical_mistranslation():
    with pytest.raises(CivicContentError, match="locale_term_drift"):
        check_locale_content(*example(
            CivicLocale.HI,
            "There will be 3 free GP visits. Transport funding will also increase.",
            "परिवहन सहायता बढ़ेगी। जीपी की 3 मुफ्त यात्राओं की योजना है।"))


def test_later_citation_can_report_a_past_event_after_the_candidates_first_date():
    first = replace(alias(), event_date=date(2026, 9, 4), published_at="2026-09-04",
                    excerpt="An initial statement was issued.")
    later = replace(alias("e2", "src-2"), event_date=date(2026, 9, 20),
                    published_at="2026-09-20", excerpt="The committee reported on 2026-09-20.")
    evidence = snapshot(
        aliases=(first, later), event_date=first.event_date,
        numeric_tokens=numeric_tokens("2026-09-04 2026-09-20"))
    draft = content(
        CivicLocale.TL, title="Mga pahayag", summary="May mga pahayag ang komite.",
        event_date=first.event_date, policy_status=CivicPolicyStatus.ANNOUNCED,
        claims=(
            CivicDraftClaim("May paunang pahayag.", ("e1",)),
            CivicDraftClaim("Nag-ulat ang komite noong 2026-09-20.", ("e2",)),
        ))
    check_locale_content(draft, evidence)


def test_a_known_source_publication_date_anchors_tense_not_a_future_official_event_date():
    draft, evidence = example(
        CivicLocale.TL, "The Forum will meet in 2027.",
        "Magpupulong ang Forum noong 2027.", day=date(2027, 1, 1))
    evidence = replace(
        evidence, aliases=(replace(evidence.aliases[0], published_at="2026-09-04"),))
    with pytest.raises(CivicContentError, match="locale_date_drift"):
        check_locale_content(draft, evidence)


def test_another_claims_financial_source_cannot_relabel_physical_books():
    physical = replace(alias(), excerpt="Libraries will return surplus books.",
                       event_date=date(2026, 9, 4))
    financial = replace(alias("e2", "src-2"),
                        excerpt="The fiscal plan returns the books to surplus.",
                        event_date=date(2026, 9, 4))
    evidence = snapshot(aliases=(physical, financial), event_date=date(2026, 9, 4))
    draft = content(
        CivicLocale.TL, title="Mga pahayag", summary="Inanunsyo ang mga plano.",
        event_date=evidence.event_date, policy_status=CivicPolicyStatus.ANNOUNCED,
        claims=(
            CivicDraftClaim("Ibabalik ang mga aklat.", ("e1",)),
            CivicDraftClaim("Babalik sa surplus ang mga account ng pamahalaan.", ("e2",)),
        ))
    check_locale_content(draft, evidence)


@pytest.mark.parametrize("past_marker", [True, False])
async def test_parsed_four_locale_pipeline_blocks_future_as_past_before_verification(
    tmp_path, past_marker,
):
    from beehive.civic.ai_gateway import CivicAiStage
    from beehive.civic.ai_pipeline import generate_civic_candidate
    from beehive.db.civic_ai_stages import get_stage_receipt
    from beehive.db.connection import connect, init_schema
    from tests.civic.civic_ai_fakes import (
        FakeCivicGateway, fenced, full_entry, generation_payload, verification_payload,
    )
    from tests.civic.test_civic_ai_pipeline import T0, _make_run, qualified_candidate

    conn = connect(str(tmp_path / "language-pipeline.db"))
    init_schema(conn)
    run = _make_run(conn)
    candidate = qualified_candidate(conn, run)
    conn.execute(
        "UPDATE civic_candidate_evidence SET excerpt = ? WHERE candidate_id = ?",
        ("The record dated 2026-08-01 announces Forum hosting in 2027.", candidate.id))
    conn.commit()
    summaries = {
        "en": "On 2026-08-01, Forum hosting in 2027 was announced.",
        "zh": "2026-08-01，宣布计划在2027年主办论坛。",
        "hi": "2026-08-01 को 2027 में फोरम की मेजबानी की योजना घोषित की गई।",
        "tl": f"Noong 2026-08-01, inanunsyo ang pagho-host ng Forum "
              f"{'noong' if past_marker else 'sa'} 2027.",
    }
    entry = full_entry(**{
        locale: {
            "title": "Forum hosting plan", "summary": summary,
            "claim_text": summary, "policy_status": "announced",
        }
        for locale, summary in summaries.items()
    })
    gateway = FakeCivicGateway(responses={
        CivicAiStage.GENERATE: fenced(generation_payload(entry=entry)),
        CivicAiStage.VERIFICATION: fenced(verification_payload()),
    })
    result = await generate_civic_candidate(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate.id,
        gateway=gateway, now=T0)
    assert result.verified is not past_marker
    if past_marker:
        assert result.rejection_code == "content_locale_date_drift"
        assert gateway.stages == [CivicAiStage.GENERATE]
        assert get_stage_receipt(conn, candidate.id, CivicAiStage.VERIFICATION) is None
    else:
        assert gateway.stages == [CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]
    conn.close()
