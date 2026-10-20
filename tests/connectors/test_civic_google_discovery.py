"""Google News items are hints, never evidence.

These tests pin the two halves of that rule: nothing here ever resolves a `news.google.com` URL
(no token decoding, no redirect following, no article fetch), and a hint only becomes citable
when the named publisher's *own* catalogued documents carry the same headline in a bounded time
window. Every other case -- unknown publisher, restricted publisher, aggregator or social source,
same-owner duplicate, off-allowlist URL, missing date, ambiguous or absent match -- stays
discovery-only.
"""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.documents import CivicDocument
from beehive.connectors.civic_catalog import PartyMediaCatalog
from beehive.connectors.civic_google_discovery import (
    CIVIC_CATALOG,
    PUBLICATION_TOLERANCE,
    DiscoveryHint,
    DiscoveryOutcome,
    find_publisher,
    may_leave_discovery,
    normalize_headline,
    resolve_hint,
    resolve_hints,
)
from beehive.connectors.civic_media import MEDIA_CATALOG
from beehive.domain.civic import CivicEvidenceTier

_PUBLISHED = datetime(2026, 8, 12, 10, 35, tzinfo=timezone.utc)
_SEEN = datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc)
_HEADLINE = "Leadership vote held after challenge fails"
_GOOGLE_URL = "https://news.google.com/rss/articles/CBMiT2h0dHBzOi8vd3d3LnN0dWZm?oc=5"


def _document(outlet: str, url: str, title: str = _HEADLINE, published_at=_PUBLISHED):
    entry = MEDIA_CATALOG.get(outlet)
    return CivicDocument(
        source_key=entry.source_key,
        external_key=url,
        canonical_url=url,
        title=title,
        tier=entry.tier,
        publisher=entry.publisher,
        first_seen_at=_SEEN,
        published_at=published_at)


_STUFF_DOCUMENT = _document("stuff", "https://www.stuff.co.nz/politics/361019424/leadership-vote")


def _hint(**overrides) -> DiscoveryHint:
    base = dict(
        title=f"{_HEADLINE} - Stuff",
        url=_GOOGLE_URL,
        publisher_name="Stuff",
        published_at=_PUBLISHED,
    )
    base.update(overrides)
    return DiscoveryHint(**base)


