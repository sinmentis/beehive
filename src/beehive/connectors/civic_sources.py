"""One enumeration of every Civic source PartyMap reads.

`beehive.civic.catalog` declares the official half and `civic_party`/`civic_media` declare the
party and media halves. They are separate modules because they are maintained by different
concerns -- an endpoint moving at Parliament has nothing to do with a masthead's licence -- but
they are not separate *models*: every entry is a `CivicSourceEntry` whose connector returns a
`CivicFetchResult` of `CivicDocument`s.

This module is where an orchestrator gets the whole list without knowing that. It also enforces
the one invariant that only becomes checkable once both halves exist: source keys are unique
across the entire catalog, so `CivicDocument.dedupe_key` (`<source_key>:<external_key>`) cannot
collide between an official source and a party one.
"""
from __future__ import annotations

from beehive.civic import catalog as official_catalog
from beehive.civic.catalog import CivicSourceEntry
from beehive.connectors.civic_catalog import PartyMediaCatalog, merge_catalogs
from beehive.connectors.civic_media import MEDIA_CATALOG
from beehive.connectors.civic_party import PARTY_CATALOG

PARTY_MEDIA_CATALOG: PartyMediaCatalog = merge_catalogs(
    PARTY_CATALOG, MEDIA_CATALOG, name="party-media")

_OFFICIAL_KEYS = frozenset(official_catalog.source_keys())
_PARTY_MEDIA_KEYS = frozenset(PARTY_MEDIA_CATALOG.keys)
_COLLISIONS = _OFFICIAL_KEYS & _PARTY_MEDIA_KEYS
if _COLLISIONS:
    raise RuntimeError(
        f"civic source keys are claimed by both halves of the catalog: {sorted(_COLLISIONS)}")


def all_sources() -> tuple[CivicSourceEntry, ...]:
    """Every catalogued Civic source, official and party/media, including the ones that cannot
    currently be read. Order is official first, then parties, then media."""
    return (*official_catalog.all_sources(), *PARTY_MEDIA_CATALOG.all_sources())


def enabled_sources(environ: dict[str, str] | None = None) -> tuple[CivicSourceEntry, ...]:
    """Every source that can actually run: the official ones whose optional credential is present,
    plus the party and media ones that are neither blocked nor licence-restricted."""
    return (
        *official_catalog.enabled_sources(environ),
        *PARTY_MEDIA_CATALOG.enabled_sources(),
    )


def get_source(source_key: str) -> CivicSourceEntry:
    try:
        return official_catalog.get_source(source_key)
    except ValueError:
        return PARTY_MEDIA_CATALOG.get(source_key)


def source_keys() -> tuple[str, ...]:
    return tuple(entry.source_key for entry in all_sources())


__all__ = [
    "PARTY_MEDIA_CATALOG",
    "all_sources",
    "enabled_sources",
    "get_source",
    "source_keys",
]
