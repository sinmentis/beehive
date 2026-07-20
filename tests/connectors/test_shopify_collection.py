from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from beehive.connectors.shopify_collection import (
    ShopifyCollectionConnector,
    _default_fetch_json,
)

_COLLECTION_URL = "https://example-store.com/collections/outlet"


def _variant(price="49.99", compare_at_price=None, available=True):
    variant = {"price": price, "available": available}
    if compare_at_price is not None:
        variant["compare_at_price"] = compare_at_price
    return variant


def _product(
    product_id=1001,
    *,
    handle="alpha-jacket",
    title="Alpha Jacket",
    variants=None,
    vendor="Acme Outdoor",
    product_type="Jacket",
    tags=None,
    published_at="2026-05-05T11:12:16+10:00",
    created_at="2026-01-01T00:00:00+00:00",
):
    return {
        "id": product_id,
        "handle": handle,
        "title": title,
        "variants": variants if variants is not None else [_variant()],
        "vendor": vendor,
        "product_type": product_type,
        "tags": tags if tags is not None else ["clearance"],
        "published_at": published_at,
        "created_at": created_at,
    }


def _page(products):
    return {"products": products}


def test_type_key():
    assert ShopifyCollectionConnector().type_key == "shopify_collection"


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"collection_url": ""},
        {"collection_url": "   "},
        {"collection_url": 123},
        {"collection_url": "not-a-url"},
        {"collection_url": "ftp://example.com/collections/outlet"},
        {"collection_url": "https:///collections/outlet"},
        {"collection_url": "/relative/path"},
    ],
)
def test_validate_config_rejects_bad_urls(config):
    connector = ShopifyCollectionConnector(fetch_json=lambda url: _page([]))
    with pytest.raises(ValueError):
        connector.validate_config(config)


def test_validate_config_accepts_a_valid_http_s_url():
    ShopifyCollectionConnector(fetch_json=lambda url: _page([])).validate_config(
        {"collection_url": _COLLECTION_URL}
    )


def test_fetch_maps_product_fields_and_metadata():
    connector = ShopifyCollectionConnector(fetch_json=lambda url: _page([_product()]))
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]

    assert item.external_id == "1001:49.99"
    assert item.title == "Alpha Jacket"
    assert item.url == "https://example-store.com/products/alpha-jacket"
    assert item.body == ""
    assert item.created_at == datetime.fromisoformat("2026-05-05T11:12:16+10:00")
    assert item.raw_metadata == {
        "price": 49.99,
        "compare_at_price": None,
        "on_sale": False,
        "available": True,
        "vendor": "Acme Outdoor",
        "product_type": "Jacket",
        "tags": ["clearance"],
    }


def test_fetch_falls_back_to_created_at_when_published_at_is_missing():
    product = _product(published_at=None)
    connector = ShopifyCollectionConnector(fetch_json=lambda url: _page([product]))
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]
    assert item.created_at == datetime.fromisoformat("2026-01-01T00:00:00+00:00")


def test_fetch_leaves_created_at_none_when_no_usable_timestamp():
    product = _product(published_at=None, created_at=None)
    connector = ShopifyCollectionConnector(fetch_json=lambda url: _page([product]))
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]
    assert item.created_at is None


def test_fetch_flags_on_sale_using_the_highest_qualifying_compare_at_price():
    # Mirrors real bivouac.co.nz data: different variants (sizes/colors) of one product can
    # carry different discount depths, so "price" and "compare_at_price" are picked independently.
    variants = [
        _variant(price="30.00", compare_at_price="50.00"),
        _variant(price="20.00", compare_at_price="25.00"),
        _variant(price="45.00"),
    ]
    product = _product(variants=variants)
    connector = ShopifyCollectionConnector(fetch_json=lambda url: _page([product]))
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]

    assert item.raw_metadata["price"] == 20.00
    assert item.raw_metadata["compare_at_price"] == 50.00
    assert item.raw_metadata["on_sale"] is True
    assert item.external_id == "1001:20.00"


def test_fetch_ignores_compare_at_price_that_does_not_exceed_price():
    product = _product(variants=[_variant(price="30.00", compare_at_price="30.00")])
    connector = ShopifyCollectionConnector(fetch_json=lambda url: _page([product]))
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]
    assert item.raw_metadata["on_sale"] is False
    assert item.raw_metadata["compare_at_price"] is None


def test_fetch_available_true_when_any_variant_is_available():
    variants = [
        _variant(price="10.00", available=False),
        _variant(price="12.00", available=True),
    ]
    product = _product(variants=variants)
    connector = ShopifyCollectionConnector(fetch_json=lambda url: _page([product]))
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]
    assert item.raw_metadata["available"] is True


def test_fetch_tags_default_to_empty_list_when_not_a_list():
    product = _product()
    product["tags"] = "clearance, jackets"  # Shopify sometimes returns a bare string
    connector = ShopifyCollectionConnector(fetch_json=lambda url: _page([product]))
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]
    assert item.raw_metadata["tags"] == []


def test_fetch_skips_a_product_with_no_usable_variant_price_and_keeps_the_rest(capsys):
    bad_product = _product(product_id=1, variants=[{"price": "not-a-number"}])
    good_product = _product(product_id=2, handle="beta-jacket", title="Beta Jacket")
    connector = ShopifyCollectionConnector(
        fetch_json=lambda url: _page([bad_product, good_product])
    )
    items = connector.fetch({"collection_url": _COLLECTION_URL})

    assert [item.external_id for item in items] == ["2:49.99"]
    assert "skipping product index=0" in capsys.readouterr().out


