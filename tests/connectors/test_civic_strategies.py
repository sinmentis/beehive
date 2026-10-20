"""The four party/media reading strategies and the one seam every fetch goes through.

Everything here runs offline through `tests.civic.civic_stubs`, the same doubles the official
connectors use: one harness, one transport, one set of assertions about what was actually
requested.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from beehive.civic.documents import CivicSourceError, CivicSourceHealth
from beehive.civic.fetching import CivicEndpoint
from beehive.connectors.civic_catalog import (
    HTML_CONTENT_TYPES,
    MAX_DOCUMENTS_PER_FETCH,
    PARTY_MEDIA_MAX_BYTES,
    PARTY_MEDIA_USER_AGENT,
    CivicSourceRestrictedError,
    CivicSourceUnavailableError,
)
from beehive.connectors.civic_media import MEDIA_CATALOG
from beehive.connectors.civic_party import PARTY_CATALOG
from beehive.connectors.civic_strategies import (
    LinkIndexConnector,
    LinkIndexDefinition,
    fetch_entry,
    parse_wall_clock,
    party_media_transport,
    title_from_url,
)
from beehive.connectors.http import ConnectorHttpError, ConnectorHttpErrorKind, HttpResponse
from beehive.domain.civic import CivicEvidenceTier
from tests.civic.civic_stubs import transport_for
from tests.connectors.civic_fixtures_loader import civic_fixture

NOW = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)

_HTML = "text/html"
_XML = "application/xml"


def _fetch(source_key, catalog, fixture, *, content_type=_XML, limit=MAX_DOCUMENTS_PER_FETCH):
    entry = catalog.get(source_key)
    transport = transport_for(
        {entry.url: civic_fixture(fixture)}, default_content_type=content_type)
    return entry, fetch_entry(entry, NOW, transport=transport, limit=limit), transport


# ---------------------------------------------------------------------------
# News sitemaps: the one sitemap shape that carries real headlines and dates
# ---------------------------------------------------------------------------

def test_news_sitemap_keeps_the_declared_headline_and_publication_time():
    _, result, _ = _fetch("green", PARTY_CATALOG, "greens_sitemap_news.xml")
    document = result.documents[0]
    assert document.title == "Only a change of Government will ease cost-of-living crisis"
    assert document.published_at == datetime(2026, 8, 12, tzinfo=ZoneInfo("Pacific/Auckland"))
    assert document.raw_metadata["publication"]["precision"] == "date"
    assert document.raw_metadata["publication"]["source"] == "news-sitemap-publication-date"
    assert document.canonical_url == (
        "https://www.greens.org.nz/only_a_change_of_government_will_ease_cost_of_living")
    assert document.tier is CivicEvidenceTier.PARTY_PRIMARY
    assert document.publisher == "Green Party of Aotearoa New Zealand"


def test_news_sitemap_entry_without_a_news_block_is_reported_not_dropped_silently():
    _, result, _ = _fetch("green", PARTY_CATALOG, "greens_sitemap_news.xml")
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("no <news:news> block" in reason for reason in result.degraded_reasons)
    assert len(result.documents) == 2


def test_news_sitemap_carries_the_three_timestamps_apart():
    _, result, _ = _fetch("odt", MEDIA_CATALOG, "odt_sitemap_news.xml")
    document = result.documents[0]
    assert document.published_at == datetime(
        2026, 8, 12, 9, 31, 26, 266000, tzinfo=timezone.utc)
    assert document.updated_at == datetime(2026, 8, 12, 9, 35, 8, 799000, tzinfo=timezone.utc)
    assert document.first_seen_at == NOW
    assert document.dedupe_key.startswith("odt:")


def test_a_sitemap_index_where_a_news_sitemap_belongs_is_a_source_error():
    entry = PARTY_CATALOG.get("green")
    transport = transport_for(
        {entry.url: civic_fixture("conservatives_sitemap_index.xml")})
    with pytest.raises(CivicSourceError, match="expected a sitemap <urlset>"):
        fetch_entry(entry, NOW, transport=transport)


# ---------------------------------------------------------------------------
# Embedded publication lists
# ---------------------------------------------------------------------------

def test_embedded_list_identity_is_the_canonical_url_not_the_pages_own_id():
    _, result, _ = _fetch("labour", PARTY_CATALOG, "labour_news.html", content_type=_HTML)
    document = result.documents[0]
    assert document.external_key == (
        "https://www.labour.org.nz/news/release-a-circus-is-the-last-thing-new-zealand-needs")
    assert document.canonical_url == document.external_key
    assert document.raw_metadata["list_item_id"] == "JPKEr2Fa4kSmTAhaQ0CrhQ=="
    assert document.published_at == datetime(2026, 8, 12, tzinfo=timezone.utc)


def test_a_re_render_with_new_opaque_ids_produces_identical_identities():
    """Labour mints a fresh base64 id per render. Two renders of the same publications -- also
    differing in trailing slash and tracking query -- must be one set of documents, or every
    fetch republishes the same releases."""
    _, first, _ = _fetch("labour", PARTY_CATALOG, "labour_news.html", content_type=_HTML)
    _, second, _ = _fetch(
        "labour", PARTY_CATALOG, "labour_news_rerendered.html", content_type=_HTML)

    assert [d.external_key for d in first.documents] == [d.external_key for d in second.documents]
    assert [d.dedupe_key for d in first.documents] == [d.dedupe_key for d in second.documents]
    assert [d.published_at for d in first.documents] == [d.published_at for d in second.documents]
    assert {d.raw_metadata["list_item_id"] for d in first.documents}.isdisjoint(
        {d.raw_metadata["list_item_id"] for d in second.documents})


def test_embedded_list_keeps_only_the_release_path():
    _, result, _ = _fetch("labour", PARTY_CATALOG, "labour_news.html", content_type=_HTML)
    urls = [d.canonical_url for d in result.documents]
    assert all("/news/" in url for url in urls)
    assert not any("/policy/" in url for url in urls)


def test_embedded_list_without_the_expected_block_is_a_source_error():
    entry = PARTY_CATALOG.get("labour")
    transport = transport_for(
        {entry.url: b"<html><body>nothing here</body></html>"}, default_content_type=_HTML)
    with pytest.raises(CivicSourceError, match="no embedded publication list"):
        fetch_entry(entry, NOW, transport=transport)


# ---------------------------------------------------------------------------
# Link index listings
# ---------------------------------------------------------------------------

def test_link_index_keeps_only_the_catalogued_link_pattern():
    _, result, _ = _fetch(
        "outdoors-freedom", PARTY_CATALOG, "nzofp_media.html", content_type=_HTML)
    urls = [d.canonical_url for d in result.documents]
    assert urls and all("media-release" in url for url in urls)
    assert not any("/shop/" in url for url in urls)
    assert len(urls) == len(set(urls)) == 3


def test_link_index_documents_are_undated_and_fall_back_to_first_seen():
    _, result, _ = _fetch(
        "outdoors-freedom", PARTY_CATALOG, "nzofp_media.html", content_type=_HTML)
    for document in result.documents:
        assert document.published_at is None
        assert document.updated_at is None
        assert document.effective_at == NOW


def test_link_index_off_site_release_is_reported_as_not_the_sources_own():
    _, result, _ = _fetch(
        "outdoors-freedom", PARTY_CATALOG, "nzofp_media.html", content_type=_HTML)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("freedom.kiwi" in reason for reason in result.degraded_reasons)


def test_link_index_needs_a_pattern():
    with pytest.raises(ValueError, match="needs a link_pattern"):
        LinkIndexDefinition(
            source_key="x",
            endpoint=CivicEndpoint(url="https://x.test/a", allowed_hosts=frozenset({"x.test"})),
            publisher="X",
            tier=CivicEvidenceTier.PARTY_PRIMARY)


def test_link_index_with_no_matching_links_is_empty_not_ok():
    definition = LinkIndexDefinition(
        source_key="outdoors-freedom",
        endpoint=CivicEndpoint(
            url="https://nzofp.co.nz/media", allowed_hosts=frozenset({"nzofp.co.nz"}),
            content_types=HTML_CONTENT_TYPES),
        publisher="NZ Outdoors & Freedom Party",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        link_pattern="media-release")
    transport = transport_for(
        {"https://nzofp.co.nz/media": b"<html><body><a href='/about'>About</a></body></html>"},
        default_content_type=_HTML)
    result = LinkIndexConnector(definition, transport).fetch(NOW)
    assert result.health is CivicSourceHealth.EMPTY
    assert result.documents == ()


# ---------------------------------------------------------------------------
# Wall-clock feeds
# ---------------------------------------------------------------------------

def test_wall_clock_feed_reads_local_times_in_its_own_zone():
    _, result, _ = _fetch("interest", MEDIA_CATALOG, "interest_latest.rss")
    # 12th Aug 26, 5:01pm New Zealand time is 05:01 UTC on the same day.
    assert result.documents[0].published_at == datetime(2026, 8, 12, 5, 1, tzinfo=timezone.utc)
    assert result.documents[0].canonical_url == (
        "https://www.interest.co.nz/public-policy/139787/pm-punishes-chris-penk")


def test_wall_clock_feed_identity_follows_the_article_link_not_the_relative_guid():
    _, result, _ = _fetch("interest", MEDIA_CATALOG, "interest_latest.rss")
    document = result.documents[0]
    assert document.external_key == document.canonical_url
    assert document.raw_metadata["feed_guid"].startswith("/public-policy/")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12th Aug 26, 5:01pm", datetime(2026, 8, 12, 5, 1, tzinfo=timezone.utc)),
        ("1st Jan 26, 12:00am", datetime(2025, 12, 31, 11, 0, tzinfo=timezone.utc)),
    ],
)
def test_wall_clock_parsing(raw, expected):
    assert parse_wall_clock(raw, timezone_name="Pacific/Auckland") == expected


@pytest.mark.parametrize("raw", ["", None, "yesterday", "12/08/2026", 5])
def test_unparseable_wall_clock_values_return_none_rather_than_guessing(raw):
    assert parse_wall_clock(raw, timezone_name="Pacific/Auckland") is None


# ---------------------------------------------------------------------------
# Guarded XML, canonical URLs, titles
# ---------------------------------------------------------------------------

_BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.greens.org.nz/&lol3;</loc></url>
</urlset>"""


