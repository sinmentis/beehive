"""Second 'monitor' Channel Source, for storefronts that are NOT Shopify. Confirmed against
www.land-sea.co.nz (NZ outdoor/fishing retailer running "N2 ERP" by First Software) -- unlike
Shopify, N2 ERP exposes no unauthenticated products.json API. But its product-listing pages
(e.g. /sale, /outlet) still render a plain, unauthenticated HTTP response (no bot-blocking on
User-Agent observed) with the current page's products embedded as a JS literal assignment:
    window.page.productTiles = [{"id":..., "name":..., "price":..., "priceWas":..., ...}, ...];
That JS array is valid JSON, so this connector regexes it out of the HTML and json.loads it --
still no headless browser needed (Chromium's memory footprint is a bad fit for this host's
512M container limit, especially right after an unrelated OOM/freeze scare on the same box).

Pagination is a `?pgNmbr=N` query param (confirmed by reading the site's own minified JS); the
same response also tells us `window.page.totalPagesJs`, an authoritative page count Shopify's
API never gives us, so -- unlike shopify_collection's pure "did we get a short page back"
guess -- this connector can stop exactly at the real last page.

A full /sale or /outlet listing runs to ~76 pages (~1,976 products) -- a different order of
magnitude from the Shopify stores' clearance-only collections (a few hundred at most), so
_MAX_PAGES bounds worst-case cost the same way shopify_collection's cap does, but the more
useful lever for a large storefront like this is scope, not depth: the site's own on-page
Filters panel (checkboxes rendered with e.g. `name="brands" value="85"`) narrows /sale or
/outlet down server-side to a `?brands=85` or `?categories=2` (comma-joined for multiple)
URL -- confirmed live (?brands=85 on /sale: 5 pages instead of 76, all Patagonia). Pointing a
Source's collection_url at one of those filtered URLs (copied straight from the site's address
bar after applying filters there) tracks that slice completely, well within _MAX_PAGES, instead
of relying on this cap to sample an arbitrary chunk of an unfiltered, unbounded catalog. This
connector doesn't need to know brands/categories are the filter mechanism -- it just always
preserves whatever query string the configured collection_url already carries, adding only
pgNmbr for pagination.

Tests inject a fake fetch_html and never touch the network."""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from beehive.connectors.base import RawItem
from beehive.connectors.registry import register
from beehive.domain.channels import ChannelKind

_USER_AGENT = "beehive/0.1 (personal information hub)"
_REQUEST_TIMEOUT_SECONDS = 20
_PAGE_SIZE = 26
# See module docstring: this is a cost/safety cap for an unfiltered listing, not the expected
# common case -- a Source scoped with ?brands=/?categories= should finish well under it.
_MAX_PAGES = 10

_PRODUCT_TILES_RE = re.compile(r"window\.page\.productTiles\s*=\s*(\[.*?\]);", re.DOTALL)
_TOTAL_PAGES_RE = re.compile(r"window\.page\.totalPagesJs\s*=\s*(\d+)")
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
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(  # noqa: S310 (module only ever builds http(s) URLs)
        request,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    ) as response:
        return response.read().decode("utf-8", errors="replace")


def _parse_page(html: str) -> tuple[list[dict], int | None]:
    tiles_match = _PRODUCT_TILES_RE.search(html)
    if tiles_match is None:
        raise ValueError("land_sea_collection page has no window.page.productTiles block")
    try:
        tiles = json.loads(tiles_match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("land_sea_collection productTiles block is not valid JSON") from exc
    total_pages_match = _TOTAL_PAGES_RE.search(html)
    total_pages = int(total_pages_match.group(1)) if total_pages_match else None
    return tiles, total_pages


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

        tiles: list[dict] = []
        page = 1
        max_pages = _MAX_PAGES
        while page <= max_pages:
            params = {**base_params, "pgNmbr": str(page)}
            page_url = f"{store_origin}{parsed.path}?{urlencode(params)}"
            page_tiles, total_pages = _parse_page(self._fetch_html(page_url))
            if page == 1 and total_pages is not None:
                max_pages = min(_MAX_PAGES, total_pages)
            if not page_tiles:
                break
            tiles.extend(page_tiles)
            if len(page_tiles) < _PAGE_SIZE:
                break
            page += 1

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
