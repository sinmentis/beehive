from __future__ import annotations

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from tests.connectors.http_stubs import urlopen_response as _urlopen_response
from beehive.connectors.base import TruncatedSnapshotError
from beehive.connectors.land_sea_collection import (
    LandSeaCollectionConnector,
    _default_fetch_html,
)

_COLLECTION_URL = "https://www.land-sea.co.nz/sale"


def _tile(
    tile_id=1001,
    *,
    product_url="/product/1001/alpha-jacket",
    name="Alpha Jacket",
    price=49.99,
    price_was=None,
    is_available=True,
    brand_name="Acme Outdoor",
    image_url="/media/1001.jpg",
):
    tile = {
        "id": tile_id,
        "productUrl": product_url,
        "name": name,
        "price": price,
        "isAvailable": is_available,
        "brandName": brand_name,
    }
    if price_was is not None:
        tile["priceWas"] = price_was
    if image_url is not None:
        tile["imageUrl"] = image_url
    return tile


def _page_html(tiles, total_pages=1):
    script = f"window.page.productTiles = {json.dumps(tiles)};"
    if total_pages is not None:
        script += f"window.page.totalPagesJs = {total_pages};"
    return f"<html><body><script>{script}</script></body></html>"


def test_type_key():
    assert LandSeaCollectionConnector().type_key == "land_sea_collection"


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"collection_url": ""},
        {"collection_url": "   "},
        {"collection_url": 123},
        {"collection_url": "not-a-url"},
        {"collection_url": "ftp://example.com/sale"},
        {"collection_url": "https:///sale"},
        {"collection_url": "/relative/path"},
    ],
)
def test_validate_config_rejects_bad_urls(config):
    connector = LandSeaCollectionConnector(fetch_html=lambda url: _page_html([]))
    with pytest.raises(ValueError):
        connector.validate_config(config)


def test_validate_config_accepts_a_valid_http_s_url():
    LandSeaCollectionConnector(fetch_html=lambda url: _page_html([])).validate_config(
        {"collection_url": _COLLECTION_URL}
    )


def test_fetch_maps_product_fields_and_metadata():
    connector = LandSeaCollectionConnector(fetch_html=lambda url: _page_html([_tile()]))
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]

    assert item.external_id == "1001"
    assert item.title == "Alpha Jacket"
    assert item.url == "https://www.land-sea.co.nz/product/1001/alpha-jacket"
    assert item.body == ""
    # Unlike Shopify, a productTiles entry never carries a timestamp of any kind.
    assert item.created_at is None
    assert item.raw_metadata == {
        "price": 49.99,
        "compare_at_price": None,
        "on_sale": False,
        "available": True,
        "vendor": "Acme Outdoor",
        "product_type": None,
        "tags": [],
        "image_url": "https://www.land-sea.co.nz/media/1001.jpg",
    }


def test_fetch_uses_the_stable_product_id_regardless_of_price():
    cheap = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([_tile(price=10.00)])
    ).fetch({"collection_url": _COLLECTION_URL})[0]
    dear = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([_tile(price=99.00)])
    ).fetch({"collection_url": _COLLECTION_URL})[0]
    assert cheap.external_id == "1001"
    assert dear.external_id == "1001"


def test_fetch_resolves_a_relative_or_object_image_field_defensively():
    object_tile = _tile(image_url=None)
    object_tile["image"] = {"src": "https://cdn.land-sea.co.nz/abc.jpg"}
    object_item = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([object_tile])
    ).fetch({"collection_url": _COLLECTION_URL})[0]
    assert object_item.raw_metadata["image_url"] == "https://cdn.land-sea.co.nz/abc.jpg"

    relative_item = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([_tile(image_url="/media/rel.jpg")])
    ).fetch({"collection_url": _COLLECTION_URL})[0]
    assert relative_item.raw_metadata["image_url"] == "https://www.land-sea.co.nz/media/rel.jpg"


def test_fetch_leaves_image_url_none_when_no_image_field_is_present():
    connector = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([_tile(image_url=None)])
    )
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]
    assert item.raw_metadata["image_url"] is None


def test_fetch_flags_on_sale_when_price_was_exceeds_price():
    connector = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([_tile(price=30.00, price_was=50.00)])
    )
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]
    assert item.raw_metadata["on_sale"] is True
    assert item.raw_metadata["compare_at_price"] == 50.00
    assert item.external_id == "1001"


