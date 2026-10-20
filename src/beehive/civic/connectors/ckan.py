"""data.govt.nz's CKAN API.

CKAN is the one source here that returns a documented, stable envelope
(`{"success": true, "result": {"results": [...]}}`), so this connector is mostly about refusing
the shapes CKAN also returns when something is wrong:

* `success: false` is an application-level error carried inside an HTTP 200. Treating it as an
  empty result set is exactly the "silent success" failure `CivicSourceHealth` exists to stop, so
  it is a hard source error.
* A dataset's identity is its CKAN `id` (a UUID), never its `name` (the slug), because a slug is
  editable and would make a rename look like a new dataset.
* `metadata_created` and `metadata_modified` map cleanly onto published/updated, which makes this
  the source where the three-timestamp split is most obviously right: a dataset published in 2019
  and revised last night is not news, and only the pair of timestamps says so.

Paging uses CKAN's `start`/`rows`, bounded by `max_pages` and `max_items` like Parliament's.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from beehive.civic.documents import (CivicDocument, CivicFetchResult, CivicSourceError,
                                      MAX_TITLE_CHARS, bounded_text, build_result, canonical_url,
                                      stable_external_key)
from beehive.civic.fetching import (CivicEndpoint, CivicTransport, DEFAULT_MAX_ITEMS,
                                     DEFAULT_MAX_PAGES, parse_iso8601, parse_json,
                                     require_mapping, require_sequence)
from beehive.domain.civic import CivicEvidenceTier


@dataclass(frozen=True)
class CkanSourceDefinition:
    source_key: str
    endpoint: CivicEndpoint
    publisher: str
    tier: CivicEvidenceTier
    # The dataset landing page prefix; CKAN search results carry a slug, not an absolute URL.
    dataset_url_base: str = ""
    # Fixed, application-controlled search parameters. Never Owner- or model-supplied.
    base_query: dict[str, str] = field(default_factory=lambda: {"q": "", "sort": "metadata_modified desc"})
    page_size: int = 50
    max_pages: int = DEFAULT_MAX_PAGES
    max_items: int = DEFAULT_MAX_ITEMS
    crawl_delay_seconds: float = 1.0


def _dataset_document(
    record: Any, definition: CkanSourceDefinition, first_seen_at: datetime,
) -> CivicDocument:
    dataset = require_mapping(record, source_key=definition.source_key, field_name="dataset")
    identity = stable_external_key(
        dataset.get("id"), source_key=definition.source_key, field_name="dataset id")
    title = dataset.get("title") or dataset.get("name")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"dataset {identity} has no title")
    slug = dataset.get("name")
    if isinstance(slug, str) and slug.strip() and definition.dataset_url_base:
        target = canonical_url(f"{definition.dataset_url_base.rstrip('/')}/{slug.strip()}")
    elif isinstance(dataset.get("url"), str) and dataset["url"].strip():
        target = canonical_url(dataset["url"], base=definition.endpoint.url)
    else:
        raise ValueError(f"dataset {identity} has no resolvable landing page")
    organization = dataset.get("organization")
    publisher = definition.publisher
    if isinstance(organization, dict):
        org_title = organization.get("title")
        if isinstance(org_title, str) and org_title.strip():
            # The publishing agency is a useful display fact. It is NOT allowed to influence the
            # tier -- that stays whatever the catalog assigned for data.govt.nz itself.
            publisher = f"{definition.publisher} \u2014 {bounded_text(org_title, limit=120)}"
    return CivicDocument(
        source_key=definition.source_key,
        external_key=identity,
        canonical_url=target,
        title=bounded_text(title, limit=MAX_TITLE_CHARS),
        tier=definition.tier,
        publisher=publisher,
        first_seen_at=first_seen_at,
        published_at=parse_iso8601(dataset.get("metadata_created")),
        updated_at=parse_iso8601(dataset.get("metadata_modified")),
        excerpt=bounded_text(dataset.get("notes")),
        raw_metadata={"ckan_name": slug if isinstance(slug, str) else None})


class CkanConnector:
    """Pages one fixed CKAN `package_search` endpoint."""

    def __init__(self, definition: CkanSourceDefinition, transport: CivicTransport | None = None):
        self._definition = definition
        self._transport = transport or CivicTransport()

    @property
    def source_key(self) -> str:
        return self._definition.source_key

    @property
    def definition(self) -> CkanSourceDefinition:
        return self._definition

    def validate_config(self, config: dict) -> None:
        if config:
            raise ValueError(f"{self._definition.source_key} config must be empty")

    def _endpoint_for(self, start: int) -> CivicEndpoint:
        query = dict(self._definition.base_query)
        query["rows"] = str(self._definition.page_size)
        query["start"] = str(start)
        separator = "&" if "?" in self._definition.endpoint.url else "?"
        return self._definition.endpoint.at(
            f"{self._definition.endpoint.url}{separator}{urlencode(query)}")

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        self.validate_config(config or {})
        definition = self._definition
        documents: list[CivicDocument] = []
        degraded: list[str] = []
        pages_fetched = 0
        reported_total: int | None = None

        for page in range(definition.max_pages):
            endpoint = self._endpoint_for(page * definition.page_size)
            response = self._transport.request(
                endpoint,
                source_key=definition.source_key,
                crawl_delay_seconds=definition.crawl_delay_seconds)
            payload = parse_json(
                response.body, source_key=definition.source_key, url=endpoint.url)
            pages_fetched += 1
            envelope = require_mapping(
                payload, source_key=definition.source_key, field_name="CKAN response")
            if envelope.get("success") is not True:
                raise CivicSourceError(
                    definition.source_key,
                    f"{endpoint.url}: CKAN reported success=false inside an HTTP 200 response")
            result = require_mapping(
                envelope.get("result"), source_key=definition.source_key, field_name="result")
            records = require_sequence(
                result.get("results"), source_key=definition.source_key, field_name="results")
            count = result.get("count")
            if isinstance(count, int):
                reported_total = count
            for record in records:
                if len(documents) >= definition.max_items:
                    break
                try:
                    documents.append(_dataset_document(record, definition, now))
                except ValueError as exc:
                    degraded.append(f"page {page}: {exc}")
            if len(records) < definition.page_size or len(documents) >= definition.max_items:
                break
        else:
            if reported_total is not None and reported_total > len(documents):
                degraded.append(
                    f"stopped at the {definition.max_pages}-page cap with {len(documents)} of "
                    f"{reported_total} datasets collected")

        if reported_total is not None and len(documents) >= definition.max_items \
                and reported_total > len(documents):
            degraded.append(
                f"stopped at the {definition.max_items}-item cap with {len(documents)} of "
                f"{reported_total} datasets collected")

        return build_result(
            definition.source_key, documents, now, degraded_reasons=degraded,
            pages_fetched=pages_fetched)


__all__ = ["CkanConnector", "CkanSourceDefinition"]
