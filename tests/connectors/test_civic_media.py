"""The media catalog: ownership groups are what corroboration counts, licence-restricted outlets
fail closed before any request, aggregators and social platforms can never be evidence, and each
enabled outlet is one canonical Civic source entry pinned to one verified endpoint."""
from datetime import datetime, timezone

import pytest

from beehive.civic.catalog import CivicSourceEntry
from beehive.civic.connectors.feeds import CivicFeedConnector
from beehive.civic.connectors.sitemaps import SitemapConnector
from beehive.civic.documents import CivicSourceError, CivicSourceHealth
from beehive.connectors.civic_catalog import (
    CivicSourceRestrictedError,
    CivicSourceUnavailableError,
    RefusingConnector,
    RestrictedConnector,
    SourceAvailability,
    UsePolicy,
    independent_owner_count,
    url_host,
)
from beehive.connectors.civic_media import (
    MEDIA_CATALOG,
    MEDIA_SOURCES,
    OWNER_CROWN_PUBLIC,
    OWNER_NZME,
    OWNER_STUFF_GROUP,
    CivicMediaConnector,
    enabled_sources,
    fetch_outlet,
)
from beehive.connectors.civic_strategies import NewsSitemapConnector, WallClockFeedConnector
from beehive.connectors.registry import get as get_connector
from beehive.domain.channels import ChannelKind
from beehive.domain.civic import CivicEvidenceTier
from tests.civic.civic_stubs import transport_for
from tests.connectors.civic_fixtures_loader import civic_fixture

NOW = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)


def _owners(*keys: str) -> list[str]:
    return [MEDIA_CATALOG.get(key).owner_id for key in keys]


def _documents(key: str, fixture: str):
    entry = MEDIA_CATALOG.get(key)
    transport = transport_for({entry.url: civic_fixture(fixture)})
    return fetch_outlet(key, NOW, transport=transport).documents


def test_every_outlet_is_a_civic_catalog_entry():
    for entry in MEDIA_SOURCES:
        assert isinstance(entry, CivicSourceEntry)
        assert entry.publisher and entry.owner_id


def test_state_funded_public_media_share_one_owner():
    assert _owners("rnz", "1news", "te-ao") == [OWNER_CROWN_PUBLIC] * 3


def test_nzme_mastheads_share_one_owner():
    assert _owners("nz-herald", "newstalk-zb") == [OWNER_NZME] * 2


def test_stuff_and_the_post_share_one_owner():
    assert _owners("stuff", "the-post") == [OWNER_STUFF_GROUP] * 2


def test_the_independents_each_carry_their_own_owner():
    independents = _owners("spinoff", "newsroom", "interest", "waatea")
    assert len(set(independents)) == 4
    assert not set(independents) & {OWNER_CROWN_PUBLIC, OWNER_NZME, OWNER_STUFF_GROUP}


def test_two_mastheads_of_one_owner_do_not_corroborate():
    stuff = _documents("stuff", "stuff_latest.atom")
    post_entry = MEDIA_CATALOG.get("the-post")
    # The Post republishes the same report; the fixture stands in for its identical Atom shape.
    post_feed = civic_fixture("stuff_latest.atom").replace(b"stuff.co.nz", b"thepost.co.nz")
    transport = transport_for({post_entry.url: post_feed})
    post = fetch_outlet("the-post", NOW, transport=transport).documents
    assert stuff and post
    assert independent_owner_count(list(stuff) + list(post), MEDIA_CATALOG) == 1


def test_different_owners_corroborate():
    stuff = _documents("stuff", "stuff_latest.atom")
    odt = _documents("odt", "odt_sitemap_news.xml")
    assert independent_owner_count(list(stuff) + list(odt), MEDIA_CATALOG) == 2


def test_restricted_outlets_carry_no_endpoint_and_never_reach_an_ai_call():
    for key in ("nz-herald", "newstalk-zb", "rnz"):
        entry = MEDIA_CATALOG.get(key)
        assert entry.use_policy is UsePolicy.RESTRICTED
        assert entry.availability is SourceAvailability.LICENCE_WITHHELD
        assert entry.url == ""
        assert entry.connector_class is RestrictedConnector
        assert not entry.may_be_evidence
        assert not entry.may_send_body_to_ai
        assert entry.note


def test_fetching_a_restricted_outlet_fails_before_any_request():
    transport = transport_for({})
    for key in ("nz-herald", "rnz"):
        with pytest.raises(CivicSourceRestrictedError):
            fetch_outlet(key, NOW, transport=transport)
    assert transport.recorded.calls == []


def test_aggregators_and_social_platforms_are_discovery_only():
    for key in ("scoop", "google-news", "msn", "yahoo-news", "facebook", "x", "reddit"):
        entry = MEDIA_CATALOG.get(key)
        assert entry.use_policy is UsePolicy.DISCOVERY_ONLY
        assert entry.tier is CivicEvidenceTier.AGGREGATOR
        assert entry.connector_class is RefusingConnector
        assert not entry.may_be_evidence
        assert not entry.may_send_body_to_ai


def test_google_news_is_catalogued_only_so_its_links_can_be_refused():
    google = MEDIA_CATALOG.get("google-news")
    assert google.url == ""
    assert google.availability is SourceAvailability.NO_PUBLIC_SURFACE
    assert MEDIA_CATALOG.for_url(
        "https://news.google.com/rss/articles/CBMiT2h0dHBz").source_key == "google-news"


def test_every_enabled_outlet_pins_one_endpoint_on_its_own_host():
    for entry in enabled_sources():
        assert entry.url
        assert url_host(entry.url) in entry.hosts
        assert entry.definition.endpoint.allowed_hosts == entry.hosts