def test_fetch_ignores_price_was_that_does_not_exceed_price():
    connector = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([_tile(price=30.00, price_was=30.00)])
    )
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]
    assert item.raw_metadata["on_sale"] is False
    assert item.raw_metadata["compare_at_price"] is None


def test_fetch_available_reflects_is_available_flag():
    connector = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([_tile(is_available=False)])
    )
    item = connector.fetch({"collection_url": _COLLECTION_URL})[0]
    assert item.raw_metadata["available"] is False


def test_fetch_skips_a_tile_with_no_usable_price_and_keeps_the_rest(capsys):
    bad_tile = _tile(tile_id=1, price="not-a-number")
    good_tile = _tile(tile_id=2, product_url="/product/2/beta-jacket", name="Beta Jacket")
    connector = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([bad_tile, good_tile])
    )
    items = connector.fetch({"collection_url": _COLLECTION_URL})

    assert [item.external_id for item in items] == ["2"]
    assert "skipping product index=0" in capsys.readouterr().out


def test_fetch_skips_a_tile_missing_id_producturl_or_name(capsys):
    bad_tile = _tile(tile_id=1)
    del bad_tile["productUrl"]
    good_tile = _tile(tile_id=2, product_url="/product/2/beta-jacket", name="Beta Jacket")
    connector = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html([bad_tile, good_tile])
    )
    items = connector.fetch({"collection_url": _COLLECTION_URL})

    assert [item.external_id for item in items] == ["2"]
    assert "skipping product index=0" in capsys.readouterr().out


def test_fetch_skips_a_non_dict_tile_entry(capsys):
    connector = LandSeaCollectionConnector(
        fetch_html=lambda url: _page_html(["not-a-tile", _tile()])
    )
    items = connector.fetch({"collection_url": _COLLECTION_URL})
    assert [item.external_id for item in items] == ["1001"]
    assert "skipping product index=0" in capsys.readouterr().out


def test_fetch_raises_when_every_tile_in_a_nonempty_page_is_unusable():
    connector = LandSeaCollectionConnector(fetch_html=lambda url: _page_html([{"id": 1}]))
    with pytest.raises(RuntimeError, match="no usable products"):
        connector.fetch({"collection_url": _COLLECTION_URL})


def test_fetch_raises_when_producttiles_block_is_missing():
    connector = LandSeaCollectionConnector(fetch_html=lambda url: "<html>no data here</html>")
    with pytest.raises(ValueError, match="productTiles"):
        connector.fetch({"collection_url": _COLLECTION_URL})


def test_fetch_raises_when_producttiles_block_is_not_valid_json():
    html = "<script>window.page.productTiles = [not valid json];</script>"
    connector = LandSeaCollectionConnector(fetch_html=lambda url: html)
    with pytest.raises(ValueError, match="not valid JSON"):
        connector.fetch({"collection_url": _COLLECTION_URL})


def test_fetch_requests_the_first_page_with_pgnmbr_query_param():
    calls = []

    def fetch_html(url):
        calls.append(url)
        return _page_html([], total_pages=1)

    LandSeaCollectionConnector(fetch_html=fetch_html).fetch(
        {"collection_url": _COLLECTION_URL}
    )

    assert len(calls) == 1
    parsed = urlparse(calls[0])
    assert parsed.path == "/sale"
    assert parse_qs(parsed.query) == {"pgNmbr": ["1"]}


def test_fetch_preserves_existing_filter_query_params_while_paginating():
    calls = []

    def fetch_html(url):
        calls.append(url)
        return _page_html([], total_pages=1)

    LandSeaCollectionConnector(fetch_html=fetch_html).fetch(
        {"collection_url": _COLLECTION_URL + "?brands=85"}
    )

    parsed = urlparse(calls[0])
    assert parse_qs(parsed.query) == {"brands": ["85"], "pgNmbr": ["1"]}


def test_fetch_strips_a_trailing_slash_from_the_collection_url():
    calls = []

    def fetch_html(url):
        calls.append(url)
        return _page_html([], total_pages=1)

    LandSeaCollectionConnector(fetch_html=fetch_html).fetch(
        {"collection_url": _COLLECTION_URL + "/"}
    )
    assert calls[0].startswith(_COLLECTION_URL + "?")


