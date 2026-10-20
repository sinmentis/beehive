# tests/civic/test_parliament.py
"""Parliament's three POST search APIs against their real captured contracts.

The three things that were wrong before and are pinned here: Bills posts the site's own full
filter object, Hansard is OData (`value[]` + `@odata.count`) at `/api/data/search` and must never
build a `/<uuid>` soft-404 URL, and an empty or self-contradictory envelope from either is a
degraded fetch rather than a quiet healthy week."""
from dataclasses import replace
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import pytest

from beehive.civic.catalog import (HANSARD_DAILY_DETAIL_ENDPOINT, HANSARD_DEBATES,
                                    PARLIAMENT_BILLS, PARLIAMENT_SELECT_COMMITTEES,
                                    hansard_daily_detail_endpoint)
from beehive.civic.connectors.parliament import HANSARD_DAILY_URL_TEMPLATE
from beehive.civic.connectors.parliament import (BILLS_SEARCH_FILTERS, HANSARD_SEARCH_FILTERS,
                                                  SELECT_COMMITTEE_EXTERNAL_URL_BY_TYPE,
                                                  SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE,
                                                  SELECT_COMMITTEES_SEARCH_FILTERS,
                                                  ParliamentApiConnector)
from beehive.civic.documents import CivicSourceError, CivicSourceHealth
from beehive.domain.civic import CivicEvidenceTier
from tests.civic.civic_stubs import (fixture_bytes, fixture_json, sequenced_transport,
                                     transport_for)

T0 = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)


def _connector(definition, body, *, content_type="application/json"):
    if isinstance(body, str):
        body = fixture_bytes(body)
    transport = transport_for(
        {definition.endpoint.url: (body, content_type)},
        default_content_type="application/json")
    connector = ParliamentApiConnector(definition, transport)
    connector.transport = transport  # type: ignore[attr-defined]
    return connector


def _paging_connector(definition, body_for):
    transport = sequenced_transport(body_for)
    connector = ParliamentApiConnector(definition, transport)
    connector.transport = transport  # type: ignore[attr-defined]
    return connector


# ============================================================================
# Bills: the exact captured request body
# ============================================================================


@pytest.fixture
def bills():
    return _connector(PARLIAMENT_BILLS, "parliament_bills_search.json")


@pytest.mark.parametrize(
    "field,expected",
    [
        ("id", None),
        ("documentPreset", 1),
        ("status", []),
        ("documentTypes", []),
        ("documentSubtypes", []),
        ("beforeCommittee", None),
        ("billStages", []),
        ("billTab", "Current"),
        ("billId", None),
        ("includeBillStages", True),
        ("parliaments", []),
        ("members", []),
        ("parties", []),
        ("portfolios", []),
        ("subjects", []),
        ("searchText", None),
        ("dateFrom", None),
        ("dateTo", None),
        ("column", 17),
        ("direction", 1),
    ])
def test_the_captured_bills_filter_object_is_posted_verbatim(bills, field, expected):
    """A partial body does not error -- it silently returns a differently-scoped or
    differently-ordered page, which is far worse."""
    bills.fetch(T0)
    payload = bills.transport.recorded.calls[0].payload
    assert payload[field] == expected


def test_the_bills_payload_also_carries_page_and_page_size(bills):
    bills.fetch(T0)
    payload = bills.transport.recorded.calls[0].payload
    assert payload["page"] == 1
    assert payload["pageSize"] == PARLIAMENT_BILLS.page_size


def test_the_bills_filter_constant_is_not_mutated_by_a_fetch(bills):
    before = dict(BILLS_SEARCH_FILTERS)
    bills.fetch(T0)
    assert BILLS_SEARCH_FILTERS == before
    assert "page" not in BILLS_SEARCH_FILTERS


def test_bills_is_a_post_to_the_documented_endpoint(bills):
    bills.fetch(T0)
    call = bills.transport.recorded.calls[0]
    assert call.method == "POST"
    assert call.url == "https://bills.parliament.nz/api/data/search"
    assert call.allowed_hosts == frozenset({"parliament.nz"})
    assert call.user_agent.startswith("PartyMap Civic Watch/")


def test_bills_parses_results_and_total_results(bills):
    result = bills.fetch(T0)
    assert result.health is CivicSourceHealth.OK
    assert len(result.documents) == 2
    assert PARLIAMENT_BILLS.result_keys == ("results",)
    assert PARLIAMENT_BILLS.total_keys == ("totalResults",)


def test_bills_uses_the_returned_uuid_as_identity(bills):
    assert bills.fetch(T0).documents[0].external_key == (
        "58c619f0-9c1f-4ce0-df81-08dd076d450c")


def test_bills_maps_the_captured_timestamps(bills):
    """The live record has `publicationDate: null` and carries its date in `lastStageDate`."""
    first = bills.fetch(T0).documents[0]
    assert first.updated_at.isoformat() == "2026-08-06T14:01:00+00:00"
    assert first.published_at is None
    assert first.raw_metadata["event_date"] == "2026-08-07"
    assert first.raw_metadata["event_date_provenance"]["source"] == "parliament-lastStageDate"
    assert first.tier is CivicEvidenceTier.OFFICIAL


