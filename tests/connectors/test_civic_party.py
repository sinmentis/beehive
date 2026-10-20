"""The party catalog: exactly PartyMap's 17 parties, each a canonical Civic source entry with one
committed strategy, one exact host, honest health, and no way to be pointed anywhere else."""
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from beehive.civic.catalog import CivicSourceEntry
from beehive.civic.connectors.feeds import CivicFeedConnector
from beehive.civic.connectors.sitemaps import SitemapConnector
from beehive.civic.documents import CivicDocument, CivicFetchResult, CivicSourceHealth
from beehive.civic.fetching import CivicTransport
from beehive.connectors.civic_catalog import (
    CivicSourceUnavailableError,
    PARTY_MEDIA_USER_AGENT,
    RefusingConnector,
    SourceAvailability,
    UsePolicy,
    url_host,
)
from beehive.connectors.civic_party import (
    EXPECTED_PARTY_COUNT,
    PARTY_CATALOG,
    PARTY_SOURCES,
    CivicPartyConnector,
    all_sources,
    civic_documents_to_raw_items,
    enabled_sources,
    fetch_party,
    source_keys,
)
from beehive.civic.production import (
    CivicHydrationStatus,
    PartyMediaDocumentHydrator,
)
from beehive.connectors.civic_strategies import (
    EmbeddedListConnector,
    LinkIndexConnector,
    NewsSitemapConnector,
)
from beehive.connectors.registry import get as get_connector
from beehive.domain.channels import ChannelKind
from beehive.domain.civic import CivicEvidenceTier
from tests.civic.civic_stubs import RecordedFetch, transport_for
from tests.connectors.civic_fixtures_loader import civic_fixture

NOW = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)

# PartyMap's own party data. Read here so the two sets cannot drift apart silently: this test is
# the only thing linking the catalog to the site that defines who the parties are.
_BASE_PARTIES = Path(__file__).resolve().parents[4] / "website_party/src/data/base-parties.ts"


def _site_parties() -> list[tuple[str, str]]:
    source = _BASE_PARTIES.read_text(encoding="utf-8")
    slugs = re.findall(r'^    slug: "([^"]+)",', source, re.M)
    websites = re.findall(r'^    website: "([^"]+)",', source, re.M)
    return list(zip(slugs, websites, strict=True))


def test_catalog_holds_exactly_the_seventeen_partymap_parties():
    assert len(PARTY_SOURCES) == EXPECTED_PARTY_COUNT == 17
    assert len(PARTY_CATALOG) == 17
    assert len(all_sources()) == 17
    assert len(source_keys()) == 17


@pytest.mark.skipif(not _BASE_PARTIES.exists(), reason="PartyMap site data is not checked out")
def test_slugs_and_hosts_match_the_partymap_site_data():
    site = _site_parties()
    assert [slug for slug, _ in site] == list(source_keys())
    for slug, website in site:
        assert url_host(website) in PARTY_CATALOG.get(slug).hosts


def test_every_party_entry_is_a_civic_catalog_entry():
    """Official and party sources enumerate through one type, so an orchestrator concatenates
    `civic.catalog.enabled_sources()` and `civic_party.enabled_sources()` without a branch."""
    for entry in PARTY_SOURCES:
        assert isinstance(entry, CivicSourceEntry)
        assert entry.tier is CivicEvidenceTier.PARTY_PRIMARY
        assert entry.publisher
        assert entry.use_policy is UsePolicy.EVIDENCE
        assert entry.owner_id == f"party:{entry.source_key}"


def test_each_party_owns_its_own_corroboration_identity():
    assert len(PARTY_CATALOG.owner_ids()) == 17


def test_thirteen_parties_are_fetchable_and_four_are_recorded_as_unavailable():
    assert len(enabled_sources()) == 13
    assert {e.source_key: e.availability for e in PARTY_CATALOG.disabled_sources()} == {
        "te-tai-tokerau": SourceAvailability.BLOCKED,
        "nz-loyal": SourceAvailability.BLOCKED,
        "free-palestine": SourceAvailability.NO_PUBLIC_SURFACE,
        "cannabis": SourceAvailability.NO_PUBLIC_SURFACE,
    }


