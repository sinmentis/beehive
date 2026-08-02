from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from beehive.connectors.base import TruncatedSnapshotError
from beehive.connectors.international_clearance import (
    InternationalClearanceConnector,
    _YOOX_PAGE_SIZE,
    _YooxSliceOverflow,
    _yoox_slice_hits,
)
from beehive.domain.channels import ChannelKind


def _end_bootstrap_html(*, website_id=16, currency="NZD") -> str:
    data = {
        "runtimeConfig": {
            "env": {
                "ALGOLIA_HOSTS": (
                    "search1web.endclothing.com,search2web.endclothing.com"
                ),
                "ALGOLIA_ID": "app-id",
                "ALGOLIA_KEY": "public-search-key",
            }
        },
        "props": {
            "initialState": {
                "config": {
                    "catalog": {
                        "providers": [
                            {"products_index": "Catalog_products_v3_gb_products"}
                        ]
                    },
                    "country": {
                        "website_id": website_id,
                        "currency_code": currency,
                    },
                }
            }
        },
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(data)}</script>"
    )


def _end_hit(
    object_id="123",
    *,
    price=30,
    compare_at_price=100,
    sale_percentage="70%",
) -> dict:
    return {
        "objectID": object_id,
        "sku": "SKU-1",
        "name": "Archive Jacket",
        "brand": "Example",
        "url_key": "example-archive-jacket",
        "small_image": "/a/b/jacket.jpg",
        "categories": ["Sale", "Sale / Over 60% off"],
        "department_hierarchy": ["Clothing", "Jackets"],
        "DepartmentV1": "Jackets",
        "gender": "Womens",
        "sale_percentage": sale_percentage,
        "promotion": {"label": "Final reduction"},
        "stock": 2,
        "status": 1,
        "for_sale_online": 1,
        "websites_available_at": [16],
        "full_price_16": compare_at_price,
        "final_price_16": price,
        "created_at": "2026-01-02 03:04:05",
    }


def _mytheresa_product(
    sku="P001",
    *,
    percentage="70",
    price=3000,
    original=10000,
) -> dict:
    return {
        "sku": sku,
        "slug": f"/example-archive-coat-{sku.lower()}",
        "name": "Archive Coat",
        "description": "Double-faced wool coat",
        "designer": "Example",
        "combinedCategoryName": "Clothing::Coats::Wool",
        "department": "Clothing",
        "mainWaregroup": "Coats",
        "displayImages": ["https://img.example/coat.jpg"],
        "enabled": True,
        "hasStock": True,
        "isPurchasable": True,
        "labels": ["final-sale"],
        "promotionLabels": [{"label": "Final sale", "type": "sale"}],
        "price": {
            "currencyCode": "EUR",
            "original": original,
            "discount": price,
            "percentage": percentage,
        },
    }


def _mytheresa_response(
    products,
    *,
    total_pages=1,
    total_items=None,
    current_page=1,
    items_per_page=60,
) -> dict:
    return {
        "data": {
            "xProductListingPageV2": {
                "pagination": {
                    "currentPage": current_page,
                    "itemsPerPage": items_per_page,
                    "totalItems": (
                        len(products) if total_items is None else total_items
                    ),
                    "totalPages": total_pages,
                },
                "products": products,
            }
        }
    }


def _outnet_variant(
    variant_id="V001",
    *,
    price="100.0",
    compare_at_price="1000.0",
    available=True,
) -> dict:
    return {
        "id": f"gid://shopify/ProductVariant/{variant_id}",
        "availableForSale": available,
        "price": {
            "amount": price,
            "currencyCode": "AUD",
        },
        "compareAtPrice": {
            "amount": compare_at_price,
            "currencyCode": "AUD",
        },
    }


def _outnet_product(
    product_id="P001",
    *,
    variants=None,
) -> dict:
    return {
        "id": f"gid://shopify/Product/{product_id}",
        "handle": f"example-dress-{product_id.lower()}",
        "title": "Example Silk Dress",
        "vendor": "Example",
        "productType": "Dresses",
        "tags": ["Final sale", "Women"],
        "availableForSale": True,
        "featuredImage": {"url": "https://cdn.example/dress.jpg"},
        "variants": {
            "pageInfo": {"hasNextPage": False},
            "nodes": variants or [_outnet_variant()],
        },
    }


