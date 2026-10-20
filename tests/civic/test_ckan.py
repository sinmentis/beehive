# tests/civic/test_ckan.py
"""data.govt.nz's CKAN API: UUID identity over editable slugs, the `success: false` inside HTTP
200 that must never look like an empty result, and bounded paging."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from beehive.civic.catalog import DATA_GOVT_CKAN
from beehive.civic.connectors.ckan import CkanConnector
from beehive.civic.documents import CivicSourceError, CivicSourceHealth
from beehive.domain.civic import CivicEvidenceTier
from tests.civic.civic_stubs import fixture_bytes, sequenced_transport

T0 = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)


def _connector(definition, body):
    if isinstance(body, str):
        body = fixture_bytes(body)
    transport = sequenced_transport(body)
    connector = CkanConnector(definition, transport)
    connector.transport = transport  # type: ignore[attr-defined]
    return connector


@pytest.fixture
def datasets():
    return _connector(DATA_GOVT_CKAN, "data_govt_package_search.json")


# ============================================================================
# Mapping
# ============================================================================


def test_the_ckan_uuid_is_the_identity_never_the_editable_slug(datasets):
    """A slug rename must not look like a brand new dataset."""
    dataset = datasets.fetch(T0).documents[0]
    assert dataset.external_key == "0f2c9a3b-51d7-4e88-9a6c-2f4b7d1e0c93"
    assert dataset.raw_metadata["ckan_name"] == "electoral-roll-statistics"


def test_the_landing_page_is_built_from_the_configured_base_and_slug(datasets):
    dataset = datasets.fetch(T0).documents[0]
    assert dataset.canonical_url == (
        "https://catalogue.data.govt.nz/dataset/electoral-roll-statistics")


def test_metadata_created_and_modified_map_onto_published_and_updated(datasets):
    """The source where the three-timestamp split is most obviously right: a dataset published in
    2019 and revised last night is not news, and only the pair says so."""
    dataset = datasets.fetch(T0).documents[0]
    assert dataset.published_at.year == 2019
    assert dataset.updated_at.date().isoformat() == "2026-08-10"
    assert dataset.published_at < dataset.updated_at


def test_the_publishing_agency_is_display_text_and_never_changes_the_tier(datasets):
    dataset = datasets.fetch(T0).documents[0]
    assert "Electoral Commission" in dataset.publisher
    assert dataset.publisher.startswith("data.govt.nz")
    assert dataset.tier is CivicEvidenceTier.OFFICIAL
    assert dataset.tier is DATA_GOVT_CKAN.tier


def test_notes_become_the_bounded_excerpt(datasets):
    assert datasets.fetch(T0).documents[0].excerpt == (
        "Quarterly enrolment counts by electorate.")


# ============================================================================
# Envelope assertions
# ============================================================================


def test_success_false_inside_an_http_200_is_a_hard_error_not_an_empty_result():
    """Exactly the "silent success" failure the health vocabulary exists to stop."""
    connector = _connector(DATA_GOVT_CKAN, b'{"success": false, "error": {"message": "nope"}}')
    with pytest.raises(CivicSourceError, match="success=false"):
        connector.fetch(T0)


def test_a_missing_result_object_is_a_source_error():
    connector = _connector(DATA_GOVT_CKAN, b'{"success": true}')
    with pytest.raises(CivicSourceError, match="JSON object"):
        connector.fetch(T0)


def test_a_non_array_results_list_is_a_source_error():
    connector = _connector(
        DATA_GOVT_CKAN, b'{"success": true, "result": {"results": {"a": 1}, "count": 0}}')
    with pytest.raises(CivicSourceError, match="JSON array"):
        connector.fetch(T0)


def test_an_empty_catalogue_is_empty_not_ok():
    connector = _connector(
        DATA_GOVT_CKAN, b'{"success": true, "result": {"results": [], "count": 0}}')
    assert connector.fetch(T0).health is CivicSourceHealth.EMPTY


def test_a_dataset_with_no_title_falls_back_to_its_slug_rather_than_being_dropped():
    """CKAN's own UI does this, and a slug is genuinely readable -- losing a real dataset over a
    cosmetic gap would be worse than an ugly title."""
    body = (
        b'{"success": true, "result": {"count": 1, "results": ['
        b'{"id": "a", "name": "electoral-roll-statistics"}]}}')
    result = _connector(DATA_GOVT_CKAN, body).fetch(T0)
    assert result.health is CivicSourceHealth.OK
    assert result.documents[0].title == "electoral-roll-statistics"


def test_a_dataset_with_neither_title_nor_slug_degrades_rather_than_losing_the_page():
    body = (
        b'{"success": true, "result": {"count": 2, "results": ['
        b'{"id": "a"},'
        b'{"id": "b", "name": "fine", "title": "Fine"}]}}')
    result = _connector(DATA_GOVT_CKAN, body).fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert len(result.documents) == 1


def test_a_dataset_with_no_resolvable_landing_page_degrades():
    definition = replace(DATA_GOVT_CKAN, dataset_url_base="")
    body = (
        b'{"success": true, "result": {"count": 1, "results": ['
        b'{"id": "a", "title": "No landing page"}]}}')
    result = _connector(definition, body).fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert "no resolvable landing page" in result.degraded_reasons[0]


def test_config_must_be_empty(datasets):
    with pytest.raises(ValueError, match="config must be empty"):
        datasets.fetch(T0, config={"q": "anything"})


# ============================================================================
# Bounded paging and the request itself
# ============================================================================


def test_the_search_query_is_application_controlled(datasets):
    datasets.fetch(T0)
    url = datasets.transport.recorded.calls[0].url
    assert url.startswith(DATA_GOVT_CKAN.endpoint.url)
    assert "sort=metadata_modified+desc" in url
    assert f"rows={DATA_GOVT_CKAN.page_size}" in url
    assert "start=0" in url


def test_the_host_allowlist_and_user_agent_are_sent(datasets):
    datasets.fetch(T0)
    call = datasets.transport.recorded.calls[0]
    assert call.allowed_hosts == frozenset({"data.govt.nz"})
    assert call.user_agent.startswith("PartyMap Civic Watch/")


def _page(size, count, prefix):
    records = ",".join(
        f'{{"id": "{prefix}-{index}", "name": "{prefix}-{index}", "title": "T{index}"}}'
        for index in range(size))
    return f'{{"success": true, "result": {{"count": {count}, "results": [{records}]}}}}'.encode()


def test_paging_stops_on_a_short_page():
    definition = replace(DATA_GOVT_CKAN, page_size=2, max_pages=5)
    pages = [_page(2, 3, "a"), _page(1, 3, "b")]
    connector = _connector(definition, lambda index: pages[min(index, len(pages) - 1)])
    result = connector.fetch(T0)
    assert result.pages_fetched == 2
    assert len(result.documents) == 3
    assert result.health is CivicSourceHealth.OK


def test_hitting_the_page_cap_with_more_upstream_degrades():
    definition = replace(DATA_GOVT_CKAN, page_size=2, max_pages=2)
    connector = _connector(definition, lambda index: _page(2, 500, f"p{index}"))
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert "2-page cap" in result.degraded_reasons[0]


def test_the_start_offset_advances_across_pages():
    definition = replace(DATA_GOVT_CKAN, page_size=2, max_pages=3)
    pages = [_page(2, 5, "a"), _page(2, 5, "b"), _page(1, 5, "c")]
    connector = _connector(definition, lambda index: pages[min(index, len(pages) - 1)])
    connector.fetch(T0)
    starts = [call.url.split("start=")[1].split("&")[0]
              for call in connector.transport.recorded.calls]
    assert starts == ["0", "2", "4"]
