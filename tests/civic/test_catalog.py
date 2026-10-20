# tests/civic/test_catalog.py
"""The application-controlled source catalog: fixed endpoints, assigned tiers, host allow-lists,
optional-never-required credentials, and the opt-in live preview.

These are the properties that make the whole package safe rather than any individual parser, so
they are asserted across every catalogued source rather than sampled."""
import pytest

from beehive.civic.catalog import (CIVIC_SOURCES, all_sources, enabled_sources, get_source,
                                    source_keys)
from beehive.civic.connectors.legislation import LEGISLATION_API_KEY_ENV, TOMBSTONE_PATHS
from beehive.civic.fetching import CIVIC_USER_AGENT
from beehive.domain.civic import CivicEvidenceTier

_IDS = [entry.source_key for entry in CIVIC_SOURCES]


# ============================================================================
# Fixed, safe endpoints
# ============================================================================


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_every_endpoint_is_https(entry):
    assert entry.definition.endpoint.url.startswith("https://")


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_every_endpoint_declares_a_host_allowlist(entry):
    assert entry.definition.endpoint.allowed_hosts


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_every_endpoint_declares_expected_content_types(entry):
    assert entry.definition.endpoint.content_types


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_every_endpoint_is_bounded(entry):
    endpoint = entry.definition.endpoint
    assert 0 < endpoint.max_bytes <= 8 * 1024 * 1024
    assert 0 < endpoint.timeout_seconds <= 60


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_no_endpoint_carries_credentials_or_a_non_standard_port(entry):
    url = entry.definition.endpoint.url
    assert "@" not in url.split("//", 1)[1].split("/", 1)[0]
    assert "key=" not in url.lower()
    assert "token=" not in url.lower()


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_no_endpoint_is_a_known_tombstone(entry):
    from urllib.parse import urlsplit

    assert urlsplit(entry.definition.endpoint.url).path.lower() not in TOMBSTONE_PATHS


def test_source_keys_are_unique():
    assert len(source_keys()) == len(set(source_keys()))


# ============================================================================
# Tier is assigned by us
# ============================================================================


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_every_official_source_is_tiered_official_by_application_config(entry):
    """Nothing reads a tier out of remote data, so a source cannot promote itself past the
    corroboration rules in beehive.domain.civic."""
    assert entry.tier is CivicEvidenceTier.OFFICIAL


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_every_source_names_a_publisher(entry):
    assert entry.publisher.strip()


def test_the_tier_lives_on_the_definition_not_in_any_response_field():
    source = get_source("nz_data_govt")
    assert source.tier is source.definition.tier


# ============================================================================
# Crawl delays match what the operators asked for
# ============================================================================


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_every_source_declares_a_crawl_delay(entry):
    assert entry.crawl_delay_seconds >= 0


def test_the_documented_crawl_delays_are_the_configured_ones():
    assert get_source("nz_electoral_commission").crawl_delay_seconds == 2.0
    assert get_source("nz_stats").crawl_delay_seconds == 10.0


# ============================================================================
# The exact current endpoints
# ============================================================================


@pytest.mark.parametrize(
    "source_key,url",
    [
        ("nz_electoral_commission", "https://elections.nz/media-and-news/rss"),
        ("nz_beehive_releases", "https://www.beehive.govt.nz/rss.xml"),
        ("nz_parliament_order_paper",
         "https://www3.parliament.nz/en/channellisting/rss/1691/"
         "92035c6e-1be3-40fb-aafa-37d4346d78cc"),
        ("nz_parliament_bills", "https://bills.parliament.nz/api/data/search"),
        ("nz_parliament_select_committees",
         "https://selectcommittees.parliament.nz/api/data/search"),
        ("nz_parliament_hansard", "https://hansard.parliament.nz/api/data/search"),
        ("nz_legislation_sitemap", "https://www.legislation.govt.nz/sitemap.xml"),
        ("nz_legislation_works", "https://api.legislation.govt.nz/v0/works"),
        ("nz_oag_reports", "https://ao.parliament.nz/sitemap.xml.gz"),
    ])
