"""Parliament's JSON search APIs: Bills, Select Committees and Hansard.

All three are POST search endpoints, but their request and response contracts are genuinely
different and were captured from the live sites rather than guessed:

* **Bills** (`https://bills.parliament.nz/api/data/search`) takes the site's own full filter
  object -- every nullable filter, every empty list, and the `column`/`direction` sort keys -- and
  answers `{"results": [...], "totalResults": N}`. The filters are not decoration: a partial body
  silently returns a differently-ordered or differently-scoped page rather than an error, so
  `BILLS_SEARCH_FILTERS` reproduces the captured body field for field.
* **Hansard** (`https://hansard.parliament.nz/api/data/search`) is an OData-shaped listing:
  `searchTab`/`types`/`orderByFields` in, `{"value": [...], "@odata.count": N}` out.
* **Select Committees** (`https://selectcommittees.parliament.nz/api/data/search`) follows the
  Bills shape.

Three rules this module exists to enforce:

* **Detail routes come from publisher links, never from HTTP status.** All three of these sites
  are client-rendered (Bills and Select Committees are Blazor; Hansard is a SPA), and every path
  under them returns the same shell with HTTP 200 -- including a well-formed but nonexistent
  UUID. Status codes therefore cannot tell a real route from a fabricated one, and an earlier
  version of this module used exactly that non-evidence to settle on `/v/0/{id}`. The routes
  below are the ones the publishers' own links use:
      Bills                https://bills.parliament.nz/v/6/{id}
      Select Committees    https://selectcommittees.parliament.nz/v/{view}/{id}, where `view` is
                           a per-DOCUMENT-TYPE code taken from the publisher's own listing links
                           (see `SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE`), except Bills, which the
                           listing links off-site to bills.parliament.nz/v/Bill/{id}
      Hansard (daily)      https://hansard.parliament.nz/hansard-transcript/{YYYY-MM-DD}
  Where a record type has no publisher-verified route, this module emits no URL and degrades the
  result rather than inventing one -- see `_hansard_daily_token`, which refuses anything that is
  not a Daily document.
* **A derived URL may only land on a host the definition already declared.** `document_url_hosts`
  is the allow-list for URLs this module *emits*, which is a separate question from the endpoint
  allow-list that governs what it *fetches*. It exists because one of these routes deliberately
  leaves its own site, and an off-site route must be an explicit, reviewable declaration rather
  than whatever a template or a response field happens to contain.
* **An empty or self-contradictory envelope is not a quiet week.** Parliament always has bills
  and always has recent sittings. `expects_results` marks the sources where zero results means
  "the request shape drifted or the API changed", so the fetch is DEGRADED rather than a healthy
  EMPTY. A negative or non-integer total, or a total that contradicts the page actually returned,
  is a hard source error.
* **Paging is bounded and honest.** Hitting the page or item cap while the API reports more
  degrades the result with an explicit reason instead of returning a short list, for the same
  reason `beehive.connectors.base.TruncatedSnapshotError` exists.

`fetch_daily_detail` is a deliberately separate, opt-in call. Hansard's
`/api/data/daily/{YYYY-MM-DD}` returns a whole sitting day's transcript metadata and is large;
ingesting it on every listing fetch would cost hundreds of KB per run for detail the listing
already summarises. Nothing calls it by default, and it never ingests HTML transcript bodies.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from beehive.civic.documents import (CivicDocument, CivicFetchResult, CivicSourceError,
                                      MAX_TITLE_CHARS, bounded_text, build_result, canonical_url,
                                      stable_external_key)
from beehive.civic.fetching import (CivicEndpoint, CivicTransport, DEFAULT_MAX_ITEMS,
                                     DEFAULT_MAX_PAGES, parse_iso8601, parse_json,
                                     require_mapping, require_sequence)
from beehive.civic.source_extraction import PublicationWitness, publication_witness
from beehive.domain.civic import CivicEvidenceTier

# The exact filter object bills.parliament.nz posts, reproduced field for field including the
# nulls and the empty lists. `documentPreset: 1` with `billTab: "Current"` is the site's default
# view, `includeBillStages: true` is what makes `lastStageDate` present in the results, and
# `column: 17` / `direction: 1` is its "most recent stage first" ordering.
BILLS_SEARCH_FILTERS: dict[str, Any] = {
    "id": None,
    "documentPreset": 1,
    "status": [],
    "documentTypes": [],
    "documentSubtypes": [],
    "beforeCommittee": None,
    "billStages": [],
    "billTab": "Current",
    "billId": None,
    "includeBillStages": True,
    "parliaments": [],
    "members": [],
    "parties": [],
    "portfolios": [],
    "subjects": [],
    "searchText": None,
    "dateFrom": None,
    "dateTo": None,
    "column": 17,
    "direction": 1,
}

# The Select Committees API takes the same body SHAPE as Bills but not the same values: it has
# far fewer sortable columns, and `column: 17` (Bills' "last stage date") answers HTTP 500 rather
# than an error the client can read. The bill-only keys are dropped because they describe nothing
# on this endpoint, and `column: 1` was verified against the live API.
SELECT_COMMITTEES_SEARCH_FILTERS: dict[str, Any] = {
    key: value for key, value in BILLS_SEARCH_FILTERS.items()
    if key not in ("billTab", "billId", "billStages", "includeBillStages", "column")
}
SELECT_COMMITTEES_SEARCH_FILTERS["column"] = 1


# Every `documentType` the Select Committees site currently publishes, mapped to the `/v/{n}/`
# view code the PUBLISHER'S OWN LINKS use. The full type list is the site's own document-type
# filter, so a type missing from this table is a genuinely new type rather than one overlooked.
#
# The site's numbering is not a site-wide constant and is not ordered: it is a per-type view
# code, which is why an earlier version of this module -- having found `/v/0` and `/v/4` and
# then searched only 0..8 -- concluded the rest of the corpus was unroutable and dropped two
# thirds of every fetch. Each entry below was read off the publisher's `business-before-
# committees` listing (the `a.table-cell-title` link of each row, paired with the same record's
# `documentType` from the listing's own `/api/data/search` response) and/or confirmed by
# rendering the route in headless chromium and matching the `<h1>`/`<title>` to the record's
# exact title. Rendering is the only usable check here: this is a Blazor app that answers HTTP
# 200 for every path, and a wrong view code renders its "Page not found" shell rather than
# failing. Each mapping below was confirmed against two or three independent records of its own
# type; a well-formed but nonexistent UUID renders that not-found shell at every code, which is
# the negative control the earlier `/v/0`-for-everything guess never had.
SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE: dict[str, int] = {
    "Briefing": 0,
    "Financial Scrutiny": 1,
    "Inquiry": 2,
    "Officers Of Parliament": 3,
    "Petition": 4,
    "Regulation Review": 5,
    "Civil Defence": 7,
    "Reports Of Offices Of Parliament": 9,
    "Reserve Bank Of New Zealand Report": 10,
    "Whole Of Government Direction": 12,
    "Privilege": 13,
    "Special Report": 14,
    "International Treaty Examination": 15,
    "Considerations Of Standing Orders And Procedures": 16,
    "Long-Term Insights Briefing": 17,
    "Bill Of Rights Report": 21,
    "Other Matters": 23,
}

# `Bill` is the one type whose listing row does NOT link within selectcommittees.parliament.nz:
# the publisher's own title link goes to the bill's page on the Bills site, keyed on the very
# same UUID the Select Committees API returns. That is the reader's real destination -- the
# bill's full legislative history -- so it is honoured rather than replaced with the
# `/v/19/{id}` route the same page uses only in its narrow-viewport list.
SELECT_COMMITTEE_EXTERNAL_URL_BY_TYPE: dict[str, str] = {
    "Bill": "https://bills.parliament.nz/v/Bill/{id}",
}


# Hansard's transcript route for one sitting day, as the publisher links it. Deliberately NOT
# `/hansard-debates/rhr/...` (that path 404s on www.parliament.nz) and never `/<uuid>`.
HANSARD_DAILY_URL_TEMPLATE = "https://hansard.parliament.nz/hansard-transcript/{token}"

# Hansard's listing request. `searchTab: 0` is the Debates tab and `types: ["Daily"]` restricts
# to whole-day documents, which are the unit that has a stable public URL.
HANSARD_SEARCH_FILTERS: dict[str, Any] = {
    "searchTab": 0,
    "types": ["Daily"],
    "orderByFields": ["SittingDate"],
    "direction": 1,
    "searchText": None,
    "parliaments": [],
    "members": [],
    "parties": [],
    "dateFrom": None,
    "dateTo": None,
}


@dataclass(frozen=True)
class ParliamentApiDefinition:
    """One fixed Parliament search API. Every field that differs between the three APIs is here,
    so the reader below has no per-source branching."""

    source_key: str
    endpoint: CivicEndpoint
    publisher: str
    tier: CivicEvidenceTier
    # Where the hits live. Bills/Committees use `results`; Hansard's OData listing uses `value`.
    result_keys: tuple[str, ...] = ("results", "value", "items", "data", "records")
    # Where the total lives. Hansard reports `@odata.count`.
    total_keys: tuple[str, ...] = ("totalResults", "@odata.count", "total", "totalCount", "count")
    id_keys: tuple[str, ...] = ("id", "uuid", "documentId", "orderPaperId")
    title_keys: tuple[str, ...] = ("title", "name", "subject", "heading")
    published_keys: tuple[str, ...] = ("publicationDate",)
    event_keys: tuple[str, ...] = ()
    updated_keys: tuple[str, ...] = ("lastModified", "lastStageDate", "modifiedDate")
    url_keys: tuple[str, ...] = ("url", "link", "documentUrl", "webUrl")
    excerpt_keys: tuple[str, ...] = ("summary", "description", "snippet", "abstract")
    # Fixed, application-supplied search body. Never built from stored config or a model.
    base_payload: dict[str, Any] = field(default_factory=dict)
    page_key: str = "page"
    page_size_key: str = "pageSize"
    page_size: int = 50
    first_page: int = 1
    max_pages: int = DEFAULT_MAX_PAGES
    max_items: int = DEFAULT_MAX_ITEMS
    crawl_delay_seconds: float = 1.0
    # Used ONLY when `allow_id_url_fallback` is True. Bills really do serve /v/<uuid>.
    url_base: str = ""
    allow_id_url_fallback: bool = True
    # Select Committees' `/v/{n}/{id}` view index is a DOCUMENT-TYPE code, not a site-wide
    # constant: a Briefing renders at /v/0/, a Petition at /v/4/, a Financial Scrutiny at /v/1/.
    # Only types verified against the publisher are listed; a record whose type is absent gets no
    # URL and is reported, per the rule that an unverified route is never fabricated.
    url_view_index_by_type: dict[str, int] = field(default_factory=dict)
    # Types whose publisher link leaves this site entirely, as an absolute `{id}` template. Kept
    # separate from the view-index table because an off-site destination is a different claim,
    # and because `document_url_hosts` has to cover it explicitly.
    url_external_by_type: dict[str, str] = field(default_factory=dict)
    url_type_keys: tuple[str, ...] = ("documentType",)
    # The allow-list for URLs this connector EMITS, which is not the same question as the
    # endpoint allow-list governing what it fetches. Empty means "no additional restriction".
    document_url_hosts: frozenset[str] = frozenset()
    # A template filled from one record field, for sources whose real public URLs are derived
    # rather than returned.
    url_template: str = ""
    url_template_keys: tuple[str, ...] = ()
    # An application-defined derivation for sources whose public URL is keyed on something other
    # than a returned string -- see `_URL_TOKEN_STRATEGIES`. The strategy name is a constant in
    # this module, never a value from a response.
    url_token_strategy: str = ""
    # True for sources that always have content, so an empty page is a signal, not a quiet week.
    expects_results: bool = True

    def route_for_type(self, document_type: str) -> str | int | None:
        """The publisher route this definition declares for one `documentType`.

        A `str` is an absolute off-site template, an `int` is a local view code, and None means
        the type has no verified route. Matching folds case and collapses whitespace so a
        publisher re-casing `Long-Term Insights Briefing` cannot silently drop a whole type --
        but it only ever matches a type this module already declared, so nothing new is
        routed by accident."""
        key = _normalized_type(document_type)
        if key is None:
            return None
        external = _normalized_lookup(tuple(self.url_external_by_type.items()))
        if key in external:
            return external[key]
        return _normalized_lookup(tuple(self.url_view_index_by_type.items())).get(key)


class UnroutableDocumentTypeError(CivicSourceError):
    """One record's `documentType` has no publisher-verified detail route.

    Its own class, and carrying the type rather than only a message, so `fetch` can group the
    whole type into a single counted reason instead of appending one indistinguishable line per
    record. That is the difference between a health signal an operator can act on and ~50 KB of
    repeated text on every fetch."""

    def __init__(self, source_key: str, document_type: Any):
        super().__init__(
            source_key,
            f"documentType {document_type!r} has no publisher-verified detail route; omitting "
            "rather than fabricating a URL")
        self.document_type = document_type


def _normalized_type(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split()).casefold()


@lru_cache(maxsize=None)
def _normalized_lookup(entries: tuple[tuple[str, Any], ...]) -> dict[str, Any]:
    """The route table keyed on its normalized type names. Cached on the table itself: it is a
    module constant, so this normalises each name once per process rather than once per record."""
    return {_normalized_type(key): value for key, value in entries
            if _normalized_type(key) is not None}


def _within_document_hosts(url: str, definition: ParliamentApiDefinition) -> str:
    """`url`, or a hard error if it left the hosts this definition declared.

    The check is on the CANONICAL url, so `HTTPS://Bills.Parliament.NZ` and a stray trailing dot
    both normalise before comparison rather than sneaking past a string match."""
    if not definition.document_url_hosts:
        return url
    host = urlsplit(url).hostname or ""
    if any(host == allowed or host.endswith(f".{allowed}")
           for allowed in definition.document_url_hosts):
        return url
    raise CivicSourceError(
        definition.source_key,
        f"derived URL {url} is on host {host!r}, which is not in this source's document-URL "
        f"allow-list {sorted(definition.document_url_hosts)}")


def _first_present(record: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _total_from(envelope: dict, definition: ParliamentApiDefinition) -> int | None:
    """The reported hit count, validated.

    A negative or non-integer total means the envelope is not the contract this connector was
    written against. That is a hard error rather than a shrug, because every completeness
    decision below is made against this number."""
    raw = _first_present(envelope, definition.total_keys)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise CivicSourceError(
            definition.source_key,
            f"search response total is {raw!r}, which is not an integer count")
    if raw < 0:
        raise CivicSourceError(
            definition.source_key, f"search response reports a negative total ({raw})")
    return raw


def _hits(payload: Any, definition: ParliamentApiDefinition) -> tuple[list, int | None]:
    envelope = require_mapping(
        payload, source_key=definition.source_key, field_name="search response")
    for key in definition.result_keys:
        if key in envelope:
            hits = require_sequence(
                envelope[key], source_key=definition.source_key, field_name=key)
            return hits, _total_from(envelope, definition)
    raise CivicSourceError(
        definition.source_key,
        f"search response has none of the expected result keys "
        f"{list(definition.result_keys)}; the API contract has changed")


def _hansard_daily_token(hit: dict) -> str | None:
    """`YYYY-MM-DD` for a Hansard Daily document.

    The sitting date is both the API's own addressing key (`/api/data/daily/{YYYY-MM-DD}`) and
    the key in the publisher's transcript route, so the derivation is anchored to returned data
    at both ends. Only `Daily` documents have such a route; anything else returns None and is
    reported, because Hansard's shell answers HTTP 200 for every path and a fabricated URL would
    pass every automated check while resolving to nothing."""
    document_type = hit.get("documentType") or hit.get("DocumentType")
    if isinstance(document_type, str) and document_type.strip().lower() != "daily":
        return None
    sitting = parse_iso8601(hit.get("sittingDate") or hit.get("SittingDate"))
    if sitting is None:
        return None
    return f"{sitting.year:04d}-{sitting.month:02d}-{sitting.day:02d}"


_URL_TOKEN_STRATEGIES = {
    "hansard_daily_date": _hansard_daily_token,
}


def _target_url(hit: dict, identity: str, definition: ParliamentApiDefinition) -> str:
    """The public page for one hit: a URL the API returned, else a derived template, else
    `/{id}` only where that is genuinely a real path."""
    raw_url = _first_present(hit, definition.url_keys)
    if isinstance(raw_url, str) and raw_url.strip():
        return _within_document_hosts(
            canonical_url(raw_url, base=definition.url_base or definition.endpoint.url),
            definition)
    if definition.url_template:
        token = None
        if definition.url_template_keys:
            candidate = _first_present(hit, definition.url_template_keys)
            token = candidate.strip() if isinstance(candidate, str) and candidate.strip() else None
        if token is None and definition.url_token_strategy:
            token = _URL_TOKEN_STRATEGIES[definition.url_token_strategy](hit)
        if token:
            return _within_document_hosts(
                canonical_url(definition.url_template.format(token=token)), definition)
    if definition.url_view_index_by_type or definition.url_external_by_type:
        document_type = _first_present(hit, definition.url_type_keys)
        route = definition.route_for_type(document_type)
        if route is None:
            raise UnroutableDocumentTypeError(definition.source_key, document_type)
        target = route.format(id=identity) if isinstance(route, str) \
            else f"{definition.url_base.rstrip('/')}/{route}/{identity}"
        return _within_document_hosts(canonical_url(target), definition)
    if definition.allow_id_url_fallback and definition.url_base:
        return _within_document_hosts(
            canonical_url(f"{definition.url_base.rstrip('/')}/{identity}"), definition)
    raise CivicSourceError(
        definition.source_key,
        f"hit {identity} has no resolvable public URL, and this source has no safe id-based "
        "fallback (an id URL here is a soft 404)")


def _unroutable_warnings(counts: Counter[str]) -> list[str]:
    """One degraded reason per unrouted `documentType`, not one per dropped record.

    Two properties this has to hold, both of which the per-record form broke:

    * **Bounded.** A page of 500 hits from an unrouted type produced 500 near-identical reasons,
      tens of kilobytes of them, on every single fetch -- carried through the fetch result, the
      health record and anything that logs or stores it. One line per type is O(types), and
      there are eighteen types.
    * **Deterministic.** Sorted by type name, so two fetches of the same corpus produce
      byte-identical reasons no matter what order the API paged them in. A reason list that
      reshuffles itself cannot be diffed between runs, and diffing between runs is the whole
      point of recording it.

    The count is what makes it actionable: it separates "a new type just appeared, two records"
    from "a route silently broke, ninety-six records"."""
    return [
        f"{count} hit{'' if count == 1 else 's'} of documentType {document_type!r} omitted: "
        "no verified view route"
        for document_type, count in sorted(counts.items())
    ]


def _document(
    record: Any, definition: ParliamentApiDefinition, first_seen_at: datetime,
) -> CivicDocument:
    hit = require_mapping(record, source_key=definition.source_key, field_name="search hit")
    identity = stable_external_key(
        _first_present(hit, definition.id_keys), source_key=definition.source_key,
        field_name="hit id")
    title = _first_present(hit, definition.title_keys)
    if not isinstance(title, str) or not title.strip():
        raise CivicSourceError(definition.source_key, f"hit {identity} has no title")
    publication = _date_witness(hit, definition.published_keys)
    event = _date_witness(hit, definition.event_keys)
    return CivicDocument(
        source_key=definition.source_key,
        external_key=identity,
        canonical_url=_target_url(hit, identity, definition),
        title=bounded_text(title, limit=MAX_TITLE_CHARS),
        tier=definition.tier,
        publisher=definition.publisher,
        first_seen_at=first_seen_at,
        published_at=publication.value if publication else None,
        updated_at=parse_iso8601(_first_present(hit, definition.updated_keys)),
        excerpt=bounded_text(_first_present(hit, definition.excerpt_keys)),
        raw_metadata={
            "api": definition.source_key,
            "publication": publication.as_metadata() if publication else None,
            "event_date": event.as_metadata()["calendar_date"] if event else None,
            "event_date_provenance": event.as_metadata() if event else None,
        })


def _date_witness(hit: dict[str, Any], keys: tuple[str, ...]) -> PublicationWitness | None:
    for key in keys:
        witness = publication_witness(hit.get(key), source=f"parliament-{key}")
        if witness is not None:
            return witness
    return None


class ParliamentApiConnector:
    """Pages one fixed Parliament search API within its declared caps."""

    def __init__(
        self, definition: ParliamentApiDefinition, transport: CivicTransport | None = None,
    ):
        self._definition = definition
        self._transport = transport or CivicTransport()

    @property
    def source_key(self) -> str:
        return self._definition.source_key

    @property
    def definition(self) -> ParliamentApiDefinition:
        return self._definition

    def validate_config(self, config: dict) -> None:
        if config:
            raise ValueError(f"{self._definition.source_key} config must be empty")

    def _payload_for(self, page: int) -> dict:
        payload = dict(self._definition.base_payload)
        payload[self._definition.page_key] = page
        payload[self._definition.page_size_key] = self._definition.page_size
        return payload

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        self.validate_config(config or {})
        definition = self._definition
        documents: list[CivicDocument] = []
        degraded: list[str] = []
        unroutable_types: Counter[str] = Counter()
        pages_fetched = 0
        reported_total: int | None = None
        hit_count = 0

        for offset in range(definition.max_pages):
            page = definition.first_page + offset
            response = self._transport.request(
                definition.endpoint,
                source_key=definition.source_key,
                crawl_delay_seconds=definition.crawl_delay_seconds,
                payload=self._payload_for(page))
            payload = parse_json(
                response.body, source_key=definition.source_key, url=definition.endpoint.url)
            pages_fetched += 1
            hits, total = _hits(payload, definition)
            if total is not None:
                reported_total = total
            hit_count += len(hits)
            for record in hits:
                if len(documents) >= definition.max_items:
                    break
                try:
                    documents.append(_document(record, definition, now))
                except UnroutableDocumentTypeError as exc:
                    unroutable_types[str(exc.document_type)] += 1
                except (CivicSourceError, ValueError) as exc:
                    degraded.append(f"page {page}: {exc}")
            if len(hits) < definition.page_size or len(documents) >= definition.max_items:
                break
        else:
            if reported_total is not None and reported_total > len(documents):
                degraded.append(
                    f"stopped at the {definition.max_pages}-page cap with "
                    f"{len(documents)} of {reported_total} results collected")

        if reported_total is not None and len(documents) >= definition.max_items \
                and reported_total > len(documents):
            degraded.append(
                f"stopped at the {definition.max_items}-item cap with "
                f"{len(documents)} of {reported_total} results collected")

        degraded.extend(self._envelope_warnings(hit_count, reported_total))
        degraded.extend(_unroutable_warnings(unroutable_types))
        return build_result(
            definition.source_key, documents, now, degraded_reasons=degraded,
            pages_fetched=pages_fetched)

    def _envelope_warnings(self, hit_count: int, reported_total: int | None) -> list[str]:
        """Where "this looks fine" and "this is actually broken" are told apart.

        Parliament always has bills and always has recent sittings, so an empty page from these
        endpoints means the request shape drifted or the API changed -- never a quiet week. A
        reported total that contradicts the page actually received means the same thing."""
        definition = self._definition
        warnings: list[str] = []
        if reported_total is None:
            warnings.append(
                "search response carried no total count, so completeness cannot be verified")
        if hit_count == 0 and definition.expects_results:
            warnings.append(
                f"{definition.source_key} returned no results at all; this source always has "
                "content, so an empty response means the request shape or API contract changed")
        if reported_total is not None and reported_total > 0 and hit_count == 0:
            warnings.append(
                f"search response reports {reported_total} results but returned an empty page")
        return warnings

    def fetch_daily_detail(
        self,
        sitting_date: date,
        now: datetime,
        *,
        detail_endpoint: CivicEndpoint | None = None,
        max_items: int = 50,
    ) -> CivicFetchResult:
        """Opt-in, bounded hydration of one Hansard sitting day.

        Deliberately NOT part of `fetch`. `/api/data/daily/{YYYY-MM-DD}` returns a whole day's
        transcript metadata, which is large; pulling it on every listing fetch would cost hundreds
        of KB per run for detail the listing already summarises. A caller asks for one specific
        day, gets at most `max_items` sections, and no HTML transcript body is ever ingested."""
        definition = self._definition
        if detail_endpoint is None:
            raise ValueError(
                f"{definition.source_key} has no daily-detail endpoint configured")
        endpoint = detail_endpoint.at(
            f"{detail_endpoint.url.rstrip('/')}/{sitting_date.isoformat()}")
        response = self._transport.request(
            endpoint, source_key=definition.source_key,
            crawl_delay_seconds=definition.crawl_delay_seconds)
        payload = parse_json(
            response.body, source_key=definition.source_key, url=endpoint.url)
        envelope = require_mapping(
            payload, source_key=definition.source_key, field_name="daily detail")
        # The live envelope is a single day object carrying `hansardDebates[]`, not an OData
        # list; the other keys are accepted so a shape change degrades rather than breaks.
        for key in ("hansardDebates", "value", "sections"):
            if key in envelope:
                sections = require_sequence(
                    envelope[key], source_key=definition.source_key, field_name=key)
                break
        else:
            raise CivicSourceError(
                definition.source_key,
                f"{endpoint.url}: daily detail has no recognised section list")

        page_url = _daily_page_url(sitting_date)
        documents: list[CivicDocument] = []
        degraded: list[str] = []
        for index, record in enumerate(sections[:max_items]):
            try:
                documents.append(
                    _section_document(record, definition, now, sitting_date, page_url))
            except (CivicSourceError, ValueError) as exc:
                degraded.append(f"section {index}: {exc}")
        if len(sections) > max_items:
            degraded.append(
                f"kept {max_items} of {len(sections)} sections for {sitting_date.isoformat()}")
        return build_result(definition.source_key, documents, now, degraded_reasons=degraded)


def _daily_page_url(sitting_date: date) -> str:
    return canonical_url(
        HANSARD_DAILY_URL_TEMPLATE.format(token=sitting_date.isoformat()))


def _section_document(
    record: Any,
    definition: ParliamentApiDefinition,
    first_seen_at: datetime,
    sitting_date: date,
    page_url: str,
) -> CivicDocument:
    """One debate section of a sitting day.

    Sections have no page of their own that can be addressed independently, so they share the
    day's URL and carry their own id as identity. Only the section's title and structural
    metadata are kept -- `content` is deliberately never read, which is what stops a day's full
    transcript being ingested."""
    section = require_mapping(record, source_key=definition.source_key, field_name="section")
    identity = stable_external_key(
        section.get("id"), source_key=definition.source_key, field_name="section id")
    title = section.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CivicSourceError(definition.source_key, f"section {identity} has no title")
    event = publication_witness(
        sitting_date.isoformat(), source="parliament-daily-sitting-date",
    )
    if event is None:
        raise CivicSourceError(definition.source_key, "invalid sitting date")
    return CivicDocument(
        source_key=definition.source_key,
        external_key=identity,
        canonical_url=page_url,
        title=bounded_text(title, limit=MAX_TITLE_CHARS),
        tier=definition.tier,
        publisher=definition.publisher,
        first_seen_at=first_seen_at,
        published_at=None,
        updated_at=None,
        excerpt=bounded_text(section.get("subtitle")),
        raw_metadata={
            "api": definition.source_key,
            "sitting_date": sitting_date.isoformat(),
            "event_date": sitting_date.isoformat(),
            "event_date_provenance": event.as_metadata(),
            "section_items": len(section.get("hansardItems") or [])})


__all__ = [
    "BILLS_SEARCH_FILTERS",
    "SELECT_COMMITTEES_SEARCH_FILTERS",
    "SELECT_COMMITTEE_EXTERNAL_URL_BY_TYPE",
    "SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE",
    "HANSARD_DAILY_URL_TEMPLATE",
    "HANSARD_SEARCH_FILTERS",
    "ParliamentApiConnector",
    "ParliamentApiDefinition",
    "UnroutableDocumentTypeError",
]