def test_a_matching_hint_resolves_to_the_publishers_own_document():
    resolution = resolve_hint(_hint(), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.RESOLVED
    assert resolution.document is _STUFF_DOCUMENT
    assert resolution.is_resolved


def test_a_resolved_hint_never_carries_a_google_url():
    resolution = resolve_hint(_hint(), [_STUFF_DOCUMENT])
    assert "news.google.com" not in resolution.document.canonical_url
    assert resolution.document.canonical_url.startswith("https://www.stuff.co.nz/")


@pytest.mark.parametrize(
    "url",
    [
        _GOOGLE_URL,
        "https://news.google.com/articles/CBMiT2h0dHBz",
        "https://www.msn.com/en-nz/news/story",
        "https://www.scoop.co.nz/stories/PA2608/S00123/release.htm",
        "https://x.com/someone/status/1",
        "https://www.facebook.com/party/posts/1",
        "https://www.nzherald.co.nz/nz/politics/story",
        "https://www.rnz.co.nz/news/political/1",
        "https://unknown-blog.test/story",
        "file:///etc/passwd",
        "not a url",
    ],
)
def test_urls_that_may_never_be_cited_as_evidence(url):
    assert not may_leave_discovery(url)


def test_only_a_citable_publishers_own_url_may_leave_discovery():
    assert may_leave_discovery("https://www.stuff.co.nz/politics/1")
    assert may_leave_discovery("https://www.greens.org.nz/a_release")


def test_a_google_hosted_hint_still_only_ever_yields_the_publishers_url():
    resolution = resolve_hint(_hint(url=_GOOGLE_URL), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.RESOLVED
    assert not may_leave_discovery(_GOOGLE_URL)
    assert resolution.document.canonical_url == _STUFF_DOCUMENT.canonical_url


def test_a_hint_carrying_an_aggregator_url_resolves_only_through_the_publisher():
    resolution = resolve_hint(
        _hint(url="https://www.msn.com/en-nz/news/story"), [_STUFF_DOCUMENT])
    assert resolution.document.canonical_url.startswith("https://www.stuff.co.nz/")


def test_an_aggregators_own_item_never_resolves():
    resolution = resolve_hint(_hint(publisher_name="Google News"), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.DISCOVERY_ONLY_PUBLISHER
    assert resolution.source_key == "google-news"
    assert resolution.document is None


def test_an_unknown_publisher_stays_discovery_only():
    resolution = resolve_hint(_hint(publisher_name="Some Blog"), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.UNKNOWN_PUBLISHER
    assert resolution.document is None


@pytest.mark.parametrize("publisher", ["NZ Herald", "Newstalk ZB", "RNZ"])
def test_a_licence_restricted_publisher_is_refused_by_name(publisher):
    resolution = resolve_hint(_hint(publisher_name=publisher), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.RESTRICTED_PUBLISHER
    assert resolution.document is None
    assert resolution.detail


def test_a_restricted_publisher_is_refused_even_with_a_matching_document():
    smuggled = CivicDocument(
        source_key="nz-herald",
        external_key="x",
        canonical_url="https://www.nzherald.co.nz/nz/politics/story",
        title=_HEADLINE,
        tier=CivicEvidenceTier.REPUTABLE_MEDIA,
        publisher="The New Zealand Herald",
        first_seen_at=_SEEN,
        published_at=_PUBLISHED)
    resolution = resolve_hint(_hint(publisher_name="NZ Herald"), [smuggled])
    assert resolution.outcome is DiscoveryOutcome.RESTRICTED_PUBLISHER
    assert resolution.document is None


@pytest.mark.parametrize("publisher", ["Scoop", "MSN", "Yahoo News"])
def test_an_aggregator_publisher_stays_discovery_only(publisher):
    resolution = resolve_hint(_hint(publisher_name=publisher), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.DISCOVERY_ONLY_PUBLISHER
    assert resolution.document is None


@pytest.mark.parametrize("publisher", ["Facebook", "X", "Reddit"])
def test_a_community_source_stays_discovery_only(publisher):
    resolution = resolve_hint(_hint(publisher_name=publisher), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.DISCOVERY_ONLY_PUBLISHER
    assert resolution.document is None


def test_a_publisher_with_no_fetchable_surface_is_refused():
    resolution = resolve_hint(_hint(publisher_name="ThreeNews"), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.PUBLISHER_UNAVAILABLE
    assert resolution.document is None


def test_a_blocked_party_publisher_is_refused():
    resolution = resolve_hint(_hint(publisher_name="New Zealand Loyal"), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.PUBLISHER_UNAVAILABLE


def test_a_hint_without_a_publication_time_stays_discovery_only():
    resolution = resolve_hint(_hint(published_at=None), [_STUFF_DOCUMENT])
    assert resolution.outcome is DiscoveryOutcome.MISSING_DATE
    assert resolution.document is None


def test_an_undated_document_cannot_match():
    """A source with no publication times -- ACT's locale sitemap, a listing page -- has no time
    window to check, so it can never satisfy a hint."""
    undated = _document(
        "stuff", "https://www.stuff.co.nz/politics/361019424/leadership-vote", published_at=None)
    resolution = resolve_hint(_hint(), [undated])
    assert resolution.outcome is DiscoveryOutcome.NO_MATCH
    assert resolution.document is None


def test_a_publication_outside_the_tolerance_does_not_match():
    late = _document(
        "stuff",
        "https://www.stuff.co.nz/politics/361019424/leadership-vote",
        published_at=_PUBLISHED + PUBLICATION_TOLERANCE + timedelta(minutes=1))
    assert resolve_hint(_hint(), [late]).outcome is DiscoveryOutcome.NO_MATCH


def test_a_publication_inside_the_tolerance_matches():
    lagging = _document(
        "stuff",
        "https://www.stuff.co.nz/politics/361019424/leadership-vote",
        published_at=_PUBLISHED - timedelta(hours=20))
    assert resolve_hint(_hint(), [lagging]).outcome is DiscoveryOutcome.RESOLVED


def test_a_different_headline_does_not_match():
    other = _document(
        "stuff",
        "https://www.stuff.co.nz/politics/361019999/other-story",
        title="Budget reaction from the regions")
    assert resolve_hint(_hint(), [other]).outcome is DiscoveryOutcome.NO_MATCH


def test_two_equally_good_matches_are_ambiguous_rather_than_a_coin_toss():
    duplicate = _document("stuff", "https://www.stuff.co.nz/politics/361019424/leadership-vote-2")
    resolution = resolve_hint(_hint(), [_STUFF_DOCUMENT, duplicate])
    assert resolution.outcome is DiscoveryOutcome.AMBIGUOUS_MATCH
    assert resolution.document is None


def test_a_match_on_another_host_is_refused():
    # What a redirect off the publisher's own domain looks like by the time it reaches here.
    off_host = CivicDocument(
        source_key="stuff",
        external_key="x",
        canonical_url="https://stuff.co.nz.attacker.test/politics/leadership-vote",
        title=_HEADLINE,
        tier=CivicEvidenceTier.REPUTABLE_MEDIA,
        publisher="Stuff",
        first_seen_at=_SEEN,
        published_at=_PUBLISHED)
    resolution = resolve_hint(_hint(), [off_host])
    assert resolution.outcome is DiscoveryOutcome.OFF_ALLOWLIST_URL
    assert resolution.document is None


def test_a_document_from_a_different_publisher_never_satisfies_a_hint():
    odt = _document("odt", "https://www.odt.co.nz/news/politics/leadership-vote-held")
    assert resolve_hint(_hint(), [odt]).outcome is DiscoveryOutcome.NO_MATCH


def test_same_owner_republication_does_not_resolve_another_mastheads_hint():
    post = _document("the-post", "https://www.thepost.co.nz/politics/leadership-vote")
    assert resolve_hint(_hint(), [post]).outcome is DiscoveryOutcome.NO_MATCH


@pytest.mark.parametrize(
    "title",
    [
        f"{_HEADLINE} - Stuff",
        f"{_HEADLINE} | Stuff",
        f"{_HEADLINE} \u2013 Stuff",
        _HEADLINE,
    ],
)
def test_publisher_suffixes_are_stripped_before_matching(title):
    assert resolve_hint(_hint(title=title), [_STUFF_DOCUMENT]).outcome is (
        DiscoveryOutcome.RESOLVED)


def test_normalization_ignores_case_punctuation_and_accents():
    assert normalize_headline("Te Pāti Māori: Kāore!") == normalize_headline(
        "te pati maori  kaore")


def test_normalization_does_not_collapse_different_headlines():
    assert normalize_headline("Rates rise capped") != normalize_headline("Rates rise scrapped")


def test_no_two_catalogued_sources_answer_to_the_same_publisher_label():
    seen: dict[str, str] = {}
    for entry in CIVIC_CATALOG:
        for label in {normalize_headline(name) for name in entry.publisher_labels}:
            assert seen.get(label, entry.source_key) == entry.source_key, (
                f"{entry.source_key} and {seen[label]} both answer to {label!r}")
            seen[label] = entry.source_key


def test_an_ambiguous_publisher_label_resolves_to_nobody():
    twins = PartyMediaCatalog(
        (MEDIA_CATALOG.get("stuff"), MEDIA_CATALOG.get("odt")), name="twins")
    assert find_publisher("Stuff", twins).source_key == "stuff"
    assert find_publisher("News", twins) is None


def test_publisher_lookup_uses_only_catalogued_labels():
    assert find_publisher("Otago Daily Times").source_key == "odt"
    assert find_publisher("ODT").source_key == "odt"
    assert find_publisher("odt").source_key == "odt"
    assert find_publisher("The Daily Otago") is None
    assert find_publisher("") is None


def test_the_merged_catalog_covers_parties_and_media_without_host_collisions():
    assert len(CIVIC_CATALOG) == 37
    assert CIVIC_CATALOG.for_url("https://www.greens.org.nz/x").source_key == "green"
    assert CIVIC_CATALOG.for_url("https://www.stuff.co.nz/x").source_key == "stuff"
    assert CIVIC_CATALOG.for_url("https://unknown.test/x") is None


def test_a_batch_keeps_hint_order_and_reports_each_outcome():
    hints = [_hint(), _hint(publisher_name="NZ Herald"), _hint(publisher_name="Some Blog")]
    outcomes = [resolution.outcome for _, resolution in resolve_hints(hints, [_STUFF_DOCUMENT])]
    assert outcomes == [
        DiscoveryOutcome.RESOLVED,
        DiscoveryOutcome.RESTRICTED_PUBLISHER,
        DiscoveryOutcome.UNKNOWN_PUBLISHER,
    ]


def test_a_hint_needs_a_title():
    with pytest.raises(ValueError, match="needs a title"):
        DiscoveryHint(title="   ", url=_GOOGLE_URL, publisher_name="Stuff")
