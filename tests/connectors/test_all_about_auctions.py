from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from beehive.connectors.all_about_auctions import (
    AllAboutAuctionsConnector,
    _BUYER_PREMIUM_RATE,
    _CRAWL_DELAY_SECONDS,
    _UPCOMING_AUCTIONS_URL,
    _default_fetch_text,
    _extract_rrp,
    _lot_page_url,
)


def _homepage(*links: tuple[str, str]) -> str:
    anchors = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
    return f"<html><body>{anchors}</body></html>"


def _homepage_with_view_vars(
    *records: dict[str, object],
    total_num_results: int | None = None,
    links: tuple[tuple[str, str], ...] = (),
) -> str:
    anchors = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
    payload = {
        "auctions": {
            "result_page": list(records),
            "query_info": {
                "total_num_results": (
                    len(records) if total_num_results is None else total_num_results
                )
            },
        }
    }
    return (
        f"<html><body>{anchors}<script>viewVars = {json.dumps(payload)};</script>"
        "</body></html>"
    )


def _lot_record(
    lot_id: str = "1-LOT123",
    *,
    lot_number: int = 7,
    title: str = "FISHER & PAYKEL OVEN",
    description: str = "Unused oven, RRP $1,500+GST, viewing advised",
    current_bid: str | None = "500.00",
    starting_price: str | None = "100.00",
    estimate_low: str | None = None,
    estimate_high: str | None = None,
    sold_price: str | None = None,
    status: str = "active",
    bidding_enabled: bool = True,
) -> dict[str, object]:
    return {
        "row_id": lot_id,
        "lot_number": lot_number,
        "title": title,
        "truncated_description": description,
        "_detail_url": f"/lots/view/{lot_id}/fisher-and-paykel-oven",
        "_slug": "fisher-and-paykel-oven",
        "cover_thumbnail": "https://images.example/oven.jpg",
        "last_updated": "2026-07-21T04:06:17Z",
        "status": status,
        "bidding_enabled": bidding_enabled,
        "currency_code": "NZD",
        "timed_auction_bid": (
            {"amount": current_bid, "updated_at": "2026-07-22T09:01:20Z"}
            if current_bid is not None
            else None
        ),
        "highest_live_bid": None,
        "starting_price": starting_price,
        "estimate_low": estimate_low,
        "estimate_high": estimate_high,
        "sold_price": sold_price,
    }


def _lot_page(
    *records: dict[str, object],
    total_num_results: int | None = None,
    page_size: int = 100,
    offset: int = 0,
) -> str:
    return json.dumps(
        {
            "result_page": list(records),
            "query_info": {
                "page_size": page_size,
                "page_start_offset": offset,
                "total_num_results": (
                    len(records) if total_num_results is None else total_num_results
                ),
            },
            "responseCode": 200,
        }
    )


def _connector(payloads: dict[str, str], calls: list[str], sleeps: list[float]):
    def fetch_text(url: str) -> str:
        calls.append(url)
        return payloads[url]

    return AllAboutAuctionsConnector(fetch_text=fetch_text, sleep=sleeps.append)


def test_type_key_and_refresh_policy():
    connector = AllAboutAuctionsConnector()
    assert connector.type_key == "all_about_auctions"
    assert connector.refresh_existing_items is True


def test_validate_config_accepts_only_an_empty_object():
    connector = AllAboutAuctionsConnector(
        fetch_text=lambda url: "", sleep=lambda seconds: None
    )
    connector.validate_config({})
    for config in (None, [], {"auction_id": "1-A"}):
        with pytest.raises(ValueError, match="empty object"):
            connector.validate_config(config)  # type: ignore[arg-type]


def test_fetch_maps_bid_rrp_description_and_auction_metadata():
    auction_id = "1-D3DUXH"
    auction_title = "Timed Online Only General Goods Auction, closing Thursday"
    lot_url = _lot_page_url(auction_id, 0)
    calls: list[str] = []
    sleeps: list[float] = []
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {
                    "row_id": auction_id,
                    "title": auction_title,
                    "effective_end_time": "2026-07-23T00:22:40Z",
                    "lot_count": 1,
                }
            ),
            lot_url: _lot_page(_lot_record()),
        },
        calls,
        sleeps,
    )

    item = connector.fetch({})[0]

    assert item.external_id == "1-LOT123"
    assert item.title == "FISHER & PAYKEL OVEN"
    assert item.url == (
        "https://auctions.allaboutauctions.co.nz/lots/view/"
        "1-LOT123/fisher-and-paykel-oven"
    )
    assert item.body == "Unused oven, RRP $1,500+GST, viewing advised"
    assert item.created_at is None
    assert item.raw_metadata == {
        "price": 500.0,
        "compare_at_price": 1500.0,
        "on_sale": True,
        "available": True,
        "vendor": None,
        "product_type": "Auction lot",
        "tags": ["auction"],
        "listing_kind": "auction_lot",
        "auction_house": "All About Auctions",
        "auction_id": auction_id,
        "auction_title": auction_title,
        "closing_at": "2026-07-23T00:22:40Z",
        "lot_number": 7,
        "currency_code": "NZD",
        "current_bid": 500.0,
        "buyer_premium_rate": _BUYER_PREMIUM_RATE,
        "estimated_cost": 585.0,
        "rrp": 1500.0,
        "rrp_excludes_gst": True,
        "starting_price": 100.0,
        "estimate_low": None,
        "estimate_high": None,
        "sold_price": None,
        "status": "active",
        "image_url": "https://images.example/oven.jpg",
    }
    assert calls == [_UPCOMING_AUCTIONS_URL, lot_url]
    assert sleeps == [_CRAWL_DELAY_SECONDS]