def test_bills_derives_the_verified_v0_detail_url(bills):
    """The live record carries `url: null`, so the page must be derived -- and `/v/{id}` (what
    this once produced) is a hard 404. `/v/6/{id}` answers 200 with the bill's own title, and a
    bogus id under it still 404s, so unlike Hansard this derivation is genuinely checkable."""
    first = bills.fetch(T0).documents[0]
    assert first.canonical_url == (
        "https://bills.parliament.nz/v/6/58c619f0-9c1f-4ce0-df81-08dd076d450c")
    assert PARLIAMENT_BILLS.allow_id_url_fallback


def test_no_bills_url_uses_a_superseded_view_index(bills):
    for document in bills.fetch(T0).documents:
        assert "/v/6/" in document.canonical_url
        assert "/v/0/" not in document.canonical_url


# ============================================================================
# Envelope health: empty and contradictory responses
# ============================================================================


def test_an_empty_bills_response_is_degraded_not_a_quiet_healthy_week():
    """Parliament always has current bills, so zero results means the request shape or the API
    contract changed."""
    result = _connector(PARLIAMENT_BILLS, b'{"results": [], "totalResults": 0}').fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("always has content" in reason for reason in result.degraded_reasons)


def test_a_missing_total_is_reported_because_completeness_cannot_be_verified():
    result = _connector(
        PARLIAMENT_BILLS,
        b'{"results": [{"id": "a", "title": "T", "url": "https://bills.parliament.nz/v/6/a"}]}'
    ).fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("no total count" in reason for reason in result.degraded_reasons)


def test_a_null_total_is_treated_the_same_as_a_missing_one():
    result = _connector(
        PARLIAMENT_BILLS,
        b'{"totalResults": null, "results": [{"id": "a", "title": "T", '
        b'"url": "https://bills.parliament.nz/v/6/a"}]}').fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("no total count" in reason for reason in result.degraded_reasons)


def test_a_negative_total_is_a_hard_source_error():
    with pytest.raises(CivicSourceError, match="negative total"):
        _connector(PARLIAMENT_BILLS, b'{"results": [], "totalResults": -1}').fetch(T0)


@pytest.mark.parametrize("total", [b'"12"', b"12.5", b"true", b"[]"])
def test_a_non_integer_total_is_a_hard_source_error(total):
    body = b'{"results": [], "totalResults": ' + total + b"}"
    with pytest.raises(CivicSourceError, match="not an integer count"):
        _connector(PARLIAMENT_BILLS, body).fetch(T0)


def test_a_total_that_contradicts_an_empty_page_is_reported():
    result = _connector(PARLIAMENT_BILLS, b'{"results": [], "totalResults": 87}').fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("returned an empty page" in reason for reason in result.degraded_reasons)


def test_a_missing_results_key_fails_loudly_as_a_contract_change():
    with pytest.raises(CivicSourceError, match="contract has changed"):
        _connector(PARLIAMENT_BILLS, b'{"unexpected": []}').fetch(T0)


def test_a_non_object_response_is_a_source_error():
    with pytest.raises(CivicSourceError, match="JSON object"):
        _connector(PARLIAMENT_BILLS, b"[1, 2, 3]").fetch(T0)


def test_a_hit_without_an_id_degrades_rather_than_losing_the_page():
    body = (
        b'{"totalResults": 2, "results": ['
        b'{"title": "No id here"},'
        b'{"id": "abc", "title": "Fine", "url": "https://bills.parliament.nz/v/6/abc"}]}')
    result = _connector(PARLIAMENT_BILLS, body).fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert len(result.documents) == 1


# ============================================================================
# Hansard: OData listing at /api/data/search
# ============================================================================


@pytest.fixture
def hansard():
    return _connector(HANSARD_DEBATES, "hansard_search.json")


def test_hansard_posts_to_the_data_search_endpoint(hansard):
    hansard.fetch(T0)
    call = hansard.transport.recorded.calls[0]
    assert call.url == "https://hansard.parliament.nz/api/data/search"
    assert call.method == "POST"


@pytest.mark.parametrize(
    "field,expected",
    [("searchTab", 0), ("types", ["Daily"]), ("orderByFields", ["SittingDate"]),
     ("direction", 1)])
def test_the_captured_hansard_listing_request_is_posted(hansard, field, expected):
    hansard.fetch(T0)
    assert hansard.transport.recorded.calls[0].payload[field] == expected


def test_hansard_sends_page_and_page_size(hansard):
    hansard.fetch(T0)
    payload = hansard.transport.recorded.calls[0].payload
    assert payload["page"] == 1
    assert payload["pageSize"] == HANSARD_DEBATES.page_size


def test_the_hansard_filter_constant_is_not_mutated(hansard):
    before = dict(HANSARD_SEARCH_FILTERS)
    hansard.fetch(T0)
    assert HANSARD_SEARCH_FILTERS == before


