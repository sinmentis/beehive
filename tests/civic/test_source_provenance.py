from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from beehive.civic.catalog import (
    BEEHIVE_RELEASES,
    HANSARD_DAILY_DETAIL_ENDPOINT,
    HANSARD_DEBATES,
    LEGISLATION_HYDRATION,
    LEGISLATION_WORKS_API,
    ORDER_PAPER,
    PARLIAMENT_BILLS,
)
from beehive.civic.connectors.feeds import CivicFeedConnector
from beehive.civic.connectors.legislation import (
    LEGISLATION_API_KEY_ENV,
    LegislationApiConnector,
    LegislationHydrator,
    document_xml_url,
)
from beehive.civic.connectors.parliament import ParliamentApiConnector
from beehive.civic.source_provenance import has_current_official_capture
from tests.civic.civic_stubs import fixture_bytes, transport_for

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def _feed(definition=BEEHIVE_RELEASES, fixture="beehive_rss.xml"):
    transport = transport_for({definition.endpoint.url: fixture_bytes(fixture)})
    return CivicFeedConnector(definition, transport).fetch(NOW).documents[0]


def _atom():
    return _feed(ORDER_PAPER, "order_paper_atom.xml")


def _bill():
    transport = transport_for({
        PARLIAMENT_BILLS.endpoint.url: (fixture_bytes("parliament_bills_search.json"), "application/json"),
    })
    return ParliamentApiConnector(PARLIAMENT_BILLS, transport).fetch(NOW).documents[0]


def _daily_hansard():
    transport = transport_for({
        f"{HANSARD_DAILY_DETAIL_ENDPOINT.url}/2026-08-06": (
            fixture_bytes("hansard_daily_detail.json"), "application/json",
        ),
    })
    return ParliamentApiConnector(HANSARD_DEBATES, transport).fetch_daily_detail(
        date(2026, 8, 6), NOW, detail_endpoint=HANSARD_DAILY_DETAIL_ENDPOINT,
    ).documents[0]


def _legislation_xml():
    url = "https://www.legislation.govt.nz/act/imperial/1267/23/en/latest/"
    transport = transport_for({document_xml_url(url): fixture_bytes("legislation_act.xml")})
    return LegislationHydrator(LEGISLATION_HYDRATION, transport).hydrate([url], NOW).documents[0]


def _legislation_api():
    transport = transport_for({
        LEGISLATION_WORKS_API.endpoint.url: (fixture_bytes("legislation_works.json"), "application/json"),
    })
    return LegislationApiConnector(
        LEGISLATION_WORKS_API, transport,
        environ={LEGISLATION_API_KEY_ENV: "fixture-key"},
    ).fetch(NOW).documents[0]


@pytest.mark.parametrize("factory", [
    _feed, _atom, _bill, _daily_hansard, _legislation_xml, _legislation_api,
])
def test_official_connector_outputs_have_recognizable_unchanged_provenance(factory):
    document = factory()
    metadata = deepcopy(document.raw_metadata)
    assert "source_capture" not in metadata
    assert has_current_official_capture(document)
    assert document.raw_metadata == metadata
    assert not has_current_official_capture(replace(document, raw_metadata={}))


@pytest.mark.parametrize("source", [
    "sitemap-lastmod", "rss-dc-date", "captured_at", "connector-published-at",
    "parliament-lastStageDate", ["rss-pubDate"],
])
def test_modification_capture_and_event_roles_cannot_certify_publication(source):
    document = _feed()
    metadata = deepcopy(document.raw_metadata)
    metadata["publication"]["source"] = source
    assert not has_current_official_capture(replace(document, raw_metadata=metadata))


@pytest.mark.parametrize("field", ["value", "raw_value", "calendar_date"])
def test_official_publication_witness_must_match_original_value_and_calendar(field):
    document = _feed()
    metadata = deepcopy(document.raw_metadata)
    metadata["publication"][field] = "2026-09-08"
    assert not has_current_official_capture(replace(document, raw_metadata=metadata))


def test_official_feed_date_is_bound_to_the_returned_item_identity():
    document = _feed()
    metadata = deepcopy(document.raw_metadata)
    metadata["feed_guid"] = "different-source-item"
    assert not has_current_official_capture(replace(document, raw_metadata=metadata))
    assert not has_current_official_capture(replace(
        document, canonical_url="https://www.beehive.govt.nz/release/different-article",
    ))


def test_copied_candidate_event_date_cannot_replace_an_official_source_witness():
    document = _bill()
    assert document.published_at is None
    metadata = deepcopy(document.raw_metadata)
    del metadata["event_date_provenance"]
    assert metadata["event_date"]
    assert not has_current_official_capture(replace(document, raw_metadata=metadata))


def test_official_event_calendar_must_match_the_original_declared_event():
    document = _bill()
    metadata = deepcopy(document.raw_metadata)
    metadata["event_date"] = "2026-09-08"
    assert not has_current_official_capture(replace(document, raw_metadata=metadata))


def test_hansard_source_event_must_match_the_official_daily_page():
    document = _daily_hansard()
    assert not has_current_official_capture(replace(
        document,
        canonical_url=document.canonical_url.replace("2026-08-06", "2026-08-07"),
    ))


def test_unknown_sources_and_off_host_links_cannot_borrow_official_metadata():
    document = _feed()
    assert not has_current_official_capture(replace(document, source_key="unregistered-official"))
    assert not has_current_official_capture(replace(
        document, canonical_url="https://example.test/release",
    ))


def test_rss_publication_provenance_cannot_guess_an_absent_timezone():
    document = _feed()
    metadata = deepcopy(document.raw_metadata)
    metadata["publication"]["raw_value"] = "Tue, 4 Aug 2026 12:00:00"
    assert not has_current_official_capture(replace(document, raw_metadata=metadata))
