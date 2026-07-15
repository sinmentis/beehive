from beehive.digest.compose import (
    ChannelDigest,
    compose_channel_digest,
    render_digest_email,
    render_digest_email_html,
)
from beehive.localization import localizer_for

_EN = localizer_for("en")
_ZH = localizer_for("zh-CN")
_DE = localizer_for("de")


def test_compose_channel_digest_caps_at_highlight_count():
    items = [{"ai_summary": f"item {i}", "url": f"https://x/{i}"} for i in range(15)]
    cd = compose_channel_digest("NZ Finance", items, [], highlight_count=8)
    assert len(cd.highlighted) == 8


def test_render_digest_shows_reassuring_empty_state():
    cd = ChannelDigest(channel_name="NZ Finance", highlighted=[], source_warnings=[])
    subject, body = render_digest_email([cd], "2026-07-09", _EN)
    assert "No new items today" in body
    assert "NZ Finance" in body


def test_render_digest_includes_source_warning():
    cd = ChannelDigest(channel_name="NZ Finance", highlighted=[],
                        source_warnings=["reddit_subreddit source fetch failed: timeout"])
    _, body = render_digest_email([cd], "2026-07-09", _EN)
    assert "⚠ reddit_subreddit source fetch failed: timeout" in body


def test_render_digest_lists_highlighted_items():
    cd = ChannelDigest(channel_name="NZ Finance",
                        highlighted=[{"ai_summary": "RBNZ cuts rates", "url": "https://x/1"}],
                        source_warnings=[])
    _, body = render_digest_email([cd], "2026-07-09", _EN)
    assert "RBNZ cuts rates" in body
    assert "https://x/1" in body


def test_render_digest_subject_includes_date():
    subject, _ = render_digest_email([], "2026-07-09", _EN)
    assert "2026-07-09" in subject


def test_render_digest_subject_includes_product_name():
    subject, _ = render_digest_email([], "2026-07-09", _EN)
    assert "Beehive" in subject


def test_render_digest_subject_and_empty_state_use_the_selected_non_english_language():
    subject, _ = render_digest_email([], "2026-07-09", _ZH)
    assert "蜂巢" in subject
    assert "2026-07-09" in subject
    cd = ChannelDigest(channel_name="NZ Finance", highlighted=[], source_warnings=[])
    _, body = render_digest_email([cd], "2026-07-09", _ZH)
    assert "今天没有新内容" in body


def test_render_digest_html_includes_channel_name():
    cd = ChannelDigest(channel_name="NZ Finance", highlighted=[], source_warnings=[])
    html = render_digest_email_html([cd], "2026-07-09", _EN)
    assert "NZ Finance" in html


def test_render_digest_html_wraps_item_summary_in_a_link_not_raw_url_text():
    cd = ChannelDigest(channel_name="NZ Finance",
                        highlighted=[{"ai_summary": "RBNZ cuts rates", "url": "https://x/1"}],
                        source_warnings=[])
    html = render_digest_email_html([cd], "2026-07-09", _EN)
    assert '<a href="https://x/1"' in html
    assert "RBNZ cuts rates</a>" in html


def test_render_digest_html_shows_warning_in_a_distinct_block():
    cd = ChannelDigest(channel_name="NZ Finance", highlighted=[],
                        source_warnings=["reddit_subreddit source fetch failed: timeout"])
    html = render_digest_email_html([cd], "2026-07-09", _EN)
    assert "⚠ reddit_subreddit source fetch failed: timeout" in html


def test_render_digest_html_shows_reassuring_empty_state():
    cd = ChannelDigest(channel_name="NZ Finance", highlighted=[], source_warnings=[])
    html = render_digest_email_html([cd], "2026-07-09", _EN)
    assert "No new items today" in html


def test_render_digest_html_includes_the_date():
    html = render_digest_email_html([], "2026-07-09", _EN)
    assert "2026-07-09" in html


def test_render_digest_html_lang_attribute_reflects_the_selected_language():
    html_en = render_digest_email_html([], "2026-07-09", _EN)
    assert '<html lang="en">' in html_en
    html_de = render_digest_email_html([], "2026-07-09", _DE)
    assert '<html lang="de">' in html_de


def test_render_digest_html_localizes_the_header_and_empty_state():
    cd = ChannelDigest(channel_name="NZ Finance", highlighted=[], source_warnings=[])
    html = render_digest_email_html([cd], "2026-07-09", _DE)
    assert "Tages\u00fcberblick" in html
    assert "Heute keine neuen Inhalte" in html


def test_render_digest_html_uses_safe_href_for_item_links():
    cd = ChannelDigest(channel_name="NZ Finance",
                        highlighted=[{"ai_summary": "Rates fall", "url": "https://x/1"}],
                        source_warnings=[])
    html = render_digest_email_html([cd], "2026-07-09", _EN)
    assert '<a href="https://x/1"' in html


def test_render_digest_html_blocks_unsafe_url_schemes():
    cd = ChannelDigest(channel_name="NZ Finance",
                        highlighted=[{"ai_summary": "Rates fall",
                                      "url": "javascript:alert(1)"}],
                        source_warnings=[])
    html = render_digest_email_html([cd], "2026-07-09", _EN)
    assert "javascript:" not in html
    assert '<a href="#"' in html


def test_render_digest_html_escapes_untrusted_text():
    cd = ChannelDigest(channel_name="<script>alert(1)</script>",
                        highlighted=[{"ai_summary": "safe text", "url": "https://x/1"}],
                        source_warnings=[])
    html = render_digest_email_html([cd], "2026-07-09", _EN)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
