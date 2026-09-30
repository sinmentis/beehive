"""Phase 4's official-institution SourceConnectors. Three trusted, credentialless RSS 2.0
feeds (RBNZ, the NZ Government "Beehive" site, and the US Federal Reserve) share one deep
implementation: HTTP fetch, RSS parsing, HTML body cleanup, RFC 2822 timestamp parsing, the
50-item cap, and the 1,500-character prompt-body cap all live here once. Each feed is a fixed
FeedDefinition wrapped in an OfficialFeedConnector with a distinct type_key; callers still see
only validate_config()/fetch(). Source config is always {} -- validate_config rejects any
non-empty config so a stored URL can never redirect the trusted endpoint. Tests inject a fake
fetch_bytes and never touch the network."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from beehive.connectors.base import RawItem
from beehive.connectors import http
from beehive.connectors.registry import register
from beehive.connectors.text import html_to_text
from beehive.domain.channels import ChannelKind

_BODY_CHAR_CAP = 1500
_FETCH_LIMIT = 50
_REQUEST_TIMEOUT_SECONDS = 30
_USER_AGENT = "beehive/0.1 (by /u/sinmentis)"

BytesFetcher = Callable[[str], bytes]


@dataclass(frozen=True)
class FeedDefinition:
    type_key: str
    url: str
    publisher: str


def _default_fetch_bytes(url: str) -> bytes:
    return http.fetch_bytes(
        url, user_agent=_USER_AGENT, timeout=_REQUEST_TIMEOUT_SECONDS).body


def _text_or_none(element) -> str | None:
    return element.text if element is not None and element.text else None


def _to_raw_item(item, publisher: str) -> RawItem:
    title = _text_or_none(item.find("title"))
    link = _text_or_none(item.find("link"))
    guid = _text_or_none(item.find("guid"))
    external_id = guid if guid is not None else link
    if title is None or link is None or external_id is None:
        raise ValueError(
            "official feed item is missing a title, link, or identifier"
        )
    pub_date = _text_or_none(item.find("pubDate"))
    created_at = parsedate_to_datetime(pub_date) if pub_date is not None else None
    raw_metadata: dict[str, Any] = {"publisher": publisher}
    category = _text_or_none(item.find("category"))
    if category is not None:
        raw_metadata["category"] = category
    return RawItem(
        external_id=external_id,
        title=title,
        url=link,
        body=html_to_text(_text_or_none(item.find("description")), cap=_BODY_CHAR_CAP),
        created_at=created_at,
        raw_metadata=raw_metadata,
    )


class OfficialFeedConnector:
    supported_channel_kinds = frozenset({ChannelKind.EDITORIAL})

    def __init__(
        self,
        definition: FeedDefinition,
        fetch_bytes: BytesFetcher = _default_fetch_bytes,
    ):
        self._definition = definition
        self._fetch_bytes = fetch_bytes

    @property
    def type_key(self) -> str:
        return self._definition.type_key

    def validate_config(self, config: dict) -> None:
        if config:
            raise ValueError(
                f"{self._definition.type_key} config must be empty"
            )

    def fetch(self, config: dict) -> list[RawItem]:
        self.validate_config(config)
        raw_xml = self._fetch_bytes(self._definition.url)
        root = ET.fromstring(raw_xml)  # noqa: S314 (trusted official feed, not user input)
        items = root.findall(".//item")[:_FETCH_LIMIT]
        return [_to_raw_item(item, self._definition.publisher) for item in items]


FEED_DEFINITIONS = (
    FeedDefinition("rbnz_news", "https://www.rbnz.govt.nz/feeds/news", "RBNZ News"),
    FeedDefinition(
        "nz_government_news", "https://www.beehive.govt.nz/rss.xml", "NZ Government"
    ),
    FeedDefinition(
        "federal_reserve_news",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "Federal Reserve",
    ),
)

for _definition in FEED_DEFINITIONS:
    register(OfficialFeedConnector(_definition))