def test_every_unavailable_party_records_a_reason_and_refuses():
    for entry in PARTY_CATALOG.disabled_sources():
        assert entry.note
        assert entry.url == ""
        assert entry.connector_class is RefusingConnector
        with pytest.raises(CivicSourceUnavailableError):
            entry.assert_fetchable()


def test_every_enabled_party_pins_one_endpoint_on_its_own_host():
    for entry in enabled_sources():
        assert entry.url
        assert url_host(entry.url) in entry.hosts
        assert entry.definition.endpoint.allowed_hosts == entry.hosts


def test_the_verified_direct_feeds_are_the_committed_endpoints():
    expected = {
        "nz-first": "https://www.nzfirst.nz/news.rss",
        "te-pati-maori": "https://www.maoriparty.org.nz/panui.rss",
        "opportunity": "https://www.opportunity.org.nz/news.rss",
        "alliance": "https://allianceparty.nz/feed/",
        "womens-rights": "https://womensrightsparty.nz/feed/",
        "animal-justice": "https://animaljustice.org.nz/feed/",
    }
    for slug, url in expected.items():
        entry = PARTY_CATALOG.get(slug)
        assert entry.url == url
        assert entry.connector_class is CivicFeedConnector


def test_each_party_uses_the_narrowest_strategy_its_site_supports():
    assert PARTY_CATALOG.get("green").connector_class is NewsSitemapConnector
    assert PARTY_CATALOG.get("labour").connector_class is EmbeddedListConnector
    assert PARTY_CATALOG.get("outdoors-freedom").connector_class is LinkIndexConnector
    for slug in ("national", "act", "vision-nz", "conservative"):
        assert PARTY_CATALOG.get(slug).connector_class is SitemapConnector


def test_whole_site_sitemaps_are_restricted_to_their_release_path():
    for slug in ("national", "act", "vision-nz", "conservative", "labour"):
        assert PARTY_CATALOG.get(slug).definition.path_includes == ("/news/",), (
            f"{slug} would ingest static pages as party-primary evidence")


def test_sites_asking_for_a_crawl_delay_get_one():
    assert PARTY_CATALOG.get("outdoors-freedom").crawl_delay_seconds == 10.0
    assert PARTY_CATALOG.get("conservative").crawl_delay_seconds == 10.0


def test_a_whole_site_sitemap_yields_only_releases_not_static_pages():
    """ACT's locale sitemap lists ~110 static pages before its first release. Reading it
    unfiltered returned a page of navigation and policy pages and no publications at all."""
    entry = PARTY_CATALOG.get("act")
    transport = transport_for({entry.url: civic_fixture("act_sitemap_en_nz_live_shape.xml")})
    result = fetch_party("act", NOW, transport=transport)
    urls = [d.canonical_url for d in result.documents]
    assert urls, "the release entries must be reachable past the static pages"
    assert all("/news/" in url for url in urls)
    for excluded in ("https://www.act.org.nz/", "https://www.act.org.nz/form",
                     "https://www.act.org.nz/people", "https://www.act.org.nz/policies",
                     "https://www.act.org.nz/policies/health"):
        assert excluded not in urls


def test_a_sitemap_without_lastmod_yields_undated_documents():
    entry = PARTY_CATALOG.get("act")
    transport = transport_for({entry.url: civic_fixture("act_sitemap_en_nz_live_shape.xml")})
    result = fetch_party("act", NOW, transport=transport)
    assert result.documents
    for document in result.documents:
        assert document.published_at is None
        assert document.updated_at is None
        assert document.effective_at == NOW


