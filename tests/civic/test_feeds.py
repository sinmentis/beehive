# tests/civic/test_feeds.py
"""The RSS/Atom Civic sources, against recorded fixtures of the real feeds.

The three identity rules under test are the ones that actually differ between these feeds:
the Electoral Commission's guid IS its page URL, Beehive's guid is a Drupal node URL distinct
from the canonical release link, and Atom identity is `<id>` with `<updated>` as its timestamp.
"""
from datetime import datetime, timezone

import pytest

from beehive.civic.catalog import (BEEHIVE_RELEASES, ELECTORAL_COMMISSION, ORDER_PAPER,
                                    RBNZ_NEWS)
from beehive.civic.connectors.feeds import CivicFeedConnector, html_to_text
from beehive.civic.documents import CivicSourceHealth
from beehive.domain.civic import CivicEvidenceTier
from tests.civic.civic_stubs import fixture_bytes, transport_for

T0 = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)


def _connector(definition, body: bytes, *, content_type="application/xml"):
    transport = transport_for({definition.endpoint.url: (body, content_type)})
    connector = CivicFeedConnector(definition, transport)
    connector.transport = transport  # type: ignore[attr-defined]
    return connector


# ============================================================================
# Electoral Commission
# ============================================================================


@pytest.fixture
def elections():
    return _connector(ELECTORAL_COMMISSION, fixture_bytes("elections_rss.xml"))


def test_electoral_commission_maps_core_fields(elections):
    result = elections.fetch(T0)
    assert result.health is CivicSourceHealth.OK
    first = result.documents[0]
    assert first.title == "Enrolment update for the 2026 General Election"
    assert first.publisher == "New Zealand Electoral Commission"
    assert first.tier is CivicEvidenceTier.OFFICIAL
    assert "94.1 percent" in first.excerpt
    assert first.raw_metadata["category"] == "Media release"


def test_electoral_commission_guid_is_the_page_url(elections):
    first = elections.fetch(T0).documents[0]
    assert first.external_key == "https://elections.nz/media-and-news/2026/enrolment-update/"


def test_electoral_commission_separates_published_updated_and_first_seen(elections):
    """The feed lists the 10 most recently UPDATED pages, so an entry can carry an older
    publication date and a newer modification date -- and first_seen_at is ours alone."""
    first = elections.fetch(T0).documents[0]
    assert first.published_at.date().isoformat() == "2026-08-04"
    assert first.updated_at.date().isoformat() == "2026-08-11"
    assert first.published_at < first.updated_at
    assert first.first_seen_at == T0
    assert first.raw_metadata["publication"]["source"] == "rss-pubDate"
    assert first.raw_metadata["publication"]["value"] == first.published_at.isoformat()
    assert first.raw_metadata["excerpt_source"] == "rss-description"


def test_first_seen_at_comes_from_our_clock_not_the_feed(elections):
    later = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert elections.fetch(later).documents[0].first_seen_at == later


def test_tracking_parameters_are_stripped_from_the_canonical_url(elections):
    second = elections.fetch(T0).documents[1]
    assert second.canonical_url == "https://elections.nz/media-and-news/2026/overseas-voting/"


def test_the_electoral_commission_crawl_delay_is_two_seconds(elections):
    elections.fetch(T0)
    assert ELECTORAL_COMMISSION.crawl_delay_seconds == 2.0


def test_the_feed_is_capped_at_ten_items():
    assert ELECTORAL_COMMISSION.max_items == 10


# ============================================================================
# Beehive
# ============================================================================


@pytest.fixture
def beehive():
    return _connector(BEEHIVE_RELEASES, fixture_bytes("beehive_rss.xml"))


def test_beehive_node_guid_is_the_identity_and_the_release_path_is_canonical(beehive):
    """The node URL survives a slug rename, so it is the stable identity; the release path is
    where a reader is sent."""
    first = beehive.fetch(T0).documents[0]
    assert first.external_key == "https://www.beehive.govt.nz/node/153241"
    assert first.canonical_url == (
        "https://www.beehive.govt.nz/release/government-announces-transport-package")
    assert first.external_key != first.canonical_url


def test_beehive_keeps_the_feed_link_and_guid_in_metadata(beehive):
    first = beehive.fetch(T0).documents[0]
    assert first.raw_metadata["feed_guid"].endswith("/node/153241")
    assert "/release/" in first.raw_metadata["feed_link"]


def test_beehive_full_html_description_becomes_bounded_plain_text(beehive):
    first = beehive.fetch(T0).documents[0]
    assert "$1.2 billion" in first.excerpt
    assert "<strong>" not in first.excerpt
    assert "Work begins in 2027." in first.excerpt


def test_speeches_and_releases_are_both_carried(beehive):
    documents = beehive.fetch(T0).documents
    assert len(documents) == 2
    assert any("/speech/" in doc.canonical_url for doc in documents)


# ============================================================================
# Order Paper (Atom)
# ============================================================================


@pytest.fixture
def order_paper():
    return _connector(ORDER_PAPER, fixture_bytes("order_paper_atom.xml"))


def test_atom_identity_is_the_entry_id(order_paper):
    entry = order_paper.fetch(T0).documents[0]
    assert entry.external_key == "urn:parliament:orderpaper:2026-08-12"


