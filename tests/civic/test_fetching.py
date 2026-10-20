# tests/civic/test_fetching.py
"""The HTTP + parsing seam: fixed endpoints, host allow-lists, crawl-delay pacing, content-type
assertions, tombstone detection and hardened XML/JSON/gzip parsing. No live network anywhere."""
import gzip
import pathlib
from datetime import datetime, timezone

import pytest

from beehive.civic.documents import CivicSourceError, CivicTombstoneError
from beehive.civic.fetching import (CIVIC_USER_AGENT, CivicEndpoint, CivicTransport, CrawlPacer,
                                     assert_not_tombstone_document, decompress_gzip,
                                     parse_iso8601, parse_json, parse_rfc2822, parse_xml,
                                     require_mapping, require_sequence)
from beehive.connectors.http import ConnectorHttpErrorKind
from tests.civic.civic_stubs import FakeClock, http_error, transport_for

_XML = frozenset({"application/xml"})
_HOSTS = frozenset({"example.govt.nz"})
_URL = "https://www.example.govt.nz/feed.xml"


def _endpoint(**overrides) -> CivicEndpoint:
    kwargs = dict(url=_URL, allowed_hosts=_HOSTS, content_types=_XML)
    kwargs.update(overrides)
    return CivicEndpoint(**kwargs)


# ============================================================================
# CivicEndpoint: fixed, https, self-consistent allow-list
# ============================================================================


def test_endpoint_requires_https():
    with pytest.raises(ValueError, match="must be https"):
        _endpoint(url="http://www.example.govt.nz/feed.xml")


def test_endpoint_requires_a_non_empty_host_allowlist():
    with pytest.raises(ValueError, match="host allowlist"):
        _endpoint(allowed_hosts=frozenset())


def test_endpoint_host_must_be_inside_its_own_allowlist():
    """Catches the copy-paste error where a new source keeps the previous one's allow-list."""
    with pytest.raises(ValueError, match="not in its own allowlist"):
        _endpoint(url="https://elsewhere.example.com/feed.xml")


def test_a_subdomain_of_an_allowed_host_is_accepted():
    assert _endpoint(url="https://sub.example.govt.nz/a").url.startswith("https://sub.")


def test_endpoint_rejects_an_unsupported_method():
    with pytest.raises(ValueError, match="unsupported"):
        _endpoint(method="DELETE")


def test_at_keeps_the_same_policy_for_a_sibling_url():
    """How a sitemap index reaches its children without widening what may be fetched."""
    child = _endpoint().at("https://www.example.govt.nz/sitemap-2.xml")
    assert child.allowed_hosts == _HOSTS
    assert child.content_types == _XML
    assert child.max_bytes == _endpoint().max_bytes


def test_at_cannot_escape_the_allowlist():
    with pytest.raises(ValueError, match="not in its own allowlist"):
        _endpoint().at("https://evil.example.com/sitemap.xml")


# ============================================================================
# CrawlPacer
# ============================================================================


def test_the_first_request_to_a_host_never_waits():
    clock = FakeClock()
    pacer = CrawlPacer(clock=clock, sleeper=clock.sleep)
    pacer.wait_for("https://a.govt.nz/x", 10.0)
    assert clock.slept == []


def test_a_second_request_waits_the_remaining_crawl_delay():
    clock = FakeClock()
    pacer = CrawlPacer(clock=clock, sleeper=clock.sleep)
    pacer.wait_for("https://a.govt.nz/x", 10.0)
    clock.advance(4.0)
    pacer.wait_for("https://a.govt.nz/y", 10.0)
    assert clock.slept == [6.0]


def test_no_wait_once_the_delay_has_already_elapsed():
    clock = FakeClock()
    pacer = CrawlPacer(clock=clock, sleeper=clock.sleep)
    pacer.wait_for("https://a.govt.nz/x", 10.0)
    clock.advance(30.0)
    pacer.wait_for("https://a.govt.nz/y", 10.0)
    assert clock.slept == []


