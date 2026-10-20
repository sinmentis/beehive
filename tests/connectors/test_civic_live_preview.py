"""Opt-in live check that the catalogued party and media endpoints still exist and still parse.

Skipped by default: the rest of this package must never touch the network, and CI must not depend
on twenty third-party sites being up. Run it deliberately, from a machine that is allowed to
identify itself to these publishers, when a fetch starts failing or before adding a source:

    BEEHIVE_CIVIC_LIVE=1 .venv/bin/python -m pytest tests/connectors/test_civic_live_preview.py

Only endpoints already committed to the catalog are requested, one small fetch each, through the
same `fetch_entry` path production uses -- so this exercises the real transport, the real host
allow-list, the real content-type assertions and the real crawl delays rather than a parallel
copy of them. Licence-restricted and unavailable sources are never requested, here or anywhere
else.
"""
import os
from datetime import datetime, timezone

import pytest

from beehive.civic.documents import CivicSourceHealth
from beehive.connectors.civic_media import MEDIA_CATALOG
from beehive.connectors.civic_party import PARTY_CATALOG
from beehive.connectors.civic_strategies import fetch_entry

_LIVE = os.environ.get("BEEHIVE_CIVIC_LIVE") == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE, reason="set BEEHIVE_CIVIC_LIVE=1 to preview the live civic endpoints")


def _check(entry):
    """A live endpoint passes when it parses under its catalogued strategy and everything it
    yielded is on its own host with this catalog's classification.

    An EMPTY result is reported as a skip rather than a failure: a small party's feed can be
    legitimately empty for months, and that is a quiet week, not a broken endpoint. A DEGRADED
    result passes and prints its reasons -- a capped crawl is the expected steady state for the
    large sitemaps."""
    result = fetch_entry(entry, datetime.now(timezone.utc), limit=3)
    assert result.source_key == entry.source_key
    for document in result.documents:
        assert entry.owns_url(document.canonical_url), (
            f"{entry.source_key}: {document.canonical_url} left its allow-list")
        assert document.title
        assert document.tier is entry.tier
        assert document.publisher == entry.publisher
        assert document.first_seen_at.tzinfo is not None
    if result.health is CivicSourceHealth.EMPTY:
        pytest.skip(f"{entry.source_key}: {entry.url} currently publishes no entries")
    assert result.health in (CivicSourceHealth.OK, CivicSourceHealth.DEGRADED)


@pytest.mark.parametrize(
    "entry", PARTY_CATALOG.enabled_sources(), ids=lambda e: e.source_key)
def test_party_endpoint_still_parses(entry):
    _check(entry)


@pytest.mark.parametrize(
    "entry", MEDIA_CATALOG.enabled_sources(), ids=lambda e: e.source_key)
def test_media_endpoint_still_parses(entry):
    _check(entry)


def test_no_unavailable_or_restricted_source_is_ever_previewed():
    for catalog in (PARTY_CATALOG, MEDIA_CATALOG):
        for entry in catalog.disabled_sources():
            assert entry not in catalog.enabled_sources()
            assert entry.url == ""
