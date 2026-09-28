"""Second 'monitor' Channel Source, for storefronts that are NOT Shopify. Confirmed against
www.land-sea.co.nz (NZ outdoor/fishing retailer running "N2 ERP" by First Software) -- unlike
Shopify, N2 ERP exposes no unauthenticated products.json API. But its product-listing pages
(e.g. /sale, /outlet) still render a plain, unauthenticated HTTP response (no bot-blocking on
User-Agent observed) with the current page's products embedded as a JS literal assignment:
    window.page.productTiles = [{"id":..., "name":..., "price":..., "priceWas":..., ...}, ...];
That JS array is valid JSON, so this connector regexes it out of the HTML and json.loads it --
still no headless browser needed (Chromium's memory footprint is a bad fit for this host's
512M container limit, especially right after an unrelated OOM/freeze scare on the same box).

Pagination is a `?pgNmbr=N` query param (confirmed by reading the site's own minified JS). The
response also carries `window.page.totalPagesJs`, but it is not reliable: in September 2026 page 1
reported 83 pages while every later page reported 8, and page numbers past the end returned the
last page again. So the listing ends at a short page or at a page that adds no new product id,
and repeated tiles are dropped (connectors.paging).

A full /sale listing runs to ~76 pages (~1,976 products), a different order of magnitude from
the Shopify stores' clearance-only collections. _MAX_PAGES is therefore high enough to follow
the site's known full catalog while still bounding a runaway listing; one that still has new
products past the cap fails the fetch (connectors.paging) rather than returning a partial
snapshot. The site's optional filter query parameters (for example `?brands=85` or
`?categories=2`) are preserved when configured, but an unfiltered Source must still fetch every
authoritative page.

Tests inject a fake fetch_html and never touch the network."""
from __future__ import annotations

import json
import re
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from beehive.connectors.base import RawItem
from beehive.connectors.http import fetch_text
from beehive.connectors.paging import Page, collect_pages
from beehive.connectors.registry import register
from beehive.domain.channels import ChannelKind

_USER_AGENT = "beehive/0.1 (personal information hub)"
_REQUEST_TIMEOUT_SECONDS = 20
_PAGE_SIZE = 26
# Covers the site's known ~76-page full catalog while bounding erroneous pagination metadata.
_MAX_PAGES = 100

_PRODUCT_TILES_RE = re.compile(r"window\.page\.productTiles\s*=\s*(\[.*?\]);", re.DOTALL)
# N2 ERP is not documented, so scan the tile fields most likely to hold an image, accepting either
# a URL string or an object carrying one under a common sub-key. A missing/foreign shape yields
# None -- no URL is ever fabricated.
_IMAGE_KEYS = (
    "imageUrl",
    "image",
    "imageSrc",
    "thumbnailUrl",
    "thumbnail",
    "mainImage",
    "mainImageUrl",
    "img",
)

HtmlFetcher = Callable[[str], str]


def _default_fetch_html(url: str) -> str:
    return fetch_text(url, user_agent=_USER_AGENT, timeout=_REQUEST_TIMEOUT_SECONDS)


def _parse_page(html: str) -> list[dict]:
    tiles_match = _PRODUCT_TILES_RE.search(html)
    if tiles_match is None:
        raise ValueError("land_sea_collection page has no window.page.productTiles block")
    try:
        return json.loads(tiles_match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("land_sea_collection productTiles block is not valid JSON") from exc


def _tile_image_url(tile: dict, store_origin: str) -> str | None:
    for key in _IMAGE_KEYS:
        raw = tile.get(key)
        candidate: str | None = None
        if isinstance(raw, str) and raw.strip():
            candidate = raw.strip()
        elif isinstance(raw, dict):
            for sub_key in ("src", "url", "href"):
                value = raw.get(sub_key)
                if isinstance(value, str) and value.strip():
                    candidate = value.strip()
                    break
        if candidate:
            # A tile URL may be relative (like productUrl); absolute URLs pass through urljoin
            # unchanged, so this never invents a host.
            return urljoin(store_origin, candidate)
    return None


def _tile_key(tile: object) -> object:
    """A tile's product id, or the tile's own identity when it has none, so a malformed tile is
    never mistaken for a repeat and still reaches _to_raw_item's validation."""
    if isinstance(tile, dict) and tile.get("id") is not None:
        return str(tile["id"])
    return id(tile)


def _to_raw_item(tile: dict, store_origin: str) -> RawItem:
    if not isinstance(tile, dict):
        raise ValueError("product tile entry must be an object")
    product_id = tile.get("id")
    product_url = tile.get("productUrl")
    name = tile.get("name")
    if product_id is None or not product_url or not name:
        raise ValueError("product tile needs an 'id', 'productUrl', and 'name'")
    try:
        price = float(tile["price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("product tile has no usable 'price'") from exc
    try:
        price_was = float(tile.get("priceWas") or 0)
    except (TypeError, ValueError):
        price_was = 0.0
    compare_at_price = price_was if price_was > price else None
    return RawItem(
        external_id=str(product_id),
        title=name,
        url=urljoin(store_origin, product_url),
        body="",
        # Unlike Shopify's product JSON, a productTiles entry carries no created/published
        # timestamp at all.
        created_at=None,
        raw_metadata={
            "price": price,
            "compare_at_price": compare_at_price,
            "on_sale": compare_at_price is not None,
            "available": bool(tile.get("isAvailable")),
            "vendor": tile.get("brandName") or None,
            "product_type": None,
            "tags": [],
            "image_url": _tile_image_url(tile, store_origin),
        },
    )


class LandSeaCollectionConnector:
    type_key = "land_sea_collection"
    supported_channel_kinds = frozenset({ChannelKind.MONITOR})

    def __init__(self, fetch_html: HtmlFetcher = _default_fetch_html):
        self._fetch_html = fetch_html

    def validate_config(self, config: dict) -> None:
        url = config.get("collection_url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("land_sea_collection config needs a non-empty 'collection_url' key")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "land_sea_collection config needs 'collection_url' to be a valid http(s) URL"
            )

    def fetch(self, config: dict) -> list[RawItem]:
        self.validate_config(config)
        parsed = urlparse(config["collection_url"].rstrip("/"))
        store_origin = f"{parsed.scheme}://{parsed.netloc}"
        base_params = dict(parse_qsl(parsed.query))

        def fetch_page(number: int) -> Page[dict]:
            params = {**base_params, "pgNmbr": str(number)}
            page_url = f"{store_origin}{parsed.path}?{urlencode(params)}"
            page_tiles = _parse_page(self._fetch_html(page_url))
            # window.page.totalPagesJs is not trusted: page 1 has reported 83 pages while every
            # later page reported 8, and pages past the end repeat the last one. The end is a
            # short page, or (via the tile key) a page that adds no new product.
            return Page(items=page_tiles, has_more=len(page_tiles) >= _PAGE_SIZE)

        tiles = collect_pages(
            fetch_page,
            max_pages=_MAX_PAGES,
            label="Land & Sea collection",
            key=_tile_key,
        )

        items = []
        for index, tile in enumerate(tiles):
            try:
                items.append(_to_raw_item(tile, store_origin))
            except Exception as exc:
                print(f"[land_sea_collection] skipping product index={index}: {exc}")
        if tiles and not items:
            raise RuntimeError("land_sea_collection page returned no usable products")
        return items


register(LandSeaCollectionConnector())
