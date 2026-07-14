from datetime import datetime, timezone

import pytest

from beehive.connectors.base import CommentFetchTarget
from beehive.connectors.reddit import RedditSubredditConnector

_ATOM_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    '<title>Personal Finance New Zealand</title>'
)
_ATOM_FOOTER = "</feed>"


def _feed(*entries_xml: str) -> bytes:
    return (_ATOM_HEADER + "".join(entries_xml) + _ATOM_FOOTER).encode("utf-8")


def _comment_target(url: str) -> CommentFetchTarget:
    return CommentFetchTarget(external_id="t3_op", url=url, raw_metadata={})


def _selftext_entry(
    entry_id="t3_1usbrjb",
    title="Rates fall",
    author="/u/kiwi_saver_nerd",
    permalink="https://www.reddit.com/r/PersonalFinanceNZ/comments/1usbrjb/rates_fall/",
    published="2026-07-10T03:11:53+00:00",
    body_paragraphs=("Body text here.",),
):
    # Mirrors the real shape observed from https://www.reddit.com/r/<sub>/hot/.rss:
    # a self-text post wraps its body in exactly one <div class="md">, followed by the
    # "submitted by ... [link] [comments]" boilerplate that trails every entry either way.
    paragraphs = " ".join(f"&lt;p&gt;{p}&lt;/p&gt;" for p in body_paragraphs)
    return (
        "<entry>"
        f'<author><name>{author}</name></author>'
        f"<content type=\"html\">&lt;!-- SC_OFF --&gt;&lt;div class=&quot;md&quot;&gt;"
        f"{paragraphs}&lt;/div&gt;&lt;!-- SC_ON --&gt; &amp;#32; submitted by &amp;#32; "
        f'&lt;a href=&quot;https://www.reddit.com/user/x&quot;&gt;{author}&lt;/a&gt; '
        f"&lt;br/&gt; &lt;span&gt;&lt;a href=&quot;{permalink}&quot;&gt;[link]&lt;/a&gt;&lt;/span&gt; "
        f'&amp;#32; &lt;span&gt;&lt;a href=&quot;{permalink}&quot;&gt;[comments]&lt;/a&gt;&lt;/span&gt;</content>'
        f"<id>{entry_id}</id>"
        f'<link href="{permalink}" />'
        f"<published>{published}</published>"
        f"<title>{title}</title>"
        "</entry>"
    )


def _link_only_entry(
    entry_id="t3_1urt6be",
    title="Putin likely to escalate Ukraine war",
    author="/u/some_reporter",
    permalink="https://www.reddit.com/r/worldnews/comments/1urt6be/putin/",
    external_link="https://www.reuters.com/world/europe/putin-article/",
    published="2026-07-09T21:02:21+00:00",
):
    # Mirrors a pure link post: NO <div class="md"> at all, just the boilerplate footer.
    return (
        "<entry>"
        f"<author><name>{author}</name></author>"
        '<content type="html">&amp;#32; submitted by &amp;#32; '
        f'&lt;a href=&quot;https://www.reddit.com/user/x&quot;&gt;{author}&lt;/a&gt; '
        f"&lt;br/&gt; &lt;span&gt;&lt;a href=&quot;{external_link}&quot;&gt;[link]&lt;/a&gt;&lt;/span&gt; "
        f'&amp;#32; &lt;span&gt;&lt;a href=&quot;{permalink}&quot;&gt;[comments]&lt;/a&gt;&lt;/span&gt;</content>'
        f"<id>{entry_id}</id>"
        f'<link href="{permalink}" />'
        f"<published>{published}</published>"
        f"<title>{title}</title>"
        "</entry>"
    )


def test_fetch_maps_selftext_entries_to_raw_items():
    fake_fetch = lambda subreddit, limit: _feed(_selftext_entry())  # noqa: E731
    connector = RedditSubredditConnector(fetch_rss=fake_fetch)
    items = connector.fetch({"subreddit": "PersonalFinanceNZ"})

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "t3_1usbrjb"
    assert item.title == "Rates fall"
    assert item.url == "https://www.reddit.com/r/PersonalFinanceNZ/comments/1usbrjb/rates_fall/"
    assert item.body == "Body text here."
    assert item.created_at == datetime(2026, 7, 10, 3, 11, 53, tzinfo=timezone.utc)
    assert item.raw_metadata["author"] == "kiwi_saver_nerd"
    assert "score" not in item.raw_metadata
    assert "num_comments" not in item.raw_metadata


def test_fetch_strips_html_tags_and_joins_multiple_paragraphs():
    entry = _selftext_entry(body_paragraphs=("First paragraph.", "Second paragraph."))
    connector = RedditSubredditConnector(fetch_rss=lambda subreddit, limit: _feed(entry))
    item = connector.fetch({"subreddit": "x"})[0]
    assert item.body == "First paragraph.\nSecond paragraph."


def test_fetch_link_only_post_has_empty_body():
    connector = RedditSubredditConnector(fetch_rss=lambda subreddit, limit: _feed(_link_only_entry()))
    item = connector.fetch({"subreddit": "worldnews"})[0]
    assert item.body == ""
    assert item.title == "Putin likely to escalate Ukraine war"