def test_metadata_only_party_document_is_hydrated_from_its_detail_page():
    entry = PARTY_CATALOG.get("act")
    article_url = "https://www.act.org.nz/news/greens-put-union-coffers-before-students"
    document = CivicDocument(
        source_key=entry.source_key,
        external_key=article_url,
        canonical_url=article_url,
        title="Greens put union coffers before students from sitemap slug",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        publisher=entry.publisher,
        first_seen_at=NOW,
    )
    article = """
    <html><body><article>
      <h1>Greens put union coffers before students</h1>
      <time datetime="2026-08-25T09:30:00+12:00">25 August 2026</time>
      <p>ACT New Zealand released a detailed statement about the proposed education policy.</p>
      <p>The release explains the party's position, the institutions affected, and the intended
      implementation. It includes enough attributable text to support a public citation.</p>
      <p>The final paragraph records the scope and timing of the proposal in plain language.</p>
    </article></body></html>
    """
    recorded = RecordedFetch(
        {article_url: (article.encode("utf-8"), "text/html")},
        default_content_type="text/html",
    )
    transport = CivicTransport(
        fetch=recorded,
        user_agent=PARTY_MEDIA_USER_AGENT,
    )

    result = PartyMediaDocumentHydrator(transport=transport)(document, NOW)

    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.dedupe_key == document.dedupe_key
    assert result.document.title == "Greens put union coffers before students"
    assert result.document.published_at.isoformat() == "2026-08-25T09:30:00+12:00"
    assert "detailed statement" in result.document.excerpt
    assert recorded.calls[0].allowed_hosts == entry.hosts
    assert recorded.calls[0].user_agent == PARTY_MEDIA_USER_AGENT


def test_template_chrome_is_stripped_from_the_text_the_extractor_really_returns():
    """Verbatim extractor output from the live party pages, so the rule is exercised against the
    strings production actually stores rather than a fixture that flatters it.

    Both ends matter beyond tidiness: this text is the public record's own proof, and it is also
    the closed set `generation.numeric_tokens` builds, so the street number in a promoter
    statement would otherwise license that number in a generated summary."""
    from beehive.civic.production import _strip_template_chrome

    act = (
        "Back Press Release Catching Australia by 2033 At present we spend about $34 billion "
        "on health, but the medicines budget is only five per cent (about $1.7 billion) of that."
    )
    assert _strip_template_chrome(act, "Catching Australia by 2033") == (
        "Catching Australia by 2033 At present we spend about $34 billion on health, but the "
        "medicines budget is only five per cent (about $1.7 billion) of that."
    )

    labour = (
        "Release: Labour will guarantee a job for graduate nurses 31 August 2026 Labour will "
        "offer every New Zealand trained nurse a job in our public health system. "
        "Support our movement today "
        "Authorised by Rob Salmond, 2 Gilmer Terrace, Wellington. "
        "Authorised by Chris Hipkins MP, Parliament Bldg, Wellington."
    )
    cleaned = _strip_template_chrome(
        labour, "Release: Labour will guarantee a job for graduate nurses")
    assert cleaned.endswith("Support our movement today")
    assert "Authorised by" not in cleaned
    assert "Gilmer Terrace" not in cleaned
    assert "2 Gilmer" not in cleaned


def test_template_chrome_stripping_fails_closed():
    """Every branch refuses to guess. A headline that begins with a navigation word keeps it, an
    unknown prefix is left alone, and prose about authorisation statements is not truncated."""
    from beehive.civic.production import _strip_template_chrome

    headline = "Back to the future, with a CGT to boot Chris Hipkins is going back to the future."
    assert _strip_template_chrome(headline, "Back to the future, with a CGT to boot") == headline

    unknown_prefix = "Sponsored feature ACT will fund clinics across the country."
    assert _strip_template_chrome(
        unknown_prefix, "ACT will fund clinics across the country.") == unknown_prefix

    about = (
        "On authorisation statements Every election advertisement must carry a promoter "
        "statement. Authorised by the promoter named on the advertisement, as the Electoral Act "
        "requires, and the rule applies to each party equally throughout the regulated period."
    )
    assert _strip_template_chrome(about, "On authorisation statements").endswith(
        "throughout the regulated period.")

    assert _strip_template_chrome("Body with no heading known.", None) == (
        "Body with no heading known.")