def test_the_delay_is_tracked_per_host():
    clock = FakeClock()
    pacer = CrawlPacer(clock=clock, sleeper=clock.sleep)
    pacer.wait_for("https://a.govt.nz/x", 10.0)
    pacer.wait_for("https://b.govt.nz/x", 10.0)
    assert clock.slept == []


def test_a_zero_delay_is_a_no_op():
    clock = FakeClock()
    pacer = CrawlPacer(clock=clock, sleeper=clock.sleep)
    pacer.wait_for("https://a.govt.nz/x", 0.0)
    pacer.wait_for("https://a.govt.nz/y", 0.0)
    assert clock.slept == []


# ============================================================================
# CivicTransport
# ============================================================================


def test_the_descriptive_user_agent_and_host_allowlist_are_always_sent():
    transport = transport_for({_URL: b"<rss/>"})
    transport.request(_endpoint(), source_key="s")
    call = transport.recorded.calls[0]
    assert call.user_agent == CIVIC_USER_AGENT
    assert call.user_agent.startswith("PartyMap Civic Watch/")
    assert "+https://partymap.co.nz" in call.user_agent
    assert call.allowed_hosts == _HOSTS


def test_the_byte_cap_is_passed_down_to_the_shared_http_client():
    transport = transport_for({_URL: b"<rss/>"})
    transport.request(_endpoint(max_bytes=1234), source_key="s")
    assert transport.recorded.calls[0].max_bytes == 1234


def test_a_post_endpoint_sends_a_json_body():
    url = "https://www.example.govt.nz/api/data/search"
    transport = transport_for({url: b"{}"}, default_content_type="application/json")
    endpoint = CivicEndpoint(
        url=url, allowed_hosts=_HOSTS, method="POST",
        content_types=frozenset({"application/json"}))
    transport.request(endpoint, source_key="s", payload={"page": 2})
    call = transport.recorded.calls[0]
    assert call.method == "POST"
    assert call.payload == {"page": 2}
    assert call.extra_headers["Content-Type"] == "application/json"


def test_an_unexpected_content_type_is_a_source_error():
    """An origin serving HTML where XML used to be is how a crawler silently starts producing
    nonsense."""
    transport = transport_for({_URL: (b"<html>hello</html>", "text/html")})
    with pytest.raises(CivicSourceError, match="expected"):
        transport.request(_endpoint(), source_key="s")


def test_a_retired_endpoint_answering_200_is_a_tombstone_not_an_empty_feed():
    body = b"<html><h1>This feed has been retired</h1><p>no longer available</p></html>"
    transport = transport_for({_URL: (body, "text/html")})
    with pytest.raises(CivicTombstoneError, match="retired-endpoint page"):
        transport.request(_endpoint(), source_key="s")


def test_a_transport_failure_becomes_a_source_error_carrying_the_source_key():
    transport = transport_for(
        {_URL: http_error(ConnectorHttpErrorKind.TRANSIENT, "upstream 503")})
    with pytest.raises(CivicSourceError) as excinfo:
        transport.request(_endpoint(), source_key="nz_stats")
    assert excinfo.value.source_key == "nz_stats"
    assert "upstream 503" in str(excinfo.value)


def test_content_type_is_sniffed_only_when_the_origin_sends_none():
    transport = transport_for({_URL: (b'<?xml version="1.0"?><rss/>', None)})
    assert transport.request(_endpoint(), source_key="s").content_type == "application/xml"


def test_crawl_delay_is_honoured_between_two_requests_to_one_host():
    clock = FakeClock()
    other = "https://www.example.govt.nz/second.xml"
    transport = transport_for({_URL: b"<rss/>", other: b"<rss/>"}, clock=clock)
    transport.request(_endpoint(), source_key="s", crawl_delay_seconds=10.0)
    transport.request(_endpoint().at(other), source_key="s", crawl_delay_seconds=10.0)
    assert clock.slept == [10.0]


# ============================================================================
# Safe parsing
# ============================================================================


