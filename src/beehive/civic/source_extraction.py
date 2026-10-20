"""Article-local publication witnesses and bounded, offline source-template extraction."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lxml import etree, html as lxml_html

from beehive.civic.documents import CivicDocument, bounded_text, canonical_url
from beehive.deep_read.extract import (
    ExtractionQuality,
    ExtractionResult,
    PartialReason,
    extract_full_article_text,
)

SOURCE_CAPTURE_VERSION = 1
PUBLICATION_TIMEZONE = "Pacific/Auckland"
_MAX_JSON_NODES = 10_000
_MAX_JSON_DEPTH = 24
_MAX_JSON_SCRIPTS = 16
_ARTICLE_TYPES = frozenset({"Article", "NewsArticle", "BlogPosting"})
_PUBLICATION_META = frozenset({
    "article:published_time", "datepublished", "publishdate", "pubdate",
})
_PUBLICATION_CLASSES = frozenset({
    "date-published", "entry-date", "post-date", "published", "published-at",
})
_EXCLUDED_CLASSES = frozenset({
    "related", "related-posts", "related-stories", "recommended", "recommendations",
})
_RELATED_WORDS = frozenset({"related", "recommended", "recommendations"})
# What may follow a related marker in a listing's class name: c-related-posts-list, not
# related-story-body.
_LISTING_WORDS = frozenset({
    "posts", "articles", "stories", "news", "content", "items", "links", "reads", "reading",
    "list", "listing", "listings", "block", "section", "widget", "cards", "entries",
    "wrapper", "container", "area", "grid", "carousel", "slider", "module", "box", "panel",
    "row", "rail",
})
# A state word before the marker makes the name a page flag, not a listing: has-related-posts,
# page--with-related.
_STATE_WORDS = frozenset({
    "has", "with", "without", "show", "hide", "no", "not", "is",
    "enable", "enabled", "disable", "disabled",
})
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VISIBLE_DATE = re.compile(r"^\d{1,2}\s+[A-Za-z]+\s+\d{4}$")
_WEEKDAY_PREFIX = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+",
    re.IGNORECASE,
)
_TEASER_LINK = re.compile(
    r"^(?:read (?:more|the (?:full|rest of the) (?:article|release))|"
    r"continue reading|view (?:the )?full (?:article|release))[.! >]*$",
    re.IGNORECASE,
)
_UPDATED_LABEL = re.compile(r"\b(?:updated|modified)\b", re.IGNORECASE)
_CONSERVATIVE_DATE_ID = "comp-mb7rec8v"
_CONSERVATIVE_BODY_CLASS = "wixui-rich-content-viewer"
_CLASS_XPATH = "{tag}[contains(concat(' ', normalize-space(@class), ' '), ' {name} ')]"
_ARC_GLOBAL_CONTENT = "Fusion.globalContent="
_MAX_ARC_METADATA_CHARS = 2_000_000
_EXTRACTION_METHODS = frozenset({"article-html", "conservative-wix-html"})


class SourceExtractionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PublicationWitness:
    value: datetime
    source: str
    precision: str
    raw_value: str
    timezone: str = PUBLICATION_TIMEZONE

    def as_metadata(self) -> dict[str, str]:
        return {
            "value": self.value.isoformat(),
            "source": self.source,
            "precision": self.precision,
            "raw_value": self.raw_value,
            "timezone": self.timezone,
            "calendar_date": self.value.astimezone(ZoneInfo(self.timezone)).date().isoformat(),
        }


@dataclass(frozen=True)
class SourceArticle:
    extraction: ExtractionResult
    publication: PublicationWitness | None
    title: str | None
    method: str


def _tree(html: str) -> lxml_html.HtmlElement:
    try:
        return lxml_html.document_fromstring(html)
    except (etree.ParserError, ValueError) as exc:
        raise SourceExtractionError("detail_html_invalid") from exc


def _text(node: lxml_html.HtmlElement) -> str:
    if not node.xpath(".//br"):
        return " ".join(node.text_content().split())
    # A line break separates words; inline styling that splits one word must stay joined.
    clone = copy.deepcopy(node)
    for line_break in clone.iter("br"):
        line_break.tail = " " + (line_break.tail or "")
    return " ".join(clone.text_content().split())


def _classes(node: lxml_html.HtmlElement) -> set[str]:
    return set((node.get("class") or "").lower().split())


def _names_related_listing(token: str) -> bool:
    parts = [part for part in re.split(r"[-_]+", token) if part]
    return any(
        part in _RELATED_WORDS
        and (index == 0 or parts[index - 1] not in _STATE_WORDS)
        and all(rest in _LISTING_WORDS for rest in parts[index + 1:])
        for index, part in enumerate(parts))


def _is_related_section(node: lxml_html.HtmlElement) -> bool:
    """Other articles listed around this one, including BEM names like news-listing--related.

    The document root and body are never a listing. A name matched only by the wider rule never
    covers the article itself: a node holding a top-level heading, the main landmark or the
    article body is this article's container."""
    if node.tag in ("html", "body"):
        return False
    classes = _classes(node)
    if classes & _EXCLUDED_CLASSES:
        return True
    return any(_names_related_listing(token) for token in classes) and not node.xpath(
        ".//h1 | .//main | .//*[@itemprop='articleBody']")