def _outnet_response(
    products,
    *,
    has_next_page=False,
    end_cursor=None,
) -> dict:
    return {
        "data": {
            "collection": {
                "products": {
                    "pageInfo": {
                        "hasNextPage": has_next_page,
                        "endCursor": end_cursor,
                    },
                    "nodes": products,
                }
            }
        },
        "extensions": {"context": {"country": "NZ", "language": "EN"}},
    }


def _yoox_hit(
    object_id="30519224MU",
    *,
    source="PriceTag",
    percentage=72.0,
    price=30,
    full_price=110,
    preowned=False,
    third_party=False,
) -> dict:
    return {
        "objectID": object_id,
        "variantId": object_id,
        "model": {
            "id": object_id[:8],
            "categories": {"macro": "CLOTHING", "micro": "Pants"},
            "brand": ["RUE\u20228ISQUIT-123"],
            "gender": "Woman",
            "mainMaterial": "Polyester",
            "seasonality": "Summer",
        },
        "modelBestOfferByPrice": {
            "retailPrice": 150,
            "retailPriceInfo": {
                "source": source,
                "trusted": True,
            },
            "fullPrice": full_price,
            "currentPrice": price,
            "markdownPercentageFromRetailPrice": 80,
            "markdownPercentageFromFullPrice": percentage,
            "currency": "USD",
        },
        "images": {"url": "/~/30/30519224mu_11_f.jpg"},
        "availableSizes": ["38", "40"],
        "availableColors": [{"name": "Cream"}],
        "colorLabel": "Cream",
        "purchasable": True,
        "visible": True,
        "preowned": preowned,
        "has3rdParty": third_party,
        "promoTags": ["clearance_longtail"],
        "saleLine": {"name": "JUST IN"},
        "composition": "100% polyester",
    }


def test_connector_interface_and_default_config():
    connector = InternationalClearanceConnector()

    assert connector.type_key == "international_clearance"
    assert connector.supported_channel_kinds == frozenset({ChannelKind.MONITOR})
    connector.validate_config({"retailer": "mytheresa"})


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"retailer": ""},
        {"retailer": "24s"},
        {"retailer": "yoox", "minimum_discount_percent": True},
        {"retailer": "yoox", "minimum_discount_percent": 49},
        {"retailer": "yoox", "minimum_discount_percent": 91},
        {"retailer": "mytheresa", "minimum_discount_percent": 71},
        {"retailer": "yoox", "minimum_discount_percent": "70"},
    ],
)
def test_validate_config_rejects_unsupported_or_unsafe_thresholds(config):
    with pytest.raises(ValueError):
        InternationalClearanceConnector().validate_config(config)


def test_outnet_maps_aud_prices_from_the_deepest_available_variant():
    calls = []

    def post(url, payload, **kwargs):
        calls.append((url, payload, kwargs))
        return _outnet_response(
            [
                _outnet_product(
                    variants=[
                        _outnet_variant(
                            "V-SHALLOW",
                            price="250.0",
                            compare_at_price="1000.0",
                        ),
                        _outnet_variant(
                            "V-DEEP",
                            price="100.0",
                            compare_at_price="1000.0",
                        ),
                        _outnet_variant(
                            "V-SOLD",
                            price="50.0",
                            compare_at_price="1000.0",
                            available=False,
                        ),
                    ]
                )
            ]
        )

    item = InternationalClearanceConnector(post=post).fetch(
        {"retailer": "the_outnet", "minimum_discount_percent": 88}
    )[0]

    assert item.external_id == "gid://shopify/Product/P001"
    assert item.url == "https://theoutnet.com/en-nz/products/example-dress-p001"
    assert item.raw_metadata == {
        "price": 100.0,
        "compare_at_price": 1000.0,
        "discount_percent": 90.0,
        "on_sale": True,
        "available": True,
        "variant_id": "gid://shopify/ProductVariant/V-DEEP",
        "vendor": "Example",
        "product_type": "Dresses",
        "tags": ["Final sale", "Women"],
        "image_url": "https://cdn.example/dress.jpg",
        "currency_code": "AUD",
        "retailer": "the_outnet",
    }
    assert calls[0][0] == "https://theoutnet.com/api/2025-07/graphql.json"
    assert calls[0][1]["variables"] == {
        "after": None,
        "pageSize": 250,
        "variantPageSize": 250,
    }
    assert calls[0][2]["allowed_hosts"] == frozenset({"theoutnet.com"})
    assert "X-Shopify-Storefront-Access-Token" in calls[0][2]["extra_headers"]


