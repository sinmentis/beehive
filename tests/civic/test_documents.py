# tests/civic/test_documents.py
"""The normalized Civic document shape: identity, canonical URLs, the three-timestamp split, and
the health vocabulary that keeps "quiet day" distinguishable from "broken source"."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.documents import (CivicDocument, CivicFetchResult, CivicSourceHealth,
                                      MAX_EXCERPT_CHARS, bounded_text, build_result,
                                      canonical_url, stable_external_key)
from beehive.domain.civic import CivicEvidenceTier

T0 = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _document(**overrides) -> CivicDocument:
    kwargs = dict(
        source_key="nz_beehive_releases", external_key="https://www.beehive.govt.nz/node/1",
        canonical_url="https://www.beehive.govt.nz/release/x", title="Release",
        tier=CivicEvidenceTier.OFFICIAL, publisher="NZ Government", first_seen_at=T0)
    kwargs.update(overrides)
    return CivicDocument(**kwargs)


# ============================================================================
# Required fields and timezone discipline
# ============================================================================


@pytest.mark.parametrize(
    "field", ["source_key", "external_key", "canonical_url", "title", "publisher"])
def test_required_fields_must_not_be_blank(field):
    with pytest.raises(ValueError):
        _document(**{field: "   "})


@pytest.mark.parametrize("field", ["first_seen_at", "published_at", "updated_at"])
def test_every_timestamp_must_be_timezone_aware(field):
    with pytest.raises(ValueError, match="timezone-aware"):
        _document(**{field: datetime(2026, 8, 12)})


# ============================================================================
# Identity
# ============================================================================


def test_dedupe_key_is_namespaced_by_source():
    """Two sources using the same upstream id must never collide into one Civic Candidate."""
    first = _document(source_key="a", external_key="shared")
    second = _document(source_key="b", external_key="shared")
    assert first.dedupe_key == "a:shared"
    assert first.dedupe_key != second.dedupe_key


def test_identity_and_canonical_url_may_legitimately_differ():
    """Beehive's guid is a Drupal node URL; the canonical link is the release path."""
    document = _document(
        external_key="https://www.beehive.govt.nz/node/153241",
        canonical_url="https://www.beehive.govt.nz/release/transport-package")
    assert document.external_key != document.canonical_url


def test_stable_external_key_accepts_the_forms_these_apis_return():
    assert stable_external_key("  abc  ", source_key="s") == "abc"
    assert stable_external_key(4211, source_key="s") == "4211"


@pytest.mark.parametrize("value", [None, "", "   ", True, 3.5, {"id": 1}])
def test_stable_external_key_refuses_anything_it_cannot_trust(value):
    """A missing id silently replaced by a content hash would make every edit look brand new."""
    with pytest.raises(ValueError):
        stable_external_key(value, source_key="s")


# ============================================================================
# Canonical URLs
# ============================================================================


def test_canonical_url_lowercases_scheme_and_host_but_preserves_the_path():
    assert canonical_url("HTTPS://WWW.Beehive.GOVT.NZ/Release/Item") == (
        "https://www.beehive.govt.nz/Release/Item")


def test_canonical_url_strips_tracking_parameters_and_fragments():
    assert canonical_url(
        "https://elections.nz/news/x?utm_source=newsletter&utm_medium=email&id=7#top"
    ) == "https://elections.nz/news/x?id=7"


def test_canonical_url_keeps_meaningful_query_parameters():
    assert canonical_url("https://x.govt.nz/a?page=2") == "https://x.govt.nz/a?page=2"


def test_the_same_document_reached_two_ways_collapses_to_one_url():
    from_feed = canonical_url("https://elections.nz/news/x")
    from_newsletter = canonical_url("https://elections.nz/news/x?utm_campaign=aug&fbclid=abc")
    assert from_feed == from_newsletter


def test_canonical_url_resolves_a_relative_link_against_its_source():
    assert canonical_url(
        "/en/pb/order-paper/2026", base="https://www.parliament.nz/feed"
    ) == "https://www.parliament.nz/en/pb/order-paper/2026"


def test_canonical_url_drops_a_default_port():
    assert canonical_url("https://x.govt.nz:443/a") == "https://x.govt.nz/a"


def test_a_same_host_http_link_is_upgraded_to_the_source_own_https_scheme():
    """NZ First's feed links its own releases over http while the feed itself is fetched over
    https from the identical host. The catalog commits that host is served over https, so this
    is one stale link to the same document, not a second resource -- and publication v1 refuses
    a non-https citation, so preserving the scheme silently loses a real record."""
    assert canonical_url(
        "http://www.nzfirst.nz/winston_peters_seniors_sole_defender",
        base="https://www.nzfirst.nz/news.rss",
    ) == "https://www.nzfirst.nz/winston_peters_seniors_sole_defender"