def test_hansard_reads_the_odata_envelope(hansard):
    result = hansard.fetch(T0)
    assert result.health is CivicSourceHealth.OK
    assert len(result.documents) == 2
    assert HANSARD_DEBATES.result_keys == ("value",)
    assert HANSARD_DEBATES.total_keys == ("@odata.count",)


def test_hansard_builds_the_transcript_route_never_a_uuid(hansard):
    """Every path under hansard.parliament.nz answers HTTP 200 with the same client-rendered
    shell, so a uuid link would pass every automated check and resolve to nothing. The sitting
    date keys both the API (`/api/data/daily/{YYYY-MM-DD}`) and the publisher's transcript
    route, so the derivation is anchored to returned data at both ends."""
    first = hansard.fetch(T0).documents[0]
    assert first.canonical_url == "https://hansard.parliament.nz/hansard-transcript/2026-08-06"
    assert first.external_key not in first.canonical_url
    assert HANSARD_DAILY_URL_TEMPLATE.endswith("/hansard-transcript/{token}")


def test_the_second_sitting_day_gets_its_own_date_route(hansard):
    second = hansard.fetch(T0).documents[1]
    assert second.canonical_url == "https://hansard.parliament.nz/hansard-transcript/2026-08-05"


def test_a_non_daily_hansard_document_gets_no_derived_url():
    """Only Daily documents are addressable by date; anything else is reported rather than given
    a URL that would not resolve to it."""
    body = (
        b'{"@odata.count": 1, "value": [{"id": "abc", "title": "A speech", '
        b'"documentType": "Speech", "sittingDate": "2026-08-06T00:00:00Z"}]}')
    result = _connector(HANSARD_DEBATES, body).fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("soft 404" in reason for reason in result.degraded_reasons)
    assert result.documents == ()


def test_hansard_id_url_fallback_is_disabled_outright():
    assert HANSARD_DEBATES.allow_id_url_fallback is False


def test_a_hansard_record_with_no_sitting_date_degrades_rather_than_publishing_a_soft_404():
    body = b'{"@odata.count": 1, "value": [{"id": "abc", "title": "No sitting date"}]}'
    result = _connector(HANSARD_DEBATES, body).fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("soft 404" in reason for reason in result.degraded_reasons)
    assert result.documents == ()


def test_hansard_maps_the_captured_lowercase_fields(hansard):
    first = hansard.fetch(T0).documents[0]
    assert first.external_key == "43a27e9c-c54f-4eff-8573-9edbd7019edd"
    assert first.published_at is None
    assert first.raw_metadata["event_date"] == "2026-08-06"
    assert first.title == "Thursday, 6 August 2026"
    assert HANSARD_DEBATES.id_keys == ("id", "documentId")
    assert HANSARD_DEBATES.url_keys == ()


def test_hansard_sitting_date_is_not_replaced_by_a_later_publication_date():
    body = (
        b'{"@odata.count":1,"value":[{"id":"fixture-debate","title":"Fixture debate",'
        b'"sittingDate":"2026-09-02T00:00:00Z",'
        b'"publicationDate":"2026-09-05T04:00:00Z"}]}'
    )
    document = _connector(HANSARD_DEBATES, body).fetch(T0).documents[0]
    assert document.published_at == datetime(2026, 9, 5, 4, tzinfo=timezone.utc)
    assert document.raw_metadata["event_date"] == "2026-09-02"
    assert document.raw_metadata["publication"]["source"] == "parliament-publicationDate"


def test_hansard_ordering_uses_the_event_witness_not_capture_time(hansard):
    document = hansard.fetch(T0).documents[0]
    assert document.published_at is None
    assert document.effective_at == datetime(2026, 8, 6, tzinfo=timezone.utc)


def test_an_empty_hansard_listing_is_degraded_not_healthy():
    result = _connector(HANSARD_DEBATES, b'{"value": [], "@odata.count": 0}').fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED


def test_a_returned_url_is_preferred_over_a_derived_one_where_a_source_provides_it():
    """Bills does return URLs, so the returned value always wins over any derivation."""
    body = (
        b'{"totalResults": 1, "results": [{"id": "abc", "title": "T", '
        b'"url": "https://bills.parliament.nz/v/6/explicit"}]}')
    document = _connector(PARLIAMENT_BILLS, body).fetch(T0).documents[0]
    assert document.canonical_url == "https://bills.parliament.nz/v/6/explicit"


# ============================================================================
# Hansard daily detail: opt-in and bounded
# ============================================================================


def test_the_daily_detail_endpoint_is_not_touched_by_a_listing_fetch(hansard):
    hansard.fetch(T0)
    assert all("/api/data/daily" not in call.url
               for call in hansard.transport.recorded.calls)


def test_daily_detail_requires_an_explicit_endpoint(hansard):
    with pytest.raises(ValueError, match="no daily-detail endpoint"):
        hansard.fetch_daily_detail(date(2026, 8, 6), T0)