def test_the_exact_current_endpoint_is_configured(source_key, url):
    assert get_source(source_key).url == url


def test_the_hansard_api_is_the_data_search_listing_endpoint():
    assert get_source("nz_parliament_hansard").url == (
        "https://hansard.parliament.nz/api/data/search")


def test_every_sitemap_source_gets_the_larger_byte_cap():
    """Legislation alone is ~5.2 MiB; the default 4 MiB cap would fail these outright."""
    from beehive.civic.connectors.sitemaps import SitemapConnector
    from beehive.civic.fetching import SITEMAP_MAX_BYTES

    sitemaps = [entry for entry in CIVIC_SOURCES
                if entry.connector_class is SitemapConnector]
    assert sitemaps
    for entry in sitemaps:
        assert entry.definition.endpoint.max_bytes == SITEMAP_MAX_BYTES, entry.source_key


def test_the_hansard_daily_detail_endpoint_is_not_a_catalogued_listing_source():
    """It is opt-in and per-day; scheduling it like a listing source would pull hundreds of KB
    per run for detail the listing already summarises."""
    from beehive.civic.catalog import hansard_daily_detail_endpoint

    assert "/api/data/daily" not in {entry.url for entry in CIVIC_SOURCES}
    assert hansard_daily_detail_endpoint().url.endswith("/api/data/daily")


def test_the_parliament_apis_are_posts():
    for key in ("nz_parliament_bills", "nz_parliament_select_committees",
                "nz_parliament_hansard"):
        assert get_source(key).definition.endpoint.method == "POST"


# ============================================================================
# Optional credentials never become required
# ============================================================================


def test_only_the_legislation_works_api_has_an_optional_key():
    keyed = [entry for entry in CIVIC_SOURCES if entry.requires_api_key_env]
    assert [entry.source_key for entry in keyed] == ["nz_legislation_works"]
    assert keyed[0].requires_api_key_env == LEGISLATION_API_KEY_ENV


def test_civic_runs_without_any_credential_at_all():
    available = enabled_sources({})
    assert len(available) == len(CIVIC_SOURCES) - 1
    assert "nz_legislation_works" not in {entry.source_key for entry in available}


def test_a_configured_key_adds_the_optional_source_back():
    available = enabled_sources({LEGISLATION_API_KEY_ENV: "secret"})
    assert len(available) == len(CIVIC_SOURCES)


def test_the_keyless_legislation_path_remains_available_without_a_key():
    """Sitemap discovery is the keyless path. There is deliberately no aggregate XML source: the
    one that used to be configured never served a listing and reported permanent emptiness as a
    healthy week."""
    keys = {entry.source_key for entry in enabled_sources({})}
    assert "nz_legislation_sitemap" in keys
    assert "nz_legislation_latest" not in keys


# ============================================================================
# Lookup
# ============================================================================


def test_an_unknown_source_key_is_refused():
    with pytest.raises(ValueError, match="unknown Civic source"):
        get_source("nz_not_a_source")


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_every_entry_builds_its_connector(entry):
    connector = entry.build()
    assert connector.source_key == entry.source_key
    assert hasattr(connector, "fetch")
    assert hasattr(connector, "validate_config")


@pytest.mark.parametrize("entry", CIVIC_SOURCES, ids=_IDS)
def test_every_connector_refuses_a_non_empty_config(entry):
    with pytest.raises(ValueError, match="config must be empty"):
        entry.build().validate_config({"url": "https://evil.example.com"})


def test_all_sources_is_the_full_catalog():
    assert all_sources() == CIVIC_SOURCES


# ============================================================================
# The user agent operators asked for
# ============================================================================


def test_the_user_agent_is_descriptive_and_contactable():
    assert CIVIC_USER_AGENT.startswith("PartyMap Civic Watch/")
    assert "+https://partymap.co.nz" in CIVIC_USER_AGENT


# ============================================================================
# The live preview is opt-in
# ============================================================================


def test_listing_the_catalog_makes_no_request(capsys):
    from scripts.civic_source_preview import main

    assert main(["--list"]) == 0
    printed = capsys.readouterr().out
    assert "nz_electoral_commission" in printed
    assert "official" in printed


