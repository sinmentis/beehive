"""The one HTTP + parsing seam every Civic connector goes through.

Built on `beehive.connectors.http`, never around it: the SSRF check, bounded body, retry
taxonomy, Content-Encoding handling and conditional GET all stay there. What this module adds is
the part that is specific to crawling official sources on someone else's infrastructure:

* **A fixed endpoint is a value, not a string.** `CivicEndpoint` pins the URL, the hosts a
  redirect may land on, the method, the acceptable content types and the byte cap together. A
  connector cannot fetch a URL that was not declared in `catalog.py`, and `allowed_hosts` is
  always passed to `http.fetch_bytes`, so even a compromised upstream cannot redirect Beehive at
  a third party.
* **Crawl delay is enforced, not documented.** Stats NZ publishes `Crawl-delay: 10` and the
  Electoral Commission 2 seconds. `CrawlPacer` holds the last-request time per host and sleeps
  the remainder before the next one, so honouring robots.txt does not depend on every connector
  remembering to. The clock and sleep are injectable, so tests assert the pacing without waiting.
* **Content type is asserted.** An origin that starts serving an HTML error page where XML used
  to be is the single most common way a crawler silently starts producing nonsense. Every fetch
  declares what it expects and fails as PROTOCOL otherwise, which is also how the NZ Legislation
  tombstone is caught rather than parsed.
* **XML parsing is hardened at the parser, not by inspection.** `xml.etree.ElementTree` does not
  fetch external entities, but it does expand internal ones, so a declared entity is a
  billion-laughs vector. An earlier version of `parse_xml` scanned `body[:4096]` for a DOCTYPE,
  which was not a defence at all: a legal 5 KB leading comment pushes the declaration past the
  window, and the ASCII pattern cannot see a UTF-16 document's null-interleaved bytes. Both
  bypasses were reproduced, and both expanded the entity. `parse_xml` now uses `defusedxml`,
  which forbids DTD and entity declarations *in the parser itself* -- so the check is
  encoding-agnostic, position-independent, and happens before any expansion can occur.
* **Everything is bounded.** Bytes per response, items per source, pages per crawl, and
  characters per extracted text all have caps that live here, so "the source got much bigger"
  degrades a crawl rather than a container.

No credentials are accepted anywhere except one optional, explicitly-named API key
(`legislation.py`), which is read from the environment and never from stored config.
"""
from __future__ import annotations

import gzip
import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

from defusedxml import DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden
from defusedxml.ElementTree import fromstring as defused_fromstring

from beehive.civic.documents import CivicSourceError, CivicTombstoneError
from beehive.connectors import http
from beehive.connectors.http import ConnectorHttpError, ConnectorHttpErrorKind

# The descriptive agent these sources' operators asked to see. Identifies the crawler, the
# product and a contact page, so an operator who wants it slowed down or stopped can find us.
CIVIC_USER_AGENT = "PartyMap Civic Watch/0.1 (+https://partymap.co.nz)"

DEFAULT_TIMEOUT_SECONDS = 30.0
# Below `http.DEFAULT_MAX_BYTES` (8 MiB): the largest legitimate civic response is a sitemap
# index of a few hundred KB. A source that suddenly needs more has changed shape and should fail
# loudly rather than be accommodated silently.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
# Sitemaps are the one legitimately large family, so they get their own higher cap.
SITEMAP_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ITEMS = 200
DEFAULT_MAX_PAGES = 10

_XML_CONTENT_TYPES = frozenset({
    "application/xml", "text/xml", "application/rss+xml", "application/atom+xml",
    "application/xhtml+xml",
})
_JSON_CONTENT_TYPES = frozenset({"application/json", "text/json", "application/problem+json"})
_GZIP_CONTENT_TYPES = frozenset({"application/gzip", "application/x-gzip", "application/octet-stream"})