# ---------------------------------------------------------------------------
# XML hardening: the parser refuses DTDs and entities, at any offset, in any encoding
# ---------------------------------------------------------------------------
#
# The previous implementation scanned `body[:4096]` for a `<!DOCTYPE` pattern. That was not a
# defence: a legal 5 KB leading comment pushes the declaration past the window, and an ASCII
# regex cannot see a UTF-16 document's null-interleaved bytes. Both bypasses were reproduced
# against the old code and both EXPANDED the entity. These pin the replacement.

_LEGAL_PAD = b"<!--" + b"A" * 5000 + b"-->"


def _entity_payload(prefix: bytes = b"") -> bytes:
    return (b'<?xml version="1.0"?>' + prefix
            + b'<!DOCTYPE r [<!ENTITY x "BOOM">]><r>&x;</r>')


@pytest.mark.parametrize(
    "label,body",
    [
        ("doctype at byte 0", _entity_payload()),
        ("after a 5KB legal comment", _entity_payload(_LEGAL_PAD)),
        ("after a 5KB processing instruction",
         _entity_payload(b"<?pi " + b"B" * 5000 + b"?>")),
        ("after leading whitespace", _entity_payload(b"\n" * 5000)),
        ("after a comment and whitespace", _entity_payload(_LEGAL_PAD + b"\n\n  ")),
    ],
    ids=lambda value: value if isinstance(value, str) else "")
def test_an_entity_declaration_is_refused_at_any_offset(label, body):
    with pytest.raises(CivicSourceError, match="never accepted"):
        parse_xml(body, source_key="s", url=_URL)


def test_a_utf16_entity_declaration_is_refused():
    """An ASCII byte pattern cannot match UTF-16 at all, so the old guard was blind to it."""
    body = ('<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE r [<!ENTITY x "BOOM">]><r>&x;</r>').encode("utf-16")
    with pytest.raises(CivicSourceError, match="never accepted"):
        parse_xml(body, source_key="s", url=_URL)


def test_a_utf16le_entity_declaration_is_refused():
    body = ('<?xml version="1.0" encoding="UTF-16LE"?>'
            '<!DOCTYPE r [<!ENTITY x "BOOM">]><r>&x;</r>').encode("utf-16-le")
    with pytest.raises(CivicSourceError):
        parse_xml(body, source_key="s", url=_URL)


def test_a_bare_doctype_is_refused_even_with_no_entity_declaration():
    """defusedxml ships `forbid_dtd=False`, so this parses out of the box unless it is enabled
    explicitly -- which is why the call passes it."""
    with pytest.raises(CivicSourceError, match="DOCTYPE"):
        parse_xml(
            b'<?xml version="1.0"?><!DOCTYPE rss SYSTEM "http://evil.test/x.dtd"><rss/>',
            source_key="s", url=_URL)


def test_an_external_entity_reference_is_refused():
    with pytest.raises(CivicSourceError):
        parse_xml(
            b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            b"<r>&x;</r>", source_key="s", url=_URL)


def test_nested_entity_amplification_is_refused_before_any_expansion():
    """Billion laughs. The declaration is refused, so nothing is expanded and memory stays
    bounded by the response cap the transport already applies."""
    body = (
        b'<?xml version="1.0"?><!DOCTYPE lol ['
        b'<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        b'<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
        b"]><lol>&d;</lol>")
    with pytest.raises(CivicSourceError):
        parse_xml(body, source_key="s", url=_URL)


def test_a_refused_document_is_never_expanded():
    """The point of refusing at declaration time: the payload never materialises."""
    body = _entity_payload(_LEGAL_PAD)
    with pytest.raises(CivicSourceError) as excinfo:
        parse_xml(body, source_key="s", url=_URL)
    assert "BOOM" not in str(excinfo.value)


def test_the_error_names_the_source_and_the_url():
    with pytest.raises(CivicSourceError) as excinfo:
        parse_xml(_entity_payload(), source_key="nz_stats", url=_URL)
    assert excinfo.value.source_key == "nz_stats"
    assert _URL in str(excinfo.value)


