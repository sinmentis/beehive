"""One catalog: an orchestrator enumerates official, party and media sources through a single
type and a single call, and no source key can collide between the two halves."""
import pytest

from beehive.civic import catalog as official_catalog
from beehive.civic.catalog import CivicSourceEntry
from beehive.connectors.civic_catalog import PartyMediaSourceEntry
from beehive.connectors.civic_media import MEDIA_CATALOG
from beehive.connectors.civic_party import PARTY_CATALOG
from beehive.connectors.civic_sources import (
    PARTY_MEDIA_CATALOG,
    all_sources,
    enabled_sources,
    get_source,
    source_keys,
)


def test_every_source_enumerates_through_one_type():
    entries = all_sources()
    assert entries
    for entry in entries:
        assert isinstance(entry, CivicSourceEntry)
        assert entry.source_key
        assert entry.publisher
        assert entry.tier
        assert callable(entry.build)


def test_the_catalog_is_both_halves_and_nothing_else():
    assert len(all_sources()) == (
        len(official_catalog.all_sources()) + len(PARTY_CATALOG) + len(MEDIA_CATALOG))
    assert len(PARTY_MEDIA_CATALOG) == len(PARTY_CATALOG) + len(MEDIA_CATALOG)


def test_source_keys_are_unique_across_the_whole_catalog():
    """`CivicDocument.dedupe_key` is `<source_key>:<external_key>`, so a key claimed by two
    sources would let two different publications share one Watch-wide identity."""
    keys = source_keys()
    assert len(keys) == len(set(keys))


def test_enabled_sources_exclude_blocked_and_licence_restricted_ones():
    enabled = {entry.source_key for entry in enabled_sources()}
    for blocked in ("nz-loyal", "te-tai-tokerau", "free-palestine", "cannabis"):
        assert blocked not in enabled
    for restricted in ("nz-herald", "newstalk-zb", "rnz"):
        assert restricted not in enabled
    assert {"green", "stuff", "odt"} <= enabled


def test_enabled_sources_still_include_the_official_half():
    enabled = {entry.source_key for entry in enabled_sources()}
    assert {entry.source_key for entry in official_catalog.enabled_sources()} <= enabled


def test_lookup_finds_either_half_and_fails_closed_for_neither():
    assert get_source("nz_electoral_commission").source_key == "nz_electoral_commission"
    assert isinstance(get_source("green"), PartyMediaSourceEntry)
    assert isinstance(get_source("stuff"), PartyMediaSourceEntry)
    with pytest.raises(ValueError, match="unknown civic source"):
        get_source("not-a-source")


def test_only_the_party_media_half_carries_ownership_and_use_policy():
    """The extra facts live on the subclass, so official sources are not forced to invent an
    owner id or a licence policy they do not have."""
    party_media = {entry.source_key for entry in PARTY_MEDIA_CATALOG}
    for entry in all_sources():
        has_policy = isinstance(entry, PartyMediaSourceEntry)
        assert has_policy == (entry.source_key in party_media)


def test_every_enabled_source_builds_a_connector_with_the_same_fetch_signature():
    """Enumeration is only uniform if what it yields can be run uniformly: the orchestrator
    builds each entry and calls `fetch(now, config=...)` without knowing which half or which
    strategy it got."""
    import inspect

    from beehive.civic.fetching import CivicTransport

    transport = CivicTransport(fetch=lambda *a, **k: pytest.fail("no request expected"))
    for entry in enabled_sources():
        connector = entry.build(transport)
        signature = inspect.signature(connector.fetch)
        assert "now" in signature.parameters, entry.source_key
        assert "config" in signature.parameters, entry.source_key
        assert connector.source_key == entry.source_key


def test_every_unavailable_party_media_source_refuses_through_the_same_interface():
    from datetime import datetime, timezone

    from beehive.civic.documents import CivicSourceError

    now = datetime.now(timezone.utc)
    disabled = PARTY_MEDIA_CATALOG.disabled_sources()
    assert len(disabled) == 15
    for entry in disabled:
        connector = entry.build(None)
        with pytest.raises(CivicSourceError):
            connector.fetch(now)
