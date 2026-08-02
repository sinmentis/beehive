"""Deep-clearance snapshots from international designer retailers.

One Source type keeps the admin and collector interface small while private retailer adapters
hide four unrelated storefront implementations. Every adapter returns only products whose
retailer-owned price history proves a deep markdown; a successful fetch is the complete current
set for that retailer and threshold, so pagination caps fail with TruncatedSnapshotError instead
of reconciling a partial catalog.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode

from beehive.connectors.base import RawItem, TruncatedSnapshotError, as_utc
from beehive.connectors.http import (
    fetch_json,
    fetch_text,
    post_json,
    post_json_browser_tls,
)
from beehive.connectors.registry import register
from beehive.domain.channels import ChannelKind

RETAILER_LABELS = {
    "the_outnet": "THE OUTNET",
    "mytheresa": "Mytheresa",
    "end": "END.",
    "yoox": "YOOX",
}

_DEFAULT_MINIMUM_DISCOUNT_PERCENT = 70
_MINIMUM_DISCOUNT_PERCENT = 50
_MAXIMUM_DISCOUNT_PERCENT = 90

_USER_AGENT = "beehive/0.1 (personal information hub)"
_TIMEOUT_SECONDS = 45
_REQUEST_ATTEMPTS = 3

_END_BOOTSTRAP_URL = "https://www.endclothing.com/nz/sale/all-sale"
_END_BOOTSTRAP_HOSTS = frozenset({"www.endclothing.com"})
_END_WEBSITE_ID = 16
_END_CURRENCY = "NZD"
_END_PAGE_SIZE = 120
_END_MAX_PAGES = 100
_END_IMAGE_PREFIX = (
    "https://media.endclothing.com/media/"
    "f_auto,q_auto:eco,w_400,h_400/prodmedia/media/catalog/product"
)
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

_OUTNET_API_URL = "https://theoutnet.com/api/2025-07/graphql.json"
_OUTNET_HOSTS = frozenset({"theoutnet.com"})
# Shopify Storefront tokens are public, read-only browser credentials by design.
_OUTNET_STOREFRONT_TOKEN = "d4d34c96cdbdbbc811dfeaa0546c3b13"
_OUTNET_CURRENCY = "AUD"
_OUTNET_PAGE_SIZE = 250
_OUTNET_VARIANT_PAGE_SIZE = 250
_OUTNET_MAX_PAGES = 30
_OUTNET_QUERY = """
query InternationalClearance(
  $after: String,
  $pageSize: Int!,
  $variantPageSize: Int!
)
@inContext(country: NZ, language: EN) {
  collection(handle: "sale") {
    products(
      first: $pageSize,
      after: $after,
      filters: [{available: true}]
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        handle
        title
        vendor
        productType
        tags
        availableForSale
        featuredImage {
          url
        }
        variants(first: $variantPageSize) {
          pageInfo {
            hasNextPage
          }
          nodes {
            id
            availableForSale
            price {
              amount
              currencyCode
            }
            compareAtPrice {
              amount
              currencyCode
            }
          }
        }
      }
    }
  }
}
""".strip()

_MYTHERESA_API_URL = "https://api.mytheresa.com/api"
_MYTHERESA_HOSTS = frozenset({"api.mytheresa.com"})
_MYTHERESA_STORE = "EURO"
_MYTHERESA_COUNTRY = "NZ"
_MYTHERESA_CURRENCY = "EUR"
_MYTHERESA_SECTIONS = ("women", "men", "kids")
_MYTHERESA_TIERS = (50, 60, 70)
_MYTHERESA_PAGE_SIZE = 60
_MYTHERESA_MAX_PAGES = 100
_MYTHERESA_SEMANTIC_ATTEMPTS = 20
_MYTHERESA_QUERY = """
query InternationalClearance(
  $page: Int,
  $size: Int,
  $slug: String,
  $filtersQueryParams: String
) {
  xProductListingPageV2(
    page: $page,
    size: $size,
    slug: $slug,
    filtersQueryParams: $filtersQueryParams
  ) {
    pagination {
      currentPage
      itemsPerPage
      totalItems
      totalPages
    }
    products {
      sku
      slug
      name
      description
      designer
      combinedCategoryName
      department
      mainWaregroup
      displayImages
      enabled
      hasStock
      isPurchasable
      labels
      promotionLabels {
        label
        type
      }
      price {
        currencyCode
        original
        discount
        percentage
      }
    }
  }
}
""".strip()

_YOOX_API_URL = (
    "https://w4870fbrwz-dsn.algolia.net/1/indexes/INDEX_YOOX_NZ_Products"
)
_YOOX_HOSTS = frozenset({"w4870fbrwz-dsn.algolia.net"})
_YOOX_APPLICATION_ID = "W4870FBRWZ"
# Public, read-only search key shipped by the YOOX NZ storefront since 2023.
_YOOX_SEARCH_KEY = "595c76d0b931d66eee711043447ad35b"
_YOOX_PAGE_SIZE = 1000
_YOOX_MAX_PAGES_PER_SLICE = 20
_YOOX_MAX_SLICE_DEPTH = 12
_YOOX_OTHER_DEPARTMENTS = (
    "Clearance_M",
    "salegirl_kid",
    "salegirl_junior",
    "salegirl_baby",
    "saleboy_kid",
    "saleboy_junior",
    "saleboy_baby",
    "saledesign",
)
_YOOX_WOMEN_PRICE_BANDS = (
    (None, 35.0),
    (35.0, 50.0),
    (50.0, 60.0),
    (60.0, 80.0),
    (80.0, 100.0),
    (100.0, 120.0),
    (120.0, 160.0),
    (160.0, 220.0),
    (220.0, 300.0),
    (300.0, 500.0),
    (500.0, 1000.0),
    (1000.0, None),
)
_YOOX_ATTRIBUTES = (
    "objectID",
    "variantId",
    "model",
    "modelBestOfferByPrice",
    "images",
    "availableSizes",
    "availableColors",
    "colorLabel",
    "purchasable",
    "visible",
    "preowned",
    "has3rdParty",
    "promoTags",
    "saleLine",
    "composition",
)
_YOOX_REFERENCE_SOURCES = frozenset({"Supplier", "PriceTag"})

JsonGetter = Callable[..., Any]
TextGetter = Callable[..., str]
JsonPoster = Callable[..., Any]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive_number(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _positive_decimal(value: object) -> float | None:
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    return _positive_number(value)


def _reported_percent(value: object) -> float | None:
    if isinstance(value, str):
        value = value.strip().removesuffix("%")
        try:
            value = float(value)
        except ValueError:
            return None
    return _number(value)


def _discount_percent(price: float, compare_at_price: float) -> float:
    return (1.0 - price / compare_at_price) * 100.0


def _meets_discount(
    price: float,
    compare_at_price: float,
    minimum_percent: int,
) -> bool:
    if compare_at_price <= price:
        return False
    actual = _discount_percent(price, compare_at_price)
    # Retailers round display prices independently from their percentage badge.
    return actual >= minimum_percent - 0.5


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _first_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            text = _first_string(item)
            if text:
                return text
    return None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return as_utc(datetime.fromisoformat(value.strip()))
    except ValueError:
        return None


def _append_until(
    items: dict[str, RawItem],
    item: RawItem | None,
    *,
    limit: int | None,
) -> bool:
    if item is not None:
        items[item.external_id] = item
    return limit is not None and len(items) >= limit


def _outnet_item(node: dict[str, Any], minimum_percent: int) -> RawItem | None:
    external_id = node.get("id")
    handle = node.get("handle")
    title = node.get("title")
    if not isinstance(external_id, str) or not external_id:
        raise ValueError("missing product id")
    if not isinstance(handle, str) or not handle:
        raise ValueError("missing handle")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("missing title")
    if not node.get("availableForSale"):
        return None
    variants = node.get("variants")
    if not isinstance(variants, dict):
        raise ValueError("missing variants")
    page_info = variants.get("pageInfo")
    if not isinstance(page_info, dict) or not isinstance(
        page_info.get("hasNextPage"),
        bool,
    ):
        raise ValueError("missing variant page information")
    if page_info["hasNextPage"]:
        raise TruncatedSnapshotError("THE OUTNET product has more than 250 variants")
    variant_nodes = variants.get("nodes")
    if not isinstance(variant_nodes, list):
        raise ValueError("missing variant nodes")

    candidates: list[tuple[float, float, float, str]] = []
    for variant in variant_nodes:
        if not isinstance(variant, dict) or not variant.get("availableForSale"):
            continue
        price_data = variant.get("price")
        compare_data = variant.get("compareAtPrice")
        if not isinstance(price_data, dict) or not isinstance(compare_data, dict):
            continue
        price = _positive_decimal(price_data.get("amount"))
        compare_at_price = _positive_decimal(compare_data.get("amount"))
        if price is None or compare_at_price is None:
            continue
        if (
            price_data.get("currencyCode") != _OUTNET_CURRENCY
            or compare_data.get("currencyCode") != _OUTNET_CURRENCY
        ):
            raise ValueError("unexpected currency")
        if not _meets_discount(price, compare_at_price, minimum_percent):
            continue
        variant_id = variant.get("id")
        if not isinstance(variant_id, str) or not variant_id:
            raise ValueError("missing variant id")
        candidates.append(
            (
                _discount_percent(price, compare_at_price),
                -price,
                compare_at_price,
                variant_id,
            )
        )
    if not candidates:
        return None
    discount_percent, negative_price, compare_at_price, variant_id = max(candidates)
    price = -negative_price

    image = node.get("featuredImage")
    image = image if isinstance(image, dict) else {}
    return RawItem(
        external_id=external_id,
        title=title.strip(),
        url=f"https://theoutnet.com/en-nz/products/{handle}",
        raw_metadata={
            "price": price,
            "compare_at_price": compare_at_price,
            "discount_percent": discount_percent,
            "on_sale": True,
            "available": True,
            "variant_id": variant_id,
            "vendor": _first_string(node.get("vendor")),
            "product_type": _first_string(node.get("productType")),
            "tags": _string_list(node.get("tags")),
            "image_url": _first_string(image.get("url")),
            "currency_code": _OUTNET_CURRENCY,
            "retailer": "the_outnet",
        },
    )


def _fetch_outnet(
    post: JsonPoster,
    minimum_percent: int,
    *,
    limit: int | None,
) -> list[RawItem]:
    items: dict[str, RawItem] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for page in range(_OUTNET_MAX_PAGES):
        response = post(
            _OUTNET_API_URL,
            {
                "query": _OUTNET_QUERY,
                "variables": {
                    "after": cursor,
                    "pageSize": _OUTNET_PAGE_SIZE,
                    "variantPageSize": _OUTNET_VARIANT_PAGE_SIZE,
                },
            },
            allowed_hosts=_OUTNET_HOSTS,
            user_agent=_USER_AGENT,
            timeout=_TIMEOUT_SECONDS,
            max_attempts=_REQUEST_ATTEMPTS,
            extra_headers={
                "X-Shopify-Storefront-Access-Token": _OUTNET_STOREFRONT_TOKEN,
            },
        )
        errors = response.get("errors") if isinstance(response, dict) else None
        if errors:
            raise ValueError(f"THE OUTNET GraphQL error: {errors}")
        try:
            context = response["extensions"]["context"]
            products = response["data"]["collection"]["products"]
            page_info = products["pageInfo"]
            nodes = products["nodes"]
        except (KeyError, TypeError) as exc:
            raise ValueError("THE OUTNET Storefront response schema changed") from exc
        if context.get("country") != "NZ":
            raise ValueError("THE OUTNET Storefront did not return New Zealand context")
        if not isinstance(nodes, list):
            raise ValueError("THE OUTNET Storefront response needs a nodes list")
        for index, node in enumerate(nodes):
            try:
                item = _outnet_item(node, minimum_percent)
            except TruncatedSnapshotError:
                raise
            except Exception as exc:
                print(
                    "[international_clearance:the_outnet] skipping product "
                    f"page={page} index={index}: {exc}"
                )
                continue
            if _append_until(items, item, limit=limit):
                return list(items.values())

        has_next_page = page_info.get("hasNextPage")
        if not isinstance(has_next_page, bool):
            raise ValueError("THE OUTNET Storefront response needs hasNextPage")
        if not has_next_page:
            return list(items.values())
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise ValueError("THE OUTNET Storefront response needs an end cursor")
        if cursor in seen_cursors:
            raise TruncatedSnapshotError(
                "THE OUTNET Storefront repeated a pagination cursor"
            )
        seen_cursors.add(cursor)

    raise TruncatedSnapshotError(
        f"THE OUTNET sale exceeded the {_OUTNET_MAX_PAGES}-page cap"
    )


def _end_bootstrap(fetch_html: TextGetter) -> dict[str, Any]:
    html = fetch_html(
        _END_BOOTSTRAP_URL,
        allowed_hosts=_END_BOOTSTRAP_HOSTS,
        user_agent=_USER_AGENT,
        timeout=_TIMEOUT_SECONDS,
        max_attempts=_REQUEST_ATTEMPTS,
        extra_headers={"Accept-Language": "en-NZ,en;q=0.9"},
    )
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        raise ValueError("END. storefront response needs __NEXT_DATA__")
    try:
        data = json.loads(match.group(1))
        environment = data["runtimeConfig"]["env"]
        config = data["props"]["initialState"]["config"]
        provider = config["catalog"]["providers"][0]
        country = config["country"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("END. storefront bootstrap schema changed") from exc

    if country.get("website_id") != _END_WEBSITE_ID:
        raise ValueError("END. storefront did not return the New Zealand website")
    if country.get("currency_code") != _END_CURRENCY:
        raise ValueError("END. storefront did not return NZD prices")

    hosts = [
        host.strip()
        for host in str(environment.get("ALGOLIA_HOSTS") or "").split(",")
        if host.strip()
    ]
    app_id = environment.get("ALGOLIA_ID")
    api_key = environment.get("ALGOLIA_KEY")
    index_name = provider.get("products_index")
    if not hosts or not all(
        isinstance(value, str) and value
        for value in (app_id, api_key, index_name)
    ):
        raise ValueError("END. storefront bootstrap is missing Algolia configuration")
    if not all(host.endswith(".endclothing.com") for host in hosts):
        raise ValueError("END. storefront returned an unexpected Algolia host")
    return {
        "hosts": tuple(hosts),
        "app_id": app_id,
        "api_key": api_key,
        "index_name": index_name,
    }


def _end_query_params(page: int, category: str) -> str:
    attributes = [
        "objectID",
        "sku",
        "name",
        "brand",
        "url_key",
        "small_image",
        "categories",
        "department_hierarchy",
        "CategoryV1",
        "DepartmentV1",
        "gender",
        "sale_percentage",
        "promotion",
        "stock",
        "size",
        "status",
        "for_sale_online",
        "websites_available_at",
        "full_price_16",
        "final_price_16",
        "created_at",
    ]
    return urlencode(
        {
            "page": page,
            "hitsPerPage": _END_PAGE_SIZE,
            "facets": "[]",
            "analytics": "false",
            "clickAnalytics": "false",
            "ruleContexts": json.dumps(
                ["browse", "web", "v3", "nz", "NZ", "sale"],
                separators=(",", ":"),
            ),
            "facetFilters": json.dumps(
                [
                    [f"categories:{category}"],
                    [f"websites_available_at:{_END_WEBSITE_ID}"],
                ],
                separators=(",", ":"),
            ),
            "numericFilters": json.dumps(
                ["stock>0", "status=1", "for_sale_online=1"],
                separators=(",", ":"),
            ),
            "attributesToRetrieve": json.dumps(attributes, separators=(",", ":")),
            "attributesToHighlight": "[]",
        }
    )


def _end_item(hit: dict[str, Any], minimum_percent: int) -> RawItem | None:
    external_id = hit.get("objectID")
    title = hit.get("name")
    url_key = hit.get("url_key")
    price = _positive_number(hit.get("final_price_16"))
    compare_at_price = _positive_number(hit.get("full_price_16"))
    reported = _reported_percent(hit.get("sale_percentage"))
    if not isinstance(external_id, str) or not external_id:
        raise ValueError("missing objectID")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("missing name")
    if not isinstance(url_key, str) or not url_key.strip():
        raise ValueError("missing url_key")
    if price is None or compare_at_price is None:
        raise ValueError("missing NZD price pair")
    if not _meets_discount(
        price,
        compare_at_price,
        minimum_percent,
    ):
        return None
    if (
        _number(hit.get("stock")) is None
        or float(hit["stock"]) <= 0
        or hit.get("status") != 1
        or hit.get("for_sale_online") != 1
        or _END_WEBSITE_ID not in (hit.get("websites_available_at") or [])
    ):
        return None

    categories = _string_list(hit.get("categories"))
    hierarchy = _string_list(hit.get("department_hierarchy"))
    promotion = hit.get("promotion") if isinstance(hit.get("promotion"), dict) else {}
    tags = categories + hierarchy
    tags.extend(
        text
        for text in (
            _first_string(hit.get("gender")),
            _first_string(promotion.get("label")),
            _first_string(promotion.get("short_promo_label")),
            _first_string(hit.get("sale_percentage")),
        )
        if text
    )
    product_type = (
        _first_string(hit.get("DepartmentV1"))
        or _first_string(hit.get("CategoryV1"))
        or (hierarchy[-1] if hierarchy else None)
    )
    image_path = _first_string(hit.get("small_image"))
    image_url = (
        f"{_END_IMAGE_PREFIX}{image_path}"
        if image_path and image_path != "no_selection"
        else None
    )
    return RawItem(
        external_id=external_id,
        title=title.strip(),
        url=f"https://www.endclothing.com/nz/{url_key.strip()}.html",
        body=_first_string(promotion.get("label")) or "",
        created_at=_parse_timestamp(hit.get("created_at")),
        raw_metadata={
            "price": price,
            "compare_at_price": compare_at_price,
            "discount_percent": _discount_percent(price, compare_at_price),
            "reported_discount_percent": reported,
            "on_sale": True,
            "available": True,
            "vendor": _first_string(hit.get("brand")),
            "product_type": product_type,
            "tags": list(dict.fromkeys(tags)),
            "image_url": image_url,
            "currency_code": _END_CURRENCY,
            "retailer": "end",
        },
    )


def _fetch_end(
    fetch_html: TextGetter,
    post: JsonPoster,
    minimum_percent: int,
    *,
    limit: int | None,
) -> list[RawItem]:
    bootstrap = _end_bootstrap(fetch_html)
    category = (
        "Sale / Over 60% off"
        if minimum_percent >= 60
        else "Sale / All Sale"
    )
    items: dict[str, RawItem] = {}
    total_pages: int | None = None
    total_hits: int | None = None
    for page in range(_END_MAX_PAGES):
        host = bootstrap["hosts"][page % len(bootstrap["hosts"])]
        auth_query = urlencode(
            {
                "x-algolia-application-id": bootstrap["app_id"],
                "x-algolia-api-key": bootstrap["api_key"],
                "x-algolia-agent": "Algolia for JavaScript (4.24.0); Browser",
            }
        )
        payload = {
            "requests": [
                {
                    "indexName": bootstrap["index_name"],
                    "params": _end_query_params(page, category),
                }
            ]
        }
        response = post(
            f"https://{host}/1/indexes/*/queries?{auth_query}",
            payload,
            content_type="application/x-www-form-urlencoded",
            allowed_hosts=frozenset(bootstrap["hosts"]),
            user_agent=_USER_AGENT,
            timeout=_TIMEOUT_SECONDS,
            max_attempts=_REQUEST_ATTEMPTS,
            extra_headers={
                "Origin": "https://www.endclothing.com",
                "Referer": "https://www.endclothing.com/",
            },
        )
        try:
            result = response["results"][0]
            hits = result["hits"]
            page_count = int(result["nbPages"])
            hit_count = int(result["nbHits"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("END. Algolia response schema changed") from exc
        if not isinstance(hits, list):
            raise ValueError("END. Algolia response needs a hits list")
        if total_pages is None:
            total_pages = page_count
            total_hits = hit_count
            if hit_count > total_pages * _END_PAGE_SIZE:
                raise TruncatedSnapshotError(
                    f"END. Algolia exposes {hit_count} hits but only "
                    f"{total_pages * _END_PAGE_SIZE} retrievable positions"
                )
            if total_pages > _END_MAX_PAGES:
                raise TruncatedSnapshotError(
                    f"END. sale has {total_pages} pages, above the {_END_MAX_PAGES}-page cap"
                )
        elif page_count != total_pages or hit_count != total_hits:
            raise ValueError("END. pagination changed during the fetch")
        expected_hits = min(
            _END_PAGE_SIZE,
            max(hit_count - page * _END_PAGE_SIZE, 0),
        )
        if len(hits) != expected_hits:
            raise TruncatedSnapshotError(
                f"END. page {page + 1} returned {len(hits)} of "
                f"{expected_hits} expected hits"
            )

        for index, hit in enumerate(hits):
            try:
                item = _end_item(hit, minimum_percent)
            except Exception as exc:
                print(f"[international_clearance:end] skipping product page={page} "
                      f"index={index}: {exc}")
                continue
            if _append_until(items, item, limit=limit):
                return list(items.values())
        if page + 1 >= total_pages:
            return list(items.values())

    raise TruncatedSnapshotError("END. sale pagination exceeded its page cap")


def _mytheresa_item(
    product: dict[str, Any],
    *,
    section: str,
    minimum_percent: int,
) -> RawItem | None:
    sku = product.get("sku")
    slug = product.get("slug")
    title = product.get("name")
    price_data = product.get("price")
    if not isinstance(price_data, dict):
        raise ValueError("missing price")
    price_minor = _positive_number(price_data.get("discount"))
    compare_minor = _positive_number(price_data.get("original"))
    reported = _reported_percent(price_data.get("percentage"))
    if not isinstance(sku, str) or not sku:
        raise ValueError("missing sku")
    if not isinstance(slug, str) or not slug.startswith("/"):
        raise ValueError("missing slug")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("missing name")
    if price_minor is None or compare_minor is None:
        raise ValueError("missing price pair")
    price = price_minor / 100.0
    compare_at_price = compare_minor / 100.0
    if not _meets_discount(
        price,
        compare_at_price,
        minimum_percent,
    ):
        return None
    if not (
        product.get("enabled", True)
        and product.get("hasStock")
        and product.get("isPurchasable")
    ):
        return None
    currency = price_data.get("currencyCode")
    if currency != _MYTHERESA_CURRENCY:
        raise ValueError(f"unexpected currency {currency!r}")

    category = _first_string(product.get("combinedCategoryName"))
    category_parts = [
        part.strip() for part in (category or "").split("::") if part.strip()
    ]
    labels = _string_list(product.get("labels"))
    promotion_labels = product.get("promotionLabels")
    if isinstance(promotion_labels, list):
        labels.extend(
            label
            for entry in promotion_labels
            if isinstance(entry, dict)
            for label in [_first_string(entry.get("label"))]
            if label
        )
    tags = list(dict.fromkeys([section, *category_parts, *labels]))
    images = _string_list(product.get("displayImages"))
    return RawItem(
        external_id=sku,
        title=title.strip(),
        url=f"https://www.mytheresa.com/euro/en/{section}{slug}",
        body=_first_string(product.get("description")) or "",
        raw_metadata={
            "price": price,
            "compare_at_price": compare_at_price,
            "discount_percent": _discount_percent(price, compare_at_price),
            "reported_discount_percent": reported,
            "on_sale": True,
            "available": True,
            "vendor": _first_string(product.get("designer")),
            "product_type": category_parts[-1] if category_parts else None,
            "tags": tags,
            "image_url": images[0] if images else None,
            "currency_code": currency,
            "retailer": "mytheresa",
        },
    )


def _fetch_mytheresa(
    post_browser: JsonPoster,
    minimum_percent: int,
    *,
    limit: int | None,
) -> list[RawItem]:
    tiers = [
        tier
        for tier in _MYTHERESA_TIERS
        if tier + 10 > minimum_percent
    ]
    items: dict[str, RawItem] = {}
    for section in _MYTHERESA_SECTIONS:
        headers = {
            "Accept-Language": "en",
            "Cache-Control": "no-cache",
            "Origin": "https://www.mytheresa.com",
            "Referer": "https://www.mytheresa.com/",
            "X-Store": _MYTHERESA_STORE,
            "X-Country": _MYTHERESA_COUNTRY,
            "X-Nsu": "false",
            "X-Op": "ntr",
            "X-Section": section,
        }
        for tier in tiers:
            total_pages: int | None = None
            total_items: int | None = None
            for page in range(1, _MYTHERESA_MAX_PAGES + 1):
                listing = None
                for semantic_attempt in range(_MYTHERESA_SEMANTIC_ATTEMPTS):
                    cache_query = urlencode(
                        {
                            "section": section,
                            "tier": tier,
                            "page": page,
                            "attempt": semantic_attempt,
                            "requested_at": time.time_ns(),
                        },
                    )
                    response = post_browser(
                        f"{_MYTHERESA_API_URL}?{cache_query}",
                        {
                            "operationName": "InternationalClearance",
                            "variables": {
                                "page": page,
                                "size": _MYTHERESA_PAGE_SIZE,
                                "slug": "/sale/previous-season",
                                "filtersQueryParams": f"reductionRange={tier}",
                            },
                            "query": _MYTHERESA_QUERY,
                        },
                        allowed_hosts=_MYTHERESA_HOSTS,
                        extra_headers=headers,
                        timeout=_TIMEOUT_SECONDS,
                        max_attempts=_REQUEST_ATTEMPTS,
                    )
                    errors = (
                        response.get("errors")
                        if isinstance(response, dict)
                        else None
                    )
                    if errors:
                        raise ValueError(f"Mytheresa GraphQL error: {errors}")
                    try:
                        candidate = response["data"]["xProductListingPageV2"]
                        candidate_products = candidate["products"]
                        candidate_pagination = candidate["pagination"]
                        candidate_page = int(candidate_pagination["currentPage"])
                        candidate_page_size = int(
                            candidate_pagination["itemsPerPage"]
                        )
                        candidate_total_items = int(
                            candidate_pagination["totalItems"]
                        )
                    except (KeyError, TypeError) as exc:
                        raise ValueError(
                            "Mytheresa GraphQL response schema changed"
                        ) from exc
                    except ValueError as exc:
                        raise ValueError(
                            "Mytheresa GraphQL response schema changed"
                        ) from exc
                    if not isinstance(candidate_products, list):
                        raise ValueError("Mytheresa response needs a products list")
                    if candidate_page_size <= 0 or candidate_total_items < 0:
                        raise ValueError("Mytheresa pagination values are invalid")
                    remaining = candidate_total_items - (
                        (page - 1) * candidate_page_size
                    )
                    expected_products = min(
                        candidate_page_size,
                        max(remaining, 0),
                    )
                    if (
                        candidate_page == page
                        and len(candidate_products) == expected_products
                        and all(
                            isinstance(product, dict)
                            and _reported_percent(
                                (product.get("price") or {}).get("percentage")
                            )
                            == tier
                            for product in candidate_products
                        )
                    ):
                        listing = candidate
                        break
                if listing is None:
                    raise RuntimeError(
                        f"Mytheresa ignored the {tier}% filter for {section} "
                        f"page {page} after "
                        f"{_MYTHERESA_SEMANTIC_ATTEMPTS} attempts"
                    )
                try:
                    pagination = listing["pagination"]
                    products = listing["products"]
                    page_count = int(pagination["totalPages"])
                    item_count = int(pagination["totalItems"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("Mytheresa GraphQL response schema changed") from exc
                if not isinstance(products, list):
                    raise ValueError("Mytheresa response needs a products list")
                if total_pages is None:
                    total_pages = page_count
                    total_items = item_count
                    if total_pages > _MYTHERESA_MAX_PAGES:
                        raise TruncatedSnapshotError(
                            f"Mytheresa {section} {tier}% sale has {total_pages} pages, "
                            f"above the {_MYTHERESA_MAX_PAGES}-page cap"
                        )
                elif page_count != total_pages or item_count != total_items:
                    raise ValueError("Mytheresa pagination changed during the fetch")

                for index, product in enumerate(products):
                    try:
                        item = _mytheresa_item(
                            product,
                            section=section,
                            minimum_percent=minimum_percent,
                        )
                    except Exception as exc:
                        print(
                            "[international_clearance:mytheresa] skipping product "
                            f"section={section} tier={tier} page={page} index={index}: {exc}"
                        )
                        continue
                    if _append_until(items, item, limit=limit):
                        return list(items.values())
                if page >= total_pages:
                    break
            else:
                raise TruncatedSnapshotError(
                    f"Mytheresa {section} {tier}% sale pagination exceeded its page cap"
                )
    return list(items.values())


class _YooxSliceOverflow(RuntimeError):
    pass


def _yoox_query_url(
    department: str,
    *,
    lower_price: float | None,
    upper_price: float | None,
    page: int,
) -> str:
    def filter_number(value: float) -> str:
        return f"{value:.12f}".rstrip("0").rstrip(".") or "0"

    numeric_filters = []
    if lower_price is not None:
        numeric_filters.append(
            "modelBestOfferByPrice.currentPrice>="
            f"{filter_number(lower_price)}"
        )
    if upper_price is not None:
        numeric_filters.append(
            "modelBestOfferByPrice.currentPrice<"
            f"{filter_number(upper_price)}"
        )
    params = {
        "facetFilters": json.dumps(
            [[f"departments:{department}"]],
            separators=(",", ":"),
        ),
        "filters": "visible:true AND purchasable:true",
        "enableRules": "false",
        "distinct": "0",
        "hitsPerPage": str(_YOOX_PAGE_SIZE),
        "page": str(page),
        "attributesToRetrieve": json.dumps(
            _YOOX_ATTRIBUTES,
            separators=(",", ":"),
        ),
        "getRankingInfo": "0",
        "analytics": "0",
        "clickAnalytics": "0",
        "enablePersonalization": "false",
    }
    if numeric_filters:
        params["numericFilters"] = json.dumps(
            numeric_filters,
            separators=(",", ":"),
        )
    return f"{_YOOX_API_URL}?{urlencode(params)}"


def _yoox_slice_hits(
    get_json: JsonGetter,
    department: str,
    *,
    lower_price: float | None,
    upper_price: float | None,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    maximum_page_count = 0
    for page in range(_YOOX_MAX_PAGES_PER_SLICE):
        response = get_json(
            _yoox_query_url(
                department,
                lower_price=lower_price,
                upper_price=upper_price,
                page=page,
            ),
            allowed_hosts=_YOOX_HOSTS,
            user_agent=_USER_AGENT,
            timeout=_TIMEOUT_SECONDS,
            max_attempts=_REQUEST_ATTEMPTS,
            extra_headers={
                "X-Algolia-Application-Id": _YOOX_APPLICATION_ID,
                "X-Algolia-API-Key": _YOOX_SEARCH_KEY,
            },
        )
        if not isinstance(response, dict):
            raise ValueError("YOOX Algolia response needs an object")
        message = response.get("message")
        if isinstance(message, str) and "20000" in message:
            raise _YooxSliceOverflow(message)
        hit_count_number = _number(response.get("nbHits"))
        page_count_number = _number(response.get("nbPages"))
        if (
            hit_count_number is None
            or page_count_number is None
            or hit_count_number < 0
            or page_count_number < 0
            or not hit_count_number.is_integer()
            or not page_count_number.is_integer()
        ):
            raise ValueError("YOOX Algolia response needs integer hit and page counts")
        hit_count = int(hit_count_number)
        page_count = int(page_count_number)
        calculated_pages = math.ceil(hit_count / _YOOX_PAGE_SIZE)
        if page_count != calculated_pages:
            raise _YooxSliceOverflow(
                "YOOX Algolia hit count exceeds its retrievable page window"
            )
        if hit_count >= _YOOX_PAGE_SIZE * _YOOX_MAX_PAGES_PER_SLICE:
            raise _YooxSliceOverflow(
                "YOOX slice reached the 20,000-hit retrieval ceiling"
            )
        maximum_page_count = max(maximum_page_count, page_count)
        page_hits = response.get("hits")
        if not isinstance(page_hits, list):
            raise ValueError("YOOX Algolia response needs a hits list")
        expected_hits = min(
            _YOOX_PAGE_SIZE,
            max(hit_count - page * _YOOX_PAGE_SIZE, 0),
        )
        if len(page_hits) != expected_hits:
            raise TruncatedSnapshotError(
                f"YOOX page {page + 1} returned {len(page_hits)} of "
                f"{expected_hits} expected hits"
            )
        hits.extend(page_hits)
        if page + 1 >= maximum_page_count:
            return hits
    raise _YooxSliceOverflow("YOOX slice filled all 20,000 retrievable positions")


def _split_yoox_band(
    lower_price: float | None,
    upper_price: float | None,
) -> tuple[tuple[float | None, float], tuple[float, float | None]]:
    if lower_price is None and upper_price is None:
        midpoint = 100.0
    elif lower_price is None:
        assert upper_price is not None
        midpoint = upper_price / 2.0
    elif upper_price is None:
        midpoint = max(lower_price + 50.0, lower_price * 2.0)
    else:
        midpoint = (lower_price + upper_price) / 2.0
    if midpoint == lower_price or midpoint == upper_price:
        raise TruncatedSnapshotError("YOOX price slice cannot be divided further")
    return (lower_price, midpoint), (midpoint, upper_price)


def _yoox_item(hit: dict[str, Any], minimum_percent: int) -> RawItem | None:
    external_id = hit.get("objectID")
    model = hit.get("model")
    price_data = hit.get("modelBestOfferByPrice")
    if not isinstance(external_id, str) or not external_id:
        raise ValueError("missing objectID")
    if not isinstance(model, dict) or not isinstance(price_data, dict):
        raise ValueError("missing model or price")
    if hit.get("preowned") or hit.get("has3rdParty"):
        return None
    if not (hit.get("purchasable") and hit.get("visible")):
        return None
    sizes = _string_list(hit.get("availableSizes"))
    if not sizes:
        return None

    price = _positive_number(price_data.get("currentPrice"))
    compare_at_price = _positive_number(price_data.get("fullPrice"))
    reported = _reported_percent(
        price_data.get("markdownPercentageFromFullPrice")
    )
    reference_info = price_data.get("retailPriceInfo")
    reference_info = reference_info if isinstance(reference_info, dict) else {}
    if reference_info.get("source") not in _YOOX_REFERENCE_SOURCES:
        return None
    if price is None or compare_at_price is None or reported is None:
        raise ValueError("missing current/full price or markdown")
    if not _meets_discount(
        price,
        compare_at_price,
        minimum_percent,
    ):
        return None
    currency = price_data.get("currency")
    if currency != "USD":
        raise ValueError(f"unexpected currency {currency!r}")

    brand_value = _first_string(model.get("brand"))
    brand = re.sub(r"-\d+$", "", brand_value or "").strip() or None
    categories = model.get("categories")
    categories = categories if isinstance(categories, dict) else {}
    macro = _first_string(categories.get("macro"))
    micro = _first_string(categories.get("micro"))
    material = _first_string(model.get("mainMaterial"))
    color = _first_string(hit.get("colorLabel"))
    title_parts = [part for part in (brand, material, micro or macro, color) if part]
    title = " ".join(dict.fromkeys(title_parts))
    if not title:
        raise ValueError("missing title fields")

    image_data = hit.get("images")
    image_data = image_data if isinstance(image_data, dict) else {}
    image_path = _first_string(image_data.get("url"))
    image_url = None
    if image_path and image_path.startswith("/~/"):
        image_url = f"https://www.yoox.com/images/items/{image_path[3:]}"
    tags = _string_list(hit.get("promoTags"))
    sale_line = hit.get("saleLine")
    if isinstance(sale_line, dict):
        sale_name = _first_string(sale_line.get("name"))
        if sale_name:
            tags.append(sale_name)
    tags.extend(
        part
        for part in (
            "clearance",
            _first_string(model.get("gender")),
            macro,
            micro,
            _first_string(model.get("seasonality")),
        )
        if part
    )
    colors = [
        name
        for entry in hit.get("availableColors") or []
        if isinstance(entry, dict)
        for name in [_first_string(entry.get("name"))]
        if name
    ]
    body_parts = [
        part
        for part in (
            _first_string(hit.get("composition")),
            f"Available sizes: {', '.join(sizes)}" if sizes else None,
            f"Colors: {', '.join(colors)}" if colors else None,
        )
        if part
    ]
    return RawItem(
        external_id=external_id,
        title=title,
        url=f"https://www.yoox.com/nz/{external_id}/item",
        body=". ".join(body_parts),
        raw_metadata={
            "price": price,
            "compare_at_price": compare_at_price,
            "retail_price": _positive_number(price_data.get("retailPrice")),
            "discount_percent": _discount_percent(price, compare_at_price),
            "reported_discount_percent": reported,
            "reference_source": reference_info.get("source"),
            "on_sale": True,
            "available": True,
            "vendor": brand,
            "product_type": micro or macro,
            "tags": list(dict.fromkeys(tags)),
            "image_url": image_url,
            "currency_code": currency,
            "retailer": "yoox",
        },
    )


def _fetch_yoox(
    get_json: JsonGetter,
    minimum_percent: int,
    *,
    limit: int | None,
) -> list[RawItem]:
    items: dict[str, RawItem] = {}

    def consume_band(
        department: str,
        lower_price: float | None,
        upper_price: float | None,
        depth: int,
    ) -> bool:
        if depth > _YOOX_MAX_SLICE_DEPTH:
            raise TruncatedSnapshotError("YOOX price slicing exceeded its depth cap")
        try:
            hits = _yoox_slice_hits(
                get_json,
                department,
                lower_price=lower_price,
                upper_price=upper_price,
            )
        except _YooxSliceOverflow:
            first, second = _split_yoox_band(lower_price, upper_price)
            return consume_band(department, *first, depth + 1) or consume_band(
                department, *second, depth + 1
            )

        for index, hit in enumerate(hits):
            try:
                item = _yoox_item(hit, minimum_percent)
            except Exception as exc:
                print(
                    "[international_clearance:yoox] skipping product "
                    f"department={department} index={index}: {exc}"
                )
                continue
            if _append_until(items, item, limit=limit):
                return True
        return False

    for band in _YOOX_WOMEN_PRICE_BANDS:
        if consume_band("Clearance_W", *band, 0):
            return list(items.values())
    for department in _YOOX_OTHER_DEPARTMENTS:
        if consume_band(department, None, None, 0):
            return list(items.values())
    return list(items.values())


class InternationalClearanceConnector:
    type_key = "international_clearance"
    supported_channel_kinds = frozenset({ChannelKind.MONITOR})

    def __init__(
        self,
        *,
        get_json: JsonGetter = fetch_json,
        get_text: TextGetter = fetch_text,
        post: JsonPoster = post_json,
        post_browser: JsonPoster = post_json_browser_tls,
    ):
        self._get_json = get_json
        self._get_text = get_text
        self._post = post
        self._post_browser = post_browser

    def validate_config(self, config: dict) -> None:
        retailer = config.get("retailer")
        if retailer not in RETAILER_LABELS:
            allowed = ", ".join(RETAILER_LABELS)
            raise ValueError(
                "international_clearance config needs 'retailer' to be one of: "
                f"{allowed}"
            )
        minimum = config.get(
            "minimum_discount_percent",
            _DEFAULT_MINIMUM_DISCOUNT_PERCENT,
        )
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or not _MINIMUM_DISCOUNT_PERCENT
            <= minimum
            <= _MAXIMUM_DISCOUNT_PERCENT
        ):
            raise ValueError(
                "international_clearance config needs 'minimum_discount_percent' "
                "to be an integer from 50 to 90"
            )
        if retailer == "mytheresa" and minimum > 70:
            raise ValueError(
                "international_clearance Mytheresa sources support discounts up to 70"
            )

    def _fetch(self, config: dict, *, limit: int | None) -> list[RawItem]:
        self.validate_config(config)
        retailer = config["retailer"]
        minimum = config.get(
            "minimum_discount_percent",
            _DEFAULT_MINIMUM_DISCOUNT_PERCENT,
        )
        if retailer == "the_outnet":
            return _fetch_outnet(
                self._post,
                minimum,
                limit=limit,
            )
        if retailer == "mytheresa":
            return _fetch_mytheresa(
                self._post_browser,
                minimum,
                limit=limit,
            )
        if retailer == "end":
            return _fetch_end(
                self._get_text,
                self._post,
                minimum,
                limit=limit,
            )
        if retailer == "yoox":
            return _fetch_yoox(
                self._get_json,
                minimum,
                limit=limit,
            )
        raise AssertionError(f"unhandled retailer: {retailer}")

    def fetch(self, config: dict) -> list[RawItem]:
        return self._fetch(config, limit=None)

    def fetch_preview(self, config: dict, *, limit: int) -> list[RawItem]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("preview limit must be a positive integer")
        return self._fetch(config, limit=limit)


register(InternationalClearanceConnector())