def test_an_off_host_http_link_keeps_its_scheme_rather_than_being_guessed_secure():
    """Only the source's own host is known to serve https. Upgrading a third party's link would
    assert something this connector never verified."""
    assert canonical_url(
        "http://other.example.nz/story",
        base="https://www.nzfirst.nz/news.rss",
    ) == "http://other.example.nz/story"


def test_an_http_source_never_upgrades_its_own_links():
    assert canonical_url(
        "http://x.govt.nz/a", base="http://x.govt.nz/feed",
    ) == "http://x.govt.nz/a"


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "file:///etc/passwd", "ftp://x.govt.nz/a", "not a url", "",
     "https://x.govt.nz:8080/a"])
def test_canonical_url_refuses_anything_that_is_not_a_plain_http_url(url):
    with pytest.raises(ValueError):
        canonical_url(url)


# ============================================================================
# Three timestamps
# ============================================================================


def test_effective_at_prefers_published_then_updated_then_first_seen():
    published = T0 - timedelta(days=3)
    updated = T0 - timedelta(days=1)
    assert _document(published_at=published, updated_at=updated).effective_at == published
    assert _document(updated_at=updated).effective_at == updated
    assert _document().effective_at == T0


def test_an_edit_is_distinguishable_from_a_new_publication():
    """The Electoral Commission feed republishes updated pages, so this distinction is the only
    thing that stops an edit being treated as a fresh story."""
    published = T0 - timedelta(days=5)
    edit = _document(published_at=published, updated_at=T0, first_seen_at=T0)
    assert edit.published_at < edit.updated_at
    assert edit.effective_at == published


# ============================================================================
# Bounded text
# ============================================================================


def test_bounded_text_collapses_whitespace_and_caps_length():
    assert bounded_text("  a\n\n  b\t c ") == "a b c"
    assert len(bounded_text("x" * (MAX_EXCERPT_CHARS + 500))) == MAX_EXCERPT_CHARS


def test_bounded_text_treats_a_missing_optional_field_as_empty():
    assert bounded_text(None) == ""
    assert bounded_text({"nested": "object"}) == ""


# ============================================================================
# Health
# ============================================================================


def test_documents_produce_an_ok_result():
    result = build_result("s", [_document()], T0)
    assert result.health is CivicSourceHealth.OK
    assert result.is_complete


def test_no_documents_is_empty_never_ok():
    """Reporting success for nothing is exactly what health exists to prevent."""
    result = build_result("s", [], T0)
    assert result.health is CivicSourceHealth.EMPTY
    assert result.documents == ()
    assert result.is_complete


def test_any_shortfall_escalates_to_degraded_and_keeps_what_was_collected():
    result = build_result("s", [_document()], T0, degraded_reasons=["child sitemap 503"])
    assert result.health is CivicSourceHealth.DEGRADED
    assert result.documents
    assert not result.is_complete
    assert result.degraded_reasons == ("child sitemap 503",)


def test_a_degraded_result_with_no_documents_is_still_degraded_not_empty():
    result = build_result("s", [], T0, degraded_reasons=["everything failed"])
    assert result.health is CivicSourceHealth.DEGRADED
    assert not result.is_complete


def test_a_degraded_result_must_say_what_was_degraded():
    with pytest.raises(ValueError, match="say what was degraded"):
        CivicFetchResult("s", (), CivicSourceHealth.DEGRADED, T0)


def test_only_a_degraded_result_may_carry_reasons():
    with pytest.raises(ValueError, match="only a degraded"):
        CivicFetchResult("s", (), CivicSourceHealth.EMPTY, T0, degraded_reasons=("x",))


def test_an_empty_result_cannot_carry_documents():
    with pytest.raises(ValueError, match="cannot carry documents"):
        CivicFetchResult("s", (_document(),), CivicSourceHealth.EMPTY, T0)


def test_an_ok_result_cannot_be_empty():
    with pytest.raises(ValueError, match="is EMPTY, not OK"):
        CivicFetchResult("s", (), CivicSourceHealth.OK, T0)


def test_a_tombstone_is_never_reported_as_empty():
    """Constructed directly because a tombstone is raised, not built -- the point is that the two
    health values are distinct and a caller can branch on them."""
    tombstoned = CivicFetchResult("s", (), CivicSourceHealth.TOMBSTONE, T0)
    assert tombstoned.health is not CivicSourceHealth.EMPTY
    assert not tombstoned.is_complete