def test_describing_a_source_without_live_makes_no_request(capsys, monkeypatch):
    """The default must never touch the network: a tool that hits fifteen government sites by
    accident is exactly what a well-behaved crawler must not ship with."""
    import beehive.connectors.http as http_module
    from scripts.civic_source_preview import main

    def explode(*args, **kwargs):
        raise AssertionError("the dry run must not perform any request")

    monkeypatch.setattr(http_module, "fetch_bytes", explode)
    assert main(["nz_electoral_commission"]) == 0
    printed = capsys.readouterr().out
    assert "dry run: no request was made" in printed
    assert "https://elections.nz/media-and-news/rss" in printed
    assert "assigned by application config" in printed


def test_an_unknown_source_key_exits_non_zero(capsys):
    from scripts.civic_source_preview import main

    assert main(["nope"]) == 2
    assert "unknown Civic source" in capsys.readouterr().err


def test_the_live_flag_is_what_performs_the_single_request(monkeypatch, capsys):
    """Exercised through a stubbed transport so the opt-in path itself is covered without any
    network access."""
    import beehive.civic.fetching as fetching
    from scripts.civic_source_preview import main
    from tests.civic.civic_stubs import RecordedFetch, fixture_bytes

    fetch = RecordedFetch(
        {get_source("nz_electoral_commission").url: (
            fixture_bytes("elections_rss.xml"), "application/xml")})
    monkeypatch.setattr(fetching, "_default_fetch", fetch)
    assert main(["nz_electoral_commission", "--live"]) == 0
    printed = capsys.readouterr().out
    assert "health          : ok" in printed
    assert "Enrolment update" in printed
    assert len(fetch.calls) == 1


# ============================================================================
# One catalog entry per endpoint
# ============================================================================
#
# Two entries over one URL fetch the same bytes twice AND namespace their dedupe keys
# differently ("nz_beehive_releases:..." vs "nz_government_news:..."), so one release becomes two
# Civic Candidates and Watch-wide deduplication -- the whole point of `dedupe_key` -- stops
# working. This is exactly what happened with the Beehive RSS feed.


def test_no_two_sources_share_an_endpoint():
    urls = [entry.url for entry in CIVIC_SOURCES]
    duplicates = sorted({url for url in urls if urls.count(url) > 1})
    assert duplicates == [], f"duplicate Civic endpoints: {duplicates}"


def test_no_two_sources_share_a_source_key():
    keys = [entry.source_key for entry in CIVIC_SOURCES]
    assert len(keys) == len(set(keys))


def test_the_beehive_rss_feed_is_catalogued_exactly_once():
    beehive = [entry for entry in CIVIC_SOURCES
               if "beehive.govt.nz" in entry.url]
    assert len(beehive) == 1
    assert beehive[0].source_key == "nz_beehive_releases"