def test_malformed_xml_is_a_source_error_not_a_parse_error():
    with pytest.raises(CivicSourceError, match="malformed XML"):
        parse_xml(b"<rss><channel>", source_key="s", url=_URL)


@pytest.mark.parametrize(
    "label,body",
    [
        ("rss", b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
                b"</channel></rss>"),
        ("atom", b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                 b"<title>T</title></feed>"),
        ("sitemap", b'<?xml version="1.0"?>'
                    b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    b"<url><loc>https://x.govt.nz/a</loc></url></urlset>"),
        ("legislation", b'<?xml version="1.0"?><act id="DLM1" date.assent="1988-07-28">'
                        b"<title>An Act</title></act>"),
        ("with a legal leading comment",
         b'<?xml version="1.0"?><!--' + b"A" * 5000 + b"--><rss><channel/></rss>"),
    ],
    ids=lambda value: value if isinstance(value, str) else "")
def test_normal_official_xml_still_parses(label, body):
    assert parse_xml(body, source_key="s", url=_URL) is not None


def test_utf16_documents_still_parse_and_keep_their_characters():
    body = ('<?xml version="1.0" encoding="UTF-16"?><rss><channel>'
            '<title>M\u0101ori wards</title></channel></rss>').encode("utf-16")
    root = parse_xml(body, source_key="s", url=_URL)
    assert root.find("./channel/title").text == "M\u0101ori wards"


def test_well_formed_xml_parses():
    assert parse_xml(b"<rss><channel/></rss>", source_key="s", url=_URL).tag == "rss"


def test_malformed_json_is_a_source_error():
    with pytest.raises(CivicSourceError, match="not valid JSON"):
        parse_json(b"{nope", source_key="s", url=_URL)


def test_gzip_payloads_are_bounded():
    payload = gzip.compress(b"x" * 5000)
    with pytest.raises(CivicSourceError, match="exceeds"):
        decompress_gzip(payload, source_key="s", url=_URL, max_bytes=100)


def test_valid_gzip_decompresses():
    assert decompress_gzip(
        gzip.compress(b"<urlset/>"), source_key="s", url=_URL, max_bytes=1000) == b"<urlset/>"


def test_invalid_gzip_is_a_source_error():
    with pytest.raises(CivicSourceError, match="invalid gzip"):
        decompress_gzip(b"not gzip at all", source_key="s", url=_URL, max_bytes=1000)


def test_schema_assertions_reject_the_wrong_json_shape():
    with pytest.raises(CivicSourceError, match="JSON object"):
        require_mapping([1, 2], source_key="s", field_name="envelope")
    with pytest.raises(CivicSourceError, match="JSON array"):
        require_sequence({"a": 1}, source_key="s", field_name="results")


# ============================================================================
# Timestamps
# ============================================================================


def test_rfc2822_dates_become_aware_utc():
    parsed = parse_rfc2822("Wed, 05 Aug 2026 11:00:00 +1200")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 12 * 3600


@pytest.mark.parametrize(
    "value", [None, "", "not a date", 12345, "Tue, 32 Zzz 2026 00:00:00 +0000"])
def test_an_unparseable_timestamp_returns_none_rather_than_losing_the_document(value):
    assert parse_rfc2822(value) is None
    assert parse_iso8601(value) is None


def test_iso8601_accepts_the_trailing_z_form_these_apis_use():
    parsed = parse_iso8601("2026-08-06T22:40:00Z")
    assert parsed.utcoffset().total_seconds() == 0


def test_iso8601_accepts_a_bare_date():
    assert parse_iso8601("2026-08-01").year == 2026


def test_a_naive_timestamp_is_treated_as_utc():
    assert parse_iso8601("2026-08-01T10:00:00").tzinfo is not None


# ============================================================================
# Runtime tombstones inside structurally valid XML
# ============================================================================

_ATOM = "http://www.w3.org/2005/Atom"


