# tests/civic/test_sitemaps.py
"""Sitemap sources: gzip handling, index-vs-urlset detection, one level of nesting, crawl-delay
pacing across a multi-request crawl, and partial-crawl honesty."""
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

from beehive.civic.catalog import OAG_SITEMAP, OMBUDSMAN_SITEMAP, STATS_NZ_SITEMAP
from beehive.civic.connectors.sitemaps import SitemapConnector
from beehive.civic.documents import CivicSourceError, CivicSourceHealth
from beehive.civic.fetching import CivicEndpoint
from beehive.connectors.http import ConnectorHttpErrorKind
from beehive.domain.civic import CivicEvidenceTier
from tests.civic.civic_stubs import FakeClock, fixture_bytes, http_error, transport_for

T0 = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)

_STATS_INDEX = STATS_NZ_SITEMAP.endpoint.url
_STATS_RELEASES = "https://www.stats.govt.nz/sitemap-releases.xml"
_STATS_METHODS = "https://www.stats.govt.nz/sitemap-methods.xml"


def _connector(definition, routes, *, clock=None):
    transport = transport_for(routes, clock=clock)
    connector = SitemapConnector(definition, transport)
    connector.transport = transport  # type: ignore[attr-defined]
    return connector


# ============================================================================
# OAG: gzipped sitemap
# ============================================================================


def test_a_gzipped_sitemap_is_decompressed_and_parsed():
    connector = _connector(
        OAG_SITEMAP,
        {OAG_SITEMAP.endpoint.url: (fixture_bytes("oag_sitemap.xml.gz"), "application/gzip")})
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.OK
    document = result.documents[0]
    assert document.canonical_url == (
        "https://ao.parliament.nz/reports/2026/local-government-procurement-review")
    assert document.publisher == "Office of the Auditor-General"
    assert document.tier is CivicEvidenceTier.OFFICIAL


def test_a_sitemap_entry_has_a_modification_date_but_no_publication_date():
    """Exactly the case the three-timestamp split exists for: a sitemap gives `lastmod` only."""
    connector = _connector(
        OAG_SITEMAP,
        {OAG_SITEMAP.endpoint.url: (fixture_bytes("oag_sitemap.xml.gz"), "application/gzip")})
    document = connector.fetch(T0).documents[0]
    assert document.published_at is None
    assert document.updated_at.date().isoformat() == "2026-08-09"
    assert document.first_seen_at == T0
    assert document.effective_at == document.updated_at


def test_a_corrupt_gzip_payload_is_a_source_error():
    connector = _connector(
        OAG_SITEMAP, {OAG_SITEMAP.endpoint.url: (b"not gzip", "application/gzip")})
    with pytest.raises(CivicSourceError, match="invalid gzip"):
        connector.fetch(T0)


def test_the_sitemap_url_is_the_document_identity():
    """A sitemap gives no id, so the canonical URL is the only stable identity available."""
    connector = _connector(
        OAG_SITEMAP,
        {OAG_SITEMAP.endpoint.url: (fixture_bytes("oag_sitemap.xml.gz"), "application/gzip")})
    document = connector.fetch(T0).documents[0]
    assert document.external_key == document.canonical_url


# ============================================================================
# Ombudsman: plain urlset
# ============================================================================


def test_a_plain_urlset_produces_one_document_per_url():
    connector = _connector(
        OMBUDSMAN_SITEMAP,
        {OMBUDSMAN_SITEMAP.endpoint.url: fixture_bytes("ombudsman_sitemap.xml")})
    result = connector.fetch(T0)
    assert len(result.documents) == 2
    assert result.pages_fetched == 1


def test_a_title_is_derived_from_the_url_never_from_remote_page_text():
    connector = _connector(
        OMBUDSMAN_SITEMAP,
        {OMBUDSMAN_SITEMAP.endpoint.url: fixture_bytes("ombudsman_sitemap.xml")})
    titles = [doc.title for doc in connector.fetch(T0).documents]
    assert titles[0] == "Opinion official information request ministry"


def test_path_filtering_keeps_the_crawl_to_the_sections_we_asked_for():
    definition = replace(OMBUDSMAN_SITEMAP, path_includes=("/resources/",))
    connector = _connector(
        definition, {definition.endpoint.url: fixture_bytes("ombudsman_sitemap.xml")})
    documents = connector.fetch(T0).documents
    assert len(documents) == 1
    assert "/resources/" in documents[0].canonical_url


# ============================================================================
# Stats NZ: index + children + crawl delay
# ============================================================================