@pytest.mark.parametrize(
    "source_key,catalog",
    [
        ("green", PARTY_CATALOG),        # news sitemap
        ("act", PARTY_CATALOG),          # canonical sitemap connector
        ("nz-first", PARTY_CATALOG),     # canonical feed connector
        ("conservative", PARTY_CATALOG),  # canonical sitemap index
        ("odt", MEDIA_CATALOG),          # news sitemap
        ("stuff", MEDIA_CATALOG),        # canonical Atom
        ("interest", MEDIA_CATALOG),     # wall-clock feed
    ],
)
def test_entity_declarations_are_refused_before_expansion(source_key, catalog):
    """Every XML source in this half of the catalog -- whichever connector reads it -- goes
    through the canonical guarded parse, so a declared entity is rejected outright rather than
    expanded."""
    entry = catalog.get(source_key)
    transport = transport_for({entry.url: _BILLION_LAUGHS})
    with pytest.raises(CivicSourceError, match="DOCTYPE or ENTITY"):
        fetch_entry(entry, NOW, transport=transport)


def test_a_doctype_alone_is_refused():
    entry = PARTY_CATALOG.get("green")
    document = b'<?xml version="1.0"?><!DOCTYPE urlset><urlset/>'
    transport = transport_for({entry.url: document})
    with pytest.raises(CivicSourceError, match="DOCTYPE or ENTITY"):
        fetch_entry(entry, NOW, transport=transport)