def _atom(entry_id="x", feed_title="Real feed", subtitle="", entry_title="An item"):
    subtitle_xml = f"<subtitle>{subtitle}</subtitle>" if subtitle else ""
    return (
        f'<?xml version="1.0"?><feed xmlns="{_ATOM}">'
        f"<title>{feed_title}</title>{subtitle_xml}"
        f"<entry><id>{entry_id}</id><title>{entry_title}</title></entry></feed>").encode()


def test_a_valid_atom_feed_declaring_url_changed_is_a_tombstone():
    """The content-type assertion only catches an HTML tombstone. This one parses cleanly, so a
    naive reader sees one entry and reports a healthy week forever."""
    root = parse_xml(_atom(entry_id="url_changed"), source_key="s", url=_URL)
    with pytest.raises(CivicTombstoneError, match="url_changed"):
        assert_not_tombstone_document(root, source_key="s", url=_URL)


@pytest.mark.parametrize("marker", ["url_changed", "URL_CHANGED", "feed_retired", "feed_removed"])
def test_every_known_tombstone_id_is_recognised(marker):
    root = parse_xml(_atom(entry_id=marker), source_key="s", url=_URL)
    with pytest.raises(CivicTombstoneError):
        assert_not_tombstone_document(root, source_key="s", url=_URL)


def test_a_feed_level_tombstone_id_is_recognised():
    body = (
        f'<?xml version="1.0"?><feed xmlns="{_ATOM}">'
        "<id>url_changed</id><title>Gone</title></feed>").encode()
    root = parse_xml(body, source_key="s", url=_URL)
    with pytest.raises(CivicTombstoneError):
        assert_not_tombstone_document(root, source_key="s", url=_URL)


@pytest.mark.parametrize(
    "title",
    ["This feed is no longer available", "The service has been retired",
     "This feed has moved", "No longer published"])
def test_retirement_prose_in_the_feed_title_is_recognised(title):
    """The two signals are checked independently because they have not always appeared
    together."""
    root = parse_xml(_atom(feed_title=title), source_key="s", url=_URL)
    with pytest.raises(CivicTombstoneError, match="retired"):
        assert_not_tombstone_document(root, source_key="s", url=_URL)


def test_retirement_prose_in_the_subtitle_is_recognised():
    root = parse_xml(
        _atom(subtitle="This feed has been discontinued"), source_key="s", url=_URL)
    with pytest.raises(CivicTombstoneError):
        assert_not_tombstone_document(root, source_key="s", url=_URL)


def test_a_healthy_atom_feed_passes():
    root = parse_xml(_atom(), source_key="s", url=_URL)
    assert_not_tombstone_document(root, source_key="s", url=_URL)


def test_a_healthy_rss_feed_passes():
    body = b'<?xml version="1.0"?><rss><channel><title>Releases</title></channel></rss>'
    root = parse_xml(body, source_key="s", url=_URL)
    assert_not_tombstone_document(root, source_key="s", url=_URL)


def test_a_genuine_release_about_a_discontinued_programme_is_not_a_tombstone():
    """Entry *content* is deliberately not scanned: a real Parliament release about a programme
    being discontinued must never be mistaken for a dead feed."""
    body = (
        f'<?xml version="1.0"?><feed xmlns="{_ATOM}"><title>Beehive releases</title>'
        "<entry><id>https://www.beehive.govt.nz/node/1</id>"
        "<title>Minister announces new transport funding</title>"
        "<content>The previous scheme has been discontinued and is no longer available."
        "</content></entry></feed>").encode()
    root = parse_xml(body, source_key="s", url=_URL)
    assert_not_tombstone_document(root, source_key="s", url=_URL)


# ============================================================================
# The landing URL is surfaced through the Civic transport
# ============================================================================