@pytest.fixture
def stats_routes():
    return {
        _STATS_INDEX: fixture_bytes("stats_sitemap_index.xml"),
        _STATS_RELEASES: fixture_bytes("stats_sitemap_releases.xml"),
        _STATS_METHODS: (
            b'<?xml version="1.0"?>'
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>https://www.stats.govt.nz/methods/a</loc></url></urlset>"),
    }


def test_a_sitemap_index_is_followed_one_level(stats_routes):
    connector = _connector(STATS_NZ_SITEMAP, stats_routes)
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.OK
    assert result.pages_fetched == 3
    assert len(result.documents) == 3


def test_index_versus_urlset_is_detected_by_root_tag_not_filename(stats_routes):
    """`sitemap.xml` is used for both shapes across these three sites."""
    connector = _connector(
        STATS_NZ_SITEMAP,
        {_STATS_INDEX: fixture_bytes("stats_sitemap_releases.xml")})
    result = connector.fetch(T0)
    assert result.pages_fetched == 1
    assert len(result.documents) == 2


def test_the_ten_second_crawl_delay_is_honoured_between_every_request(stats_routes):
    clock = FakeClock()
    connector = _connector(STATS_NZ_SITEMAP, stats_routes, clock=clock)
    connector.fetch(T0)
    assert STATS_NZ_SITEMAP.crawl_delay_seconds == 10.0
    # Three requests to one host: the first is free, the next two each wait the full delay.
    assert clock.slept == [10.0, 10.0]


def test_one_failing_child_degrades_the_result_and_keeps_the_others(stats_routes):
    stats_routes[_STATS_METHODS] = http_error(ConnectorHttpErrorKind.TRANSIENT, "503")
    connector = _connector(STATS_NZ_SITEMAP, stats_routes)
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert len(result.documents) == 2
    assert "child sitemap" in result.degraded_reasons[0]


def test_more_children_than_the_cap_degrades_rather_than_crawling_them_all(stats_routes):
    definition = replace(STATS_NZ_SITEMAP, max_children=1)
    connector = _connector(definition, stats_routes)
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert "2 children" in result.degraded_reasons[0]
    assert result.pages_fetched == 2


def test_nesting_beyond_one_level_is_reported_never_recursed_into(stats_routes):
    stats_routes[_STATS_METHODS] = fixture_bytes("stats_sitemap_index.xml")
    connector = _connector(STATS_NZ_SITEMAP, stats_routes)
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("beyond one level" in reason for reason in result.degraded_reasons)


def test_the_url_budget_bounds_the_whole_crawl(stats_routes):
    definition = replace(STATS_NZ_SITEMAP, max_urls=2)
    connector = _connector(definition, stats_routes)
    result = connector.fetch(T0)
    assert len(result.documents) == 2
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("2-URL cap" in reason for reason in result.degraded_reasons)


def test_an_entry_with_no_loc_degrades_rather_than_losing_the_sitemap():
    body = (
        b'<?xml version="1.0"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><lastmod>2026-08-01</lastmod></url>"
        b"<url><loc>https://www.ombudsman.parliament.nz/a</loc></url></urlset>")
    connector = _connector(OMBUDSMAN_SITEMAP, {OMBUDSMAN_SITEMAP.endpoint.url: body})
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert len(result.documents) == 1


def test_an_unsafe_loc_is_skipped_and_reported():
    body = (
        b'<?xml version="1.0"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>javascript:alert(1)</loc></url>"
        b"<url><loc>https://www.ombudsman.parliament.nz/a</loc></url></urlset>")
    connector = _connector(OMBUDSMAN_SITEMAP, {OMBUDSMAN_SITEMAP.endpoint.url: body})
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert len(result.documents) == 1


def test_an_empty_sitemap_is_empty_not_ok():
    body = (
        b'<?xml version="1.0"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
    connector = _connector(OMBUDSMAN_SITEMAP, {OMBUDSMAN_SITEMAP.endpoint.url: body})
    assert connector.fetch(T0).health is CivicSourceHealth.EMPTY


def test_a_child_sitemap_outside_the_host_allowlist_is_refused(stats_routes):
    stats_routes[_STATS_INDEX] = (
        b'<?xml version="1.0"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://evil.example.com/sitemap.xml</loc></sitemap></sitemapindex>")
    connector = _connector(STATS_NZ_SITEMAP, stats_routes)
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("evil.example.com" in reason for reason in result.degraded_reasons)


def test_config_must_be_empty():
    connector = _connector(
        OMBUDSMAN_SITEMAP,
        {OMBUDSMAN_SITEMAP.endpoint.url: fixture_bytes("ombudsman_sitemap.xml")})
    with pytest.raises(ValueError, match="config must be empty"):
        connector.fetch(T0, config={"url": "https://evil.example.com/sitemap.xml"})


def test_the_endpoint_declares_a_gzip_content_type_for_oag():
    assert OAG_SITEMAP.is_gzipped
    assert "application/gzip" in OAG_SITEMAP.endpoint.content_types
    assert isinstance(OAG_SITEMAP.endpoint, CivicEndpoint)


# ============================================================================
# A capped crawl is never reported as OK
# ============================================================================


@pytest.mark.parametrize(
    "definition", [OAG_SITEMAP, OMBUDSMAN_SITEMAP], ids=lambda d: d.source_key)
def test_a_capped_flat_urlset_is_degraded_never_ok(definition):
    """The flat `<urlset>` path used to truncate silently -- only the index path reported its
    cap. A consumer reading "500 URLs, healthy" would conclude the rest had been withdrawn."""
    urls = "".join(
        f"<url><loc>https://{urlsplit(definition.endpoint.url).hostname}/doc-{index}</loc>"
        f"<lastmod>2026-08-01</lastmod></url>" for index in range(50))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>").encode()
    if definition.is_gzipped:
        import gzip
        body = gzip.compress(body)
        content_type = "application/gzip"
    else:
        content_type = "application/xml"

    capped = replace(definition, max_urls=10)
    connector = _connector(capped, {capped.endpoint.url: (body, content_type)})
    result = connector.fetch(T0)

    assert len(result.documents) == 10
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("kept 10 of 50 matching URLs" in reason for reason in result.degraded_reasons)


def test_an_uncapped_flat_urlset_stays_ok():
    connector = _connector(
        OMBUDSMAN_SITEMAP,
        {OMBUDSMAN_SITEMAP.endpoint.url: fixture_bytes("ombudsman_sitemap.xml")})
    assert connector.fetch(T0).health is CivicSourceHealth.OK


def test_the_cap_reason_counts_only_urls_that_passed_the_path_filter():
    """Otherwise a filtered-out marketing page would look like a suppressed document."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://www.ombudsman.parliament.nz/resources/a</loc></url>"
        "<url><loc>https://www.ombudsman.parliament.nz/resources/b</loc></url>"
        "<url><loc>https://www.ombudsman.parliament.nz/about-us/contact</loc></url>"
        "</urlset>").encode()
    definition = replace(OMBUDSMAN_SITEMAP, path_includes=("/resources/",), max_urls=1)
    connector = _connector(definition, {definition.endpoint.url: body})
    result = connector.fetch(T0)
    assert any("kept 1 of 2 matching URLs" in reason for reason in result.degraded_reasons)


# ============================================================================
# Percent-decoded titles
# ============================================================================
#
# Sitemap paths are percent-encoded, and a title is a display field. Splitting before decoding
# produced literal escape text: ACT's /news/new-zealand%E2%80%99s-new-religion became
# "New zealand%e2%80%99s new religion".


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.act.org.nz/news/new-zealand%E2%80%99s-new-religion",
         "New zealand\u2019s new religion"),
        ("https://x.govt.nz/news/caf%C3%A9-review", "Caf\u00e9 review"),
        ("https://x.govt.nz/news/report%20on%20rates", "Report on rates"),
        ("https://x.govt.nz/news/m%C4%81ori-wards", "M\u0101ori wards"),
    ])
def test_a_percent_encoded_segment_is_decoded_before_it_becomes_a_title(url, expected):
    from beehive.civic.connectors.sitemaps import _title_from_url

    assert _title_from_url(url) == expected


def test_no_title_ever_retains_a_percent_escape():
    from beehive.civic.connectors.sitemaps import _title_from_url

    assert "%" not in _title_from_url(
        "https://www.act.org.nz/news/new-zealand%E2%80%99s-new-religion")


def test_a_malformed_escape_degrades_one_title_rather_than_the_sitemap():
    from beehive.civic.connectors.sitemaps import _title_from_url

    assert _title_from_url("https://x.govt.nz/news/bad%ZZescape") == "Bad%zzescape"


def test_control_characters_never_reach_a_title():
    """Percent-decoding can yield C0 controls from a malformed or hostile path, and this is a
    display field."""
    from beehive.civic.connectors.sitemaps import _title_from_url

    title = _title_from_url("https://x.govt.nz/news/alpha%00%07%1Fbeta")
    assert all(ord(char) >= 0x20 for char in title)
    assert title == "Alpha beta"


def test_an_undecodable_byte_sequence_is_replaced_not_raised():
    from beehive.civic.connectors.sitemaps import _title_from_url

    assert _title_from_url("https://x.govt.nz/news/%FF%FE-report")


def test_decoding_does_not_change_the_canonical_url():
    """Only the derived title is decoded; the URL itself stays exactly as the sitemap published
    it, because that is what identity and deduplication are keyed on."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://www.ombudsman.parliament.nz/news/caf%C3%A9-review</loc></url>"
        "</urlset>").encode()
    connector = _connector(OMBUDSMAN_SITEMAP, {OMBUDSMAN_SITEMAP.endpoint.url: body})
    document = connector.fetch(T0).documents[0]
    assert document.canonical_url.endswith("/news/caf%C3%A9-review")
    assert document.title == "Caf\u00e9 review"