def test_fetch_skips_a_product_with_no_variants_field_at_all(capsys):
    bad_product = _product(product_id=1)
    del bad_product["variants"]
    good_product = _product(product_id=2, handle="beta-jacket", title="Beta Jacket")
    connector = ShopifyCollectionConnector(
        fetch_json=lambda url: _page([bad_product, good_product])
    )
    items = connector.fetch({"collection_url": _COLLECTION_URL})

    assert [item.external_id for item in items] == ["2:49.99"]
    assert "skipping product index=0" in capsys.readouterr().out


def test_fetch_skips_a_product_missing_id_handle_or_title(capsys):
    bad_product = _product(product_id=1)
    del bad_product["handle"]
    good_product = _product(product_id=2, handle="beta-jacket", title="Beta Jacket")
    connector = ShopifyCollectionConnector(
        fetch_json=lambda url: _page([bad_product, good_product])
    )
    items = connector.fetch({"collection_url": _COLLECTION_URL})

    assert [item.external_id for item in items] == ["2:49.99"]
    assert "skipping product index=0" in capsys.readouterr().out


def test_fetch_skips_a_non_dict_product_entry(capsys):
    connector = ShopifyCollectionConnector(
        fetch_json=lambda url: _page(["not-a-product", _product()])
    )
    items = connector.fetch({"collection_url": _COLLECTION_URL})
    assert [item.external_id for item in items] == ["1001:49.99"]
    assert "skipping product index=0" in capsys.readouterr().out


def test_fetch_raises_when_every_product_in_a_nonempty_payload_is_unusable():
    connector = ShopifyCollectionConnector(fetch_json=lambda url: _page([{"id": 1}]))
    with pytest.raises(RuntimeError, match="no usable products"):
        connector.fetch({"collection_url": _COLLECTION_URL})


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"products": None}, {"products": "not-a-list"}]
)
def test_fetch_rejects_a_malformed_response_envelope(payload):
    connector = ShopifyCollectionConnector(fetch_json=lambda url: payload)
    with pytest.raises(ValueError, match="products"):
        connector.fetch({"collection_url": _COLLECTION_URL})


def test_fetch_requests_the_products_json_endpoint_with_pagination_params():
    calls = []

    def fetch_json(url):
        calls.append(url)
        return _page([])

    ShopifyCollectionConnector(fetch_json=fetch_json).fetch(
        {"collection_url": _COLLECTION_URL}
    )

    assert len(calls) == 1
    parsed = urlparse(calls[0])
    assert parsed.path == "/collections/outlet/products.json"
    assert parse_qs(parsed.query) == {"limit": ["250"], "page": ["1"]}


def test_fetch_strips_a_trailing_slash_from_the_collection_url():
    calls = []

    def fetch_json(url):
        calls.append(url)
        return _page([])

    ShopifyCollectionConnector(fetch_json=fetch_json).fetch(
        {"collection_url": _COLLECTION_URL + "/"}
    )
    assert calls[0].startswith(_COLLECTION_URL + "/products.json")


def test_fetch_pages_until_a_short_page_signals_the_last_page():
    full_page = [_product(product_id=i) for i in range(250)]
    short_page = [_product(product_id=1000)]
    pages = {1: full_page, 2: short_page}
    calls = []

    def fetch_json(url):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        calls.append(page)
        return _page(pages.get(page, []))

    items = ShopifyCollectionConnector(fetch_json=fetch_json).fetch(
        {"collection_url": _COLLECTION_URL}
    )
    assert calls == [1, 2]
    assert len(items) == 251


def test_fetch_stops_at_an_empty_page_without_requesting_further_pages():
    full_page = [_product(product_id=i) for i in range(250)]
    pages = {1: full_page, 2: []}
    calls = []

    def fetch_json(url):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        calls.append(page)
        return _page(pages.get(page, []))

    items = ShopifyCollectionConnector(fetch_json=fetch_json).fetch(
        {"collection_url": _COLLECTION_URL}
    )
    assert calls == [1, 2]
    assert len(items) == 250


def test_fetch_caps_pagination_at_four_pages_even_if_more_data_is_available():
    calls = []

    def fetch_json(url):
        calls.append(url)
        page = int(parse_qs(urlparse(url).query)["page"][0])
        # Every page comes back "full" (250 items): an unbounded storefront could otherwise
        # make this paginate forever.
        return _page([_product(product_id=page * 1000 + i) for i in range(250)])

    items = ShopifyCollectionConnector(fetch_json=fetch_json).fetch(
        {"collection_url": _COLLECTION_URL}
    )
    assert len(calls) == 4
    assert len(items) == 1000


def test_default_json_fetch_uses_user_agent_and_timeout():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"products": []}'

    with patch(
        "beehive.connectors.shopify_collection.urllib.request.urlopen",
        return_value=response,
    ) as urlopen:
        payload = _default_fetch_json(f"{_COLLECTION_URL}/products.json")

    request = urlopen.call_args.args[0]
    assert request.get_header("User-agent") == "beehive/0.1 (personal information hub)"
    assert urlopen.call_args.kwargs["timeout"] == 20
    assert payload == {"products": []}


def test_default_json_fetch_raises_on_invalid_json():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"not json"

    with patch(
        "beehive.connectors.shopify_collection.urllib.request.urlopen",
        return_value=response,
    ):
        with pytest.raises(ValueError, match="invalid JSON"):
            _default_fetch_json(f"{_COLLECTION_URL}/products.json")
