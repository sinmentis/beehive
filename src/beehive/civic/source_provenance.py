"""Recognize current official-source date evidence without inventing article extraction."""
from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from beehive.civic.catalog import LEGISLATION_HYDRATION, all_sources
from beehive.civic.connectors.feeds import FeedSourceDefinition
from beehive.civic.connectors.legislation import (
    LEGISLATION_HOSTS,
    LegislationApiDefinition,
    LegislationHydrationDefinition,
    document_xml_url,
)
from beehive.civic.connectors.parliament import HANSARD_DAILY_URL_TEMPLATE, ParliamentApiDefinition
from beehive.civic.documents import CivicDocument, canonical_url
from beehive.civic.fetching import CivicEndpoint
from beehive.civic.source_extraction import (
    PUBLICATION_TIMEZONE,
    PublicationWitness,
    publication_witness,
)
from beehive.domain.civic import CivicEvidenceTier

_DEFINITIONS = {entry.source_key: entry.definition for entry in all_sources()}
_DEFINITIONS[LEGISLATION_HYDRATION.source_key] = LEGISLATION_HYDRATION
_XML_PUBLICATION_SOURCES = frozenset({
    "legislation-xml-attribute-date.gazetted",
    "legislation-xml-element-publishedDate",
})
_XML_EVENT_SOURCES = frozenset({
    "legislation-xml-attribute-date.assent",
    "legislation-xml-attribute-date.gazetted",
    "legislation-xml-attribute-date.signed",
    "legislation-xml-attribute-date.first.valid",
    "legislation-xml-element-assentDate",
    "legislation-xml-element-commencementDate",
    "legislation-xml-element-date",
})


def _witness(metadata: Any, allowed_sources: frozenset[str]) -> PublicationWitness | None:
    if (
        not isinstance(metadata, dict) or not isinstance(metadata.get("source"), str)
        or metadata["source"] not in allowed_sources
    ):
        return None
    source = metadata["source"]
    raw = metadata.get("raw_value")
    if (
        not isinstance(raw, str) or not raw.strip() or len(raw) > 100
        or metadata.get("timezone") != PUBLICATION_TIMEZONE
    ):
        return None
    if source == "rss-pubDate":
        try:
            instant = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if instant is None or instant.tzinfo is None:
            return None
        parsed = PublicationWitness(instant, source, "datetime", raw)
    else:
        parsed = publication_witness(raw, source=source)
    recorded = publication_witness(metadata.get("value"), source=source)
    if (
        parsed is None or recorded is None or recorded.value != parsed.value
        or metadata.get("precision") != parsed.precision
        or metadata.get("calendar_date") != parsed.as_metadata()["calendar_date"]
    ):
        return None
    return parsed


def _feed_identity(document: CivicDocument, definition: FeedSourceDefinition) -> bool:
    metadata = document.raw_metadata
    if definition.is_atom:
        identity = metadata.get("atom_id") or metadata.get("atom_link")
        target = metadata.get("atom_link") or metadata.get("atom_id")
    else:
        guid = metadata.get("feed_guid")
        link = metadata.get("feed_link")
        identity = guid if definition.guid_is_identity and guid else link
        target = link or guid
    if not isinstance(identity, str) or identity.strip() != document.external_key:
        return False
    if not isinstance(target, str):
        return False
    try:
        return canonical_url(target, base=definition.endpoint.url) == document.canonical_url
    except ValueError:
        return False


def has_current_official_capture(document: CivicDocument) -> bool:
    """Use catalog-owned source roles, original field values and explicit date precision.

    An official event can remain usable with publication unknown. Candidate-context event dates
    and generic connector-published fallbacks are not source evidence.
    """
    definition = _DEFINITIONS.get(document.source_key)
    if definition is None or document.tier is not CivicEvidenceTier.OFFICIAL:
        return False
    metadata = document.raw_metadata
    event_sources: frozenset[str] = frozenset()
    if isinstance(definition, FeedSourceDefinition):
        if not _feed_identity(document, definition):
            return False
        publication_sources = frozenset({
            "atom-published" if definition.is_atom else "rss-pubDate",
        })
        hosts = definition.endpoint.allowed_hosts
    elif isinstance(definition, ParliamentApiDefinition):
        if metadata.get("api") != document.source_key:
            return False
        publication_sources = frozenset({"parliament-publicationDate"})
        event_sources = frozenset(
            f"parliament-{key}" for key in definition.event_keys
            if key in {"sittingDate", "introductionDate", "lastStageDate"}
        )
        if definition.url_token_strategy == "hansard_daily_date":
            event_sources |= {"parliament-daily-sitting-date"}
        hosts = definition.endpoint.allowed_hosts
    elif isinstance(definition, LegislationApiDefinition):
        if metadata.get("legislation_source") != "works_api":
            return False
        publication_sources = frozenset({"legislation-works-publishedDate"})
        event_sources = frozenset({"legislation-works-assentDate"})
        hosts = LEGISLATION_HOSTS
    elif isinstance(definition, LegislationHydrationDefinition):
        try:
            expected_xml = document_xml_url(document.canonical_url)
        except ValueError:
            return False
        if (
            expected_xml is None or metadata.get("legislation_xml_url") != expected_xml
            or metadata.get("sitemap_external_key") != document.canonical_url
        ):
            return False
        publication_sources = _XML_PUBLICATION_SOURCES
        event_sources = _XML_EVENT_SOURCES
        hosts = LEGISLATION_HOSTS
    else:
        return False
    try:
        parts = urlsplit(document.canonical_url)
        if parts.username or parts.password or parts.port not in (None, 443):
            return False
        CivicEndpoint(url=document.canonical_url, allowed_hosts=hosts)
    except ValueError:
        return False
    publication_metadata = metadata.get("publication")
    publication = _witness(publication_metadata, publication_sources)
    if publication_metadata is not None and publication is None:
        return False
    if document.published_at is not None:
        if publication is None or publication.value != document.published_at:
            return False
    elif publication is not None:
        return False
    event_metadata = metadata.get("event_date_provenance")
    event = _witness(event_metadata, event_sources)
    if event_metadata is not None and event is None:
        return False
    if event is not None:
        day = event.as_metadata()["calendar_date"]
        if metadata.get("event_date") != day:
            return False
        if metadata.get("sitting_date") is not None and metadata["sitting_date"] != day:
            return False
        if (
            isinstance(definition, ParliamentApiDefinition)
            and definition.url_token_strategy == "hansard_daily_date"
            and document.canonical_url != canonical_url(HANSARD_DAILY_URL_TEMPLATE.format(token=day))
        ):
            return False
    elif metadata.get("event_date") is not None or metadata.get("sitting_date") is not None:
        return False
    return publication is not None or event is not None
