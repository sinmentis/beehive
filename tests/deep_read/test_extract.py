import pytest

from beehive.deep_read.extract import (
    ExtractionQuality,
    PartialReason,
    extract_article_text,
)

# A realistic article shape: headline, byline noise Trafilatura should strip, several
# substantive paragraphs, and boilerplate nav/footer text that a real extractor discards.
_REALISTIC_ARTICLE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><title>City Council Approves New Transit Line After Years of Debate</title></head>
<body>
  <nav><a href="/">Home</a><a href="/world">World</a><a href="/local">Local</a></nav>
  <header><h1>City Council Approves New Transit Line After Years of Debate</h1>
    <p class="byline">By A. Reporter | Updated 2 hours ago</p></header>
  <article>
    <p>The city council voted 7-2 on Tuesday night to approve funding for the long-delayed
    Riverside light rail extension, ending nearly a decade of planning disputes between
    neighborhood groups, transit advocates, and the mayor's office.</p>
    <p>Construction is expected to begin next spring and take approximately three years to
    complete, according to the transit authority's latest estimate. The project will add
    six new stations along a corridor that currently has no rail service.</p>
    <p>Opponents of the plan, including several small business owners along the proposed
    route, argued the project would cause years of disruptive construction with little
    direct benefit to their neighborhoods. Supporters countered that the extension would
    cut commute times for tens of thousands of residents.</p>
    <p>"This has been a long time coming," the mayor said in a statement after the vote.
    "Our residents deserve a transit system that actually gets them where they need to go."</p>
  </article>
  <footer>&copy; 2026 Example News. All rights reserved. <a href="/privacy">Privacy</a></footer>