def test_daily_detail_reads_the_live_hansard_debates_envelope():
    body = fixture_bytes("hansard_daily_detail.json")
    url = "https://hansard.parliament.nz/api/data/daily/2026-08-06"
    transport = transport_for({url: (body, "application/json")},
                              default_content_type="application/json")
    connector = ParliamentApiConnector(HANSARD_DEBATES, transport)
    result = connector.fetch_daily_detail(
        date(2026, 8, 6), T0, detail_endpoint=hansard_daily_detail_endpoint())
    assert result.health is CivicSourceHealth.OK
    assert len(result.documents) == 2
    assert transport.recorded.calls[0].url == url
    section = result.documents[1]
    assert section.title.startswith("Start of Sitting Day")
    assert section.canonical_url == "https://hansard.parliament.nz/hansard-transcript/2026-08-06"
    assert section.raw_metadata["section_items"] == 1


def test_daily_detail_never_ingests_transcript_content():
    """`content` is deliberately never read: that is what stops a day's full transcript being
    pulled into an excerpt."""
    body = fixture_bytes("hansard_daily_detail.json")
    url = "https://hansard.parliament.nz/api/data/daily/2026-08-06"
    transport = transport_for({url: (body, "application/json")},
                              default_content_type="application/json")
    connector = ParliamentApiConnector(HANSARD_DEBATES, transport)
    result = connector.fetch_daily_detail(
        date(2026, 8, 6), T0, detail_endpoint=hansard_daily_detail_endpoint())
    assert all("content" not in document.raw_metadata for document in result.documents)


def test_a_daily_detail_envelope_with_no_section_list_fails_loudly():
    url = "https://hansard.parliament.nz/api/data/daily/2026-08-06"
    transport = transport_for({url: (b'{"title": "x"}', "application/json")},
                              default_content_type="application/json")
    connector = ParliamentApiConnector(HANSARD_DEBATES, transport)
    with pytest.raises(CivicSourceError, match="no recognised section list"):
        connector.fetch_daily_detail(
            date(2026, 8, 6), T0, detail_endpoint=hansard_daily_detail_endpoint())


def test_daily_detail_is_bounded_and_reports_what_it_dropped():
    body = fixture_bytes("hansard_daily_detail.json")
    url = "https://hansard.parliament.nz/api/data/daily/2026-08-06"
    transport = transport_for({url: (body, "application/json")},
                              default_content_type="application/json")
    connector = ParliamentApiConnector(HANSARD_DEBATES, transport)
    result = connector.fetch_daily_detail(
        date(2026, 8, 6), T0, detail_endpoint=hansard_daily_detail_endpoint(), max_items=1)
    assert result.health is CivicSourceHealth.DEGRADED
    assert len(result.documents) == 1
    assert "kept 1 of 2 sections" in result.degraded_reasons[0]


def test_the_daily_detail_endpoint_is_byte_bounded():
    """A sitting day's transcript metadata is large; this is what stops it becoming a 300 KB
    HTML ingest by default."""
    assert HANSARD_DAILY_DETAIL_ENDPOINT.max_bytes == 2 * 1024 * 1024
    assert HANSARD_DAILY_DETAIL_ENDPOINT.url == "https://hansard.parliament.nz/api/data/daily"


# ============================================================================
# Select Committees
# ============================================================================


def test_select_committees_uses_the_bills_shaped_contract():
    connector = _connector(
        PARLIAMENT_SELECT_COMMITTEES, "parliament_committees_search.json")
    document = connector.fetch(T0).documents[0]
    assert document.external_key == "433da253-0d48-4312-e0c1-08dee61bcf04"
    assert document.published_at.date().isoformat() == "2026-07-20"
    assert document.title == "Briefing to hear from the Commerce Commission"
    assert PARLIAMENT_SELECT_COMMITTEES.result_keys == ("results",)


def test_select_committees_posts_the_bills_shape_with_its_own_sort_column():
    """Bills' `column: 17` answers HTTP 500 here -- this endpoint has far fewer sortable columns,
    and it fails with a 500 rather than an error a client can read."""
    connector = _connector(
        PARLIAMENT_SELECT_COMMITTEES, "parliament_committees_search.json")
    connector.fetch(T0)
    payload = connector.transport.recorded.calls[0].payload
    assert payload["documentPreset"] == 1
    assert payload["column"] == 1
    assert payload["direction"] == 1


def test_the_committees_body_drops_the_bill_only_keys():
    """They describe nothing on this endpoint."""
    for key in ("billTab", "billId", "billStages", "includeBillStages"):
        assert key not in SELECT_COMMITTEES_SEARCH_FILTERS
    assert SELECT_COMMITTEES_SEARCH_FILTERS["status"] == []
    assert SELECT_COMMITTEES_SEARCH_FILTERS["documentPreset"] == 1


def test_the_bills_and_committees_filter_constants_stay_distinct():
    assert BILLS_SEARCH_FILTERS["column"] == 17
    assert SELECT_COMMITTEES_SEARCH_FILTERS["column"] == 1


# ============================================================================
# Bounded paging
# ============================================================================