# A tombstoned endpoint answers 200 with a human page saying the feed is gone. Matched on the
# response body only after the content type has already failed the XML/JSON assertion, so a
# legitimate document containing these words is never mistaken for one.
_TOMBSTONE_MARKERS = (
    b"no longer available", b"has been discontinued", b"this feed has been retired",
    b"no longer published", b"page not found", b"has been removed",
)

# The harder case: a *structurally valid* Atom feed whose only entry is a retirement notice. NZ
# Legislation's retired subscription feeds do exactly this -- content type is XML, the document
# parses, and every naive reader gets "one item" or "zero items" and calls it a healthy week.
# These id values are the machine-readable half of that notice; the text markers below are the
# human half, and either alone is enough because the two have not always appeared together.
_TOMBSTONE_ATOM_IDS = frozenset({"url_changed", "urlchanged", "feed_retired", "feed_removed"})
_TOMBSTONE_TEXT_MARKERS = (
    "url_changed", "no longer available", "no longer published", "has been retired",
    "has been discontinued", "has been removed", "this feed has moved", "feed has changed",
)
_ATOM_NS = "{http://www.w3.org/2005/Atom}"

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CivicEndpoint:
    """One fixed, application-declared request. Nothing here is ever built from stored config,
    remote data, or a model response."""

    url: str
    allowed_hosts: frozenset[str]
    method: str = "GET"
    accept: str | None = None
    content_types: frozenset[str] = field(default_factory=lambda: _XML_CONTENT_TYPES)
    max_bytes: int = DEFAULT_MAX_BYTES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.method.upper() not in ("GET", "POST"):
            raise ValueError(f"unsupported Civic endpoint method: {self.method!r}")
        parts = urlsplit(self.url)
        if parts.scheme != "https":
            raise ValueError(f"Civic endpoints must be https: {self.url!r}")
        if not parts.hostname:
            raise ValueError(f"Civic endpoint has no host: {self.url!r}")
        if not self.allowed_hosts:
            raise ValueError(f"Civic endpoint needs an explicit host allowlist: {self.url!r}")
        host = parts.hostname.lower()
        if not any(host == allowed or host.endswith(f".{allowed}")
                   for allowed in self.allowed_hosts):
            raise ValueError(
                f"Civic endpoint host {host!r} is not in its own allowlist "
                f"{sorted(self.allowed_hosts)!r}")

    def at(self, url: str) -> CivicEndpoint:
        """The same endpoint policy pointed at another URL within the same allowlist -- how a
        sitemap index reaches its sub-sitemaps without widening what may be fetched."""
        return CivicEndpoint(
            url=url, allowed_hosts=self.allowed_hosts, method=self.method, accept=self.accept,
            content_types=self.content_types, max_bytes=self.max_bytes,
            timeout_seconds=self.timeout_seconds)


class CrawlPacer:
    """Per-host crawl-delay enforcement.

    Holds the last request time per host and sleeps the remainder before the next one. Shared by
    every connector in one fetch cycle, so two Stats NZ sub-sitemaps fetched back to back respect
    the same 10-second gap the site asked for. `clock` and `sleeper` are injected so tests assert
    the pacing arithmetic without ever sleeping."""

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 sleeper: Sleeper | None = None):
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._last_request_at: dict[str, float] = {}
        self.slept_seconds: list[tuple[str, float]] = []

    def wait_for(self, url: str, crawl_delay_seconds: float) -> None:
        if crawl_delay_seconds <= 0:
            return
        host = (urlsplit(url).hostname or "").lower()
        now = self._clock()
        last = self._last_request_at.get(host)
        if last is not None:
            remaining = crawl_delay_seconds - (now - last)
            if remaining > 0:
                self._sleeper(remaining)
                self.slept_seconds.append((host, remaining))
                now = self._clock()
        self._last_request_at[host] = now


@dataclass(frozen=True)
class CivicResponse:
    """`final_url` is where the redirect chain actually ended, surfaced from
    `beehive.connectors.http.HttpResponse.final_url`.

    Reporting, never a control: every hop has already been re-validated against this endpoint's
    own `allowed_hosts` before it was followed, so an off-allow-list landing is impossible by
    the time a caller sees this. It is here for callers that resolve relative links against the
    document they actually received, or that record provenance."""

    body: bytes
    content_type: str
    final_url: str