def test_every_source_key_yields_a_distinct_dedupe_namespace():
    """`CivicDocument.dedupe_key` is `source_key:external_key`, so distinct keys over one
    endpoint would silently split one story into two Candidates."""
    from beehive.civic.documents import CivicDocument
    from datetime import datetime, timezone

    seen = {}
    for entry in CIVIC_SOURCES:
        document = CivicDocument(
            source_key=entry.source_key, external_key="shared-upstream-id",
            canonical_url="https://example.govt.nz/a", title="T", tier=entry.tier,
            publisher=entry.publisher,
            first_seen_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
        assert document.dedupe_key not in seen
        seen[document.dedupe_key] = entry.source_key


# ============================================================================
# Source contracts
# ============================================================================
#
# One assertion per source family about the thing that has actually broken before: a detail route
# derived from a status-code guess, an unverified path kind, or a fabricated URL shape.


def test_the_parliament_detail_routes_are_the_rendered_ones():
    """Bills uses one site-wide view index; Select Committees uses a per-documentType one."""
    assert get_source("nz_parliament_bills").definition.url_base == (
        "https://bills.parliament.nz/v/6")
    committees = get_source("nz_parliament_select_committees").definition
    assert committees.url_base == "https://selectcommittees.parliament.nz/v"
    assert committees.url_view_index_by_type["Briefing"] == 0
    assert committees.url_view_index_by_type["Petition"] == 4
    assert committees.url_view_index_by_type["Financial Scrutiny"] == 1


def test_the_committee_route_table_covers_every_type_the_publisher_lists():
    """The site's own document-type filter is the complete current type list. A type missing
    here is silently dropped from every fetch, which is how this source came to omit two thirds
    of its corpus, so the whole list is pinned rather than a sample of it."""
    committees = get_source("nz_parliament_select_committees").definition
    routed = set(committees.url_view_index_by_type) | set(committees.url_external_by_type)
    assert routed == {
        "Bill", "Bill Of Rights Report", "Briefing", "Civil Defence",
        "Considerations Of Standing Orders And Procedures", "Financial Scrutiny", "Inquiry",
        "International Treaty Examination", "Long-Term Insights Briefing",
        "Officers Of Parliament", "Other Matters", "Petition", "Privilege", "Regulation Review",
        "Reports Of Offices Of Parliament", "Reserve Bank Of New Zealand Report",
        "Special Report", "Whole Of Government Direction"}


def test_the_committee_view_codes_are_distinct():
    """They are per-type codes, so two types sharing one would mean a copy-paste rather than a
    reading of the publisher's links."""
    indices = get_source(
        "nz_parliament_select_committees").definition.url_view_index_by_type
    assert len(set(indices.values())) == len(indices)


def test_the_only_off_site_committee_route_is_the_publishers_bills_link():
    committees = get_source("nz_parliament_select_committees").definition
    assert committees.url_external_by_type == {
        "Bill": "https://bills.parliament.nz/v/Bill/{id}"}
    assert committees.document_url_hosts == frozenset({"parliament.nz"})


def test_no_source_still_uses_the_superseded_v0_view_index():
    """`/v/0` came from reading HTTP status on a client-rendered app, which cannot distinguish a
    real route from a fabricated one."""
    for entry in CIVIC_SOURCES:
        assert "/v/0" not in getattr(entry.definition, "url_base", "")


def test_the_hansard_route_is_the_transcript_path():
    from beehive.civic.connectors.parliament import HANSARD_DAILY_URL_TEMPLATE

    assert HANSARD_DAILY_URL_TEMPLATE == (
        "https://hansard.parliament.nz/hansard-transcript/{token}")
    assert "/Hansard/Debates/" not in HANSARD_DAILY_URL_TEMPLATE


def test_only_sources_with_a_verified_route_allow_id_derived_urls():
    """Hansard has no per-record page, so it must never fall back to `/{id}`."""
    assert get_source("nz_parliament_hansard").definition.allow_id_url_fallback is False
    assert get_source("nz_parliament_bills").definition.allow_id_url_fallback is True
    # Select Committees has no blanket route either -- only per-type verified ones.
    assert get_source(
        "nz_parliament_select_committees").definition.allow_id_url_fallback is False


def test_the_legislation_path_kinds_match_the_live_sitemap():
    from beehive.civic.connectors.legislation import document_xml_url

    live_kinds = {
        "act": "https://www.legislation.govt.nz/act/imperial/1267/23/en/latest/",
        "secondary-legislation":
            "https://www.legislation.govt.nz/secondary-legislation/pco-drafted/1887/1/en/latest/",
        "amendment-paper":
            "https://www.legislation.govt.nz/amendment-paper/government/2006/50/en/latest/",
        "bill": "https://www.legislation.govt.nz/bill/government/1998/238/en/latest/",
    }
    for kind, sample in live_kinds.items():
        assert document_xml_url(sample), f"{kind} is published by the sitemap but not covered"


def test_no_civic_source_url_contains_a_placeholder_or_template():
    for entry in CIVIC_SOURCES:
        assert "{" not in entry.url and "}" not in entry.url


def test_every_source_declares_a_publisher_and_an_official_tier():
    for entry in CIVIC_SOURCES:
        assert entry.publisher.strip()
        assert entry.tier is CivicEvidenceTier.OFFICIAL