def test_the_final_url_is_surfaced_from_the_shared_http_client():
    """Reporting only: every hop was already re-validated against this endpoint's own
    allowed_hosts before it was followed, so an off-allow-list landing cannot occur."""
    from beehive.connectors.http import HttpResponse

    def fetch(url, **kwargs):
        return HttpResponse(
            body=b"<rss/>", content_type="application/xml",
            final_url="https://www.example.govt.nz/feed-moved.xml")

    transport = CivicTransport(fetch=fetch, pacer=CrawlPacer(clock=FakeClock()))
    response = transport.request(_endpoint(), source_key="s")
    assert response.final_url == "https://www.example.govt.nz/feed-moved.xml"


def test_the_final_url_defaults_to_the_requested_endpoint():
    transport = transport_for({_URL: b"<rss/>"})
    assert transport.request(_endpoint(), source_key="s").final_url == _URL


# ============================================================================
# The hardening holds through the real transport and connector paths
# ============================================================================
#
# parse_xml is the single XML seam for every Civic connector -- official feeds, sitemaps,
# legislation hydration, and the party/media connectors in beehive.connectors, which call it via
# CivicTransport. These drive a hostile payload through the real objects rather than the parser
# alone, so a future connector that reached for ElementTree directly would show up here.


_HOSTILE = (b'<?xml version="1.0"?><!--' + b"A" * 5000
            + b'--><!DOCTYPE r [<!ENTITY x "BOOM">]><rss><channel><title>&x;</title>'
              b"</channel></rss>")


def test_a_padded_entity_payload_is_refused_through_the_civic_transport():
    from beehive.civic.connectors.feeds import CivicFeedConnector, FeedSourceDefinition
    from beehive.domain.civic import CivicEvidenceTier

    definition = FeedSourceDefinition(
        source_key="hostile_feed",
        endpoint=CivicEndpoint(url=_URL, allowed_hosts=_HOSTS, content_types=_XML),
        publisher="Test", tier=CivicEvidenceTier.OFFICIAL)
    connector = CivicFeedConnector(definition, transport_for({_URL: _HOSTILE}))
    with pytest.raises(CivicSourceError, match="never accepted"):
        connector.fetch(datetime(2026, 8, 13, tzinfo=timezone.utc))


def test_a_hostile_sitemap_is_refused_through_the_sitemap_connector():
    from beehive.civic.catalog import OMBUDSMAN_SITEMAP
    from beehive.civic.connectors.sitemaps import SitemapConnector

    body = (b'<?xml version="1.0"?><!--' + b"A" * 5000
            + b'--><!DOCTYPE u [<!ENTITY x "BOOM">]>'
              b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              b"<url><loc>https://www.ombudsman.parliament.nz/&x;</loc></url></urlset>")
    connector = SitemapConnector(
        OMBUDSMAN_SITEMAP, transport_for({OMBUDSMAN_SITEMAP.endpoint.url: body}))
    with pytest.raises(CivicSourceError, match="never accepted"):
        connector.fetch(datetime(2026, 8, 13, tzinfo=timezone.utc))


def test_a_hostile_legislation_document_is_refused_during_hydration():
    from beehive.civic.catalog import legislation_hydrator

    page = "https://www.legislation.govt.nz/act/imperial/1267/23/en/latest/"
    xml = "https://www.legislation.govt.nz/act/imperial/1267/23/en/latest.xml"
    body = (b'<?xml version="1.0"?><!--' + b"A" * 5000
            + b'--><!DOCTYPE act [<!ENTITY x "BOOM">]><act id="DLM1"><title>&x;</title></act>')
    hydrator = legislation_hydrator(transport_for({xml: body}))
    with pytest.raises(CivicSourceError, match="never accepted"):
        hydrator.hydrate([page], datetime(2026, 8, 13, tzinfo=timezone.utc))


def test_a_utf16_hostile_feed_is_refused_through_the_transport():
    from beehive.civic.connectors.feeds import CivicFeedConnector, FeedSourceDefinition
    from beehive.domain.civic import CivicEvidenceTier

    body = ('<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE r [<!ENTITY x "BOOM">]>'
            "<rss><channel><title>&x;</title></channel></rss>").encode("utf-16")
    definition = FeedSourceDefinition(
        source_key="hostile_utf16",
        endpoint=CivicEndpoint(url=_URL, allowed_hosts=_HOSTS, content_types=_XML),
        publisher="Test", tier=CivicEvidenceTier.OFFICIAL)
    connector = CivicFeedConnector(definition, transport_for({_URL: body}))
    with pytest.raises(CivicSourceError):
        connector.fetch(datetime(2026, 8, 13, tzinfo=timezone.utc))