def test_fetch_prefers_lot_extended_closing_time():
    auction_id = "1-A"
    record = _lot_record()
    record["extended_end_time"] = "2026-07-23T03:15:00Z"
    record["auction"] = {
        "effective_end_time": "2026-07-23T02:00:00Z",
        "extended_end_time": "2026-07-23T03:00:00Z",
    }
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {
                    "row_id": auction_id,
                    "title": "Auction",
                    "effective_end_time": "2026-07-23T01:00:00Z",
                    "lot_count": 1,
                }
            ),
            _lot_page_url(auction_id, 0): _lot_page(record),
        },
        [],
        [],
    )

    item = connector.fetch({})[0]

    assert item.raw_metadata["closing_at"] == "2026-07-23T03:15:00Z"


def test_fetch_uses_nested_auction_closing_time_when_homepage_omits_it():
    auction_id = "1-A"
    record = _lot_record()
    record["extended_end_time"] = None
    record["auction"] = {
        "effective_end_time": "2026-07-23T02:00:00Z",
        "extended_end_time": None,
    }
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {"row_id": auction_id, "title": "Auction", "lot_count": 1}
            ),
            _lot_page_url(auction_id, 0): _lot_page(record),
        },
        [],
        [],
    )

    item = connector.fetch({})[0]

    assert item.raw_metadata["closing_at"] == "2026-07-23T02:00:00Z"


def test_fetch_keeps_no_bid_distinct_from_a_zero_price():
    auction_id = "1-A"
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {"row_id": auction_id, "title": "Auction", "lot_count": 1}
            ),
            _lot_page_url(auction_id, 0): _lot_page(
                _lot_record(
                    current_bid=None,
                    description="Unused stock",
                    starting_price="250.00",
                )
            ),
        },
        [],
        [],
    )

    metadata = connector.fetch({})[0].raw_metadata

    assert metadata["price"] is None
    assert metadata["current_bid"] is None
    assert metadata["estimated_cost"] is None
    assert metadata["starting_price"] == 250.0
    assert metadata["on_sale"] is False


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("RRP $15,000+GST", (15000.0, True)),
        ("R.R.P. NZ$2,499.95 incl GST", (2499.95, False)),
        ("RRP: 1040 ex GST", (1040.0, True)),
        ("No retail price supplied", (None, False)),
    ],
)
def test_extract_rrp_handles_common_description_formats(description, expected):
    assert _extract_rrp(description) == expected


def test_fetch_paginates_in_batches_of_one_hundred():
    auction_id = "1-A"
    first_page = [
        _lot_record(f"1-LOT{index}", lot_number=index) for index in range(100)
    ]
    second_page = [
        _lot_record(f"1-LOT{index}", lot_number=index) for index in range(100, 150)
    ]
    calls: list[str] = []
    sleeps: list[float] = []
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {"row_id": auction_id, "title": "Large auction", "lot_count": 150}
            ),
            _lot_page_url(auction_id, 0): _lot_page(*first_page, total_num_results=150),
            _lot_page_url(auction_id, 100): _lot_page(
                *second_page,
                total_num_results=150,
                offset=100,
            ),
        },
        calls,
        sleeps,
    )

    items = connector.fetch({})

    assert len(items) == 150
    assert calls == [
        _UPCOMING_AUCTIONS_URL,
        _lot_page_url(auction_id, 0),
        _lot_page_url(auction_id, 100),
    ]
    assert sleeps == [_CRAWL_DELAY_SECONDS, _CRAWL_DELAY_SECONDS]


def test_fetch_uses_embedded_data_to_discover_auctions_missing_from_page_links():
    calls: list[str] = []
    sleeps: list[float] = []
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {"row_id": "1-A", "title": "First auction", "lot_count": 1},
                {"row_id": "1-B", "title": "Second auction", "lot_count": 1},
                links=(("/auctions/1-A/first", "First auction"),),
            ),
            _lot_page_url("1-A", 0): _lot_page(_lot_record("1-LOTA")),
            _lot_page_url("1-B", 0): _lot_page(_lot_record("1-LOTB")),
        },
        calls,
        sleeps,
    )

    items = connector.fetch({})

    assert [item.external_id for item in items] == ["1-LOTA", "1-LOTB"]
    assert items[1].raw_metadata["auction_title"] == "Second auction"
    assert calls == [
        _UPCOMING_AUCTIONS_URL,
        _lot_page_url("1-A", 0),
        _lot_page_url("1-B", 0),
    ]