def _full_page(size, total, prefix="id"):
    hits = ",".join(
        f'{{"id": "{prefix}-{index}", "title": "T{index}", '
        f'"url": "https://bills.parliament.nz/v/6/{prefix}-{index}"}}'
        for index in range(size))
    return f'{{"totalResults": {total}, "results": [{hits}]}}'.encode()


def test_paging_stops_when_a_short_page_arrives():
    definition = replace(PARLIAMENT_BILLS, page_size=2, max_pages=5)
    pages = [_full_page(2, 3), _full_page(1, 3, "second")]
    connector = _paging_connector(definition, lambda i: pages[min(i, len(pages) - 1)])
    result = connector.fetch(T0)
    assert result.pages_fetched == 2
    assert len(result.documents) == 3
    assert result.health is CivicSourceHealth.OK


def test_hitting_the_page_cap_with_more_upstream_degrades():
    definition = replace(PARLIAMENT_BILLS, page_size=2, max_pages=2)
    connector = _paging_connector(definition, lambda i: _full_page(2, 99, f"p{i}"))
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("2-page cap" in reason for reason in result.degraded_reasons)


def test_hitting_the_item_cap_with_more_upstream_degrades():
    definition = replace(PARLIAMENT_BILLS, page_size=4, max_pages=5, max_items=2)
    connector = _paging_connector(definition, lambda i: _full_page(4, 50))
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert len(result.documents) == 2
    assert any("2-item cap" in reason for reason in result.degraded_reasons)


def test_the_page_number_increments_across_requests():
    definition = replace(PARLIAMENT_BILLS, page_size=2, max_pages=3)
    pages = [_full_page(2, 5), _full_page(2, 5, "b"), _full_page(1, 5, "c")]
    connector = _paging_connector(definition, lambda i: pages[min(i, len(pages) - 1)])
    connector.fetch(T0)
    assert [call.payload["page"] for call in connector.transport.recorded.calls] == [1, 2, 3]


def test_config_must_be_empty(bills):
    with pytest.raises(ValueError, match="config must be empty"):
        bills.fetch(T0, config={"endpoint": "https://evil.example.com"})


# ============================================================================
# Route contracts, derived from captured data
# ============================================================================
#
# These derive each route from the captured listing/API record rather than restating a literal,
# so a field-mapping change cannot pass by accident. None of them claims HTTP verification:
# Bills and Select Committees are Blazor and Hansard is a SPA, so every path -- including a
# well-formed but nonexistent UUID -- answers 200 with an identical shell.


def test_the_bills_route_is_derived_from_the_captured_records_own_id(bills):
    record = fixture_json("parliament_bills_search.json")["results"][0]
    document = bills.fetch(T0).documents[0]
    assert document.canonical_url == f"https://bills.parliament.nz/v/6/{record['id']}"
    assert record["url"] is None, "the live record carries no URL, which is why we derive one"


def test_the_committees_route_is_derived_from_the_captured_record():
    record = fixture_json("parliament_committees_search.json")["results"][0]
    index = PARLIAMENT_SELECT_COMMITTEES.url_view_index_by_type[record["documentType"]]
    connector = _connector(
        PARLIAMENT_SELECT_COMMITTEES, "parliament_committees_search.json")
    assert connector.fetch(T0).documents[0].canonical_url == (
        f"https://selectcommittees.parliament.nz/v/{index}/{record['id']}")


def test_the_hansard_route_is_derived_from_the_captured_sitting_date(hansard):
    for record, document in zip(
            fixture_json("hansard_search.json")["value"], hansard.fetch(T0).documents):
        day = record["sittingDate"][:10]
        assert document.canonical_url == (
            f"https://hansard.parliament.nz/hansard-transcript/{day}")


def test_no_parliament_route_embeds_a_uuid_where_the_publisher_uses_a_date(hansard):
    for document in hansard.fetch(T0).documents:
        assert document.external_key not in document.canonical_url


def test_a_record_with_no_verified_route_is_omitted_and_degraded():
    """The standing rule: omit and report, never fabricate."""
    body = (
        b'{"@odata.count": 1, "value": [{"id": "abc", "title": "A speech", '
        b'"documentType": "Speech", "sittingDate": "2026-08-06T00:00:00Z"}]}')
    result = _connector(HANSARD_DEBATES, body).fetch(T0)
    assert result.documents == ()
    assert result.health is CivicSourceHealth.DEGRADED


# ============================================================================
# Select Committee detail routes, one per live documentType
# ============================================================================
#
# Every mapping asserted below was read off the publisher's own `business-before-committees`
# listing (each row's `a.table-cell-title` href, paired with that record's `documentType` from
# the listing's own `/api/data/search` response) and confirmed by rendering the route in
# headless chromium against two or three independent records of that type, matching the page's
# `<h1>`/`<title>` to the record's exact title. Rendering is the only check that works here:
# this is a Blazor app that answers HTTP 200 for every path, and the negative control -- a
# well-formed but nonexistent UUID -- renders the same "Page not found" shell at every view
# code, which is exactly what an earlier status-code-based guess could not see.