def test_outnet_follows_authoritative_cursor_pagination():
    calls = []
    responses = [
        _outnet_response(
            [
                _outnet_product(
                    "P-SHALLOW",
                    variants=[
                        _outnet_variant(
                            price="200.0",
                            compare_at_price="1000.0",
                        )
                    ],
                )
            ],
            has_next_page=True,
            end_cursor="next-page",
        ),
        _outnet_response([_outnet_product("P-DEEP")]),
    ]

    def post(url, payload, **kwargs):
        calls.append(payload)
        return responses.pop(0)

    items = InternationalClearanceConnector(post=post).fetch(
        {"retailer": "the_outnet", "minimum_discount_percent": 88}
    )

    assert [item.external_id for item in items] == [
        "gid://shopify/Product/P-DEEP"
    ]
    assert calls[0]["variables"] == {
        "after": None,
        "pageSize": 250,
        "variantPageSize": 250,
    }
    assert calls[1]["variables"] == {
        "after": "next-page",
        "pageSize": 250,
        "variantPageSize": 250,
    }


def test_outnet_fails_instead_of_truncating_an_authoritative_catalog():
    calls = 0

    def post(url, payload, **kwargs):
        nonlocal calls
        calls += 1
        return _outnet_response(
            [],
            has_next_page=True,
            end_cursor=f"more-{calls}",
        )

    with pytest.raises(TruncatedSnapshotError, match="30-page cap"):
        InternationalClearanceConnector(post=post).fetch(
            {"retailer": "the_outnet", "minimum_discount_percent": 88}
        )


def test_mytheresa_maps_minor_units_currency_and_product_metadata():
    calls = []

    def post_browser(url, payload, **kwargs):
        calls.append((url, payload, kwargs))
        section = kwargs["extra_headers"]["X-Section"]
        products = [_mytheresa_product()] if section == "women" else []
        return _mytheresa_response(products)

    connector = InternationalClearanceConnector(post_browser=post_browser)
    items = connector.fetch(
        {"retailer": "mytheresa", "minimum_discount_percent": 70}
    )

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "P001"
    assert item.url == (
        "https://www.mytheresa.com/euro/en/women/"
        "example-archive-coat-p001"
    )
    assert item.body == "Double-faced wool coat"
    assert item.raw_metadata == {
        "price": 30.0,
        "compare_at_price": 100.0,
        "discount_percent": 70.0,
        "reported_discount_percent": 70.0,
        "on_sale": True,
        "available": True,
        "vendor": "Example",
        "product_type": "Wool",
        "tags": [
            "women",
            "Clothing",
            "Coats",
            "Wool",
            "final-sale",
            "Final sale",
        ],
        "image_url": "https://img.example/coat.jpg",
        "currency_code": "EUR",
        "retailer": "mytheresa",
    }
    assert {call[2]["extra_headers"]["X-Section"] for call in calls} == {
        "women",
        "men",
        "kids",
    }
    assert all(call[2]["extra_headers"]["X-Store"] == "EURO" for call in calls)
    assert all(call[1]["variables"]["filtersQueryParams"] == "reductionRange=70"
               for call in calls)


def test_mytheresa_fetches_the_bucket_containing_a_non_tier_threshold():
    calls = []

    def post_browser(url, payload, **kwargs):
        calls.append(payload["variables"]["filtersQueryParams"])
        tier = int(calls[-1].split("=")[1])
        products = (
            [_mytheresa_product(percentage=str(tier), price=4500, original=10000)]
            if kwargs["extra_headers"]["X-Section"] == "women"
            else []
        )
        return _mytheresa_response(products)

    items = InternationalClearanceConnector(
        post_browser=post_browser
    ).fetch_preview(
        {"retailer": "mytheresa", "minimum_discount_percent": 55},
        limit=1,
    )

    assert [item.external_id for item in items] == ["P001"]
    assert calls == ["reductionRange=50"]


def test_mytheresa_retries_a_response_that_ignored_the_discount_filter():
    responses = [
        _mytheresa_response([_mytheresa_product(percentage="30")], total_pages=900),
        _mytheresa_response([_mytheresa_product()]),
    ]
    calls = 0

    def post_browser(url, payload, **kwargs):
        nonlocal calls
        calls += 1
        return responses.pop(0)

    items = InternationalClearanceConnector(
        post_browser=post_browser
    ).fetch_preview(
        {"retailer": "mytheresa", "minimum_discount_percent": 70},
        limit=1,
    )

    assert [item.external_id for item in items] == ["P001"]
    assert calls == 2