def test_fetch_handles_missing_author():
    entry = (
        "<entry>"
        "<content>content</content>"
        "<id>t3_noauthor</id>"
        '<link href="https://www.reddit.com/r/x/comments/t3_noauthor/" />'
        "<published>2026-07-10T00:00:00+00:00</published>"
        "<title>No author post</title>"
        "</entry>"
    )
    connector = RedditSubredditConnector(fetch_rss=lambda subreddit, limit: _feed(entry))
    item = connector.fetch({"subreddit": "x"})[0]
    assert item.raw_metadata["author"] == "[deleted]"


def test_fetch_truncates_long_selftext():
    entry = _selftext_entry(body_paragraphs=("x" * 3000,))
    connector = RedditSubredditConnector(fetch_rss=lambda subreddit, limit: _feed(entry))
    item = connector.fetch({"subreddit": "x"})[0]
    assert len(item.body) == 1500


def test_fetch_maps_every_entry_in_the_feed():
    connector = RedditSubredditConnector(
        fetch_rss=lambda subreddit, limit: _feed(
            _selftext_entry(entry_id="t3_a", title="First"),
            _link_only_entry(entry_id="t3_b", title="Second"),
        )
    )
    items = connector.fetch({"subreddit": "x"})
    assert [i.external_id for i in items] == ["t3_a", "t3_b"]
    assert [i.title for i in items] == ["First", "Second"]


def test_fetch_requests_the_hot_rss_endpoint_with_the_configured_subreddit():
    captured = {}

    def fake_fetch(subreddit, limit):
        captured["subreddit"] = subreddit
        captured["limit"] = limit
        return _feed()

    connector = RedditSubredditConnector(fetch_rss=fake_fetch)
    connector.fetch({"subreddit": "PersonalFinanceNZ"})
    assert captured["subreddit"] == "PersonalFinanceNZ"
    assert captured["limit"] == 50


def test_validate_config_requires_subreddit():
    connector = RedditSubredditConnector(fetch_rss=lambda subreddit, limit: _feed())
    with pytest.raises(ValueError, match="subreddit"):
        connector.validate_config({})
    connector.validate_config({"subreddit": "PersonalFinanceNZ"})  # does not raise


def test_type_key():
    assert RedditSubredditConnector(fetch_rss=lambda subreddit, limit: _feed()).type_key == "reddit_subreddit"


def test_fetch_comments_returns_the_first_comment_after_the_op():
    op_entry = _selftext_entry(entry_id="t3_op", title="Rates fall")
    comment_entry = _selftext_entry(entry_id="t1_comment1", title="",
                                     body_paragraphs=("This is the top comment.",))
    connector = RedditSubredditConnector(
        fetch_comment_rss=lambda item_url: _feed(op_entry, comment_entry))

    comments = connector.fetch_comments(_comment_target("https://www.reddit.com/r/x/comments/t3_op/rates_fall/"))
    assert comments == ["This is the top comment."]


def test_fetch_comments_returns_empty_list_when_post_has_no_comments():
    op_entry = _selftext_entry(entry_id="t3_op", title="Rates fall")
    connector = RedditSubredditConnector(fetch_comment_rss=lambda item_url: _feed(op_entry))

    comments = connector.fetch_comments(_comment_target("https://www.reddit.com/r/x/comments/t3_op/rates_fall/"))
    assert comments == []


def test_fetch_comments_passes_the_item_url_through_to_the_fetcher():
    captured = {}

    def fake_fetch_comment_rss(item_url):
        captured["item_url"] = item_url
        return _feed(_selftext_entry())

    connector = RedditSubredditConnector(fetch_comment_rss=fake_fetch_comment_rss)
    connector.fetch_comments(_comment_target("https://www.reddit.com/r/x/comments/abc/title/"))
    assert captured["item_url"] == "https://www.reddit.com/r/x/comments/abc/title/"


def test_fetch_comments_truncates_long_comment_text():
    op_entry = _selftext_entry(entry_id="t3_op")
    comment_entry = _selftext_entry(entry_id="t1_c", title="", body_paragraphs=("x" * 3000,))
    connector = RedditSubredditConnector(
        fetch_comment_rss=lambda item_url: _feed(op_entry, comment_entry))

    comments = connector.fetch_comments(_comment_target("https://www.reddit.com/r/x/comments/t3_op/x/"))
    assert len(comments[0]) == 1500


def test_fetch_comments_handles_a_comment_entry_with_no_content_element():
    op_entry = _selftext_entry(entry_id="t3_op", title="Rates fall")
    comment_entry_no_content = (
        "<entry>"
        "<id>t1_c</id>"
        '<link href="https://www.reddit.com/r/x/c1/" />'
        "<published>2026-07-10T00:00:00+00:00</published>"
        "<title>a comment</title>"
        "</entry>"
    )
    connector = RedditSubredditConnector(
        fetch_comment_rss=lambda item_url: _feed(op_entry, comment_entry_no_content))

    comments = connector.fetch_comments(_comment_target("https://www.reddit.com/r/x/comments/t3_op/rates_fall/"))
    assert comments == []