def _excluded(node: lxml_html.HtmlElement) -> bool:
    return any(
        ancestor.tag in ("aside", "footer", "nav") or _is_related_section(ancestor)
        for ancestor in (node, *node.iterancestors())
    )


def _without_related_sections(html: str, tree: lxml_html.HtmlElement) -> str:
    """The page for full-text extraction, minus listings of other articles.

    Pages without such a listing are passed through unchanged, byte for byte."""
    related = [
        node for node in tree.iter()
        if isinstance(node.tag, str) and _is_related_section(node)
        and not any(_is_related_section(ancestor) for ancestor in node.iterancestors())
    ]
    if not related:
        return html
    cleaned = copy.deepcopy(tree)
    outermost = [
        node for node in cleaned.iter()
        if isinstance(node.tag, str) and _is_related_section(node)
        and not any(_is_related_section(ancestor) for ancestor in node.iterancestors())
    ]
    for node in outermost:
        node.drop_tree()
    return lxml_html.tostring(cleaned, encoding="unicode")


def article_title(html: str) -> str | None:
    return _title(_tree(html))


def _title(tree: lxml_html.HtmlElement) -> str | None:
    titles = {
        _text(node)
        for node in tree.xpath("//h1")
        if not _excluded(node) and 4 <= len(_text(node)) <= 300
    }
    return next(iter(titles)) if len(titles) == 1 else None