def test_mytheresa_fails_instead_of_truncating_an_authoritative_catalog():
    def post_browser(url, payload, **kwargs):
        return _mytheresa_response([_mytheresa_product()], total_pages=101)

    with pytest.raises(TruncatedSnapshotError, match="101 pages"):
        InternationalClearanceConnector(post_browser=post_browser).fetch(
            {"retailer": "mytheresa", "minimum_discount_percent": 70}
        )


def test_mytheresa_rejects_a_short_nonfinal_page():
    def post_browser(url, payload, **kwargs):
        return _mytheresa_response(
            [],
            total_pages=2,
            total_items=61,
        )

    with pytest.raises(RuntimeError, match="after 20 attempts"):
        InternationalClearanceConnector(post_browser=post_browser).fetch(
            {"retailer": "mytheresa", "minimum_discount_percent": 70}
        )


def test_end_bootstraps_algolia_and_maps_nzd_prices():
    calls = []

    def post(url, payload, **kwargs):
        calls.append((url, payload, kwargs))
        request = payload["requests"][0]
        params = parse_qs(request["params"])
        assert params["facetFilters"] == [
            '[["categories:Sale / Over 60% off"],["websites_available_at:16"]]'
        ]
        return {
            "results": [
                {
                    "hits": [_end_hit()],
                    "nbPages": 1,
                    "nbHits": 1,
                }
            ]
        }

    connector = InternationalClearanceConnector(
        get_text=lambda url, **kwargs: _end_bootstrap_html(),
        post=post,
    )
    item = connector.fetch(
        {"retailer": "end", "minimum_discount_percent": 70}
    )[0]

    assert item.external_id == "123"
    assert item.url == "https://www.endclothing.com/nz/example-archive-jacket.html"
    assert item.created_at is not None and item.created_at.utcoffset().total_seconds() == 0
    assert item.raw_metadata["price"] == 30.0
    assert item.raw_metadata["compare_at_price"] == 100.0
    assert item.raw_metadata["currency_code"] == "NZD"
    assert item.raw_metadata["vendor"] == "Example"
    assert item.raw_metadata["product_type"] == "Jackets"
    assert item.raw_metadata["image_url"].endswith("/a/b/jacket.jpg")
    assert "x-algolia-api-key=public-search-key" in calls[0][0]
    assert calls[0][2]["content_type"] == "application/x-www-form-urlencoded"


def test_end_rejects_a_geo_redirect_or_currency_change():
    for website_id, currency in [(1, "NZD"), (16, "AUD")]:
        connector = InternationalClearanceConnector(
            get_text=lambda url, website_id=website_id, currency=currency, **kwargs: (
                _end_bootstrap_html(
                    website_id=website_id,
                    currency=currency,
                )
            )
        )
        with pytest.raises(ValueError):
            connector.fetch({"retailer": "end", "minimum_discount_percent": 70})


def test_end_fails_instead_of_truncating_more_than_the_page_cap():
    def post(url, payload, **kwargs):
        return {
            "results": [
                {
                    "hits": [_end_hit()],
                    "nbPages": 101,
                    "nbHits": 12120,
                }
            ]
        }

    connector = InternationalClearanceConnector(
        get_text=lambda url, **kwargs: _end_bootstrap_html(),
        post=post,
    )
    with pytest.raises(TruncatedSnapshotError, match="101 pages"):
        connector.fetch({"retailer": "end", "minimum_discount_percent": 70})


def test_end_fails_when_algolia_caps_the_retrievable_window():
    def post(url, payload, **kwargs):
        return {
            "results": [
                {
                    "hits": [_end_hit()],
                    "nbPages": 9,
                    "nbHits": 1400,
                }
            ]
        }

    connector = InternationalClearanceConnector(
        get_text=lambda url, **kwargs: _end_bootstrap_html(),
        post=post,
    )
    with pytest.raises(TruncatedSnapshotError, match="retrievable positions"):
        connector.fetch({"retailer": "end", "minimum_discount_percent": 70})


def test_end_preview_stops_after_the_requested_number_of_products():
    calls = 0

    def post(url, payload, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "results": [
                {
                    "hits": [_end_hit("1"), _end_hit("2"), _end_hit("3")],
                    "nbPages": 1,
                    "nbHits": 3,
                }
            ]
        }

    items = InternationalClearanceConnector(
        get_text=lambda url, **kwargs: _end_bootstrap_html(),
        post=post,
    ).fetch_preview(
        {"retailer": "end", "minimum_discount_percent": 70},
        limit=2,
    )

    assert [item.external_id for item in items] == ["1", "2"]
    assert calls == 1