def test_the_verified_endpoints_are_the_committed_ones():
    expected = {
        "1news": "https://www.1news.co.nz/arc/outboundfeeds/rss/?outputType=xml",
        "te-ao": "https://www.teaonews.co.nz/arc/outboundfeeds/rss/?outputType=xml",
        "stuff": "https://www.stuff.co.nz/rss",
        "the-post": "https://www.thepost.co.nz/rss",
        "odt": "https://www.odt.co.nz/sitemaps/odt/sitemap_news1.xml",
        "spinoff": "https://thespinoff.co.nz/sitemaps/articles/1.xml",
        "newsroom": "https://newsroom.co.nz/feed/",
        "interest": "https://www.interest.co.nz/rss",
        "waatea": "https://waateanews.com/feed/",
    }
    assert {entry.source_key: entry.url for entry in enabled_sources()} == expected


def test_each_outlet_uses_the_strategy_its_surface_needs():
    assert MEDIA_CATALOG.get("odt").connector_class is NewsSitemapConnector
    assert MEDIA_CATALOG.get("interest").connector_class is WallClockFeedConnector
    assert MEDIA_CATALOG.get("spinoff").connector_class is SitemapConnector
    for key in ("1news", "te-ao", "stuff", "the-post", "newsroom", "waatea"):
        assert MEDIA_CATALOG.get(key).connector_class is CivicFeedConnector


def test_only_the_redirect_destination_host_is_allow_listed_for_te_ao():
    te_ao = MEDIA_CATALOG.get("te-ao")
    assert te_ao.hosts == frozenset({"www.teaonews.co.nz"})
    assert not te_ao.owns_url("https://www.teaomaori.news/story")


def test_interest_declares_the_timezone_its_wall_clock_dates_are_in():
    assert MEDIA_CATALOG.get("interest").definition.publication_timezone == "Pacific/Auckland"


def test_atom_documents_separate_published_from_updated():
    documents = _documents("stuff", "stuff_latest.atom")
    assert documents[0].published_at == datetime(2026, 8, 12, 10, 35, 42, tzinfo=timezone.utc)
    assert documents[0].updated_at == datetime(2026, 8, 12, 10, 36, tzinfo=timezone.utc)
    assert documents[0].first_seen_at == NOW


def test_documents_carry_the_catalogs_tier_not_the_documents_claim():
    entry = MEDIA_CATALOG.get("odt")
    document = civic_fixture("odt_sitemap_news.xml").replace(
        b"<news:name>Otago Daily Times</news:name>", b"<news:name>Official Gazette</news:name>")
    transport = transport_for({entry.url: document})
    documents = fetch_outlet("odt", NOW, transport=transport).documents
    assert documents
    for record in documents:
        assert record.tier is CivicEvidenceTier.REPUTABLE_MEDIA
        assert record.publisher == "Otago Daily Times"


def test_config_must_name_a_catalogued_outlet_and_nothing_else():
    connector = CivicMediaConnector()
    connector.validate_config({"outlet": "stuff"})
    with pytest.raises(ValueError, match="unknown civic source"):
        connector.validate_config({"outlet": "not-an-outlet"})
    with pytest.raises(ValueError, match="must be exactly"):
        connector.validate_config({"outlet": "stuff", "url": "https://evil.test/rss"})
    with pytest.raises(ValueError, match="must be exactly"):
        connector.validate_config({})
    with pytest.raises(ValueError, match="must be a string key"):
        connector.validate_config({"outlet": 3})


def test_fetch_requests_only_the_catalogued_endpoint():
    entry = MEDIA_CATALOG.get("odt")
    transport = transport_for({entry.url: civic_fixture("odt_sitemap_news.xml")})
    CivicMediaConnector(transport=transport).fetch({"outlet": "odt"})
    assert transport.recorded.urls == [entry.url]


def test_an_unavailable_outlet_fails_visibly_rather_than_being_scraped():
    connector = CivicMediaConnector(transport=transport_for({}))
    with pytest.raises(CivicSourceUnavailableError, match="no_public_surface"):
        connector.fetch({"outlet": "three-news"})


def test_a_document_of_the_wrong_shape_is_a_named_civic_error():
    entry = MEDIA_CATALOG.get("stuff")
    transport = transport_for(
        {entry.url: (b"<html><body>not a feed</body></html>", "text/html")})
    with pytest.raises(CivicSourceError, match="expected one of"):
        CivicMediaConnector(transport=transport).fetch({"outlet": "stuff"})


def test_an_outlet_with_nothing_to_report_is_empty_not_ok():
    entry = MEDIA_CATALOG.get("newsroom")
    transport = transport_for({entry.url: (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Newsroom</title>'
        b"<link>https://newsroom.co.nz</link></channel></rss>")})
    result = fetch_outlet("newsroom", NOW, transport=transport)
    assert result.health is CivicSourceHealth.EMPTY
    assert result.is_complete


def test_preview_is_bounded():
    entry = MEDIA_CATALOG.get("stuff")
    connector = CivicMediaConnector(
        transport=transport_for({entry.url: civic_fixture("stuff_latest.atom")}))
    assert len(connector.fetch_preview({"outlet": "stuff"}, limit=1)) == 1
    with pytest.raises(ValueError, match="preview limit must be positive"):
        connector.fetch_preview({"outlet": "stuff"}, limit=0)


def test_every_unavailable_media_source_declares_a_reason():
    for entry in MEDIA_CATALOG.disabled_sources():
        assert entry.note, f"{entry.source_key} must record why it is unavailable"


def test_connector_is_registered_for_editorial_channels_only():
    connector = get_connector("civic_media_report")
    assert connector.supported_channel_kinds == frozenset({ChannelKind.EDITORIAL})
    assert len(MEDIA_SOURCES) == len(MEDIA_CATALOG) == 20
