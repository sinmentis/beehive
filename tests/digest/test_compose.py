"""Presentation-layer tests for the event-driven digest: build_event_view resolves the
(Channel kind, event type) matrix into a localized label/detail, and the two renderers turn a
list of ChannelDigest sections into plain text / HTML. Content selection, capping and delivery
marking live in digest/send.py (see tests/digest/test_send.py); these tests only pin how one
already-selected event looks."""
import pytest
from lxml.html import fromstring

from beehive.digest.compose import (
    ChannelDigest,
    EventView,
    build_event_view,
    compose_channel_digest,
    render_digest_email,
    render_digest_email_html,
)
from beehive.localization import localizer_for

_EN = localizer_for("en")
_ZH = localizer_for("zh-CN")
_DE = localizer_for("de")

_SUBJECT = "Custom Group Subject \u00b7 2026-07-09"


def _event(**overrides) -> dict:
    """A raw deliverable-event row shaped like db.item_events.list_ready_events_for_channels."""
    event = {
        "id": 1,
        "channel_id": 1,
        "channel_kind": "editorial",
        "event_type": "discovered",
        "item_ai_summary": "RBNZ cuts rates",
        "item_title": "Rates fall",
        "item_url": "https://x/1",
        "payload": {},
        "item_raw_metadata": {},
    }
    event.update(overrides)
    return event


# --- build_event_view: the single (kind, event type) -> label/detail authority ----------------

def test_editorial_discovered_is_a_bare_linked_summary():
    view = build_event_view(_event(channel_kind="editorial", event_type="discovered"), _EN)
    assert view.label == ""
    assert view.detail == ""
    assert view.headline == "RBNZ cuts rates"


def test_monitor_discovered_is_labelled_new():
    view = build_event_view(_event(channel_kind="monitor", event_type="discovered"), _EN)
    assert view.label == "New"
    assert view.detail == ""


def test_monitor_price_drop_shows_old_and_new_numbers():
    view = build_event_view(
        _event(
            channel_kind="monitor",
            event_type="price_drop",
            payload={"old_price": 50.0, "new_price": 39.99},
        ),
        _EN,
    )
    assert view.label == "Price drop"
    assert view.detail == "50 \u2192 39.99"  # whole old price trimmed, fractional kept


def test_monitor_price_drop_includes_currency_code():
    view = build_event_view(
        _event(
            channel_kind="monitor",
            event_type="price_drop",
            payload={
                "old_price": 1500.0,
                "new_price": 600.0,
                "currency_code": "EUR",
            },
        ),
        _EN,
    )

    assert view.detail == "EUR 1,500 \u2192 EUR 600"


def test_monitor_back_in_stock_is_labelled():
    view = build_event_view(
        _event(channel_kind="monitor", event_type="back_in_stock"), _EN)
    assert view.label == "Back in stock"
    assert view.detail == ""


def test_tracker_discovered_includes_its_closing_time_when_present():
    view = build_event_view(
        _event(
            channel_kind="tracker",
            event_type="discovered",
            item_raw_metadata={"closing_at": "2026-08-01T10:00:00"},
        ),
        _EN,
    )
    assert view.label == "New tracked item"
    assert view.detail == "Closes: 2026-08-01 22:00 NZST"


def test_tracker_discovered_without_a_closing_time_has_no_detail():
    view = build_event_view(_event(channel_kind="tracker", event_type="discovered"), _EN)
    assert view.label == "New tracked item"
    assert view.detail == ""


def test_tracker_deadline_uses_the_group_timezone_without_machine_precision():
    view = build_event_view(
        _event(channel_kind="tracker", item_raw_metadata={"closing_at": "2026-03-08T07:30:45.123456+00:00"}),
        _EN, timezone_name="America/New_York",
    )
    assert view.detail == "Closes: 2026-03-08 03:30 EDT"
    invalid = build_event_view(
        _event(channel_kind="tracker", item_raw_metadata={"closing_at": "not-a-date"}), _EN,
    )
    assert invalid.detail == ""