def test_end_rejects_a_short_nonfinal_page():
    def post(url, payload, **kwargs):
        return {
            "results": [
                {
                    "hits": [_end_hit()],
                    "nbPages": 2,
                    "nbHits": 121,
                }
            ]
        }

    connector = InternationalClearanceConnector(
        get_text=lambda url, **kwargs: _end_bootstrap_html(),
        post=post,
    )
    with pytest.raises(TruncatedSnapshotError, match="returned 1 of 120"):
        connector.fetch({"retailer": "end", "minimum_discount_percent": 70})


def test_yoox_keeps_only_deep_direct_inventory_with_real_reference_prices():
    calls = []

    def get_json(url, **kwargs):
        calls.append((url, kwargs))
        return {
            "hits": [
                _yoox_hit("30519224MU"),
                _yoox_hit("30519225MU", source="Estimate"),
                _yoox_hit(
                    "30519226MU",
                    percentage=70.0,
                    price=70,
                    full_price=100,
                ),
                _yoox_hit("30519227MU", preowned=True),
                _yoox_hit("30519228MU", third_party=True),
            ],
            "nbHits": 5,
            "nbPages": 1,
        }

    items = InternationalClearanceConnector(get_json=get_json).fetch(
        {"retailer": "yoox", "minimum_discount_percent": 70}
    )

    assert [candidate.external_id for candidate in items] == ["30519224MU"]
    item = items[0]
    assert item.external_id == "30519224MU"
    assert item.title == "RUE\u20228ISQUIT Polyester Pants Cream"
    assert item.url == "https://www.yoox.com/nz/30519224MU/item"
    assert item.raw_metadata["price"] == 30.0
    assert item.raw_metadata["compare_at_price"] == 110.0
    assert item.raw_metadata["retail_price"] == 150.0
    assert item.raw_metadata["discount_percent"] == pytest.approx(72.7272727)
    assert item.raw_metadata["reported_discount_percent"] == 72.0
    assert item.raw_metadata["reference_source"] == "PriceTag"
    assert item.raw_metadata["currency_code"] == "USD"
    assert item.raw_metadata["image_url"] == (
        "https://www.yoox.com/images/items/30/30519224mu_11_f.jpg"
    )
    assert calls[0][1]["extra_headers"]["X-Algolia-Application-Id"] == (
        "W4870FBRWZ"
    )
    query = parse_qs(urlparse(calls[0][0]).query)
    assert query["facetFilters"] == ['[["departments:Clearance_W"]]']
    assert query["distinct"] == ["0"]


def test_yoox_full_fetch_deduplicates_variants_seen_in_multiple_departments():
    def get_json(url, **kwargs):
        return {
            "hits": [_yoox_hit()],
            "nbHits": 1,
            "nbPages": 1,
        }

    items = InternationalClearanceConnector(get_json=get_json).fetch(
        {"retailer": "yoox", "minimum_discount_percent": 70}
    )

    assert [item.external_id for item in items] == ["30519224MU"]


def test_yoox_slice_reports_the_algolia_20000_hit_ceiling():
    calls = 0

    def get_json(url, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "hits": [{}] * _YOOX_PAGE_SIZE,
            "nbHits": 20_000,
            "nbPages": 20,
        }

    with pytest.raises(_YooxSliceOverflow, match="20,000"):
        _yoox_slice_hits(
            get_json,
            "Clearance_W",
            lower_price=30,
            upper_price=40,
        )

    assert calls == 1


def test_yoox_slice_rejects_a_short_nonfinal_page():
    def get_json(url, **kwargs):
        return {
            "hits": [{}],
            "nbHits": 1001,
            "nbPages": 2,
        }

    with pytest.raises(TruncatedSnapshotError, match="returned 1 of 1000"):
        _yoox_slice_hits(
            get_json,
            "Clearance_W",
            lower_price=30,
            upper_price=40,
        )


def test_yoox_slice_tolerates_live_hit_count_drift_between_pages():
    responses = [
        {
            "hits": [{}] * _YOOX_PAGE_SIZE,
            "nbHits": 1001,
            "nbPages": 2,
        },
        {
            "hits": [{}, {}],
            "nbHits": 1002,
            "nbPages": 2,
        },
    ]

    hits = _yoox_slice_hits(
        lambda url, **kwargs: responses.pop(0),
        "Clearance_W",
        lower_price=30,
        upper_price=40,
    )

    assert len(hits) == 1002