def test_fetch_falls_back_to_rendered_auction_links():
    auction_id = "1-A"
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage(
                (f"/auctions/{auction_id}", "General auction")
            ),
            _lot_page_url(auction_id, 0): _lot_page(_lot_record()),
        },
        [],
        [],
    )

    item = connector.fetch({})[0]

    assert item.raw_metadata["auction_title"] == "General auction"


def test_fetch_rejects_an_incomplete_embedded_auction_result_page():
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {"row_id": "1-A", "title": "First auction", "lot_count": 1},
                total_num_results=2,
            )
        },
        [],
        [],
    )

    with pytest.raises(RuntimeError, match="only 1 of 2 upcoming auctions"):
        connector.fetch({})


def test_fetch_skips_announced_auctions_without_published_lots():
    calls: list[str] = []
    sleeps: list[float] = []
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {
                    "row_id": "1-PENDING",
                    "title": "Announced auction",
                    "lot_count": 0,
                },
                {
                    "row_id": "1-PUBLISHED",
                    "title": "Published auction",
                    "lot_count": 1,
                },
            ),
            _lot_page_url("1-PUBLISHED", 0): _lot_page(_lot_record()),
        },
        calls,
        sleeps,
    )

    assert [item.external_id for item in connector.fetch({})] == ["1-LOT123"]
    assert calls == [_UPCOMING_AUCTIONS_URL, _lot_page_url("1-PUBLISHED", 0)]
    assert sleeps == [_CRAWL_DELAY_SECONDS]


def test_fetch_ignores_terms_and_conditions_boilerplate_lots():
    auction_id = "1-A"
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {"row_id": auction_id, "title": "Auction", "lot_count": 2}
            ),
            _lot_page_url(auction_id, 0): _lot_page(
                _lot_record("1-TERMS", title="TERMS & CONDITIONS"),
                _lot_record("1-LOT123"),
            ),
        },
        [],
        [],
    )

    assert [item.external_id for item in connector.fetch({})] == ["1-LOT123"]


def test_fetch_rejects_invalid_lot_json():
    auction_id = "1-A"
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {"row_id": auction_id, "title": "Auction", "lot_count": 1}
            ),
            _lot_page_url(auction_id, 0): "{not-json",
        },
        [],
        [],
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        connector.fetch({})


def test_fetch_rejects_a_truncated_lot_result_set():
    auction_id = "1-A"
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {"row_id": auction_id, "title": "Auction", "lot_count": 2}
            ),
            _lot_page_url(auction_id, 0): _lot_page(_lot_record(), total_num_results=2),
            _lot_page_url(auction_id, 100): _lot_page(total_num_results=2, offset=100),
        },
        [],
        [],
    )

    with pytest.raises(RuntimeError, match="ended before all lots were returned"):
        connector.fetch({})


def test_fetch_returns_empty_without_extra_requests_when_no_auctions_are_upcoming():
    calls: list[str] = []
    sleeps: list[float] = []

    items = _connector({_UPCOMING_AUCTIONS_URL: _homepage()}, calls, sleeps).fetch({})

    assert items == []
    assert calls == [_UPCOMING_AUCTIONS_URL]
    assert sleeps == []


def test_repeated_fetches_keep_the_crawl_delay_across_cycles():
    calls: list[str] = []
    sleeps: list[float] = []
    connector = _connector({_UPCOMING_AUCTIONS_URL: _homepage()}, calls, sleeps)

    connector.fetch({})
    connector.fetch({})

    assert calls == [_UPCOMING_AUCTIONS_URL, _UPCOMING_AUCTIONS_URL]
    assert sleeps == [_CRAWL_DELAY_SECONDS]


def test_fetch_raises_when_upcoming_auctions_have_no_usable_lots():
    auction_id = "1-A"
    connector = _connector(
        {
            _UPCOMING_AUCTIONS_URL: _homepage_with_view_vars(
                {"row_id": auction_id, "title": "Auction", "lot_count": 1}
            ),
            _lot_page_url(auction_id, 0): _lot_page(
                _lot_record("1-TERMS", title="TERMS AND CONDITIONS")
            ),
        },
        [],
        [],
    )

    with pytest.raises(RuntimeError, match="no usable lots"):
        connector.fetch({})


def test_default_text_fetch_uses_ajax_headers_and_timeout():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok": true}'
    url = _lot_page_url("1-A", 0)

    with patch(
        "beehive.connectors.all_about_auctions.urllib.request.urlopen",
        return_value=response,
    ) as urlopen:
        payload = _default_fetch_text(url)

    request = urlopen.call_args.args[0]
    assert request.get_header("User-agent") == "beehive/0.1 (personal information hub)"
    assert request.get_header("X-requested-with") == "XMLHttpRequest"
    assert request.get_header("Referer") == "https://auctions.allaboutauctions.co.nz/"
    assert urlopen.call_args.kwargs["timeout"] == 20
    assert payload == '{"ok": true}'
