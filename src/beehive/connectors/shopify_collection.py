"""First 'monitor' Channel Source: watches one collection page of any storefront running on
Shopify (confirmed against arcteryx.co.nz, bivouac.co.nz, and furtherfaster.co.nz -- three NZ
outdoor retailers whose "outlet"/"sale" collections are plain, unauthenticated Shopify storefronts).
Shopify exposes every collection's contents as JSON for free at
<collection_url>/products.json -- no scraping, no headless browser, no API key.

Unlike editorial connectors (each fetched item is either new-to-us or already-seen), a price
monitor's interesting signal IS a state change on an already-seen product. Rather than adding a
second table to track that, external_id encodes the product's *current lowest variant price*
(f"{product_id}:{price}") -- so insert_new's existing UNIQUE(source_id, external_id) dedup
already gives us exactly the semantics run_cycle.py's monitor-kind skip comment describes
("track deterministic state changes"): a product entering the collection for the first time,
OR an already-seen product's price moving to a value we haven't recorded before, both look like
a fresh row; a re-fetch at an unchanged price is silently ignored, same as it already is for
every other connector. Downstream alerting on top of that (e.g. "tell me when a NEW row lands
with on_sale=True") is intentionally left for a later step -- this connector's only job is
fetch-and-normalize.

Tests inject a fake fetch_json and never touch the network."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from beehive.connectors.base import RawItem
from beehive.connectors.registry import register

_USER_AGENT = "beehive/0.1 (personal information hub)"
_REQUEST_TIMEOUT_SECONDS = 20
_PAGE_SIZE = 250
# A clearance/outlet collection is a small slice of a store's catalog in practice; this caps
# worst case at 1,000 products/cycle rather than trusting an unbounded storefront to stay small.
_MAX_PAGES = 4

JsonFetcher = Callable[[str], Any]


def _default_fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(  # noqa: S310 (module only ever builds http(s) URLs)
        request,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    ) as response:
        payload = response.read()
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
        external_id=f"{product_id}:{summary['price']:.2f}",
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
        },
    )


class ShopifyCollectionConnector:
    type_key = "shopify_collection"

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

    def fetch(self, config: dict) -> list[RawItem]:
        self.validate_config(config)
        collection_url = config["collection_url"].rstrip("/")
        parsed = urlparse(collection_url)
        store_origin = f"{parsed.scheme}://{parsed.netloc}"

        products: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            params = urlencode({"limit": _PAGE_SIZE, "page": page})
            payload = self._fetch_json(f"{collection_url}/products.json?{params}")
            if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
                raise ValueError("Shopify collection response needs a 'products' list")
            page_products = payload["products"]
            if not page_products:
                break
            products.extend(page_products)
            if len(page_products) < _PAGE_SIZE:
                break

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