def test_hydrated_text_keeps_body_that_only_resembles_boilerplate():
    """End to end through the real extractor: an article discussing authorisation statements
    keeps its text, so the promoter-statement rule can never truncate reporting."""
    entry = PARTY_CATALOG.get("act")
    article_url = "https://www.act.org.nz/news/on-authorisation-statements"
    document = CivicDocument(
        source_key=entry.source_key,
        external_key=article_url,
        canonical_url=article_url,
        title="On authorisation statements",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        publisher=entry.publisher,
        first_seen_at=NOW,
        published_at=NOW,
    )
    body = (
        "Every election advertisement must carry a promoter statement. "
        "Authorised by the promoter named on the advertisement, as the Electoral Act requires, "
        "and the rule applies to each party equally throughout the regulated period."
    )
    article = f"""
    <html><body><div>
      <div><h1>On authorisation statements</h1></div>
      <div><p>{body}</p></div>
    </div></body></html>
    """
    recorded = RecordedFetch(
        {article_url: (article.encode("utf-8"), "text/html")},
        default_content_type="text/html",
    )
    result = PartyMediaDocumentHydrator(
        transport=CivicTransport(fetch=recorded, user_agent=PARTY_MEDIA_USER_AGENT),
    )(document, NOW)

    assert result.status is CivicHydrationStatus.HYDRATED
    assert "Authorised by the promoter named" in result.document.excerpt


def test_hydration_refuses_off_host_and_preserves_unknown_publication_without_guessing():
    entry = PARTY_CATALOG.get("act")
    off_host = CivicDocument(
        source_key=entry.source_key,
        external_key="https://example.test/release",
        canonical_url="https://example.test/release",
        title="Off-host release",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        publisher=entry.publisher,
        first_seen_at=NOW,
    )
    recorded = RecordedFetch({}, default_content_type="text/html")
    hydrator = PartyMediaDocumentHydrator(
        transport=CivicTransport(
            fetch=recorded,
            user_agent=PARTY_MEDIA_USER_AGENT,
        ),
    )

    off_host_result = hydrator(off_host, NOW)

    assert off_host_result.status is CivicHydrationStatus.UNAVAILABLE
    assert off_host_result.error_code == "detail_off_host"
    assert recorded.calls == []

    insecure = replace(
        off_host,
        source_key=entry.source_key,
        external_key="http://www.act.org.nz/news/insecure-release",
        canonical_url="http://www.act.org.nz/news/insecure-release",
        publisher=entry.publisher,
    )
    insecure_result = hydrator(insecure, NOW)

    assert insecure_result.status is CivicHydrationStatus.UNAVAILABLE
    assert insecure_result.error_code == "detail_url_invalid"
    assert recorded.calls == []

    article_url = "https://www.act.org.nz/news/undated-release"
    undated = replace(
        off_host,
        external_key=article_url,
        canonical_url=article_url,
    )
    article = (
        "<html><body>"
        '<article class="related"><time datetime="2019-03-02T10:00:00+13:00">'
        "Related story</time></article>"
        "<article><h1>Undated release</h1>"
        '<time datetime="2026-08-25T09:30:00+12:00">25 August</time><p>'
        + "A complete attributable policy release with no publication date. " * 10
        + "</p><footer>Copyright 2026</footer></article></body></html>"
    )
    undated_fetch = RecordedFetch(
        {article_url: (article.encode("utf-8"), "text/html")},
        default_content_type="text/html",
    )

    undated_result = PartyMediaDocumentHydrator(
        transport=CivicTransport(
            fetch=undated_fetch,
            user_agent=PARTY_MEDIA_USER_AGENT,
        ),
    )(undated, NOW)

    assert undated_result.status is CivicHydrationStatus.HYDRATED
    assert undated_result.document.published_at is None
    assert undated_result.document.raw_metadata["source_capture"]["publication"] is None


def test_a_party_that_published_nothing_is_empty_not_a_failure():
    """Animal Justice's feed is valid and frequently carries no items. That is a quiet week, not
    a broken source, and the two must never look alike."""
    entry = PARTY_CATALOG.get("animal-justice")
    empty_feed = (
        b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        b"<title>Animal Justice Party Aotearoa NZ</title>"
        b"<link>https://animaljustice.org.nz</link></channel></rss>"
    )
    transport = transport_for({entry.url: empty_feed})
    result = fetch_party("animal-justice", NOW, transport=transport)
    assert result.health is CivicSourceHealth.EMPTY
    assert result.documents == ()
    assert result.is_complete


