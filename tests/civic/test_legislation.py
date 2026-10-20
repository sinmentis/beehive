# tests/civic/test_legislation.py
"""NZ Legislation: sitemap discovery plus bounded per-document hydration, the optional Works API,
and the two endpoints that must never be configured as sources.

The correction pinned here is that there is no aggregate keyless XML feed. `/subscribe/en/latest.
xml` was configured as one and never served a listing, which produced a permanently empty source
that reported itself healthy -- so it now sits in `TOMBSTONE_PATHS` alongside the retired RSS
path and cannot be reintroduced."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from beehive.civic.catalog import (LEGISLATION_HYDRATION, LEGISLATION_SITEMAP,
                                    LEGISLATION_WORKS_API, get_source, legislation_hydrator,
                                    source_keys)
from beehive.civic.connectors.legislation import (LEGISLATION_API_KEY_ENV, TOMBSTONE_PATHS,
                                                   LegislationApiConnector,
                                                   LegislationHydrator,
                                                   assert_not_tombstone, document_xml_url)
from beehive.civic.connectors.sitemaps import SitemapConnector
from beehive.civic.documents import CivicSourceHealth, CivicTombstoneError
from beehive.civic.fetching import CivicEndpoint, SITEMAP_MAX_BYTES
from beehive.domain.civic import CivicEvidenceTier
from tests.civic.civic_stubs import fixture_bytes, transport_for

T0 = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
_LEG_HOSTS = frozenset({"legislation.govt.nz", "www.legislation.govt.nz"})
# The shape the live sitemap actually publishes.
_ACT_PAGE = "https://www.legislation.govt.nz/act/imperial/1267/23/en/latest/"
_ACT_XML = "https://www.legislation.govt.nz/act/imperial/1267/23/en/latest.xml"


# ============================================================================
# The endpoints that must never be sources
# ============================================================================


def test_both_dead_endpoints_are_recorded():
    assert "/subscribe/nzpco-rss.xml" in TOMBSTONE_PATHS
    assert "/subscribe/en/latest.xml" in TOMBSTONE_PATHS


@pytest.mark.parametrize(
    "path", ["/subscribe/nzpco-rss.xml", "/subscribe/en/latest.xml"])
def test_configuring_a_dead_endpoint_is_refused_before_any_request(path):
    with pytest.raises(CivicTombstoneError, match="not a live aggregate source"):
        assert_not_tombstone(f"https://www.legislation.govt.nz{path}", source_key="s")


def test_the_nonexistent_aggregate_source_is_gone_from_the_catalog():
    """It reported a permanently empty feed as a healthy week, which is the exact failure the
    health vocabulary exists to prevent."""
    assert "nz_legislation_latest" not in source_keys()


def test_the_tombstone_check_ignores_a_trailing_slash():
    with pytest.raises(CivicTombstoneError):
        assert_not_tombstone(
            "https://www.legislation.govt.nz/subscribe/en/latest.xml/", source_key="s")


def test_a_live_document_path_is_accepted():
    assert_not_tombstone(_ACT_PAGE, source_key="s")


def test_a_definition_pointed_at_a_dead_path_cannot_be_constructed():
    with pytest.raises(CivicTombstoneError):
        replace(
            LEGISLATION_WORKS_API,
            endpoint=CivicEndpoint(
                url="https://www.legislation.govt.nz/subscribe/en/latest.xml",
                allowed_hosts=_LEG_HOSTS))


# ============================================================================
# Step 1: sitemap discovery
# ============================================================================


def test_the_keyless_discovery_path_is_the_sitemap():
    entry = get_source("nz_legislation_sitemap")
    assert entry.url == "https://www.legislation.govt.nz/sitemap.xml"
    assert entry.tier is CivicEvidenceTier.OFFICIAL


def test_the_legislation_sitemap_gets_the_larger_byte_cap():
    """~5.2 MiB and ~43,000 URLs: the default 4 MiB cap would fail the fetch outright."""
    assert LEGISLATION_SITEMAP.endpoint.max_bytes == SITEMAP_MAX_BYTES
    assert SITEMAP_MAX_BYTES == 8 * 1024 * 1024


def test_the_sitemap_yields_canonical_work_urls():
    connector = SitemapConnector(
        LEGISLATION_SITEMAP,
        transport_for({
            LEGISLATION_SITEMAP.endpoint.url: fixture_bytes(
                "legislation_sitemap_sample.xml")}))
    documents = connector.fetch(T0).documents
    urls = [document.canonical_url for document in documents]
    assert _ACT_PAGE in urls
    # Every live kind is represented in the sample, including the tilde form that yields no XML.
    assert any("/secondary-legislation/pco-drafted/" in url for url in urls)
    assert any("/amendment-paper/" in url for url in urls)
    assert any("/bill/" in url for url in urls)


def test_a_large_sitemap_parses_within_the_cap_and_reports_the_shortfall():
    """The real corpus is far larger than any sane per-run item budget, so the cap is
    unavoidable -- what matters is that the shortfall is stated, not silently truncated."""
    urls = "".join(
        f"<url><loc>https://www.legislation.govt.nz/act/public/2026/{index + 1:04d}/en/latest/"
        f"</loc><lastmod>2026-08-01</lastmod></url>"
        for index in range(3000))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>").encode()
    assert len(body) < SITEMAP_MAX_BYTES

    definition = replace(LEGISLATION_SITEMAP, max_urls=100)
    connector = SitemapConnector(
        definition, transport_for({definition.endpoint.url: body}))
    result = connector.fetch(T0)

    assert len(result.documents) == 100
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("kept 100 of 3000 matching URLs" in reason
               for reason in result.degraded_reasons)


# ============================================================================
# Step 2: bounded per-document hydration
# ============================================================================


@pytest.mark.parametrize(
    "page_url,expected",
    [
        # The four kinds the live sitemap actually publishes, with their real entry counts:
        # secondary-legislation 20,980 / act 17,470 / amendment-paper 2,806 / bill 1,893.
        ("https://www.legislation.govt.nz/act/imperial/1267/23/en/latest/",
         "https://www.legislation.govt.nz/act/imperial/1267/23/en/latest.xml"),
        ("https://www.legislation.govt.nz/secondary-legislation/pco-drafted/2024/119/en/latest/",
         "https://www.legislation.govt.nz/secondary-legislation/pco-drafted/2024/119/en/latest.xml"),
        ("https://www.legislation.govt.nz/amendment-paper/government/2006/50/en/latest/",
         "https://www.legislation.govt.nz/amendment-paper/government/2006/50/en/latest.xml"),
        ("https://www.legislation.govt.nz/bill/government/1998/238/en/latest/",
         "https://www.legislation.govt.nz/bill/government/1998/238/en/latest.xml"),
        # The older link shape still served across the site.
        ("https://www.legislation.govt.nz/act/public/2026/0031/latest/DLM4326000.html",
         "https://www.legislation.govt.nz/act/public/2026/0031/en/latest.xml"),
    ])
def test_every_live_sitemap_kind_derives_its_xml_url(page_url, expected):
    assert document_xml_url(page_url) == expected


def test_all_four_live_kinds_are_covered():
    """A kind missing from the pattern silently drops that whole slice of the corpus -- and
    secondary legislation alone is 20,980 of the 43,149 entries."""
    for kind, sample in [
        ("act", "https://www.legislation.govt.nz/act/imperial/1267/23/en/latest/"),
        ("secondary-legislation",
         "https://www.legislation.govt.nz/secondary-legislation/pco-drafted/1887/1/en/latest/"),
        ("amendment-paper",
         "https://www.legislation.govt.nz/amendment-paper/government/2006/50/en/latest/"),
        ("bill", "https://www.legislation.govt.nz/bill/government/1998/238/en/latest/"),
    ]:
        assert document_xml_url(sample) is not None, kind


def test_kinds_that_do_not_exist_in_the_sitemap_are_not_matched():
    """`regulation` and `sop` were assumed by an earlier version and appear nowhere as PATH
    kinds -- confusingly they are XML ROOT TAGS, which is how the wrong assumption survived."""
    assert document_xml_url(
        "https://www.legislation.govt.nz/regulation/public/2026/0114/en/latest/") is None
    assert document_xml_url(
        "https://www.legislation.govt.nz/sop/government/2006/50/en/latest/") is None


def test_agency_drafted_tilde_items_derive_no_url_because_their_xml_is_not_served():
    """9,673 secondary-legislation entries use a tilde-prefixed internal number, and their .xml
    answers HTTP 500. Deriving anyway would spend a request each to collect a guaranteed error."""
    assert document_xml_url(
        "https://www.legislation.govt.nz/secondary-legislation/agency-drafted/2000/~274499/en/latest/"
    ) is None


@pytest.mark.parametrize(
    "page_url",
    [
        "https://www.legislation.govt.nz/about/index.html",
        "https://www.legislation.govt.nz/",
        "https://www.legislation.govt.nz/act/public/2026/0031",
        "https://example.com/act/public/2026/0031/en/latest/",
        "not a url",
    ])
def test_an_unverified_path_derives_no_url_rather_than_guessing_one(page_url):
    """The site soft-404s a surprising number of near-miss paths, so a guessed URL can hydrate a
    not-found page into what a reader sees as the text of an Act."""
    assert document_xml_url(page_url) is None


# ============================================================================
# Root attributes are where identity and dates actually live
# ============================================================================

_KIND_FIXTURES = {
    "act": ("https://www.legislation.govt.nz/act/imperial/1267/23/en/latest/",
            "legislation_act.xml"),
    "secondary-legislation": (
        "https://www.legislation.govt.nz/secondary-legislation/pco-drafted/2024/119/en/latest/",
        "legislation_secondary.xml"),
    "amendment-paper": (
        "https://www.legislation.govt.nz/amendment-paper/government/2006/50/en/latest/",
        "legislation_amendment_paper.xml"),
    "bill": ("https://www.legislation.govt.nz/bill/government/1998/238/en/latest/",
             "legislation_bill.xml"),
}


def _hydrate(kind):
    page, fixture = _KIND_FIXTURES[kind]
    transport = transport_for({document_xml_url(page): fixture_bytes(fixture)})
    return LegislationHydrator(LEGISLATION_HYDRATION, transport).hydrate([page], T0)


@pytest.mark.parametrize(
    "kind,external_key,root",
    [("act", "DLM10900", "act"),
     ("secondary-legislation", "LMS963891", "regulation"),
     ("amendment-paper", "DLM1106000", "sop"),
     ("bill", "DLM947600", "bill")])
def test_the_dlm_or_lms_id_comes_from_the_root_attribute(kind, external_key, root):
    """Identity is a root ATTRIBUTE, not an element -- and the root tag differs per kind, which
    is why the parser must not key off the tag."""
    document = _hydrate(kind).documents[0]
    assert document.external_key == external_key
    assert document.raw_metadata["legislation_root"] == root


def test_an_acts_assent_and_as_at_dates_are_read_from_root_attributes():
    document = _hydrate("act").documents[0]
    assert document.published_at is None
    assert document.raw_metadata["event_date"] == "1988-07-28"
    assert document.raw_metadata["event_date_provenance"]["source"] == (
        "legislation-xml-attribute-date.assent")
    assert document.updated_at.date().isoformat() == "2008-01-01"     # date.as.at
    assert document.title == "Statute of Marlborough 1267"


def test_secondary_legislation_uses_gazetted_and_as_at():
    """It carries no assent date; `date.gazetted` is the strongest publication statement and
    `date.signed`/`date.first.valid` back it up."""
    document = _hydrate("secondary-legislation").documents[0]
    assert document.published_at.date().isoformat() == "2024-06-06"   # date.gazetted
    assert document.updated_at.date().isoformat() == "2024-06-04"     # date.as.at
    assert document.title.startswith("Income Tax (Deemed Rate of Return")


def test_the_nulldate_sentinel_is_never_parsed_as_a_date():
    """`date.terminated="nulldate"` is a real value the site emits for a live instrument."""
    from beehive.civic.fetching import parse_iso8601

    body = fixture_bytes("legislation_secondary.xml").decode()
    assert 'date.terminated="nulldate"' in body
    assert parse_iso8601("nulldate") is None


def test_an_amendment_paper_has_no_dates_and_says_so_rather_than_inventing_them():
    document = _hydrate("amendment-paper").documents[0]
    assert document.published_at is None
    assert document.updated_at is None
    assert document.title == "Criminal Procedure Bill"


def test_an_amendment_paper_falls_back_to_billref_for_its_title():
    """Those documents carry no <title> at all."""
    assert "<title>" not in fixture_bytes("legislation_amendment_paper.xml").decode()
    assert _hydrate("amendment-paper").documents[0].title == "Criminal Procedure Bill"


def test_every_fixture_is_trimmed_from_a_real_document():
    for _kind, (_page, fixture) in _KIND_FIXTURES.items():
        assert b"trimmed from https://www.legislation.govt.nz/" in fixture_bytes(fixture)


# ============================================================================
# Identity replacement is deterministic
# ============================================================================


def test_hydration_records_the_sitemap_identity_it_replaces():
    """A sitemap document is keyed on its URL; a hydrated one on the DLM/LMS id. Recording the
    replaced key is what lets a caller migrate an existing Candidate instead of the same
    instrument appearing under two dedupe keys."""
    page, _fixture = _KIND_FIXTURES["act"]
    document = _hydrate("act").documents[0]
    assert document.raw_metadata["sitemap_external_key"] == page
    assert document.canonical_url == page
    assert document.external_key != page


def test_hydrating_the_same_document_twice_yields_the_same_identity():
    first = _hydrate("act").documents[0]
    second = _hydrate("act").documents[0]
    assert first.external_key == second.external_key
    assert first.dedupe_key == second.dedupe_key


def test_the_sitemap_and_hydrated_documents_agree_on_the_canonical_url():
    """The URL is what ties the two views together, so it must survive hydration untouched."""
    page, _fixture = _KIND_FIXTURES["act"]
    sitemap_connector = SitemapConnector(
        LEGISLATION_SITEMAP,
        transport_for({LEGISLATION_SITEMAP.endpoint.url: fixture_bytes(
            "legislation_sitemap_sample.xml")}))
    discovered = {d.canonical_url for d in sitemap_connector.fetch(T0).documents}
    assert _hydrate("act").documents[0].canonical_url == page
    assert all(url.startswith("https://www.legislation.govt.nz/") for url in discovered)


def test_hydration_fetches_the_derived_xml_and_keeps_the_page_url_canonical():
    transport = transport_for({_ACT_XML: fixture_bytes("legislation_act.xml")})
    hydrator = LegislationHydrator(LEGISLATION_HYDRATION, transport)
    result = hydrator.hydrate([_ACT_PAGE], T0)

    assert result.health is CivicSourceHealth.OK
    document = result.documents[0]
    assert document.external_key == "DLM10900"
    assert document.title == "Statute of Marlborough 1267"
    assert document.canonical_url == _ACT_PAGE
    assert document.raw_metadata["legislation_xml_url"] == _ACT_XML
    assert document.published_at is None
    assert document.raw_metadata["event_date"] == "1988-07-28"
    assert document.updated_at.date().isoformat() == "2008-01-01"


def test_hydration_is_bounded_and_says_so():
    pages = [
        f"https://www.legislation.govt.nz/act/public/2026/{index + 1:04d}/en/latest/"
        for index in range(5)]
    routes = {
        document_xml_url(page): fixture_bytes("legislation_act.xml") for page in pages}
    definition = replace(LEGISLATION_HYDRATION, max_documents=2)
    hydrator = LegislationHydrator(definition, transport_for(routes))
    result = hydrator.hydrate(pages, T0)

    assert result.pages_fetched == 2
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("2-document hydration cap" in reason for reason in result.degraded_reasons)


def test_an_unverified_url_is_skipped_and_reported_without_a_request():
    transport = transport_for({_ACT_XML: fixture_bytes("legislation_act.xml")})
    hydrator = LegislationHydrator(LEGISLATION_HYDRATION, transport)
    result = hydrator.hydrate(
        ["https://www.legislation.govt.nz/about/index.html", _ACT_PAGE], T0)

    assert len(result.documents) == 1
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("no XML URL was derived" in reason for reason in result.degraded_reasons)
    assert transport.recorded.urls == [_ACT_XML]


def test_hydration_needs_no_credential():
    transport = transport_for({_ACT_XML: fixture_bytes("legislation_act.xml")})
    LegislationHydrator(LEGISLATION_HYDRATION, transport).hydrate([_ACT_PAGE], T0)
    headers = transport.recorded.calls[0].extra_headers or {}
    assert not any("key" in name.lower() for name in headers)


def test_hydration_is_byte_bounded_per_document():
    transport = transport_for({_ACT_XML: fixture_bytes("legislation_act.xml")})
    LegislationHydrator(LEGISLATION_HYDRATION, transport).hydrate([_ACT_PAGE], T0)
    assert transport.recorded.calls[0].max_bytes == 2 * 1024 * 1024


def test_hydration_config_must_be_empty():
    hydrator = legislation_hydrator(transport_for({}))
    with pytest.raises(ValueError, match="config must be empty"):
        hydrator.hydrate([], T0, config={"url": "https://evil.example.com"})


def test_hydrating_nothing_is_empty_not_ok():
    assert legislation_hydrator(transport_for({})).hydrate(
        [], T0).health is CivicSourceHealth.EMPTY


def test_the_hydrator_is_not_a_catalogued_listing_source():
    """It discovers nothing on its own, so listing it beside the listing sources would invite it
    being scheduled like one."""
    assert "nz_legislation_document" not in source_keys()


# ============================================================================
# Runtime XML tombstones
# ============================================================================


def test_the_real_pco_atom_tombstone_is_caught_during_hydration():
    """The fixture is the live body of the retired PCO feed, and it is the hard case in full:
    HTTP 200, `application/xml`, well-formed Atom, one entry -- so every content-type and
    parse check passes and a naive reader reports a healthy week forever."""
    transport = transport_for(
        {_ACT_XML: fixture_bytes("legislation_atom_tombstone.xml")})
    hydrator = LegislationHydrator(LEGISLATION_HYDRATION, transport)
    with pytest.raises(CivicTombstoneError, match="url_changed"):
        hydrator.hydrate([_ACT_PAGE], T0)


def test_the_real_tombstones_feed_title_is_innocuous():
    """Why the feed-level id is the load-bearing signal: the real document's title is just
    "New Zealand Legislation" and the retirement wording lives in entry CONTENT, which is
    deliberately never scanned -- so prose matching alone would have missed this entirely."""
    from beehive.civic.fetching import parse_xml

    root = parse_xml(
        fixture_bytes("legislation_atom_tombstone.xml"), source_key="s", url="u")
    title = root.find("{http://www.w3.org/2005/Atom}title").text
    assert "retired" not in title.lower()
    assert "no longer" not in title.lower()
    assert root.find("{http://www.w3.org/2005/Atom}id").text == "url_changed"


def test_an_html_tombstone_is_still_caught_by_the_content_type_assertion():
    transport = transport_for(
        {_ACT_XML: (fixture_bytes("legislation_tombstone.html"), "text/html")})
    hydrator = LegislationHydrator(LEGISLATION_HYDRATION, transport)
    with pytest.raises(CivicTombstoneError, match="retired-endpoint page"):
        hydrator.hydrate([_ACT_PAGE], T0)


# ============================================================================
# The optional Works API is unchanged and still optional
# ============================================================================


def _works(body=None, environ=None):
    body = body if body is not None else fixture_bytes("legislation_works.json")
    transport = transport_for(
        {LEGISLATION_WORKS_API.endpoint.url: (body, "application/json")},
        default_content_type="application/json")
    connector = LegislationApiConnector(
        LEGISLATION_WORKS_API, transport, environ=environ if environ is not None else {})
    connector.transport = transport  # type: ignore[attr-defined]
    return connector


def test_the_api_key_is_optional_and_absent_by_default():
    assert not _works(environ={}).is_available


def test_a_configured_key_makes_the_source_available():
    assert _works(environ={LEGISLATION_API_KEY_ENV: "secret"}).is_available


def test_fetching_without_a_key_fails_loudly_rather_than_sending_an_empty_header():
    with pytest.raises(ValueError, match="it is optional"):
        _works(environ={}).fetch(T0)


def test_the_key_is_sent_as_a_header_and_never_accepted_from_config():
    connector = _works(environ={LEGISLATION_API_KEY_ENV: "secret-key"})
    connector.fetch(T0)
    assert connector.transport.recorded.calls[0].extra_headers[
        "Ocp-Apim-Subscription-Key"] == "secret-key"
    with pytest.raises(ValueError, match="config must be empty"):
        connector.fetch(T0, config={"api_key": "attacker-supplied"})


def test_works_api_maps_its_records():
    document = _works(environ={LEGISLATION_API_KEY_ENV: "k"}).fetch(T0).documents[0]
    assert document.external_key == "DLM4326000"
    assert document.published_at is None
    assert document.raw_metadata["event_date"] == "2026-08-01"
    assert document.excerpt == "Amends enrolment deadlines."


def test_works_api_keeps_assent_independent_of_publication():
    body = (
        b'{"works":[{"id":"fixture-act","title":"Fixture Act",'
        b'"url":"https://www.legislation.govt.nz/act/public/2026/0001/latest/fixture.html",'
        b'"assentDate":"2026-09-02","publishedDate":"2026-09-05"}]}'
    )
    document = _works(
        body=body, environ={LEGISLATION_API_KEY_ENV: "fixture-key"},
    ).fetch(T0).documents[0]
    assert document.published_at.date().isoformat() == "2026-09-05"
    assert document.raw_metadata["event_date"] == "2026-09-02"
    assert document.raw_metadata["publication"]["source"] == "legislation-works-publishedDate"


def test_the_works_endpoint_is_pinned_to_the_api_host():
    connector = _works(environ={LEGISLATION_API_KEY_ENV: "k"})
    connector.fetch(T0)
    assert connector.transport.recorded.calls[0].allowed_hosts == frozenset(
        {"api.legislation.govt.nz"})


def test_the_keyless_paths_remain_available_without_a_key():
    from beehive.civic.catalog import enabled_sources

    keys = {entry.source_key for entry in enabled_sources({})}
    assert "nz_legislation_sitemap" in keys
    assert "nz_legislation_works" not in keys