def test_configured_navigation_keeps_all_fifty_entries_and_only_links_saved_briefs():
    events = [
        _event(
            id=index, item_id=index, item_url=f"https://shop.example/{index}",
            item_ai_summary=f"Offer {index}: 30% off, or 40% off the other option",
        )
        for index in range(1, 51)
    ]
    digest = compose_channel_digest(
        "Deals", "editorial", events, [], _EN, channel_id=9,
        site_url="https://news.example/beehive/", saved_brief_ids=frozenset({1, 3}),
    )
    markup = render_digest_email_html([digest], "2026-10-29", _EN, _SUBJECT)
    _, plain = render_digest_email([digest], "2026-10-29", _EN, _SUBJECT)
    assert markup.count('href="https://shop.example/') == 50
    assert len(digest.events) == 50
    assert "Offer 50: 30% off, or 40% off the other option" in markup
    assert 'href="https://news.example/beehive/channels/9?show_read=1"' in markup
    assert "https://news.example/beehive/items/1/brief?origin=channel" in markup
    assert "https://news.example/beehive/items/3/brief?origin=channel" in markup
    assert "https://news.example/beehive/items/2/brief" not in markup
    assert "Open Beehive: https://news.example/beehive" in plain
    assert "Open saved brief: https://news.example/beehive/items/1/brief" in plain
    assert 'target="_blank" rel="noopener noreferrer"' in markup
    assert len(fromstring(markup).xpath('//td[@class="entry-block"]')) == 50
    assert len(markup.encode("utf-8")) < 102_400


def test_unconfigured_website_does_not_invent_internal_links():
    digest = compose_channel_digest(
        "Deals", "editorial", [_event(item_id=1)], [], _EN,
        channel_id=9, saved_brief_ids=frozenset({1}),
    )
    markup = render_digest_email_html([digest], "2026-10-29", _EN, _SUBJECT)
    assert "Open channel" not in markup
    assert "Open saved brief" not in markup
    assert "/items/1/brief" not in markup


def test_headline_falls_back_to_title_when_summary_is_missing():
    view = build_event_view(_event(item_ai_summary=None, item_title="Raw title"), _EN)
    assert view.headline == "Raw title"


def test_event_labels_are_localized():
    view = build_event_view(_event(channel_kind="monitor", event_type="price_drop"), _DE)
    assert view.label == "Preissenkung"


# --- compose_channel_digest -------------------------------------------------------------------

def test_compose_channel_digest_builds_kind_aware_views():
    cd = compose_channel_digest(
        "Arc'teryx Outlet",
        "monitor",
        [_event(channel_kind="monitor", event_type="discovered")],
        [],
        _EN,
    )
    assert cd.channel_kind == "monitor"
    assert [type(e) for e in cd.events] == [EventView]
    assert cd.events[0].label == "New"


# --- plain text -------------------------------------------------------------------------------

def test_plain_text_editorial_line_is_the_familiar_summary_and_url():
    cd = compose_channel_digest(
        "NZ Finance", "editorial", [_event()], [], _EN)
    _, body = render_digest_email([cd], "2026-07-09", _EN, _SUBJECT)
    assert "- RBNZ cuts rates (https://x/1)" in body


def test_plain_text_tags_event_semantics_without_raw_json():
    cd = compose_channel_digest(
        "Clearance",
        "monitor",
        [
            _event(channel_kind="monitor", event_type="price_drop",
                   item_ai_summary="Jacket", item_url="https://x/2",
                   payload={"old_price": 200.0, "new_price": 150.0}),
        ],
        [],
        _EN,
    )
    _, body = render_digest_email([cd], "2026-07-09", _EN, _SUBJECT)
    assert "- [Price drop \u00b7 200 \u2192 150] Jacket (https://x/2)" in body
    assert "old_price" not in body  # no raw payload leaks into the email


def test_plain_text_includes_source_warning():
    cd = ChannelDigest(
        channel_name="NZ Finance", channel_kind="editorial", events=[],
        source_warnings=["reddit_subreddit source fetch failed: timeout"])
    _, body = render_digest_email([cd], "2026-07-09", _EN, _SUBJECT)
    assert "! reddit_subreddit source fetch failed: timeout" in body


def test_plain_text_returns_the_given_subject_verbatim():
    subject, _ = render_digest_email([], "2026-07-09", _EN, _SUBJECT)
    assert subject == _SUBJECT


# --- HTML -------------------------------------------------------------------------------------

def test_html_keeps_channel_context_without_visible_headings():
    cd = compose_channel_digest(
        "Watchlist", "tracker",
        [_event(channel_kind="tracker", event_type="discovered")], [], _EN)
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    assert "Watchlist" in html
    assert not fromstring(html).xpath("//h1 | //h2 | //hr")