def test_a_benign_feed_still_flows_through_the_same_path():
    """The floor: hardening must not have broken ordinary ingestion."""
    from beehive.civic.connectors.feeds import CivicFeedConnector, FeedSourceDefinition
    from beehive.domain.civic import CivicEvidenceTier

    body = (b'<?xml version="1.0"?><rss><channel>'
            b"<item><title>A release</title><link>https://www.example.govt.nz/a</link>"
            b"<guid>https://www.example.govt.nz/a</guid></item></channel></rss>")
    definition = FeedSourceDefinition(
        source_key="benign_feed",
        endpoint=CivicEndpoint(url=_URL, allowed_hosts=_HOSTS, content_types=_XML),
        publisher="Test", tier=CivicEvidenceTier.OFFICIAL)
    connector = CivicFeedConnector(definition, transport_for({_URL: body}))
    result = connector.fetch(datetime(2026, 8, 13, tzinfo=timezone.utc))
    assert [d.title for d in result.documents] == ["A release"]


def test_the_parser_is_the_only_xml_entry_point_in_the_civic_package():
    """A connector that reached for ElementTree directly would bypass every guarantee above."""
    import pathlib

    package = pathlib.Path(beehive_civic_root())
    offenders = []
    for path in package.rglob("*.py"):
        source = path.read_text()
        if "ET.fromstring" in source and path.name != "fetching.py":
            offenders.append(path.name)
    assert offenders == [], f"these bypass parse_xml: {offenders}"


def beehive_civic_root() -> str:
    import beehive.civic

    return str(pathlib.Path(beehive.civic.__file__).parent)


def _atom_entries(*summaries, feed_title="Real feed"):
    entries = "".join(
        f"<entry><id>https://example.test/{index}</id><title>Item {index}</title>"
        f"<summary>{summary}</summary></entry>"
        for index, summary in enumerate(summaries))
    return (
        f'<?xml version="1.0"?><feed xmlns="{_ATOM}">'
        f"<title>{feed_title}</title>{entries}</feed>").encode()


def test_a_busy_news_feed_reporting_that_something_was_removed_is_not_a_tombstone():
    """The 2026-09-30 Stuff failure: a healthy 72-entry feed carried a story summary saying a
    contaminated tap "has been removed", and the whole source failed as a retired endpoint. A
    feed still publishing many distinct items is by definition not a retirement notice."""
    body = _atom_entries(
        "A council water fountain has been removed after a contamination scare.",
        "Councillors debate the transport budget.",
        "Weather warning issued for the lower North Island.")
    root = parse_xml(body, source_key="stuff", url=_URL)
    assert_not_tombstone_document(root, source_key="stuff", url=_URL)


def test_a_single_entry_retirement_notice_in_an_entry_summary_is_still_a_tombstone():
    """The case the entry scan exists for: one entry, and that entry is the notice itself."""
    body = _atom_entries("This feed is no longer available. Please update your reader.")
    root = parse_xml(body, source_key="s", url=_URL)
    with pytest.raises(CivicTombstoneError, match="retired"):
        assert_not_tombstone_document(root, source_key="s", url=_URL)


def test_a_single_entry_retirement_notice_in_an_entry_title_is_still_a_tombstone():
    body = (
        f'<?xml version="1.0"?><feed xmlns="{_ATOM}"><title>Subscriptions</title>'
        "<entry><id>https://example.test/1</id>"
        "<title>This feed has been retired</title></entry></feed>").encode()
    root = parse_xml(body, source_key="s", url=_URL)
    with pytest.raises(CivicTombstoneError, match="retired"):
        assert_not_tombstone_document(root, source_key="s", url=_URL)