def test_atom_uses_the_alternate_link_never_the_self_link(order_paper):
    entry = order_paper.fetch(T0).documents[0]
    assert entry.canonical_url == "https://www.parliament.nz/en/pb/order-paper/2026-08-12"


def test_atom_updated_becomes_updated_at(order_paper):
    entry = order_paper.fetch(T0).documents[0]
    assert entry.updated_at.isoformat() == "2026-08-12T02:00:00+00:00"
    assert entry.published_at.isoformat() == "2026-08-11T20:00:00+00:00"
    assert entry.raw_metadata["publication"]["source"] == "atom-published"


# ============================================================================
# Shared behavior
# ============================================================================


def test_config_must_be_empty_so_a_stored_url_can_never_redirect_the_endpoint(elections):
    with pytest.raises(ValueError, match="config must be empty"):
        elections.fetch(T0, config={"url": "https://evil.example.com/rss"})


def test_a_feed_with_no_items_is_empty_not_ok():
    connector = _connector(
        ELECTORAL_COMMISSION, b'<?xml version="1.0"?><rss><channel/></rss>')
    result = connector.fetch(T0)
    assert result.health is CivicSourceHealth.EMPTY
    assert result.documents == ()


def test_one_malformed_entry_degrades_the_result_and_keeps_the_rest():
    body = (
        b'<?xml version="1.0"?><rss><channel>'
        b"<item><title>Good</title><link>https://elections.nz/a</link>"
        b"<guid>https://elections.nz/a</guid></item>"
        b"<item><link>https://elections.nz/b</link></item>"
        b"</channel></rss>")
    result = _connector(ELECTORAL_COMMISSION, body).fetch(T0)
    assert result.health is CivicSourceHealth.DEGRADED
    assert len(result.documents) == 1
    assert "missing a title" in result.degraded_reasons[0]


def test_the_request_carries_the_declared_host_allowlist(elections):
    elections.fetch(T0)
    call = elections.transport.recorded.calls[0]
    assert call.allowed_hosts == ELECTORAL_COMMISSION.endpoint.allowed_hosts
    assert "elections.nz" in call.allowed_hosts


# ============================================================================
# Reuse of the existing Channel feeds
# ============================================================================


def test_the_rbnz_feed_reuses_the_existing_channel_definition():
    """Reuse rather than a second constant pointed at the same URL, which is how two views of one
    source drift apart when an endpoint moves."""
    from beehive.connectors.official_feeds import FEED_DEFINITIONS

    by_key = {feed.type_key: feed for feed in FEED_DEFINITIONS}
    assert RBNZ_NEWS.endpoint.url == by_key["rbnz_news"].url
    assert RBNZ_NEWS.publisher == by_key["rbnz_news"].publisher


def test_the_channel_beehive_feed_is_not_adapted_a_second_time():
    """official_feeds' `nz_government_news` points at the very same rss.xml BEEHIVE_RELEASES
    already reads. Adapting it too would fetch the same bytes twice and, worse, namespace the
    dedupe keys differently -- so one release would become two Civic Candidates and defeat
    Watch-wide deduplication."""
    from beehive.civic.catalog import source_keys
    from beehive.connectors.official_feeds import FEED_DEFINITIONS

    by_key = {feed.type_key: feed for feed in FEED_DEFINITIONS}
    assert BEEHIVE_RELEASES.endpoint.url == by_key["nz_government_news"].url
    assert "nz_government_news" not in source_keys()


def test_the_surviving_beehive_definition_is_the_richer_one():
    """The adapted Channel feed treats the link as identity; this one keeps the Drupal node URL,
    which survives a slug rename."""
    assert BEEHIVE_RELEASES.guid_is_identity


def test_an_adapted_feed_parses_with_the_civic_normalizer():
    body = (
        b'<?xml version="1.0"?><rss><channel>'
        b"<item><title>OCR held at 3.00 percent</title>"
        b"<link>https://www.rbnz.govt.nz/news/1</link>"
        b"<guid>https://www.rbnz.govt.nz/news/1</guid>"
        b"<pubDate>Wed, 09 Jul 2026 14:00:00 +1200</pubDate>"
        b"<description>&lt;p&gt;Held.&lt;/p&gt;</description></item>"
        b"</channel></rss>")
    document = _connector(RBNZ_NEWS, body).fetch(T0).documents[0]
    assert document.source_key == "nz_rbnz_news"
    assert document.tier is CivicEvidenceTier.OFFICIAL
    assert document.excerpt == "Held."


# ============================================================================
# HTML extraction
# ============================================================================


def test_html_to_text_keeps_block_boundaries_and_drops_markup():
    assert html_to_text("<p>One</p><p>Two</p>") == "One Two"
    assert html_to_text("<b>bold</b> text") == "bold text"


def test_html_to_text_drops_script_and_style_content_entirely():
    """Inline script source must never end up as display text beside a civic statement."""
    assert html_to_text("<script>var x = 1;</script>Real text") == "Real text"
    assert html_to_text("<style>.a{color:red}</style>Real text") == "Real text"


def test_html_to_text_treats_a_missing_description_as_empty():
    assert html_to_text(None) == ""