def test_html_groups_summary_event_label_and_source_in_one_block():
    cd = compose_channel_digest(
        "Clearance", "monitor",
        [_event(channel_kind="monitor", event_type="discovered",
                item_ai_summary="Cheap tent", item_url="https://x/3")],
        [], _EN)
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    block, = fromstring(html).xpath('//td[@class="entry-block"]')
    assert block.xpath('string(p[@class="summary"])') == "Cheap tent"
    assert block.xpath('string(p[@class="decision"]/strong)') == "New"
    source, = block.xpath('p[@class="references"]/a')
    assert source.get("href") == "https://x/3"
    assert source.text == "x"
    assert "Cheap tent" not in source.text_content()


def test_html_shows_price_detail_line():
    cd = compose_channel_digest(
        "Clearance", "monitor",
        [_event(channel_kind="monitor", event_type="price_drop",
                payload={"old_price": 50.0, "new_price": 40.0})],
        [], _EN)
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    decision, = fromstring(html).xpath('//p[@class="decision"]')
    assert decision.text_content() == "Price drop: 50 \u2192 40"
    assert decision.xpath("string(strong)") == "40"


def test_html_shows_warning_in_a_distinct_block():
    cd = ChannelDigest(
        channel_name="NZ Finance", channel_kind="editorial", events=[],
        source_warnings=["reddit_subreddit source fetch failed: timeout"])
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    assert "reddit_subreddit source fetch failed: timeout" in html


def test_html_includes_the_date_and_page_title():
    html = render_digest_email_html([], "2026-07-09", _EN, _SUBJECT)
    assert "2026-07-09" in html
    assert f"<title>{_SUBJECT}</title>" in html


def test_html_lang_attribute_reflects_the_selected_language():
    assert '<html lang="en">' in render_digest_email_html([], "2026-07-09", _EN, _SUBJECT)
    assert '<html lang="de">' in render_digest_email_html([], "2026-07-09", _DE, _SUBJECT)


def test_html_localizes_the_preheader_and_event_labels():
    cd = compose_channel_digest(
        "Clearance", "monitor",
        [_event(channel_kind="monitor", event_type="discovered")], [], _DE)
    html = render_digest_email_html([cd], "2026-07-09", _DE, _SUBJECT)
    assert "Tages\u00fcberblick" in html
    assert ">Neu</strong>" in html


def test_plain_text_empty_state_is_localized_header_only_for_warning_sections():
    # A warning-only section still renders in the selected language's framing.
    cd = ChannelDigest(
        channel_name="NZ Finance", channel_kind="editorial", events=[],
        source_warnings=["reddit_subreddit source fetch failed: timeout"])
    _, body = render_digest_email([cd], "2026-07-09", _ZH, _SUBJECT)
    assert "NZ Finance" in body


# --- URL / HTML safety (must survive the event-model rewrite) ----------------------------------

def test_html_uses_safe_href_for_event_links():
    cd = compose_channel_digest(
        "NZ Finance", "editorial",
        [_event(item_url="https://x/1")], [], _EN)
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    assert '<a href="https://x/1"' in html


def test_html_blocks_unsafe_url_schemes():
    cd = compose_channel_digest(
        "NZ Finance", "editorial",
        [_event(item_url="javascript:alert(1)")], [], _EN)
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    assert "javascript:" not in html
    assert '<a href="#"' in html


def test_html_escapes_untrusted_text():
    cd = compose_channel_digest(
        "<script>alert(1)</script>", "editorial",
        [_event(item_ai_summary="<b>boom</b>")], [], _EN)
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>boom</b>" not in html
    assert "&lt;b&gt;boom&lt;/b&gt;" in html


@pytest.mark.parametrize("url,label,href", [
    ("https://www.news.example:8443/story?query=1", "news.example",
     "https://www.news.example:8443/story?query=1"),
    ("javascript:alert(1)", "Open source", "#"),
    ("https://[invalid", "Open source", "#"),
    ("", "Open source", "#"),
])
def test_source_reference_uses_the_host_or_a_safe_localized_fallback(url, label, href):
    digest = compose_channel_digest("News", "editorial", [_event(item_url=url)], [], _EN)
    document = fromstring(render_digest_email_html([digest], "2026-10-30", _EN, _SUBJECT))
    source, = document.xpath('//p[@class="references"]/a')
    assert source.text == label
    assert source.get("href") == href


