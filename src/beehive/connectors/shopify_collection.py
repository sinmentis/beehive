"""First 'monitor' Channel Source: watches one collection page of any storefront running on
Shopify (confirmed against arcteryx.co.nz, bivouac.co.nz, and furtherfaster.co.nz -- three NZ
outdoor retailers whose "outlet"/"sale" collections are plain, unauthenticated Shopify storefronts).
Shopify exposes every collection's contents as JSON for free at
<collection_url>/products.json -- no scraping, no headless browser, no API key.

Unlike editorial connectors (each fetched item is either new-to-us or already-seen), a price
monitor's interesting signal IS a state change on an already-seen product. external_id is the
product's stable Shopify id, so the same listing keeps one Item row across cycles; its current
price/availability live in raw_metadata and are refreshed in place by the mutable-snapshot
persistence path (db/items.py's upsert_mutable_item), which is also where a price move or a
return to stock is turned into a deliverable event. This connector's only job is
fetch-and-normalize -- it does not itself decide what changed. The lowest current variant price,
whether it is on sale, and whether any variant is available are captured for that downstream
comparison, along with a best-effort image_url for presentation.

Optional config key 'vendors': a list of brand names to keep, matched case-insensitively
against each product's 'vendor' field. This filtering happens locally, AFTER fetching every
page of the collection: Shopify's public products.json feed has no server-side vendor/
product_type/tag filter of its own (confirmed empirically -- storefront filter-widget query
params and #fragments the shopper's browser understands are silently ignored by this endpoint,
which only recognises 'limit'/'page'), so there is no URL to build that would filter upstream.

Tests inject a fake fetch_json and never touch the network."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from beehive.connectors.base import RawItem
from beehive.connectors.registry import register
from beehive.domain.channels import ChannelKind

_USER_AGENT = "beehive/0.1 (personal information hub)"
_REQUEST_TIMEOUT_SECONDS = 20
_REQUEST_ATTEMPTS = 3
_PAGE_SIZE = 250
# A clearance/outlet collection is a small slice of a store's catalog in practice; this caps
# worst case at 1,000 products/cycle rather than trusting an unbounded storefront to stay small.
_MAX_PAGES = 4

JsonFetcher = Callable[[str], Any]


def _default_fetch_json(url: str) -> Any:
    for attempt in range(1, _REQUEST_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(  # noqa: S310 (only builds http(s) URLs)
                request,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                payload = response.read()
            break
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if not retryable or attempt == _REQUEST_ATTEMPTS:
                raise
            exc.close()
            time.sleep(attempt)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Shopify collection returned invalid JSON from {url}") from exc


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _variant_summary(variants: list) -> dict[str, Any]:
    prices: list[float] = []
    compare_prices: list[float] = []
    available = False
    for variant in variants if isinstance(variants, list) else []:
        if not isinstance(variant, dict):
            continue
        try:
            price = float(variant["price"])
        except (KeyError, TypeError, ValueError):
            continue
        prices.append(price)
        try:
            compare_price = float(variant.get("compare_at_price") or 0)
        except (TypeError, ValueError):
            compare_price = 0.0
        if compare_price > price:
            compare_prices.append(compare_price)
        if variant.get("available"):
            available = True
    if not prices:
        raise ValueError("product has no variant with a usable price")
    lowest_price = min(prices)
    return {
        "price": lowest_price,
        "compare_at_price": max(compare_prices) if compare_prices else None,
        "on_sale": bool(compare_prices),
        "available": available,
    }


def _image_url_from_candidate(value: Any) -> str | None:
    """A usable image URL from one Shopify image shape, or None. Shopify returns images either as
    objects carrying a 'src' URL or, in some feeds, as a bare URL string; anything else yields
    None rather than a guessed URL."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        src = value.get("src")
        if isinstance(src, str) and src.strip():
            return src.strip()
    return None