class CivicTransport:
    """Performs one bounded, host-pinned request and asserts its content type.

    A connector never calls `http.fetch_bytes` itself: doing so would let it skip the pacer, the
    content-type assertion, or the allowlist. Tests inject a fake `fetch` with the same signature
    and never touch the network."""

    def __init__(
        self,
        *,
        fetch=None,
        pacer: CrawlPacer | None = None,
        user_agent: str = CIVIC_USER_AGENT,
    ):
        self._fetch = fetch or _default_fetch
        self._pacer = pacer or CrawlPacer()
        self._user_agent = user_agent

    @property
    def pacer(self) -> CrawlPacer:
        return self._pacer

    def request(
        self,
        endpoint: CivicEndpoint,
        *,
        source_key: str,
        crawl_delay_seconds: float = 0.0,
        payload: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> CivicResponse:
        """Fetches `endpoint`, honouring its crawl delay, and returns the bounded body with the
        content type the origin declared.

        Raises `CivicSourceError` for any transport failure and `CivicTombstoneError` when a
        successful response is recognisably a retired-endpoint page rather than the declared
        format -- the failure mode a naive parser reports as "zero items, success"."""
        self._pacer.wait_for(endpoint.url, crawl_delay_seconds)
        headers = dict(extra_headers or {})
        body = None
        if endpoint.method.upper() == "POST":
            headers.setdefault("Content-Type", "application/json")
            body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
        try:
            response = self._fetch(
                endpoint.url,
                allowed_hosts=endpoint.allowed_hosts,
                user_agent=self._user_agent,
                accept=endpoint.accept,
                extra_headers=headers or None,
                timeout=endpoint.timeout_seconds,
                max_bytes=endpoint.max_bytes,
                method=endpoint.method.upper(),
                body=body,
            )
        except ConnectorHttpError as exc:
            raise CivicSourceError(source_key, f"{endpoint.url}: {exc}") from exc
        if response.body is None:
            raise CivicSourceError(
                source_key, f"{endpoint.url}: response carried no body")
        content_type = (response.content_type or "").split(";", 1)[0].strip().lower()
        if not content_type:
            content_type = _sniff_content_type(response.body)
        if endpoint.content_types and content_type not in endpoint.content_types:
            _raise_for_unexpected_content_type(
                source_key, endpoint, content_type, response.body)
        return CivicResponse(
            body=response.body,
            content_type=content_type,
            final_url=getattr(response, "final_url", None) or endpoint.url)


def _default_fetch(url: str, **kwargs):
    return http.fetch_bytes(url, **kwargs)


def _sniff_content_type(body: bytes) -> str:
    """Only used when an origin sends no Content-Type at all. Deliberately minimal: enough to
    tell the three shapes these sources use apart, never enough to guess a document into being
    something it is not."""
    head = body[:512].lstrip()
    if head.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if head.startswith(b"<?xml") or head.startswith(b"<rss") or head.startswith(b"<feed"):
        return "application/xml"
    if head.startswith(b"{") or head.startswith(b"["):
        return "application/json"
    return "application/octet-stream"


def _raise_for_unexpected_content_type(
    source_key: str, endpoint: CivicEndpoint, content_type: str, body: bytes,
) -> None:
    lowered = body[:4096].lower()
    if content_type.startswith("text/html") or content_type == "application/octet-stream":
        if any(marker in lowered for marker in _TOMBSTONE_MARKERS):
            raise CivicTombstoneError(
                source_key,
                f"{endpoint.url}: answered HTTP 200 with a retired-endpoint page "
                f"(content-type {content_type!r}) -- this source no longer exists")
    raise CivicSourceError(
        source_key,
        f"{endpoint.url}: expected one of {sorted(endpoint.content_types)}, got "
        f"{content_type!r}")


def parse_xml(body: bytes, *, source_key: str, url: str) -> ET.Element:
    """Parses XML with DTD and entity declarations forbidden by the parser itself.

    `defusedxml.ElementTree.fromstring` installs a parser whose `doctype`/`entity` handlers raise
    rather than process, so the decision is made by the XML parser as it reads the stream. That
    matters more than it sounds: the previous implementation searched `body[:4096]` for a
    `<!DOCTYPE` pattern, and

      * a legal 5 KB leading comment (or processing instruction, or whitespace) pushes the
        declaration past the window, and
      * the ASCII regex cannot match a UTF-16 document, whose bytes are null-interleaved,

    so both a padded and a UTF-16 payload parsed cleanly AND expanded their entities. Neither is
    exotic; both are one-line changes to a hostile response. Delegating to the parser removes the
    whole class -- there is no window to slip past and no encoding to hide in, because the parser
    has already decoded the stream by the time it sees the declaration.

    Entity amplification is refused at declaration time, so nothing is ever expanded and memory
    stays bounded by the response cap `CivicTransport` already applies. Every failure -- forbidden
    DTD, forbidden entity, or plain malformed XML -- is reported as `CivicSourceError`, because a
    caller's response to all three is identical: this source did not return the document it was
    supposed to."""
    try:
        # `forbid_dtd=True` is NOT the default -- defusedxml ships forbid_dtd=False, so a bare
        # `<!DOCTYPE rss SYSTEM "http://evil/x.dtd">` with no inline entity declaration parses
        # happily out of the box. None of these sources uses a DTD, so forbidding it outright
        # costs nothing and closes the remaining door.
        return defused_fromstring(
            body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    # The "DOCTYPE or ENTITY" phrasing is deliberately preserved across both branches: it is the
    # wording callers in beehive.connectors already match on, and the two cases are one rule from
    # a caller's point of view. The specific defusedxml exception is appended for diagnosis.
    except DTDForbidden as exc:
        raise CivicSourceError(
            source_key,
            f"{url}: XML declares a DOCTYPE or ENTITY, which is never accepted "
            f"(DTD forbidden: {exc})") from exc
    except (EntitiesForbidden, ExternalReferenceForbidden) as exc:
        raise CivicSourceError(
            source_key,
            f"{url}: XML declares a DOCTYPE or ENTITY, which is never accepted "
            f"(entity or external reference forbidden: {exc})") from exc
    except ET.ParseError as exc:
        raise CivicSourceError(source_key, f"{url}: malformed XML: {exc}") from exc
    except ValueError as exc:
        # defusedxml raises ValueError for an unsupported/undeclared encoding before parsing.
        raise CivicSourceError(source_key, f"{url}: unreadable XML: {exc}") from exc


def _element_text(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def assert_not_tombstone_document(root: ET.Element, *, source_key: str, url: str) -> None:
    """Refuses a structurally valid XML feed that is actually a retirement notice.

    The content-type assertion in `CivicTransport` only catches a tombstone served as HTML. The
    harder case -- and the one NZ Legislation's retired subscription feeds actually produce -- is
    a well-formed Atom document that parses cleanly, carries `<id>url_changed</id>`, and says in
    prose that the feed has moved. A reader that only checks content types sees one entry (or
    none) and reports a healthy, quiet week forever.

    Two independent signals are checked because they have not always appeared together: the
    machine-readable id, and retirement prose in the document's own headline text.

    Whose headline text counts depends on how much the feed is still publishing. Feed-level
    title, subtitle and description always count: they describe the endpoint itself. An entry's
    title and summary only count when the feed carries at most one entry, because that is the
    shape of the case this scan exists for -- NZ Legislation's retired feeds, where the single
    entry *is* the notice. A feed still publishing several distinct items is reporting the news,
    not announcing its own death, and its entries routinely say things like "has been removed"
    about the world. Scanning them cost the 2026-09-30 slot a healthy 74-entry source."""
    ids = [_element_text(root.find(f"{_ATOM_NS}id"))]
    entries = root.findall(f"{_ATOM_NS}entry")
    for entry in entries:
        ids.append(_element_text(entry.find(f"{_ATOM_NS}id")))
    for value in ids:
        if value and value.strip().lower().lstrip("/") in _TOMBSTONE_ATOM_IDS:
            raise CivicTombstoneError(
                source_key,
                f"{url}: the feed itself declares <id>{value}</id>, which marks a retired or "
                "relocated endpoint -- this is never a quiet week")

    headline_parts = [
        _element_text(root.find(f"{_ATOM_NS}title")),
        _element_text(root.find(f"{_ATOM_NS}subtitle")),
        _element_text(root.find("title")),
        _element_text(root.find("./channel/title")),
        _element_text(root.find("./channel/description")),
    ]
    if len(entries) <= 1:
        for entry in entries:
            headline_parts.append(_element_text(entry.find(f"{_ATOM_NS}title")))
            headline_parts.append(_element_text(entry.find(f"{_ATOM_NS}summary")))
    headline = " ".join(part for part in headline_parts if part).lower()
    for marker in _TOMBSTONE_TEXT_MARKERS:
        if marker in headline:
            raise CivicTombstoneError(
                source_key,
                f"{url}: the only text this endpoint publishes says it is retired "
                f"({marker!r}); a valid-looking XML tombstone is still a tombstone")


def parse_json(body: bytes, *, source_key: str, url: str) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CivicSourceError(source_key, f"{url}: response is not valid JSON: {exc}") from exc


def decompress_gzip(body: bytes, *, source_key: str, url: str, max_bytes: int) -> bytes:
    """Bounded gunzip for the sitemaps that are served as `.xml.gz`.

    `http.fetch_bytes` already bounds *transfer* encoding; this is a gzip *content* payload, so
    the same decompression-bomb protection has to be applied again here. Decompressed one chunk
    past the cap so exceeding it is detected rather than inferred."""
    try:
        with gzip.GzipFile(fileobj=_BytesReader(body)) as handle:
            decoded = handle.read(max_bytes + 1)
    except (OSError, EOFError) as exc:
        raise CivicSourceError(source_key, f"{url}: invalid gzip payload: {exc}") from exc
    if len(decoded) > max_bytes:
        raise CivicSourceError(
            source_key, f"{url}: decompressed sitemap exceeds the {max_bytes} byte cap")
    return decoded


class _BytesReader:
    """Minimal file-like wrapper so GzipFile streams from bytes without an extra dependency."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        else:
            self._pos = len(self._data) + offset
        return self._pos

    def tell(self) -> int:
        return self._pos


def parse_rfc2822(value: Any) -> datetime | None:
    """RSS `pubDate`. Returns None rather than raising for an absent or unparseable value: a
    missing timestamp is normal and must not lose the whole document."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_iso8601(value: Any) -> datetime | None:
    """Atom `updated`, sitemap `lastmod`, and every Parliament/CKAN JSON timestamp. Accepts the
    trailing `Z` form these APIs use, which `datetime.fromisoformat` only handles from 3.11."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def require_mapping(value: Any, *, source_key: str, field_name: str) -> dict:
    if not isinstance(value, dict):
        raise CivicSourceError(
            source_key, f"expected {field_name} to be a JSON object, got {type(value).__name__}")
    return value


def require_sequence(value: Any, *, source_key: str, field_name: str) -> list:
    if not isinstance(value, list):
        raise CivicSourceError(
            source_key, f"expected {field_name} to be a JSON array, got {type(value).__name__}")
    return value


__all__ = [
    "CIVIC_USER_AGENT",
    "CivicEndpoint",
    "CivicResponse",
    "CivicTransport",
    "CrawlPacer",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_PAGES",
    "SITEMAP_MAX_BYTES",
    "ConnectorHttpErrorKind",
    "assert_not_tombstone_document",
    "decompress_gzip",
    "parse_iso8601",
    "parse_json",
    "parse_rfc2822",
    "parse_xml",
    "require_mapping",
    "require_sequence",
]