def test_tracking_parameters_are_stripped_from_citable_urls():
    _, result, _ = _fetch(
        "labour", PARTY_CATALOG, "labour_news_rerendered.html", content_type=_HTML)
    urls = [d.canonical_url for d in result.documents]
    assert not any("utm_" in url for url in urls)
    assert "https://www.labour.org.nz/news/release-backing-small-business-to-innovate" in urls


def test_titles_derived_from_urls_are_percent_decoded():
    assert title_from_url(
        "https://www.act.org.nz/news/new-zealand%E2%80%99s-new-religion"
    ) == "new zealand’s new religion"


# ---------------------------------------------------------------------------
# fetch_entry: the rules that are not a strategy's business
# ---------------------------------------------------------------------------

def test_documents_off_the_sources_host_never_leave_the_fetch():
    """What an off-host redirect, a hijacked feed, and a syndicated link all look like from
    here: a document whose URL is not this source's own. It is dropped and reported."""
    entry = PARTY_CATALOG.get("nz-first")
    transport = transport_for({entry.url: civic_fixture("nz_first_news.rss")})
    result = fetch_entry(entry, NOW, transport=transport)
    assert all("nzfirst.nz" in d.canonical_url for d in result.documents)
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("example-news.test" in reason for reason in result.degraded_reasons)