@pytest.fixture
def committees():
    return _connector(PARLIAMENT_SELECT_COMMITTEES, "parliament_committees_search.json")


def _committee_records():
    return fixture_json("parliament_committees_search.json")["results"]


def _committee_documents(result):
    return {document.external_key: document for document in result.documents}


ROUTED_TYPES = sorted(
    set(SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE) | set(SELECT_COMMITTEE_EXTERNAL_URL_BY_TYPE))


def _unrouted_reasons(result):
    return [reason for reason in result.degraded_reasons
            if "no verified view route" in reason]


def _committee_page(counts, page_size=None, total=None):
    """One search page built from `{documentType: how many hits}`, ids numbered per type."""
    hits = [
        f'{{"id": "{document_type.replace(" ", "-").lower()}-{index}", '
        f'"title": "Record {index} of {document_type}", '
        f'"documentType": "{document_type}", "publicationDate": "2026-07-20T00:00:00Z"}}'
        for document_type, count in counts.items() for index in range(count)
    ]
    size = page_size if page_size is not None else len(hits)
    reported = total if total is not None else len(hits)
    return f'{{"totalResults": {reported}, "page": 1, "pageSize": {size}, ' \
           f'"results": [{",".join(hits)}]}}'.encode()


def _one_page_committees(counts, **overrides):
    """A committees connector over exactly one page of `counts`.

    `page_size` is set past the hit count so the page is short and paging stops after it --
    otherwise the stub, which answers every request with the same body, would be re-counted once
    per page and the assertions below would be about the stub rather than the aggregation."""
    total = sum(counts.values())
    definition = replace(
        PARLIAMENT_SELECT_COMMITTEES, page_size=total + 1, **overrides)
    return _connector(definition, _committee_page(counts, page_size=total + 1))


@pytest.mark.parametrize("document_type", ROUTED_TYPES)
def test_every_live_committee_type_derives_its_publisher_route(committees, document_type):
    """One case per type the publisher currently lists, each derived from the captured record's
    own id rather than restated as a literal, so a field-mapping change cannot pass by
    accident."""
    documents = _committee_documents(committees.fetch(T0))
    record = next(r for r in _committee_records() if r["documentType"] == document_type)
    expected_route = SELECT_COMMITTEE_EXTERNAL_URL_BY_TYPE.get(document_type)
    if expected_route is None:
        index = SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE[document_type]
        expected = f"https://selectcommittees.parliament.nz/v/{index}/{record['id']}"
    else:
        expected = expected_route.format(id=record["id"])
    assert documents[record["id"]].canonical_url == expected


def test_the_committee_view_codes_are_the_ones_the_publisher_links():
    """The literal table, pinned once. Written out rather than derived because these numbers are
    the whole evidential content of this change: 0 and 4 were already known, and searching only
    0..8 for the rest is what produced the 66% omission this replaces."""
    assert SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE == {
        "Briefing": 0,
        "Financial Scrutiny": 1,
        "Inquiry": 2,
        "Officers Of Parliament": 3,
        "Petition": 4,
        "Regulation Review": 5,
        "Civil Defence": 7,
        "Reports Of Offices Of Parliament": 9,
        "Reserve Bank Of New Zealand Report": 10,
        "Whole Of Government Direction": 12,
        "Privilege": 13,
        "Special Report": 14,
        "International Treaty Examination": 15,
        "Considerations Of Standing Orders And Procedures": 16,
        "Long-Term Insights Briefing": 17,
        "Bill Of Rights Report": 21,
        "Other Matters": 23,
    }


def test_a_bill_links_to_the_bills_site_on_the_committees_own_id(committees):
    """The one type the publisher sends off-site. The Bills page is keyed on the very same UUID
    the Select Committees API returns, so the reader lands on the bill's own legislative history
    rather than a committee view of it."""
    record = next(r for r in _committee_records() if r["documentType"] == "Bill")
    document = _committee_documents(committees.fetch(T0))[record["id"]]
    assert document.canonical_url == (
        f"https://bills.parliament.nz/v/Bill/{record['id']}")
    assert document.source_key == "nz_parliament_select_committees"
    assert document.external_key == record["id"]


def test_the_bills_host_is_the_only_off_site_committee_destination(committees):
    hosts = {urlsplit(document.canonical_url).hostname
             for document in committees.fetch(T0).documents}
    assert hosts == {"selectcommittees.parliament.nz", "bills.parliament.nz"}


def test_an_off_site_route_outside_the_declared_hosts_is_a_hard_error():
    """The off-site route is a declaration, not a licence: a template pointed anywhere else
    fails loudly instead of publishing a link Beehive never reviewed."""
    definition = replace(
        PARLIAMENT_SELECT_COMMITTEES,
        url_external_by_type={"Bill": "https://bills.example.com/v/Bill/{id}"})
    connector = _connector(definition, "parliament_committees_search.json")
    result = connector.fetch(T0)
    assert not any(
        "example.com" in document.canonical_url for document in result.documents)
    assert any("document-URL allow-list" in reason for reason in result.degraded_reasons)