def publication_witness(
    value: Any, *, source: str, timezone_name: str = PUBLICATION_TIMEZONE,
) -> PublicationWitness | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        return None
    value = " ".join(value.split())
    calendar_value = _WEEKDAY_PREFIX.sub("", value)
    zone = ZoneInfo(timezone_name)
    if _DATE_ONLY.fullmatch(calendar_value) or _VISIBLE_DATE.fullmatch(calendar_value):
        try:
            day = (
                datetime.strptime(calendar_value, "%Y-%m-%d").date()
                if _DATE_ONLY.fullmatch(calendar_value)
                else datetime.strptime(calendar_value, "%d %B %Y").date()
            )
        except ValueError:
            return None
        return PublicationWitness(
            datetime.combine(day, time.min, zone), source, "date", value, timezone_name,
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A clock time without an offset is not an instant. Do not silently label it UTC.
        return None
    return PublicationWitness(parsed, source, "datetime", value, timezone_name)


def _json(value: str) -> Any:
    try:
        data = json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise SourceExtractionError("detail_structured_data_invalid") from exc
    pending = [(data, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise SourceExtractionError("detail_structured_data_limit")
        if isinstance(node, dict):
            pending.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    return data


def _article_nodes(data: Any):
    if isinstance(data, list):
        for item in data:
            yield from _article_nodes(item)
    elif isinstance(data, dict):
        types = data.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if isinstance(types, list) and any(
            isinstance(kind, str) and kind.rsplit("/", 1)[-1] in _ARTICLE_TYPES
            for kind in types
        ):
            yield data
        # Do not descend through arbitrary recommendations or itemListElement entries.
        for key in ("@graph", "mainEntity"):
            if key in data:
                yield from _article_nodes(data[key])


def _same_url(value: Any, url: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        left, right = canonical_url(value, base=url), canonical_url(url)
    except ValueError:
        return False
    return left == right or _without_trailing_slash(left) == _without_trailing_slash(right)


def _without_trailing_slash(url: str) -> str:
    """One trailing slash on a non-root path names the same page (Labour's canonical adds it)."""
    parts = urlsplit(url)
    path = parts.path
    if len(path) > 1 and path.endswith("/") and not path.endswith("//"):
        path = path[:-1]
    return urlunsplit(parts._replace(path=path))


def _belongs_to_article(node: dict[str, Any], url: str, title: str | None) -> bool:
    identities: list[Any] = []
    if "url" in node:
        identities.append(node["url"])
    identity = node.get("@id")
    if isinstance(identity, str) and identity.startswith(("https://", "http://")):
        identities.append(identity)
    main = node.get("mainEntityOfPage")
    if isinstance(main, str):
        identities.append(main)
    elif isinstance(main, dict) and "@id" in main:
        identities.append(main["@id"])
    if identities:
        return all(_same_url(identity, url) for identity in identities)
    headline = node.get("headline")
    return bool(
        title and isinstance(headline, str)
        and " ".join(headline.split()).casefold() == title.casefold()
    )


def _choose_publication(
    witnesses: list[PublicationWitness],
) -> PublicationWitness | None:
    if not witnesses:
        return None
    zone = ZoneInfo(PUBLICATION_TIMEZONE)
    days = {item.value.astimezone(zone).date() for item in witnesses}
    instants = {item.value for item in witnesses if item.precision == "datetime"}
    if len(days) != 1 or len(instants) > 1:
        raise SourceExtractionError("detail_publication_date_conflict")
    return next(
        (item for item in witnesses if item.precision == "datetime"), witnesses[0],
    )


def _is_update_time(node: lxml_html.HtmlElement) -> bool:
    parent = node.getparent()
    if parent is None:
        return True
    previous = node.getprevious()
    before = previous.tail if previous is not None else parent.text
    label = (
        _text(previous)
        if previous is not None and previous.tag in ("span", "strong", "b")
        and len(_text(previous)) <= 40 else ""
    )
    markers = " ".join((
        node.get("class") or "", node.get("itemprop") or "",
        parent.get("class") or "",
    )).replace("-", " ").replace("_", " ")
    return bool(
        node.get("itemprop") == "dateModified"
        or _UPDATED_LABEL.search(markers)
        or _UPDATED_LABEL.search(f"{label} {(before or '')[-40:]} {_text(node)}")
    )


def _publication(
    tree: lxml_html.HtmlElement, *, url: str, source_key: str,
) -> PublicationWitness | None:
    witnesses: list[PublicationWitness] = []
    title = _title(tree)

    def add(value: Any, source: str) -> None:
        witness = publication_witness(value, source=source)
        if witness is not None:
            witnesses.append(witness)

    scripts = tree.xpath('//script[@type="application/ld+json"]')
    if len(scripts) > _MAX_JSON_SCRIPTS:
        raise SourceExtractionError("detail_structured_data_limit")
    for script in scripts:
        for node in _article_nodes(_json(script.text or "")):
            if _belongs_to_article(node, url, title):
                add(node.get("datePublished"), "article-jsonld-datePublished")
    articles = tree.xpath("//article")
    current_articles = [
        node for node in articles
        if title and not _excluded(node)
        and [_text(heading) for heading in node.xpath(".//h1")] == [title]
    ]
    current = current_articles[0] if len(current_articles) == 1 else None
    for node in tree.xpath("//meta"):
        key = (node.get("property") or node.get("name") or node.get("itemprop") or "").lower()
        if key in _PUBLICATION_META and not _excluded(node):
            if node.getparent().tag == "head" or (
                current is not None
                and next(node.iterancestors("article"), None) is current
            ):
                add(node.get("content"), f"article-meta-{key}")

    for node in tree.xpath("//time | //*[@itemprop='datePublished']"):
        if _excluded(node) or node.tag == "meta" or _is_update_time(node):
            continue
        parents = tuple(node.iterancestors())
        if current is None or next(node.iterancestors("article"), None) is not current:
            continue
        explicit = (
            node.get("itemprop") == "datePublished" or "pubdate" in node.attrib
            or bool(_classes(node) & _PUBLICATION_CLASSES)
        )
        if explicit:
            add(node.get("datetime") or node.get("content") or _text(node), "article-visible-datePublished")
        elif (
            len(articles) == 1 and len(current.xpath(".//time")) == 1
            and node.get("itemprop") is None
            and (
                (
                    node.getparent() is current and node.getprevious() is not None
                    and node.getprevious().tag == "h1"
                )
                or any(parent.tag == "header" for parent in parents[:parents.index(current)])
            )
        ):
            add(node.get("datetime"), "article-local-time")

    if source_key == "national":
        for heading in tree.xpath("//h1"):
            if _excluded(heading):
                continue
            sections = [node for node in heading.iterancestors() if node.tag == "section"]
            if not sections:
                continue
            section = sections[0]
            if len(section.xpath(".//h1")) != 1:
                continue
            for byline in section.xpath(".//div"):
                if {"flex", "flex-wrap", "items-baseline"} <= _classes(byline):
                    for span in byline.xpath("./span"):
                        if (
                            span.get("itemprop") in (None, "datePublished")
                            and not _is_update_time(span)
                        ):
                            add(_text(span), "national-visible-byline")
    if source_key == "conservative":
        dates = tree.xpath("//*[@id=$id]", id=_CONSERVATIVE_DATE_ID)
        if len(dates) == 1 and not _excluded(dates[0]):
            add(_text(dates[0]), "conservative-visible-publication")
    if source_key == "act":
        for header in tree.xpath('//*[@data-framer-name="Header"]'):
            headings = header.xpath(".//h1")
            times = header.xpath(".//time")
            if (
                not _excluded(header) and len(headings) == len(times) == 1
                and _text(headings[0]) == title and not _is_update_time(times[0])
                and times[0].get("itemprop") in (None, "datePublished")
            ):
                add(times[0].get("datetime"), "act-framer-header-time")
                add(_text(times[0]), "act-framer-header-visible-date")
    if source_key == "labour":
        release = _labour_release(tree)
        if release is not None and release[1] is not None:
            add(_text(release[1]), "labour-visible-publication-date")
    if source_key == "te-ao":
        add(_arc_first_publish_date(tree, url), "te-ao-arc-first-publish-date")
    if source_key == "interest":
        witness = _interest_by_line_publication(tree)
        if witness is not None:
            witnesses.append(witness)
    return _choose_publication(witnesses)


def _arc_first_publish_date(tree: lxml_html.HtmlElement, url: str) -> str | None:
    """The page's own Arc XP content object (ANS), bound by story type and canonical path.

    Preferred to the visible byline, whose styled-component class names change every build."""
    scripts = tree.xpath("//script[@id='fusion-metadata']")
    if len(scripts) != 1:
        return None
    text = scripts[0].text or ""
    if len(text) > _MAX_ARC_METADATA_CHARS or text.count(_ARC_GLOBAL_CONTENT) != 1:
        return None
    start = text.index(_ARC_GLOBAL_CONTENT) + len(_ARC_GLOBAL_CONTENT)
    try:
        content, _ = json.JSONDecoder().raw_decode(text, start)
    except (ValueError, RecursionError):
        return None
    if (not isinstance(content, dict) or content.get("type") != "story"
            or not _same_url(content.get("canonical_url"), url)):
        return None
    value = content.get("first_publish_date")
    return value if isinstance(value, str) else None


def _interest_by_line_publication(tree: lxml_html.HtmlElement) -> PublicationWitness | None:
    """interest.co.nz's story by-line stamp, a New Zealand wall-clock time like its feed's.

    Bound to the by-line directly inside the story's own content column; a sidebar or other
    nested by-line, or a second stamp anywhere in the column, binds nothing."""
    from beehive.connectors.civic_strategies import parse_wall_clock

    columns = tree.xpath("//*[@id='story-wrapper']/*[@id='content-column']")
    if len(columns) != 1 or _excluded(columns[0]):
        return None
    stamps = columns[0].xpath(".//" + _CLASS_XPATH.format(tag="span", name="published-at"))
    if len(stamps) != 1 or _excluded(stamps[0]):
        return None
    by_line = next(
        (parent for parent in stamps[0].iterancestors() if "by-line" in _classes(parent)), None)
    if by_line is None or by_line.getparent() is not columns[0]:
        return None
    raw = _text(stamps[0])
    value = parse_wall_clock(raw, timezone_name=PUBLICATION_TIMEZONE)
    if value is None:
        return None
    return PublicationWitness(
        value, "interest-by-line-published-at", "datetime", raw, PUBLICATION_TIMEZONE)


def _labour_release(
    tree: lxml_html.HtmlElement,
) -> tuple[lxml_html.HtmlElement, lxml_html.HtmlElement | None] | None:
    """Labour's Umbraco release (no h1): the single news-page's own page-title heading, in a
    block directly inside that page, and the publication date printed after it in that block.

    A heading or date anywhere else, such as a card for another release, binds nothing."""
    pages = [
        node for node in tree.xpath("//" + _CLASS_XPATH.format(tag="div", name="news-page"))
        if not _excluded(node)
    ]
    if len(pages) != 1:
        return None
    page = pages[0]
    headings = [
        node for node in page.xpath(".//" + _CLASS_XPATH.format(tag="h2", name="page-title"))
        if not _excluded(node)
    ]
    if len(headings) != 1:
        return None
    heading = headings[0]
    block = heading.getparent()
    if block is None or block.getparent() is not page:
        return None
    dates = [
        node for node in page.xpath(
            ".//" + _CLASS_XPATH.format(tag="p", name="news-publication-date"))
        if not _excluded(node)
    ]
    date = (
        dates[0] if len(dates) == 1 and dates[0] in heading.itersiblings()
        and not _is_update_time(dates[0]) else None)
    return heading, date


def _labour_title(tree: lxml_html.HtmlElement) -> str | None:
    release = _labour_release(tree)
    if release is None:
        return None
    title = _text(release[0])
    return title if 4 <= len(title) <= 300 else None


def publication_from_html(
    html: str, *, url: str, source_key: str,
) -> PublicationWitness | None:
    return _publication(_tree(html), url=url, source_key=source_key)


def document_publication(document: CivicDocument) -> PublicationWitness | None:
    if document.published_at is None:
        return None
    capture = document.raw_metadata.get("source_capture")
    metadata = (
        capture.get("publication") if isinstance(capture, dict)
        else document.raw_metadata.get("publication")
    )
    if isinstance(metadata, dict):
        witness = publication_witness(metadata.get("value"), source="stored-publication")
        if not witness or witness.value != document.published_at:
            return None
        if (
            metadata.get("precision") not in ("date", "datetime")
            or not isinstance(metadata.get("source"), str)
            or not metadata["source"].strip()
            or not isinstance(metadata.get("raw_value"), str)
            or not metadata["raw_value"].strip()
            or not isinstance(metadata.get("timezone"), str)
        ):
            return None
        try:
            zone = ZoneInfo(metadata["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            return None
        if metadata.get("calendar_date") != document.published_at.astimezone(zone).date().isoformat():
            return None
        return PublicationWitness(
            document.published_at, metadata["source"], metadata["precision"],
            metadata["raw_value"], metadata["timezone"],
        )
    if isinstance(capture, dict):
        return None
    return publication_witness(
        document.published_at.isoformat(), source="connector-published-at",
    )


def _conservative_html(
    tree: lxml_html.HtmlElement, *, url: str,
) -> tuple[str, str | None]:
    """Wix ships the release in the rendered viewer's SSR properties, not its empty outer DOM."""
    canonicals = tree.xpath('//link[@rel="canonical"]/@href')
    if len(canonicals) != 1 or not _same_url(canonicals[0], url):
        raise SourceExtractionError("detail_article_identity_missing")
    roots = [
        node for node in tree.xpath("//div[@class]")
        if _CONSERVATIVE_BODY_CLASS in _classes(node)
    ]
    if len(roots) != 1:
        raise SourceExtractionError("detail_embedded_article_missing")
    owners = [node for node in roots[0].iterancestors() if node.get("id")]
    if not owners:
        raise SourceExtractionError("detail_embedded_article_missing")
    component_id = owners[0].get("id")
    scripts = tree.xpath('//script[@id="wix-warmup-data"][@type="application/json"]')
    if len(scripts) != 1:
        raise SourceExtractionError("detail_embedded_article_missing")
    data = _json(scripts[0].text or "")
    platform = data.get("platform") if isinstance(data, dict) else None
    updates = platform.get("ssrPropsUpdates") if isinstance(platform, dict) else None
    if not isinstance(updates, list):
        raise SourceExtractionError("detail_embedded_article_missing")
    contents = [
        props[component_id]["content"]
        for props in updates
        if isinstance(props, dict) and isinstance(props.get(component_id), dict)
        and "content" in props[component_id]
    ]
    if len(contents) != 1 or not isinstance(contents[0], dict):
        raise SourceExtractionError("detail_embedded_article_ambiguous")
    nodes = contents[0].get("nodes")
    if not isinstance(nodes, list):
        raise SourceExtractionError("detail_embedded_article_missing")
    fragments: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            raise SourceExtractionError("detail_embedded_content_unsupported")
        if node.get("type") == "PARAGRAPH" and node.get("nodes") == []:
            continue
        html_data = node.get("htmlData")
        if (
            node.get("type") != "HTML" or node.get("nodes")
            or not isinstance(html_data, dict) or html_data.get("source") != "HTML"
            or not isinstance(html_data.get("html"), str) or not html_data["html"].strip()
        ):
            raise SourceExtractionError("detail_embedded_content_unsupported")
        fragments.append(html_data["html"])
    if len(fragments) != 1:
        raise SourceExtractionError("detail_embedded_article_ambiguous")
    titles = tree.xpath('//meta[@property="og:title"]/@content')
    title = titles[0] if len(titles) == 1 else None
    if title:
        title = re.sub(r"\s*\|\s*CONSERVATIVES\.NZ\s*$", "", title, flags=re.IGNORECASE)
        if not 4 <= len(title) <= 300:
            title = None
    return fragments[0], title


def _is_teaser(tree: lxml_html.HtmlElement, extracted_text: str = "") -> bool:
    scopes = tree.xpath("//main")
    if len(scopes) != 1:
        scopes = [
            node for node in tree.xpath("//article")
            if not _excluded(node) and (
                node.get("itemprop") == "articleBody" or node.xpath(".//h1")
            )
        ]
    scope = scopes[0] if len(scopes) == 1 else tree
    paragraphs = [node for node in scope.xpath(".//p") if _text(node) and not _excluded(node)]
    positions = {node: index for index, node in enumerate(scope.iter())}
    body = " ".join(extracted_text.split())
    for link in scope.xpath(".//a"):
        label = _text(link)
        if _excluded(link) or not _TEASER_LINK.fullmatch(label):
            continue
        # A newsletter can cite background material while its own full body continues.
        # Only generic interior references get this exception, not terminal continuation.
        following_body = [
            node for node in paragraphs
            if positions[node] > positions[link]
            and len(_text(node).split()) >= 20 and _text(node) in body
        ]
        preceding = [node for node in paragraphs if positions[node] < positions[link]]
        clipped = False
        for node in reversed(preceding):
            prefix = _text(node)
            if prefix.casefold().endswith(label.casefold()):
                prefix = prefix[:-len(label)].rstrip()
            if prefix:
                clipped = prefix.endswith(("...", "\u2026"))
                break
        if (label.casefold().rstrip(".! >") == "read more"
                and not clipped and len(following_body) >= 2):
            continue
        return True
    return bool(paragraphs and _text(paragraphs[-1]).endswith(("...", "\u2026")))


def extract_source_article(html: str, *, url: str, source_key: str) -> SourceArticle:
    tree = _tree(html)
    canonicals = tree.xpath('//link[@rel="canonical"]/@href')
    if canonicals and (len(canonicals) != 1 or not _same_url(canonicals[0], url)):
        raise SourceExtractionError("detail_article_identity_mismatch")
    title = _title(tree)
    if title is None and source_key == "labour":
        title = _labour_title(tree)
    method = "article-html"
    content_html = html
    if source_key == "conservative":
        content_html, title = _conservative_html(tree, url=url)
        method = "conservative-wix-html"
    body_tree = tree if method == "article-html" else _tree(content_html)
    extraction = extract_full_article_text(
        _without_related_sections(content_html, tree)
        if method == "article-html" else content_html)
    if _is_teaser(
        body_tree,
        extraction.text if extraction.quality is ExtractionQuality.COMPLETE else "",
    ):
        raise SourceExtractionError("detail_article_teaser")
    if (
        method == "conservative-wix-html"
        and extraction.reasons == (PartialReason.SHORT_CONTENT,)
        and len(extraction.text) >= 80 and len(extraction.text.split()) >= 12
        and (
            body_tree.xpath("//article[@itemprop='articleBody']")
            or (
                [_text(node).casefold() for node in body_tree.xpath("//h1")] == ["press release"]
                and re.search(r"</body>\s*</html>\s*$", content_html, re.IGNORECASE)
            )
        )
        and extraction.text.rstrip().endswith((".", "!", "?"))
    ):
        # Only a positively identified complete body can override the length heuristic.
        extraction = ExtractionResult(
            ExtractionQuality.COMPLETE, extraction.text, (), extraction.char_count,
        )
    return SourceArticle(
        extraction=extraction,
        publication=_publication(tree, url=url, source_key=source_key),
        title=title,
        method=method,
    )


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def capture_metadata(
    document: CivicDocument,
    *,
    article: SourceArticle,
    body: str,
    captured_at: datetime,
    fetched_url: str,
) -> dict[str, Any]:
    body = " ".join(body.split())
    excerpt = bounded_text(body)
    return {
        "version": SOURCE_CAPTURE_VERSION,
        "source_key": document.source_key,
        "external_key": document.dedupe_key,
        "requested_url": document.canonical_url,
        "fetched_url": fetched_url,
        "captured_at": captured_at.isoformat(),
        "listing_published_at": document.published_at.isoformat() if document.published_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
        "publication": article.publication.as_metadata() if article.publication else None,
        "extraction": {
            "method": article.method,
            "quality": article.extraction.quality.value,
            "full_text_sha256": text_sha256(body),
            "full_text_chars": len(body),
            "excerpt_sha256": text_sha256(excerpt),
            "excerpt_chars": len(excerpt),
            "excerpt_truncated": len(excerpt) < len(body),
        },
    }


def has_full_article_extraction(document: CivicDocument) -> bool:
    metadata = document.raw_metadata.get("source_capture")
    if not isinstance(metadata, dict) or metadata.get("version") != SOURCE_CAPTURE_VERSION:
        return False
    extraction = metadata.get("extraction")
    if not isinstance(extraction, dict):
        return False
    full_chars = extraction.get("full_text_chars")
    full_hash = extraction.get("full_text_sha256")
    return bool(
        document.excerpt.strip()
        and metadata.get("source_key") == document.source_key
        and metadata.get("external_key") == document.dedupe_key
        and metadata.get("requested_url") == document.canonical_url
        and extraction.get("method") in _EXTRACTION_METHODS
        and extraction.get("quality") == ExtractionQuality.COMPLETE.value
        and isinstance(full_chars, int) and not isinstance(full_chars, bool)
        and full_chars >= len(document.excerpt)
        and isinstance(full_hash, str) and re.fullmatch(r"[a-f0-9]{64}", full_hash)
        and extraction.get("excerpt_chars") == len(document.excerpt)
        and extraction.get("excerpt_truncated") is (full_chars > len(document.excerpt))
        and (full_chars > len(document.excerpt) or full_hash == text_sha256(document.excerpt))
        and extraction.get("excerpt_sha256") == text_sha256(document.excerpt)
    )


def has_current_article_capture(document: CivicDocument) -> bool:
    if not has_full_article_extraction(document):
        return False
    metadata = document.raw_metadata["source_capture"]
    if "publication" not in metadata:
        return False
    publication = metadata["publication"]
    if publication is None:
        return document.published_at is None
    return document_publication(document) is not None
