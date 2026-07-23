"""Presentation-layer tests for the event-driven digest: build_event_view resolves the
(Channel kind, event type) matrix into a localized label/detail, and the two renderers turn a
list of ChannelDigest sections into plain text / HTML. Content selection, capping and delivery
marking live in digest/send.py (see tests/digest/test_send.py); these tests only pin how one
already-selected event looks."""
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
    assert view.detail == "Closes: 2026-08-01T10:00:00"


def test_tracker_discovered_without_a_closing_time_has_no_detail():
    view = build_event_view(_event(channel_kind="tracker", event_type="discovered"), _EN)
    assert view.label == "New tracked item"
    assert view.detail == ""


def test_headline_falls_back_to_title_when_summary_is_missing():
    view = build_event_view(_event(item_ai_summary=None, item_title="Raw title"), _EN)
    assert view.headline == "Raw title"


def test_event_labels_are_localized():
    view = build_event_view(_event(channel_kind="monitor", event_type="price_drop"), _DE)
    assert view.label == "Preissenkung"


# --- compose_channel_digest -------------------------------------------------------------------

def test_compose_channel_digest_builds_views_and_kind_accent():
    cd = compose_channel_digest(
        "Arc'teryx Outlet",
        "monitor",
        [_event(channel_kind="monitor", event_type="discovered")],
        [],
        _EN,
    )
    assert cd.channel_kind == "monitor"
    assert cd.accent == "#0f766e"
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

def test_html_includes_channel_name_and_kind_accent():
    cd = compose_channel_digest(
        "Watchlist", "tracker",
        [_event(channel_kind="tracker", event_type="discovered")], [], _EN)
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    assert "Watchlist" in html
    assert "#b45309" in html  # tracker accent applied to the section rule


def test_html_wraps_headline_in_a_link_and_shows_the_label_badge():
    cd = compose_channel_digest(
        "Clearance", "monitor",
        [_event(channel_kind="monitor", event_type="discovered",
                item_ai_summary="Cheap tent", item_url="https://x/3")],
        [], _EN)
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    assert '<a href="https://x/3"' in html
    assert "Cheap tent</a>" in html
    assert ">New</span>" in html


def test_html_shows_price_detail_line():
    cd = compose_channel_digest(
        "Clearance", "monitor",
        [_event(channel_kind="monitor", event_type="price_drop",
                payload={"old_price": 50.0, "new_price": 40.0})],
        [], _EN)
    html = render_digest_email_html([cd], "2026-07-09", _EN, _SUBJECT)
    assert "50 \u2192 40" in html


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


def test_html_localizes_the_header_and_event_labels():
    cd = compose_channel_digest(
        "Clearance", "monitor",
        [_event(channel_kind="monitor", event_type="discovered")], [], _DE)
    html = render_digest_email_html([cd], "2026-07-09", _DE, _SUBJECT)
    assert "Tages\u00fcberblick" in html  # localized header
    assert ">Neu</span>" in html  # localized "New" badge


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