def test_an_unrouted_type_is_omitted_and_reported_once_per_type(committees):
    """Per-TYPE, not per-record: a new document type is one fact an operator can act on, and
    reporting it per record buries it under hundreds of identical lines."""
    result = committees.fetch(T0)
    assert not any(
        document.title.startswith("A record of the not-yet-routed type")
        for document in result.documents)
    assert result.health is CivicSourceHealth.DEGRADED
    assert _unrouted_reasons(result) == [
        "1 hit of documentType 'Another New Type' omitted: no verified view route",
        "2 hits of documentType 'Unheard Of Type' omitted: no verified view route",
    ]


def test_an_unrouted_type_never_costs_a_routed_one(committees):
    """The regression this whole change exists for: one unknown type must not take the rest of
    the page down with it."""
    records = _committee_records()
    routable = [r for r in records if r["documentType"] in ROUTED_TYPES]
    result = committees.fetch(T0)
    assert len(result.documents) == len(routable)
    assert {document.external_key for document in result.documents} == {
        record["id"] for record in routable}


def test_a_republished_type_casing_still_routes():
    """Folded case and collapsed whitespace, so `Long-term  insights briefing` still resolves --
    but only ever onto a type this module already declared."""
    body = (
        b'{"totalResults": 2, "results": ['
        b'{"id": "aaa", "title": "A briefing", "documentType": "long-term  INSIGHTS briefing", '
        b'"publicationDate": "2026-07-20T00:00:00Z"},'
        b'{"id": "bbb", "title": "Something else", "documentType": "Long-Term Insights", '
        b'"publicationDate": "2026-07-20T00:00:00Z"}]}')
    result = _connector(PARLIAMENT_SELECT_COMMITTEES, body).fetch(T0)
    assert [document.canonical_url for document in result.documents] == [
        "https://selectcommittees.parliament.nz/v/17/aaa"]
    assert any("'Long-Term Insights'" in reason for reason in result.degraded_reasons)


def test_a_missing_or_blank_document_type_is_omitted_not_guessed():
    body = (
        b'{"totalResults": 2, "results": ['
        b'{"id": "aaa", "title": "No type at all", "publicationDate": "2026-07-20T00:00:00Z"},'
        b'{"id": "bbb", "title": "Blank type", "documentType": "   ", '
        b'"publicationDate": "2026-07-20T00:00:00Z"}]}')
    result = _connector(PARLIAMENT_SELECT_COMMITTEES, body).fetch(T0)
    assert result.documents == ()
    assert result.health is CivicSourceHealth.DEGRADED


def test_committee_routes_are_canonicalized_and_id_stable(committees):
    """Deterministic identity: the route is the source's own id, canonicalized, and two fetches
    of the same page produce the same URLs."""
    first = committees.fetch(T0)
    second = _connector(
        PARLIAMENT_SELECT_COMMITTEES, "parliament_committees_search.json").fetch(T0)
    assert [d.canonical_url for d in first.documents] == [
        d.canonical_url for d in second.documents]
    for document in first.documents:
        assert document.canonical_url.startswith("https://")
        assert "#" not in document.canonical_url
        assert document.external_key in document.canonical_url


def test_committees_still_refuse_a_blanket_id_route():
    """Without a type table there is no safe `/v/{id}`, and this source must never acquire one:
    every path on this site answers HTTP 200."""
    definition = replace(
        PARLIAMENT_SELECT_COMMITTEES, url_view_index_by_type={}, url_external_by_type={})
    result = _connector(definition, "parliament_committees_search.json").fetch(T0)
    assert result.documents == ()
    assert PARLIAMENT_SELECT_COMMITTEES.allow_id_url_fallback is False


# ============================================================================
# Unresolved types: aggregated, counted, ordered, bounded
# ============================================================================


def test_one_reason_per_unresolved_type_carries_that_types_own_count():
    """The count is what makes the reason actionable -- "a new type appeared, two records" and
    "a route silently broke, ninety-six records" need different responses."""
    result = _one_page_committees(
        {"Ghost Type": 96, "Spectre Type": 3, "Briefing": 2}).fetch(T0)
    assert _unrouted_reasons(result) == [
        "96 hits of documentType 'Ghost Type' omitted: no verified view route",
        "3 hits of documentType 'Spectre Type' omitted: no verified view route",
    ]
    assert len(result.documents) == 2


def test_the_unresolved_reasons_are_ordered_by_type_not_by_arrival():
    """Sorted by type name, so two fetches of the same corpus produce byte-identical reasons no
    matter what order the API paged them in. A reason list that reshuffles between runs cannot
    be diffed between runs, which is most of why it is recorded."""
    expected = [
        "2 hits of documentType 'Alpha Type' omitted: no verified view route",
        "3 hits of documentType 'Mu Type' omitted: no verified view route",
        "1 hit of documentType 'Zeta Type' omitted: no verified view route",
    ]
    for counts in ({"Zeta Type": 1, "Alpha Type": 2, "Mu Type": 3},
                   {"Mu Type": 3, "Zeta Type": 1, "Alpha Type": 2}):
        assert _unrouted_reasons(_one_page_committees(counts).fetch(T0)) == expected