def test_tracker_deadline_is_primary_and_its_label_stays_with_the_source():
    digest = compose_channel_digest(
        "Auctions", "tracker",
        [_event(channel_kind="tracker", item_raw_metadata={"closing_at": "2026-10-31T06:30:00Z"})],
        [], _EN, timezone_name="Pacific/Auckland",
    )
    document = fromstring(render_digest_email_html([digest], "2026-10-30", _EN, _SUBJECT))
    block, = document.xpath('//td[@class="entry-block"]')
    assert block.xpath('string(p[@class="decision"])') == "Closes: 2026-10-31 19:30 NZDT"
    assert block.xpath('p[@class="decision"]/strong')
    assert "19:30 NZDT" in block.xpath('p[@class="decision"]/strong/span/text()')
    assert block.xpath('string(p[@class="references"])').startswith("New tracked item")
    _, plain = render_digest_email([digest], "2026-10-30", _EN, _SUBJECT)
    assert "[New tracked item \u00b7 Closes: 2026-10-31 19:30 NZDT]" in plain


def test_formatted_prices_do_not_allow_html_from_malformed_payload_values():
    digest = compose_channel_digest(
        "Deals", "monitor",
        [_event(channel_kind="monitor", event_type="price_drop",
                payload={"old_price": "<img src=x>", "new_price": "<script>alert(1)</script>"})],
        [], _EN,
    )
    markup = render_digest_email_html([digest], "2026-10-30", _EN, _SUBJECT)
    document = fromstring(markup)
    assert not document.xpath("//img | //script")
    assert document.xpath('string(//p[@class="decision"]/strong)') == "<script>alert(1)</script>"
    assert "&lt;img" in markup


def test_warning_precedes_affected_entries_and_quiet_channel_status_is_retained():
    digests = [
        compose_channel_digest(
            "Deals", "monitor", [_event(channel_kind="monitor")],
            ["One source timed out."], _EN, status="2/3 sources OK", remaining_count=4,
            channel_id=2, site_url="https://beehive.example",
        ),
        compose_channel_digest(
            "Quiet channel", "editorial", [], [], _EN,
            status="No changes since the last email",
        ),
    ]
    markup = render_digest_email_html(digests, "2026-10-30", _EN, _SUBJECT)
    document = fromstring(markup)
    assert markup.index("One source timed out.") < markup.index('class="entry-block"')
    assert "Deals: One source timed out." in document.text_content()
    assert len(document.xpath('//td[@class="entry-block"]')) == 1
    assert "Quiet channel: No changes since the last email" in document.xpath('string(//div[@class="notes"])')
    assert document.xpath('string(//p[@class="channel-return"])').startswith(
        "\u2026and 4 more not shown. View Deals",
    )
    assert document.xpath('//p[@class="channel-return"]/a/text()') == ["View Deals"]


@pytest.mark.parametrize("language", ["en", "zh-CN", "ja", "ko", "es", "fr", "de"])
def test_all_locales_render_named_channel_links_and_source_fallback(language):
    localizer = localizer_for(language)
    digest = compose_channel_digest(
        "Channel <one>", "editorial", [_event(item_url="")], [], localizer,
        channel_id=1, site_url="https://beehive.example",
    )
    markup = render_digest_email_html([digest], "2026-10-30", localizer, _SUBJECT)
    document = fromstring(markup)
    assert document.xpath('string(//p[@class="channel-return"]/a)') == localizer.text(
        "background.digest_view_channel", channel="Channel <one>",
    )
    assert document.xpath('string(//p[@class="references"]/a)') == localizer.text(
        "background.digest_open_source",
    )
    assert not document.xpath("//one")


def test_chinese_email_uses_consistent_punctuation_for_facts_warnings_and_status():
    digest = compose_channel_digest(
        "Deals", "monitor",
        [_event(channel_kind="monitor", event_type="price_drop",
                payload={"old_price": 50, "new_price": 40})],
        ["Source timeout"], _ZH, status="2/3 OK", remaining_count=4,
        channel_id=1, site_url="https://beehive.example",
    )
    document = fromstring(render_digest_email_html([digest], "2026-10-30", _ZH, _SUBJECT))
    assert document.xpath('string(//p[@class="decision"])') == "\u964d\u4ef7\uff1a50 \u2192 40"
    assert document.xpath('string(//p[@class="source-warning"])') == "Deals\uff1aSource timeout"
    assert document.xpath('string(//div[@class="notes"]/p[1])') == "Deals\uff1a2/3 OK"
    assert document.xpath('string(//p[@class="channel-return"])').startswith(
        "\u53e6\u6709 4 \u6761\u672a\u663e\u793a\u3002 ",
    )