def test_a_wholly_foreign_document_yields_no_documents_at_all():
    entry = PARTY_CATALOG.get("nz-first")
    foreign = civic_fixture("nz_first_news.rss").replace(
        b"https://www.nzfirst.nz", b"https://mirror.attacker.test")
    transport = transport_for({entry.url: foreign})
    result = fetch_entry(entry, NOW, transport=transport)
    assert result.documents == ()
    assert result.health is CivicSourceHealth.DEGRADED
    assert sum("mirror.attacker.test" in reason for reason in result.degraded_reasons) == 3


def test_the_document_count_is_bounded_and_the_cap_is_reported():
    entry = PARTY_CATALOG.get("act")
    transport = transport_for({entry.url: civic_fixture("act_sitemap_en_nz_live_shape.xml")})
    result = fetch_entry(entry, NOW, transport=transport, limit=2)
    assert len(result.documents) == 2
    assert result.health is CivicSourceHealth.DEGRADED
    assert any("2-document cap" in reason for reason in result.degraded_reasons)


def test_the_transport_is_given_the_host_allowlist():
    entry, _, transport = _fetch("nz-first", PARTY_CATALOG, "nz_first_news.rss")
    call = transport.recorded.calls[0]
    assert call.allowed_hosts == entry.hosts
    assert call.max_bytes == PARTY_MEDIA_MAX_BYTES


def test_the_default_transport_identifies_partymap_and_impersonates_nobody():
    entry = PARTY_CATALOG.get("nz-first")
    agents: list[str] = []

    def fake_fetch(url, **kwargs):
        agents.append(kwargs["user_agent"])
        return HttpResponse(
            body=civic_fixture("nz_first_news.rss"), content_type="application/rss+xml")

    fetch_entry(entry, NOW, transport=party_media_transport(fetch=fake_fetch))
    assert agents == [PARTY_MEDIA_USER_AGENT]
    assert PARTY_MEDIA_USER_AGENT.startswith("PartyMap")
    assert "partymap.co.nz" in PARTY_MEDIA_USER_AGENT
    assert not any(
        vendor in PARTY_MEDIA_USER_AGENT.lower()
        for vendor in ("mozilla", "chrome", "safari", "claude", "gpt", "openai", "anthropic"))


def test_an_html_page_where_a_feed_belongs_fails_before_parsing():
    entry = PARTY_CATALOG.get("nz-first")
    transport = transport_for(
        {entry.url: (b"<!doctype html><html><body>Blocked</body></html>", "text/html")})
    with pytest.raises(CivicSourceError, match="expected one of"):
        fetch_entry(entry, NOW, transport=transport)