def test_a_whole_page_of_one_unresolved_type_costs_one_reason_not_a_page_of_them():
    """The regression this replaces: 500 hits of an unrouted type produced 500 near-identical
    reasons -- tens of kilobytes carried through the result, the health record and every log of
    either -- on every single fetch."""
    result = _one_page_committees({"Ghost Type": 500}).fetch(T0)
    assert _unrouted_reasons(result) == [
        "500 hits of documentType 'Ghost Type' omitted: no verified view route"]
    assert len("".join(result.degraded_reasons)) < 500


def test_the_reasons_stay_bounded_at_the_scale_that_actually_broke():
    """The measured case: the live corpus was omitting 330 hits across several types, and the
    per-record form spent roughly 50 KB of reason text on every fetch to say a handful of
    things. Reasons are O(types), not O(records), so the same fetch now costs a few hundred
    bytes -- and the types, counts and ordering all survive."""
    counts = {"Ghost Type": 220, "Spectre Type": 96, "Wraith Type": 13, "Briefing": 4}
    result = _one_page_committees(counts).fetch(T0)
    reasons = _unrouted_reasons(result)
    assert reasons == [
        "220 hits of documentType 'Ghost Type' omitted: no verified view route",
        "96 hits of documentType 'Spectre Type' omitted: no verified view route",
        "13 hits of documentType 'Wraith Type' omitted: no verified view route",
    ]
    per_record_bytes = sum(
        len(f"page 1: nz_parliament_select_committees: hit {document_type.lower()}-{index} is "
            f"documentType {document_type!r}, which has no verified view route; omitting rather "
            "than fabricating a URL")
        for document_type, count in counts.items() if document_type != "Briefing"
        for index in range(count))
    assert per_record_bytes > 40_000
    assert len("".join(result.degraded_reasons)) < 1_000
    assert len(reasons) == 3


def test_an_all_unresolved_page_is_still_honestly_degraded():
    """Aggregation shortens the reasons; it must not soften them. Nothing was published, so the
    fetch is DEGRADED and incomplete rather than a healthy empty week."""
    result = _one_page_committees({"Ghost Type": 4}).fetch(T0)
    assert result.documents == ()
    assert result.health is CivicSourceHealth.DEGRADED
    assert result.is_complete is False


def test_a_fully_routed_page_carries_no_unresolved_reason_at_all():
    result = _one_page_committees(
        {"Briefing": 2, "Financial Scrutiny": 2, "Bill": 1}).fetch(T0)
    assert _unrouted_reasons(result) == []
    assert len(result.documents) == 5
    assert result.health is CivicSourceHealth.OK


def test_recovered_routes_let_the_item_cap_stop_the_fetch_earlier():
    """Unrouted hits never counted towards `max_items`, so the connector kept paging past a
    corpus it was discarding: a page that is mostly unrouted costs a request and buys almost
    nothing. With the routes recovered, the same cap is reached on the first page."""
    definition = replace(
        PARLIAMENT_SELECT_COMMITTEES, page_size=6, max_items=6, max_pages=10)
    routed = _committee_page({"Financial Scrutiny": 6}, page_size=6, total=600)
    mostly_unrouted = _committee_page(
        {"Financial Scrutiny": 2, "Ghost Type": 4}, page_size=6, total=600)

    recovered = _paging_connector(definition, lambda _: routed)
    recovered_result = recovered.fetch(T0)
    assert recovered_result.pages_fetched == 1
    assert len(recovered.transport.recorded.calls) == 1
    assert len(recovered_result.documents) == 6

    unrecovered = _paging_connector(definition, lambda _: mostly_unrouted)
    unrecovered_result = unrecovered.fetch(T0)
    assert unrecovered_result.pages_fetched == 3
    assert len(unrecovered.transport.recorded.calls) == 3
    assert len(unrecovered_result.documents) == 6
    # Eight, not twelve: the item cap stops the third page mid-way, so only the two pages that
    # ran to completion contributed their unrouted hits. The count reports what was actually
    # seen and discarded rather than an extrapolation.
    assert _unrouted_reasons(unrecovered_result) == [
        "8 hits of documentType 'Ghost Type' omitted: no verified view route"]


def test_the_page_cap_still_reports_what_it_left_behind():
    """Recovering routes must not quieten the cap: stopping short of a reported total is still
    an honest incompleteness, reported separately from any unresolved type."""
    definition = replace(
        PARLIAMENT_SELECT_COMMITTEES, page_size=2, max_pages=2, max_items=50)
    body = _committee_page({"Briefing": 2}, page_size=2, total=99)
    result = _paging_connector(definition, lambda _: body).fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("2-page cap" in reason for reason in result.degraded_reasons)
    assert _unrouted_reasons(result) == []