def test_fetch_stops_at_a_page_that_only_repeats_the_last_one():
    # The live site's shape: page 1 claims 83 pages, pages past the real end (3 here) repeat the
    # last page instead of coming back empty. Only the real pages count, each product once.
    pages = {n: [_tile(tile_id=n * 1000 + i) for i in range(26)] for n in (1, 2, 3)}
    calls = []

    def fetch_html(url):
        page = int(parse_qs(urlparse(url).query)["pgNmbr"][0])
        calls.append(page)
        return _page_html(pages.get(page, pages[3]), total_pages=83 if page == 1 else 3)

    items = LandSeaCollectionConnector(fetch_html=fetch_html).fetch(
        {"collection_url": _COLLECTION_URL}
    )

    assert calls == [1, 2, 3, 4]
    assert len(items) == 78
    assert len({item.external_id for item in items}) == 78


def test_fetch_keeps_one_listing_per_product_when_a_page_repeats_a_tile():
    first = _tile(tile_id=1, price=100.0)
    repeat = _tile(tile_id=1, price=90.0)
    page = [first, repeat] + [_tile(tile_id=n) for n in range(2, 26)]

    def fetch_html(url):
        number = int(parse_qs(urlparse(url).query)["pgNmbr"][0])
        return _page_html(page if number == 1 else [], total_pages=1)

    items = LandSeaCollectionConnector(fetch_html=fetch_html).fetch(
        {"collection_url": _COLLECTION_URL}
    )

    assert [item.external_id for item in items].count("1") == 1
    assert len(items) == 25


def test_fetch_follows_pages_bigger_than_the_expected_size():
    # A site-side page-size increase must not end pagination after the first page.
    pages = {n: [_tile(tile_id=n * 1000 + i) for i in range(30)] for n in (1, 2, 3)}
    calls = []

    def fetch_html(url):
        page = int(parse_qs(urlparse(url).query)["pgNmbr"][0])
        calls.append(page)
        return _page_html(pages.get(page, []), total_pages=None)

    items = LandSeaCollectionConnector(fetch_html=fetch_html).fetch(
        {"collection_url": _COLLECTION_URL}
    )

    assert calls == [1, 2, 3, 4]
    assert len(items) == 90


def test_fetch_follows_long_listings_to_their_short_last_page():
    calls = []

    def fetch_html(url):
        page = int(parse_qs(urlparse(url).query)["pgNmbr"][0])
        calls.append(page)
        item_count = 2 if page == 16 else 26
        tiles = [_tile(tile_id=page * 1000 + i) for i in range(item_count)]
        return _page_html(tiles, total_pages=16)

    items = LandSeaCollectionConnector(fetch_html=fetch_html).fetch(
        {"collection_url": _COLLECTION_URL}
    )

    assert calls == list(range(1, 17))
    assert len(items) == 392


def test_fetch_falls_back_to_a_short_page_when_total_pages_is_missing():
    full_page = [_tile(tile_id=i) for i in range(26)]
    short_page = [_tile(tile_id=100)]
    pages = {1: full_page, 2: short_page}
    calls = []

    def fetch_html(url):
        page = int(parse_qs(urlparse(url).query)["pgNmbr"][0])
        calls.append(page)
        return _page_html(pages.get(page, []), total_pages=None)

    items = LandSeaCollectionConnector(fetch_html=fetch_html).fetch(
        {"collection_url": _COLLECTION_URL}
    )
    assert calls == [1, 2]
    assert len(items) == 27


def test_fetch_raises_when_new_products_continue_past_the_page_cap():
    calls = []

    def fetch_html(url):
        page = int(parse_qs(urlparse(url).query)["pgNmbr"][0])
        calls.append(page)
        return _page_html([_tile(tile_id=page * 1000 + i) for i in range(26)], total_pages=176)

    # Returning the first 100 pages would retire every listing past them on ingest.
    with pytest.raises(TruncatedSnapshotError, match="exceeded the 100-page cap"):
        LandSeaCollectionConnector(fetch_html=fetch_html).fetch(
            {"collection_url": _COLLECTION_URL}
        )
    assert calls == list(range(1, 102))


def test_default_html_fetch_uses_user_agent_and_timeout():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _urlopen_response(b"<html>ok</html>")
        html = _default_fetch_html(_COLLECTION_URL)

    request = urlopen.call_args.args[0]
    assert request.get_header("User-agent") == "beehive/0.1 (personal information hub)"
    assert urlopen.call_args.kwargs["timeout"] == 20
    assert html == "<html>ok</html>"
