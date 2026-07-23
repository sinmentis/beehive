"""Monitor upcoming lots from All About Auctions without fetching every lot page.

The public homepage embeds the complete upcoming-auction result set. The site's same-origin
``/ajax/lots/`` endpoint then returns up to 100 public lot summaries per request, including the
description, current bid, estimates, image, and stable lot URL. Descriptions often contain a
seller-stated RRP, which is extracted as a reference value while preserving whether it excludes
GST. This keeps request volume bounded and avoids both a headless browser and the authenticated
Auction Mobility API behind the site.

The site's robots.txt asks crawlers to wait 10 seconds between requests. The connector enforces
that delay across the homepage and every paginated lot request, including repeated fetch cycles.
Tests inject a no-op sleeper and fake text fetcher, so they never touch the network or wait.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse

from beehive.connectors.base import RawItem
from beehive.connectors.registry import register
from beehive.channels.tracker import AllAboutAuctionsTrackerAdapter
from beehive.domain.channels import ChannelKind

_ORIGIN = "https://auctions.allaboutauctions.co.nz"
_UPCOMING_AUCTIONS_URL = f"{_ORIGIN}/"
_AJAX_LOTS_URL = f"{_ORIGIN}/ajax/lots/"
_USER_AGENT = "beehive/0.1 (personal information hub)"
_REQUEST_TIMEOUT_SECONDS = 20
_CRAWL_DELAY_SECONDS = 10
_MAX_UPCOMING_AUCTIONS = 30
_LOT_PAGE_SIZE = 100
_MAX_LOTS_PER_AUCTION = 1000
_BUYER_PREMIUM_RATE = 0.17
_LOT_FIELDSET = "timed-auction absentee-bid highest-live-bid summary"

_VIEW_VARS_ASSIGNMENT_RE = re.compile(r"\bviewVars\s*=\s*")
_AUCTION_ID_RE = re.compile(r"^1-[A-Za-z0-9]+$")
_AUCTION_PATH_RE = re.compile(r"^/auctions/(?P<auction_id>1-[A-Za-z0-9]+)(?:/|$)")
_LOT_PATH_RE = re.compile(r"^/lots/view/(?P<lot_id>1-[A-Za-z0-9]+)(?:/|$)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_RRP_RE = re.compile(
    r"(?<![A-Za-z0-9])R\.?\s*R\.?\s*P\.?(?![A-Za-z0-9])"
    r"\s*(?:[:\-–—]\s*)?(?:(?:NZD|NZ)\s*)?\$?\s*"
    r"(?P<amount>\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_RRP_EXCLUDES_GST_RE = re.compile(
    r"(?:\+\s*GST\b|\bEX(?:CL(?:UDING)?)?\.?\s*GST\b)",
    re.IGNORECASE,
)

TextFetcher = Callable[[str], str]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class _AuctionRef:
    auction_id: str
    title: str
    closing_at: str | None = None
    lot_count: int | None = None


def _default_fetch_text(url: str) -> str:
    headers = {"User-Agent": _USER_AGENT}
    if urlparse(url).path == urlparse(_AJAX_LOTS_URL).path:
        headers.update(
            {
                "Referer": _UPCOMING_AUCTIONS_URL,
                "X-Requested-With": "XMLHttpRequest",
            }
        )
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(  # noqa: S310 (connector only requests its fixed HTTPS origin)
        request,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    ) as response:
        return response.read().decode("utf-8", errors="replace")


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _clean_description(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _normalize_text(html.unescape(_HTML_TAG_RE.sub(" ", value)))


def _parse_amount(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        if not cleaned:
            return None
        try:
            amount = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    return amount if amount >= 0 else None


def _extract_rrp(description: str) -> tuple[float | None, bool]:
    match = _RRP_RE.search(description)
    if match is None:
        return None, False
    amount = _parse_amount(match.group("amount"))
    if amount is None:
        return None, False
    suffix = description[match.end() : match.end() + 32]
    return amount, _RRP_EXCLUDES_GST_RE.search(suffix) is not None


def _lot_page_url(auction_id: str, offset: int) -> str:
    params = urlencode(
        {
            "n": _LOT_PAGE_SIZE,
            "order_by": "auction_date lot_number",
            "order": "desc asc",
            "fieldset": _LOT_FIELDSET,
            "auctionId": auction_id,
            "lotsRange": "null",
            "paramsType": "server",
            "o": offset,
        }
    )
    return f"{_AJAX_LOTS_URL}?{params}"


class _UpcomingAuctionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._active_auction_id: str | None = None
        self._active_text: list[str] = []
        self._titles_by_id: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != urlparse(_ORIGIN).netloc:
            return
        match = _AUCTION_PATH_RE.match(parsed.path)
        if match is None:
            return
        self._active_auction_id = match.group("auction_id")
        self._active_text = []
        self._titles_by_id.setdefault(self._active_auction_id, "")

    def handle_data(self, data: str) -> None:
        if self._active_auction_id is not None:
            self._active_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._active_auction_id is None:
            return
        title = _normalize_text("".join(self._active_text))
        current = self._titles_by_id[self._active_auction_id]
        if len(title) > len(current):
            self._titles_by_id[self._active_auction_id] = title
        self._active_auction_id = None
        self._active_text = []

    def auctions(self) -> list[_AuctionRef]:
        return [
            _AuctionRef(
                auction_id=auction_id,
                title=title or f"All About Auctions {auction_id}",
            )
            for auction_id, title in self._titles_by_id.items()
        ]


def _parse_embedded_upcoming_auctions(html_text: str) -> list[_AuctionRef] | None:
    assignment = _VIEW_VARS_ASSIGNMENT_RE.search(html_text)
    if assignment is None:
        return None

    try:
        view_vars, _ = json.JSONDecoder().raw_decode(html_text, assignment.end())
    except json.JSONDecodeError as exc:
        raise ValueError(
            "All About Auctions returned invalid embedded auction data"
        ) from exc
    if not isinstance(view_vars, dict):
        raise ValueError("All About Auctions embedded auction data must be an object")

    auction_data = view_vars.get("auctions")
    if not isinstance(auction_data, dict):
        raise ValueError(
            "All About Auctions embedded auction data has no auctions object"
        )
    records = auction_data.get("result_page")
    if not isinstance(records, list):
        raise ValueError("All About Auctions embedded auction data has no result page")

    query_info = auction_data.get("query_info")
    if query_info is not None and not isinstance(query_info, dict):
        raise ValueError(
            "All About Auctions embedded auction query data must be an object"
        )
    total_num_results = (
        query_info.get("total_num_results") if isinstance(query_info, dict) else None
    )
    if total_num_results is not None:
        if isinstance(total_num_results, bool) or not isinstance(
            total_num_results, int
        ):
            raise ValueError(
                "All About Auctions embedded auction result count must be an integer"
            )
        if total_num_results > len(records):
            raise RuntimeError(
                "All About Auctions embedded page exposed only "
                f"{len(records)} of {total_num_results} upcoming auctions"
            )

    auctions: list[_AuctionRef] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(
                "All About Auctions embedded auction record must be an object"
            )
        auction_id = record.get("row_id")
        if (
            not isinstance(auction_id, str)
            or _AUCTION_ID_RE.fullmatch(auction_id) is None
        ):
            raise ValueError(
                "All About Auctions embedded auction record has an invalid ID"
            )
        title = record.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError("All About Auctions embedded auction title must be text")
        closing_at = record.get("effective_end_time")
        if closing_at is not None and not isinstance(closing_at, str):
            raise ValueError(
                "All About Auctions embedded auction closing time must be text"
            )
        lot_count = record.get("lot_count")
        if lot_count is not None:
            if (
                isinstance(lot_count, bool)
                or not isinstance(lot_count, int)
                or lot_count < 0
            ):
                raise ValueError(
                    "All About Auctions embedded auction lot count must be "
                    "a non-negative integer"
                )
            if lot_count == 0:
                continue
        auctions.append(
            _AuctionRef(
                auction_id=auction_id,
                title=_normalize_text(title or "")
                or f"All About Auctions {auction_id}",
                closing_at=closing_at,
                lot_count=lot_count,
            )
        )
    return auctions


def _parse_upcoming_auctions(html_text: str) -> list[_AuctionRef]:
    auctions = _parse_embedded_upcoming_auctions(html_text)
    if auctions is None:
        parser = _UpcomingAuctionParser()
        parser.feed(html_text)
        parser.close()
        auctions = parser.auctions()
    if len(auctions) > _MAX_UPCOMING_AUCTIONS:
        raise RuntimeError(
            "All About Auctions returned more than "
            f"{_MAX_UPCOMING_AUCTIONS} upcoming auctions"
        )
    return auctions


def _canonical_lot_title(title: str) -> str:
    return _normalize_text(re.sub(r"\s*&\s*", " and ", title)).casefold()


def _is_ignored_lot_title(title: str) -> bool:
    return _canonical_lot_title(title) == "terms and conditions"


def _bid_amount(record: dict[str, Any]) -> float | None:
    for key in ("timed_auction_bid", "highest_live_bid"):
        bid = record.get(key)
        if isinstance(bid, dict):
            amount = _parse_amount(bid.get("amount"))
            if amount is not None:
                return amount
    return None


def _lot_url(record: dict[str, Any], lot_id: str) -> str:
    detail_path = record.get("_detail_url")
    if isinstance(detail_path, str):
        parsed = urlparse(detail_path)
        match = _LOT_PATH_RE.match(parsed.path)
        if not parsed.netloc and match is not None and match.group("lot_id") == lot_id:
            return urljoin(_ORIGIN, parsed.path)
    return f"{_ORIGIN}/lots/view/{lot_id}"


def _lot_closing_at(record: dict[str, Any], auction: _AuctionRef) -> str | None:
    candidates = [record.get("extended_end_time")]
    auction_data = record.get("auction")
    if isinstance(auction_data, dict):
        candidates.extend(
            (
                auction_data.get("extended_end_time"),
                auction_data.get("effective_end_time"),
            )
        )
    elif auction_data is not None:
        raise ValueError("All About Auctions lot auction context must be an object")
    candidates.append(auction.closing_at)

    for value in candidates:
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("All About Auctions lot closing time must be text")
        if value.strip():
            return value
    return None


def _to_raw_item(record: dict[str, Any], auction: _AuctionRef) -> RawItem | None:
    if not isinstance(record, dict):
        raise ValueError("All About Auctions lot record must be an object")
    lot_id = record.get("row_id")
    title = record.get("title")
    if (
        not isinstance(lot_id, str)
        or _AUCTION_ID_RE.fullmatch(lot_id) is None
        or not isinstance(title, str)
        or not title.strip()
    ):
        raise ValueError("All About Auctions lot needs a valid ID and title")
    title = _normalize_text(title)
    if _is_ignored_lot_title(title):
        return None

    description = _clean_description(record.get("truncated_description"))
    rrp, rrp_excludes_gst = _extract_rrp(description)
    current_bid = _bid_amount(record)
    estimated_cost = (
        round(current_bid * (1 + _BUYER_PREMIUM_RATE), 2)
        if current_bid is not None
        else None
    )
    status = record.get("status") if isinstance(record.get("status"), str) else None
    bidding_enabled = record.get("bidding_enabled")
    available = bool(
        (bidding_enabled if isinstance(bidding_enabled, bool) else status == "active")
        and status not in {"closed", "sold", "withdrawn"}
    )
    currency_code = (
        record.get("currency_code")
        if isinstance(record.get("currency_code"), str)
        else None
    )
    raw_metadata = {
        "price": current_bid,
        "compare_at_price": rrp,
        "on_sale": bool(
            estimated_cost is not None and rrp is not None and estimated_cost < rrp
        ),
        "available": available,
        "vendor": None,
        "product_type": "Auction lot",
        "tags": ["auction"],
        "listing_kind": "auction_lot",
        "auction_house": "All About Auctions",
        "auction_id": auction.auction_id,
        "auction_title": auction.title,
        "lot_number": record.get("lot_number"),
        "currency_code": currency_code,
        "current_bid": current_bid,
        "buyer_premium_rate": _BUYER_PREMIUM_RATE,
        "estimated_cost": estimated_cost,
        "rrp": rrp,
        "rrp_excludes_gst": rrp_excludes_gst,
        "starting_price": _parse_amount(record.get("starting_price")),
        "estimate_low": _parse_amount(record.get("estimate_low")),
        "estimate_high": _parse_amount(record.get("estimate_high")),
        "sold_price": _parse_amount(record.get("sold_price")),
        "status": status,
        "image_url": (
            record.get("cover_thumbnail")
            if isinstance(record.get("cover_thumbnail"), str)
            else None
        ),
    }
    closing_at = _lot_closing_at(record, auction)
    if closing_at is not None:
        raw_metadata["closing_at"] = closing_at

    return RawItem(
        external_id=lot_id,
        title=title,
        url=_lot_url(record, lot_id),
        body=description,
        raw_metadata=raw_metadata,
    )


def _parse_lot_page(
    payload_text: str,
    auction: _AuctionRef,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"All About Auctions lots for {auction.auction_id} returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"All About Auctions lots for {auction.auction_id} must be an object"
        )
    records = payload.get("result_page")
    query_info = payload.get("query_info")
    if not isinstance(records, list) or not isinstance(query_info, dict):
        raise ValueError(
            f"All About Auctions lots for {auction.auction_id} need result_page "
            "and query_info"
        )
    total = query_info.get("total_num_results")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or total > _MAX_LOTS_PER_AUCTION
    ):
        raise ValueError(
            f"All About Auctions lots for {auction.auction_id} have an invalid total"
        )
    page_offset = query_info.get("page_start_offset")
    if page_offset is not None and page_offset != offset:
        raise ValueError(
            f"All About Auctions lots for {auction.auction_id} returned the wrong offset"
        )
    return records, total


class AllAboutAuctionsConnector:
    type_key = "all_about_auctions"
    supported_channel_kinds = frozenset({ChannelKind.TRACKER})
    tracker_adapter = AllAboutAuctionsTrackerAdapter()
    refresh_existing_items = True

    def __init__(
        self,
        fetch_text: TextFetcher = _default_fetch_text,
        sleep: Sleeper = time.sleep,
    ):
        self._fetch_text = fetch_text
        self._sleep = sleep
        self._has_requested = False

    def _fetch_with_delay(self, url: str) -> str:
        if self._has_requested:
            self._sleep(_CRAWL_DELAY_SECONDS)
        self._has_requested = True
        return self._fetch_text(url)

    def validate_config(self, config: dict) -> None:
        if not isinstance(config, dict) or config:
            raise ValueError("all_about_auctions config must be an empty object")

    def _fetch_auction_lots(self, auction: _AuctionRef) -> list[RawItem]:
        items: list[RawItem] = []
        offset = 0
        expected_total: int | None = None
        while True:
            records, total = _parse_lot_page(
                self._fetch_with_delay(_lot_page_url(auction.auction_id, offset)),
                auction,
                offset,
            )
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise RuntimeError(
                    f"All About Auctions lot total changed during {auction.auction_id}"
                )
            for index, record in enumerate(records, start=offset):
                try:
                    item = _to_raw_item(record, auction)
                except Exception as exc:
                    print(
                        "[all_about_auctions] skipping "
                        f"auction={auction.auction_id} lot_index={index}: {exc}"
                    )
                    continue
                if item is not None:
                    items.append(item)

            collected = offset + len(records)
            if collected >= total:
                break
            if not records or len(records) < _LOT_PAGE_SIZE:
                raise RuntimeError(
                    f"All About Auctions {auction.auction_id} ended before all lots "
                    "were returned"
                )
            offset += _LOT_PAGE_SIZE
        return items

    def fetch(self, config: dict) -> list[RawItem]:
        self.validate_config(config)
        auctions = _parse_upcoming_auctions(
            self._fetch_with_delay(_UPCOMING_AUCTIONS_URL)
        )
        items_by_id: dict[str, RawItem] = {}
        for auction in auctions:
            for item in self._fetch_auction_lots(auction):
                items_by_id.setdefault(item.external_id, item)
        if auctions and not items_by_id:
            raise RuntimeError("All About Auctions returned no usable lots")
        return list(items_by_id.values())


register(AllAboutAuctionsConnector())