def _extract_image_url(product: dict) -> str | None:
    images = product.get("images")
    if isinstance(images, list):
        for image in images:
            url = _image_url_from_candidate(image)
            if url:
                return url
    for key in ("image", "featured_image"):
        url = _image_url_from_candidate(product.get(key))
        if url:
            return url
    return None


def _to_raw_item(product: dict, store_origin: str) -> RawItem:
    if not isinstance(product, dict):
        raise ValueError("product entry must be an object")
    product_id = product.get("id")
    handle = product.get("handle")
    title = product.get("title")
    if product_id is None or not handle or not title:
        raise ValueError("product needs an 'id', 'handle', and 'title'")
    summary = _variant_summary(product.get("variants"))
    tags = product.get("tags")
    return RawItem(
        external_id=str(product_id),
        title=title,
        url=f"{store_origin}/products/{handle}",
        body="",
        created_at=_parse_timestamp(product.get("published_at") or product.get("created_at")),
        raw_metadata={
            "price": summary["price"],
            "compare_at_price": summary["compare_at_price"],
            "on_sale": summary["on_sale"],
            "available": summary["available"],
            "vendor": product.get("vendor"),
            "product_type": product.get("product_type"),
            "tags": tags if isinstance(tags, list) else [],
            "image_url": _extract_image_url(product),
        },
    )


class ShopifyCollectionConnector:
    type_key = "shopify_collection"
    supported_channel_kinds = frozenset({ChannelKind.MONITOR})

    def __init__(self, fetch_json: JsonFetcher = _default_fetch_json):
        self._fetch_json = fetch_json

    def validate_config(self, config: dict) -> None:
        url = config.get("collection_url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("shopify_collection config needs a non-empty 'collection_url' key")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "shopify_collection config needs 'collection_url' to be a valid http(s) URL"
            )
        vendors = config.get("vendors")
        if vendors is not None and (
            not isinstance(vendors, list)
            or not all(isinstance(vendor, str) and vendor.strip() for vendor in vendors)
        ):
            raise ValueError(
                "shopify_collection config's 'vendors', if present, must be a list of "
                "non-empty brand name strings"
            )

    def fetch(self, config: dict) -> list[RawItem]:
        self.validate_config(config)
        # Parse rather than string-concatenate: collection_url may carry a storefront filter
        # widget's query string or #fragment (copied from the browser's address bar the same way
        # a land_sea_collection filtered URL is), and naively appending "/products.json?..." to
        # the raw string would fold that suffix into the existing query/fragment instead of
        # reaching the JSON endpoint. Any such filter params are dropped here rather than merged
        # in: Shopify's /products.json feed is a fixed, unfiltered dump of the collection that
        # ignores app-injected filter params regardless, so keeping them would be a no-op at best.
        parsed = urlparse(config["collection_url"].rstrip("/"))
        store_origin = f"{parsed.scheme}://{parsed.netloc}"

        products: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            params = urlencode({"limit": _PAGE_SIZE, "page": page})
            payload = self._fetch_json(f"{store_origin}{parsed.path}/products.json?{params}")
            if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
                raise ValueError("Shopify collection response needs a 'products' list")
            page_products = payload["products"]
            if not page_products:
                break
            products.extend(page_products)
            if len(page_products) < _PAGE_SIZE:
                break

        # Applied once, after every page is in hand, so pagination above still sees the true,
        # unfiltered page sizes -- filtering per-page would make a partial last page look short
        # for the wrong reason and could stop pagination early.
        vendors = config.get("vendors")
        if vendors:
            allowed = {vendor.strip().casefold() for vendor in vendors}
            products = [
                product
                for product in products
                if isinstance(product, dict)
                and str(product.get("vendor") or "").strip().casefold() in allowed
            ]

        items = []
        for index, product in enumerate(products):
            try:
                items.append(_to_raw_item(product, store_origin))
            except Exception as exc:
                print(f"[shopify_collection] skipping product index={index}: {exc}")
        if products and not items:
            raise RuntimeError("Shopify collection returned no usable products")
        return items


register(ShopifyCollectionConnector())