</body>
</html>
"""

_EMPTY_HTML = "<html><head><title>Nothing</title></head><body><nav></nav><footer></footer></body></html>"

_PAYWALL_HTML = """
<html><body><article>
<h1>Exclusive Report</h1>
<p>Subscribe to continue reading this exclusive report and support our journalism.</p>
</article></body></html>
"""


def test_extracts_and_classifies_a_realistic_article_as_complete():
    result = extract_article_text(_REALISTIC_ARTICLE_HTML)

    assert result.quality == ExtractionQuality.COMPLETE
    assert result.reasons == ()
    assert "Riverside light rail" in result.text
    assert "mayor" in result.text
    # boilerplate nav/footer text should not leak into extracted content
    assert "Privacy" not in result.text
    assert result.char_count == len(result.text)


def test_unusable_when_extractor_returns_none():
    result = extract_article_text("<html></html>", extractor=lambda html: None)

    assert result.quality == ExtractionQuality.UNUSABLE
    assert result.reasons == ()
    assert result.text == ""
    assert result.char_count == 0


def test_unusable_when_extractor_returns_only_whitespace():
    result = extract_article_text("<html></html>", extractor=lambda html: "   \n\t  ")

    assert result.quality == ExtractionQuality.UNUSABLE
    assert result.text == ""


def test_unusable_for_boilerplate_only_page():
    result = extract_article_text(_EMPTY_HTML)
    assert result.quality == ExtractionQuality.UNUSABLE


def test_whitespace_is_normalized():
    raw = "Title\r\n\r\n\r\n\r\nPara one   with    extra   spaces.\r\nPara two.\t\t\n\n\n\nPara three."
    result = extract_article_text("<html></html>", extractor=lambda html: raw)

    assert "\r" not in result.text
    assert "   " not in result.text
    assert "\n\n\n" not in result.text
    assert result.text.startswith("Title")
    assert not result.text.endswith("\n")


def test_extraction_truncated_when_text_exceeds_max_chars():
    long_text = "word " * 10_000
    result = extract_article_text("<html></html>", extractor=lambda html: long_text, max_chars=100)

    assert result.quality == ExtractionQuality.PARTIAL
    assert PartialReason.EXTRACTION_TRUNCATED in result.reasons
    assert len(result.text) == 100
    assert result.char_count == 100


def test_prompt_budget_truncated_when_smaller_than_max_chars():
    long_text = "word " * 10_000
    result = extract_article_text(
        "<html></html>", extractor=lambda html: long_text, max_chars=5000, prompt_budget_chars=50)

    assert result.quality == ExtractionQuality.PARTIAL
    assert PartialReason.EXTRACTION_TRUNCATED in result.reasons  # max_chars(5000) still binds first
    assert PartialReason.PROMPT_BUDGET_TRUNCATED in result.reasons
    assert len(result.text) == 50


def test_prompt_budget_not_flagged_when_it_does_not_further_truncate():
    text = "word " * 20  # 100 chars, comfortably under both caps
    result = extract_article_text(
        "<html></html>", extractor=lambda html: text, max_chars=5000, prompt_budget_chars=1000,
        min_usable_chars=1)

    assert PartialReason.EXTRACTION_TRUNCATED not in result.reasons
    assert PartialReason.PROMPT_BUDGET_TRUNCATED not in result.reasons


def test_max_chunk_truncated_when_tighter_than_other_caps():
    long_text = "word " * 10_000
    result = extract_article_text(
        "<html></html>", extractor=lambda html: long_text,
        max_chars=5000, prompt_budget_chars=2000, max_chunk_chars=30)

    assert result.quality == ExtractionQuality.PARTIAL
    assert PartialReason.EXTRACTION_TRUNCATED in result.reasons
    assert PartialReason.PROMPT_BUDGET_TRUNCATED in result.reasons
    assert PartialReason.MAX_CHUNK_TRUNCATED in result.reasons
    assert len(result.text) == 30


def test_transport_truncated_flag_is_recorded_independently_of_length():
    text = "a fully complete short article body that is still above the usable minimum length."
    result = extract_article_text(
        "<html></html>", extractor=lambda html: text, transport_truncated=True, min_usable_chars=10)

    assert result.quality == ExtractionQuality.PARTIAL
    assert result.reasons == (PartialReason.TRANSPORT_TRUNCATED,)


def test_short_content_is_flagged_partial_not_unusable():
    result = extract_article_text("<html></html>", extractor=lambda html: "Too short.", min_usable_chars=200)

    assert result.quality == ExtractionQuality.PARTIAL
    assert result.reasons == (PartialReason.SHORT_CONTENT,)
    assert result.text == "Too short."


@pytest.mark.parametrize("marker", [
    "Subscribe to continue reading this article.",
    "This content is for subscribers only. Please log in.",
    "Sign in to continue reading the rest of this story.",
    "You have reached your limit of free articles this month.",
])
def test_paywall_like_markers_are_detected_case_insensitively(marker):
    result = extract_article_text("<html></html>", extractor=lambda html: marker.upper(), min_usable_chars=1)

    assert result.quality == ExtractionQuality.PARTIAL
    assert result.reasons == (PartialReason.PAYWALL_LIKE,)


def test_paywall_marker_takes_precedence_over_short_content_reason():
    text = "Subscribe to continue reading."
    result = extract_article_text("<html></html>", extractor=lambda html: text, min_usable_chars=1000)

    assert result.reasons == (PartialReason.PAYWALL_LIKE,)
    assert PartialReason.SHORT_CONTENT not in result.reasons


def test_reason_order_is_fixed_and_deterministic_across_all_caps():
    long_text = "word " * 10_000

    result = extract_article_text(
        long_text, extractor=lambda html: html,
        transport_truncated=True, max_chars=200, prompt_budget_chars=100, max_chunk_chars=50,
        min_usable_chars=1000,
    )

    assert result.reasons == (
        PartialReason.TRANSPORT_TRUNCATED,
        PartialReason.EXTRACTION_TRUNCATED,
        PartialReason.PROMPT_BUDGET_TRUNCATED,
        PartialReason.MAX_CHUNK_TRUNCATED,
        PartialReason.SHORT_CONTENT,
    )


def test_extraction_is_pure_and_never_touches_the_network(monkeypatch):
    """Guards against a future change accidentally wiring network I/O into extraction:
    stub out socket creation and confirm a normal extraction call still succeeds."""
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("extract_article_text must never open a socket")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    result = extract_article_text(_REALISTIC_ARTICLE_HTML)
    assert result.quality == ExtractionQuality.COMPLETE
