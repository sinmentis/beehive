import pytest

from beehive.localization import localizer_for
from beehive.source_labels import derived_source_label, source_display_name, source_label


@pytest.fixture
def t():
    return localizer_for("en")


@pytest.mark.parametrize(
    ("source_type", "config", "expected"),
    [
        ("reddit_subreddit", {"subreddit": "PersonalFinanceNZ"}, "r/PersonalFinanceNZ"),
        ("google_news_query", {"query": "OCR"}, '"OCR"'),
        ("all_about_auctions", {}, "All About Auctions"),
        (
            "shopify_collection",
            {"collection_url": "https://shop.example.com/collections/sale?page=2"},
            "shop.example.com/collections/sale",
        ),
        ("international_clearance", {"retailer": "mytheresa"}, "Mytheresa"),
        ("rbnz_news", {}, "RBNZ News"),
        ("hackernews_stories", {"feed": "top"}, "HN · Top"),
        ("something_new", {}, "something_new"),
    ],
)
def test_source_label_names_each_source_type(t, source_type, config, expected):
    assert source_label(source_type, config, t) == expected


def test_admin_label_keeps_the_clearance_threshold(t):
    source = {
        "type": "international_clearance",
        "config": '{"retailer": "yoox", "minimum_discount_percent": 80}',
    }
    assert derived_source_label(source, t) == "YOOX · 80%+"


def test_display_name_prefers_the_owner_name(t):
    source = {"type": "reddit_subreddit", "config": '{"subreddit": "nz"}', "name": " NZ news "}
    assert source_display_name(source, t) == "NZ news"
    assert source_display_name({**source, "name": ""}, t) == "r/nz"


def test_unreadable_config_still_gets_a_label(t):
    assert source_display_name({"type": "google_news_query", "config": "not json"}, t) == '""'
    assert source_display_name({"type": "rbnz_news"}, t) == "RBNZ News"


def test_channel_source_line_names_the_clearance_retailer(t):
    from beehive.web.public import _source_summary

    sources = [{"type": "international_clearance", "config": '{"retailer": "end"}'}]
    assert _source_summary(sources, t) == "END."