def test_a_blocked_party_refuses_before_any_request():
    entry = PARTY_CATALOG.get("nz-loyal")
    transport = transport_for({})
    with pytest.raises(CivicSourceUnavailableError, match="403"):
        fetch_entry(entry, NOW, transport=transport)
    assert transport.recorded.calls == []


def test_a_licence_restricted_outlet_refuses_before_any_request():
    entry = MEDIA_CATALOG.get("nz-herald")
    transport = transport_for({})
    with pytest.raises(CivicSourceRestrictedError, match="forbid automated and AI use"):
        fetch_entry(entry, NOW, transport=transport)
    assert transport.recorded.calls == []


def test_a_limit_of_zero_is_refused():
    entry = PARTY_CATALOG.get("nz-first")
    with pytest.raises(ValueError, match="limit must be positive"):
        fetch_entry(entry, NOW, transport=transport_for({}), limit=0)


def test_a_crawl_delay_is_honoured_between_requests_to_one_host():
    entry = PARTY_CATALOG.get("conservative")
    routes = {
        entry.url: civic_fixture("conservatives_sitemap_index.xml"),
        "https://www.conservatives.nz/dynamic-news_p_a8aa61ec_1cf1_4158_8131_f4d76c9ec79e"
        "_0_5000-sitemap.xml": civic_fixture("conservatives_news_sitemap.xml"),
        "https://www.conservatives.nz/store-categories-sitemap.xml": civic_fixture(
            "conservatives_news_sitemap.xml"),
        "https://www.conservatives.nz/pages-sitemap.xml": civic_fixture(
            "conservatives_news_sitemap.xml"),
    }
    transport = transport_for(routes)
    result = fetch_entry(entry, NOW, transport=transport)
    assert result.pages_fetched == 4
    # Three follow-up requests to one host, each paced by the site's own 10 s crawl delay.
    assert transport.clock.slept == [10.0, 10.0, 10.0]
    assert all("/news/" in d.canonical_url for d in result.documents)


def test_a_refused_off_host_redirect_fails_before_anything_is_parsed():
    """`beehive.connectors.http` re-validates every redirect hop against the same allow-list and
    refuses an off-allow-list one as UNSAFE_URL. This asserts the whole chain arrives here as a
    named `CivicSourceError` with no document parsed, rather than a party connector quietly
    reading a mirror's copy of a release."""
    entry = PARTY_CATALOG.get("nz-first")
    refusal = ConnectorHttpError(
        ConnectorHttpErrorKind.UNSAFE_URL,
        f"{entry.url}: refused redirect to https://mirror.attacker.test/news.rss: "
        "host 'mirror.attacker.test' is not one of the allowed hosts")
    transport = transport_for({entry.url: refusal})
    with pytest.raises(CivicSourceError, match="refused redirect"):
        fetch_entry(entry, NOW, transport=transport)


def test_relative_links_resolve_against_the_url_the_response_actually_came_from():
    """The shared client reports where a redirect chain ended, and every hop was already
    allow-list checked, so resolving a listing page's relative links against the landing URL is
    both safe and more correct than resolving against the URL requested."""
    entry = PARTY_CATALOG.get("outdoors-freedom")
    landing = "https://nzofp.co.nz/media/"
    transport = transport_for(
        {entry.url: civic_fixture("nzofp_media.html")}, default_content_type=_HTML)
    real_request = transport.request

    def request_with_redirect(*args, **kwargs):
        response = real_request(*args, **kwargs)
        return type(response)(
            body=response.body, content_type=response.content_type, final_url=landing)

    transport.request = request_with_redirect
    result = fetch_entry(entry, NOW, transport=transport)
    assert result.documents
    assert all(d.raw_metadata["listing_page"] == landing for d in result.documents)
    assert all(entry.owns_url(d.canonical_url) for d in result.documents)