def test_a_parse_failure_is_never_reported_as_an_empty_week():
    entry = PARTY_CATALOG.get("animal-justice")
    transport = transport_for({entry.url: b"<rss><channel><item>"})
    with pytest.raises(Exception, match="malformed XML"):
        fetch_party("animal-justice", NOW, transport=transport)


def test_a_partial_crawl_reports_what_was_missed():
    entry = PARTY_CATALOG.get("nz-first")
    transport = transport_for({entry.url: civic_fixture("nz_first_news.rss")})
    result = fetch_party("nz-first", NOW, transport=transport)
    assert result.health is CivicSourceHealth.DEGRADED
    assert result.degraded_reasons
    assert result.documents
    assert not result.is_complete


def test_config_must_name_a_catalogued_party_and_nothing_else():
    connector = CivicPartyConnector()
    connector.validate_config({"party": "green"})
    with pytest.raises(ValueError, match="unknown civic source"):
        connector.validate_config({"party": "not-a-party"})
    with pytest.raises(ValueError, match="must be exactly"):
        connector.validate_config({})
    with pytest.raises(ValueError, match="must be exactly"):
        connector.validate_config({"party": "green", "url": "https://evil.test/feed"})
    with pytest.raises(ValueError, match="must be a string slug"):
        connector.validate_config({"party": ["green"]})


def test_channel_items_carry_the_catalogs_own_classification():
    entry = PARTY_CATALOG.get("nz-first")
    transport = transport_for({entry.url: civic_fixture("nz_first_news.rss")})
    items = CivicPartyConnector(transport=transport).fetch_preview({"party": "nz-first"}, limit=1)
    metadata = items[0].raw_metadata
    assert metadata["civic_source"] == "nz-first"
    assert metadata["civic_owner"] == "party:nz-first"
    assert metadata["civic_tier"] == "party_primary"
    assert metadata["civic_use_policy"] == "evidence"
    assert metadata["civic_may_be_evidence"] is True
    assert metadata["publisher"] == "New Zealand First"
    assert metadata["civic_dedupe_key"].startswith("nz-first:")
    assert metadata["civic_health"] == "degraded"


def test_a_tombstone_never_reaches_a_channel_as_a_quiet_week():
    document = CivicDocument(
        source_key="nz-first",
        external_key="x",
        canonical_url="https://www.nzfirst.nz/x",
        title="x",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        publisher="New Zealand First",
        first_seen_at=NOW)
    result = CivicFetchResult(
        source_key="nz-first",
        documents=(document,),
        health=CivicSourceHealth.TOMBSTONE,
        fetched_at=NOW)
    with pytest.raises(RuntimeError, match="tombstone, not an empty week"):
        civic_documents_to_raw_items(result, PARTY_CATALOG)


def test_blocked_party_fetch_fails_visibly_through_the_channel_adapter():
    connector = CivicPartyConnector(transport=transport_for({}))
    with pytest.raises(CivicSourceUnavailableError, match="403"):
        connector.fetch({"party": "nz-loyal"})


def test_party_without_a_publication_surface_fails_visibly():
    connector = CivicPartyConnector(transport=transport_for({}))
    with pytest.raises(CivicSourceUnavailableError, match="no_public_surface"):
        connector.fetch({"party": "cannabis"})


def test_no_party_is_licence_restricted():
    # A party's own releases are always citable; the restricted policy exists for media terms.
    for entry in PARTY_SOURCES:
        assert entry.use_policy is not UsePolicy.RESTRICTED


def test_preview_is_bounded_and_rejects_a_useless_limit():
    entry = PARTY_CATALOG.get("nz-first")
    connector = CivicPartyConnector(
        transport=transport_for({entry.url: civic_fixture("nz_first_news.rss")}))
    assert len(connector.fetch_preview({"party": "nz-first"}, limit=1)) == 1
    with pytest.raises(ValueError, match="preview limit must be positive"):
        connector.fetch_preview({"party": "nz-first"}, limit=0)


def test_connector_is_registered_for_editorial_channels_only():
    connector = get_connector("civic_party_release")
    assert connector.supported_channel_kinds == frozenset({ChannelKind.EDITORIAL})
