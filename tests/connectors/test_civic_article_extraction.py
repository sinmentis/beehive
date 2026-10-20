import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from beehive.civic.documents import CivicDocument
from beehive.civic.production import CivicHydrationStatus, PartyMediaDocumentHydrator
from beehive.civic.source_extraction import has_current_article_capture
from beehive.connectors.http import ConnectorHttpError, ConnectorHttpErrorKind
from beehive.connectors.civic_party import PARTY_CATALOG
from tests.civic.civic_stubs import transport_for
from tests.connectors.civic_fixtures_loader import civic_fixture

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
LASTMOD = datetime.fromisoformat("2026-09-04T08:41:29.788+00:00")


def _document(source_key="national", **changes):
    entry = PARTY_CATALOG.get(source_key)
    url = f"https://{next(iter(entry.hosts))}/news/fixture-announcement"
    return replace(CivicDocument(
        source_key=source_key,
        external_key=url,
        canonical_url=url,
        title="Fixture title from a sitemap",
        tier=entry.tier,
        publisher=entry.publisher,
        first_seen_at=NOW,
        updated_at=LASTMOD,
    ), **changes)


def _hydrate(page, document=None):
    document = document or _document()
    transport = transport_for(
        {document.canonical_url: (page, "text/html")},
    )
    result = PartyMediaDocumentHydrator(transport)(document, NOW)
    return result, transport


def _national_page():
    return civic_fixture("national_article_dates.html").decode()


def _conservative_page(body=None):
    page = civic_fixture("conservative_embedded_article.html").decode()
    if body is None:
        return page
    pattern = r'(<script id="wix-warmup-data" type="application/json">)(.*?)(</script>)'
    match = re.search(pattern, page, re.S)
    data = json.loads(match[2])
    content = data["platform"]["ssrPropsUpdates"][0]["comp-mb4y0a9l"]["content"]
    content["nodes"][0]["htmlData"]["html"] = (
        "<html><body><h1>Press Release</h1>"
        f"<article itemprop='articleBody'>{body}</article></body></html>"
    )
    return page[:match.start(2)] + json.dumps(data) + page[match.end(2):]


@pytest.mark.parametrize(
    ("published", "byline", "local_date"),
    [
        ("2026-09-02T23:58:38.731Z", "03 September 2026", "2026-09-03"),
        ("2026-09-01T19:50:01.000Z", "02 September 2026", "2026-09-02"),
        ("2026-09-01T19:49:58.000Z", "02 September 2026", "2026-09-02"),
        ("2026-08-28T20:10:24.000Z", "29 August 2026", "2026-08-29"),
    ],
)
def test_national_article_date_ignores_related_times_and_modification(
    published, byline, local_date,
):
    page = _national_page().replace(
        "2026-09-02T23:58:38.731Z", published,
    ).replace("03 September 2026", byline)
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.published_at == datetime.fromisoformat(published)
    assert result.document.published_at != LASTMOD
    assert result.document.published_at.astimezone(
        ZoneInfo("Pacific/Auckland")).date().isoformat() == local_date


def test_national_visible_byline_is_a_local_date_not_utc_midnight():
    page = re.sub(
        r'<script type="application/ld\+json">.*?</script>', "", _national_page(), flags=re.S,
    )
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.published_at.isoformat() == "2026-09-03T00:00:00+12:00"
    publication = result.document.raw_metadata["source_capture"]["publication"]
    assert publication["precision"] == "date"
    assert publication["timezone"] == "Pacific/Auckland"
    assert publication["source"] == "national-visible-byline"


def test_modification_and_footer_dates_do_not_rescue_an_undated_article():
    page = """
    <html><body><article><h1>An undated fixture release</h1>
    <p>This synthetic article contains a complete description of a proposed public service.
    It explains the scope, conditions, and intended next steps in detail. No publication date
    is present in the article, and its URL must not be interpreted as a date.</p></article>
    <footer><time class="published" datetime="2026-09-04">Site update</time></footer>
    </body></html>
    """
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.published_at is None
    assert result.document.raw_metadata["source_capture"]["publication"] is None


def test_rss_summary_does_not_skip_real_article_hydration():
    document = _document(excerpt="A short RSS description, not the full article.")
    result, transport = _hydrate(_national_page(), document)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert len(transport.recorded.calls) == 1
    assert "end of the fixture release" in result.document.excerpt
    assert result.document.published_at != document.updated_at


def test_conservative_reads_the_rendered_wix_html_component_not_the_empty_shell():
    result, transport = _hydrate(_conservative_page(), _document("conservative"))
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.title == "A fixture candidate announcement"
    assert result.document.published_at.isoformat() == "2026-08-14T00:00:00+12:00"
    assert "complete embedded article" in result.document.excerpt
    assert "top of page" not in result.document.excerpt
    assert "bottom of page" not in result.document.excerpt
    assert len(transport.recorded.calls) == 1


def test_complete_short_embedded_release_is_not_a_teaser():
    body = (
        "<p>The party has opened consultation on its transport proposal. "
        "Written submissions close at the end of the month.</p>"
    )
    result, _ = _hydrate(_conservative_page(body), _document("conservative"))
    assert result.status is CivicHydrationStatus.HYDRATED
    assert "Written submissions close" in result.document.excerpt


@pytest.mark.parametrize("body", [
    "<p>A brief preview of a longer article...</p><a href='/full'>Read more</a>",
    "<p>This content is for subscribers. Subscribe to continue reading.</p>",
    "<p>Coming soon.</p>",
])
def test_short_or_paywalled_embedded_teasers_remain_unavailable(body):
    result, _ = _hydrate(_conservative_page(body), _document("conservative"))
    assert result.status is CivicHydrationStatus.UNAVAILABLE


def test_current_full_article_provenance_is_the_only_safe_hydration_cache():
    result, _ = _hydrate(_national_page())
    transport = transport_for({})
    hydrator = PartyMediaDocumentHydrator(transport)
    cached = hydrator(result.document, NOW)
    assert has_current_article_capture(cached.document)
    assert cached.status is CivicHydrationStatus.NOT_NEEDED
    assert transport.recorded.calls == []
    changed = replace(result.document, published_at=LASTMOD)
    assert not has_current_article_capture(changed)
    assert not has_current_article_capture(replace(result.document, excerpt="Changed excerpt."))


def test_legacy_hydration_flags_are_not_a_current_capture_contract():
    document = _document(
        excerpt="Previously extracted text with no auditable source date.",
        published_at=LASTMOD,
        raw_metadata={"detail_hydrated": True, "detail_extraction_quality": "complete"},
    )
    result, transport = _hydrate(_national_page(), document)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert len(transport.recorded.calls) == 1
    assert result.document.published_at != LASTMOD


def test_complete_short_wix_email_release_needs_no_artificial_article_tag():
    body = (
        "<p>The party has opened consultation on its transport proposal. "
        "Written submissions close at the end of the month.</p>"
    )
    page = _conservative_page(body).replace(
        "<article itemprop='articleBody'>", "<div>",
    ).replace("</article>", "</div>")
    result, _ = _hydrate(page, _document("conservative"))
    assert result.status is CivicHydrationStatus.HYDRATED


@pytest.mark.parametrize("time_markup", [
    'Updated <time datetime="2026-09-04T08:41:29Z">4 September 2026</time>',
    '<time class="published updated" datetime="2026-09-04T08:41:29Z">4 September 2026</time>',
    '<time itemprop="startDate" datetime="2026-09-04T08:41:29Z">4 September 2026</time>',
])
def test_an_article_local_update_or_event_time_is_not_a_publication(time_markup):
    page = (
        f"<html><body><article><h1>Fixture release</h1>{time_markup}<p>"
        + "This is a complete synthetic release about a proposed public service. " * 5
        + "</p></article></body></html>"
    )
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.published_at is None
    assert result.document.raw_metadata["source_capture"]["publication"] is None


@pytest.mark.parametrize("kind", [
    ConnectorHttpErrorKind.ACCESS_DENIED, ConnectorHttpErrorKind.TOO_LARGE,
    ConnectorHttpErrorKind.NOT_FOUND,
])
def test_detail_transport_failures_keep_their_specific_cause_and_never_fall_back(kind):
    document = _document(excerpt="RSS preview.")
    transport = transport_for({
        document.canonical_url: ConnectorHttpError(kind, "fixture source refusal"),
    })
    result = PartyMediaDocumentHydrator(transport)(document, NOW)
    assert result.status is CivicHydrationStatus.UNAVAILABLE
    assert result.error_code == f"detail_fetch_{kind.value}"
    assert result.document.excerpt == document.excerpt
    assert len(transport.recorded.calls) == 1


def test_article_path_allowlist_is_enforced_before_fetch():
    document = _document(canonical_url="https://www.national.org.nz/team/fixture")
    transport = transport_for({})
    result = PartyMediaDocumentHydrator(transport)(document, NOW)
    assert result.status is CivicHydrationStatus.UNAVAILABLE
    assert result.error_code == "detail_path_disallowed"
    assert transport.recorded.calls == []


@pytest.mark.parametrize(("attributes", "published", "source"), [
    (
        'datetime="2026-08-12T00:00:00.000Z"',
        datetime(2026, 8, 12, tzinfo=timezone.utc),
        "act-framer-header-time",
    ),
    (
        "",
        datetime.fromisoformat("2026-08-12T00:00:00+12:00"),
        "act-framer-header-visible-date",
    ),
    (
        'itemprop="startDate" datetime="2026-08-12T00:00:00.000Z"',
        None, None,
    ),
])
def test_act_framer_date_is_bound_to_the_current_article_header(attributes, published, source):
    page = f"""
    <html><body><div data-framer-name="Content">
      <div data-framer-name="Header + Banner"><div data-framer-name="Header">
        <div data-framer-component-type="RichTextContainer"><p>
          <time {attributes}>Wednesday, 12 August 2026</time>
        </p></div>
        <div data-framer-component-type="RichTextContainer"><h1>Fixture release</h1></div>
      </div></div>
      <p>This complete synthetic release describes a proposed public service and its scope.
      It identifies the intended beneficiaries, explains the consultation process, and sets
      out the next steps. None of these fixture statements describe real people or events.</p>
    </div><footer><time datetime="2026-09-08">Website update</time></footer></body></html>
    """
    result, _ = _hydrate(page, _document("act", updated_at=None))
    if published is None:
        assert result.status is CivicHydrationStatus.HYDRATED
        assert result.document.published_at is None
        assert result.document.raw_metadata["source_capture"]["publication"] is None
        return
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.published_at == published
    assert result.document.raw_metadata["source_capture"]["publication"]["source"] == source


def test_related_story_microdata_cannot_date_an_undated_main_article():
    page = re.sub(
        r'<script type="application/ld\+json">.*?</script>', "", _national_page(), flags=re.S,
    ).replace("<span>03 September 2026</span>", "")
    page = page.replace(
        "</main>",
        '</main><article class="news-card"><h2>Another story</h2>'
        '<meta itemprop="datePublished" content="2026-09-04T10:00:00Z"></article>',
    )
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.published_at is None


def test_conflicting_article_local_publication_dates_fail_closed():
    page = _national_page().replace("03 September 2026", "02 September 2026")
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.UNAVAILABLE
    assert result.error_code == "detail_publication_date_conflict"


def test_jsonld_date_from_a_different_article_is_not_a_publication_witness():
    page = _national_page().replace("<span>03 September 2026</span>", "")
    page = page.replace(
        '"@type":"NewsArticle"', '"@type":"NewsArticle","url":"https://www.national.org.nz/news/other"',
    )
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.published_at is None


@pytest.mark.parametrize(("byline", "expected"), [
    ("01 October 2026", "2026-10-01T00:00:00+13:00"),
    ("01 June 2026", "2026-06-01T00:00:00+12:00"),
])
def test_date_only_byline_uses_the_source_timezone_including_daylight_saving(byline, expected):
    page = re.sub(
        r'<script type="application/ld\+json">.*?</script>', "", _national_page(), flags=re.S,
    ).replace("03 September 2026", byline)
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.published_at.isoformat() == expected
    assert result.document.raw_metadata["source_capture"]["publication"]["precision"] == "date"


def test_wix_unbound_or_remote_embeds_are_not_an_extraction_fallback():
    page = _conservative_page().replace('"source":"HTML"', '"source":"URL"')
    result, transport = _hydrate(page, _document("conservative"))
    assert result.status is CivicHydrationStatus.UNAVAILABLE
    assert result.error_code == "detail_embedded_content_unsupported"
    assert len(transport.recorded.calls) == 1
    page = _conservative_page().replace('id="comp-mb4y0a9l"', 'id="different-component"')
    result, transport = _hydrate(page, _document("conservative"))
    assert result.status is CivicHydrationStatus.UNAVAILABLE
    assert result.error_code == "detail_embedded_article_ambiguous"
    assert len(transport.recorded.calls) == 1


def test_related_read_more_links_do_not_make_the_current_article_a_teaser():
    page = _national_page().replace(
        "</aside>", '<a href="/news/related-article">Read more</a></aside>',
    )
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED


def _newsletter_body():
    return """
    <h2>Community update</h2>
    <p>This complete newsletter describes a public proposal, explains the consultation process,
    identifies the communities involved, and gives readers the background needed to understand
    how the next decision will be made.</p>
    <p><a href="/news/background-paper">Read more</a></p>
    <h2>Local services</h2>
    <p>A separate section reports a completed community meeting and explains the questions
    residents raised about local services, including access, accountability, and the timetable
    for receiving a response from the responsible agency.</p>
    <h2>Next steps</h2>
    <p>The complete final paragraph describes how residents can participate in the next
    consultation and explains that the proposal remains open for public feedback before
    a decision is announced through the usual publication channels.</p>
    """


def test_complete_newsletter_can_link_background_articles_inside_its_body():
    page = re.sub(r"<main>.*?</main>", "<main>" + _newsletter_body() + "</main>",
                  _national_page(), flags=re.S)
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert "complete final paragraph" in result.document.excerpt
    assert has_current_article_capture(result.document)


def test_terminal_continuation_link_still_refuses_a_long_preview():
    body = _newsletter_body().replace(
        '<p><a href="/news/background-paper">Read more</a></p>', "")
    page = re.sub(
        r"<main>.*?</main>",
        "<main>" + body + '<a href="/full-article">Read more</a></main>',
        _national_page(), flags=re.S)
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.UNAVAILABLE
    assert result.error_code == "detail_article_teaser"


_CANONICAL = 'href="https://www.national.org.nz/news/fixture-announcement"'


def test_a_canonical_that_only_adds_a_trailing_slash_is_the_same_article():
    page = _national_page().replace(
        _CANONICAL, 'href="https://www.national.org.nz/news/fixture-announcement/"')
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert has_current_article_capture(result.document)


@pytest.mark.parametrize("canonical", [
    "https://www.national.org.nz/news/other-announcement/",
    "https://www.national.org.nz/news/",
    "https://www.national.org.nz/",
    "https://www.national.org.nz/news/fixture-announcement//",
    "https://www.national.org.nz/news/fixture-announcement/?page=2",
])
def test_a_canonical_for_another_page_is_still_an_identity_mismatch(canonical):
    page = _national_page().replace(_CANONICAL, f'href="{canonical}"')
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.UNAVAILABLE
    assert result.error_code == "detail_article_identity_mismatch"


@pytest.mark.parametrize(("heading", "expected"), [
    ("The Right<br/>to a Roof: Our Housing Policy", "The Right to a Roof: Our Housing Policy"),
    ("The Right<br>to a Roof", "The Right to a Roof"),
    ("Line one<br/>  <br/>line two", "Line one line two"),
    ("Te P<span>āti</span> Māori policy", "Te Pāti Māori policy"),
    ("<strong>Bold</strong>ly split", "Boldly split"),
])
def test_a_heading_line_break_keeps_its_word_boundary(heading, expected):
    from beehive.civic.source_extraction import article_title

    assert article_title(f"<html><body><h1>{heading}</h1></body></html>") == expected


def test_a_hydrated_title_keeps_the_word_boundary_at_a_line_break():
    page = _national_page().replace(
        "<h1>A dated fixture announcement</h1>", "<h1>A dated<br/>fixture announcement</h1>")
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert result.document.title == "A dated fixture announcement"


_RELATED_CARDS = """
  <section class="news-listing news-listing--related reveal">
    <h2 class="news-related-heading">More News</h2>
    <div class="news-grid">
      <article class="news-card"><div class="news-card-body">
        <h3><a href="https://www.national.org.nz/news/other-release">Another release</a></h3>
        <p>A different release about a separate policy opens with a sentence that stops mid…</p>
      </div></article>
    </div>
  </section>
"""


def test_related_news_cards_neither_make_a_teaser_nor_enter_the_evidence():
    page = _national_page().replace("</main>", _RELATED_CARDS + "</main>")
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.HYDRATED
    assert "This is the end of the fixture release." in result.document.excerpt
    assert "A different release" not in result.document.excerpt
    assert "Another release" not in result.document.excerpt


def test_a_body_that_really_ends_mid_sentence_is_still_a_teaser():
    page = _national_page().replace(
        "This is the end of the fixture release.</p>",
        "The remainder of this release continues on another page and…</p>",
    ).replace("</main>", _RELATED_CARDS.replace("stops mid…", "ends here.") + "</main>")
    result, _ = _hydrate(page)
    assert result.status is CivicHydrationStatus.UNAVAILABLE
    assert result.error_code == "detail_article_teaser"


def _labour_page():
    return civic_fixture("labour_article.html").decode()


def test_labour_release_is_dated_and_titled_from_its_own_news_page():
    result, _ = _hydrate(_labour_page(), _document("labour"))
    assert result.status is CivicHydrationStatus.HYDRATED
    assert has_current_article_capture(result.document)
    local = result.document.published_at.astimezone(ZoneInfo("Pacific/Auckland"))
    assert local.date().isoformat() == "2026-09-07"
    assert result.document.title == "Release: A fixture announcement"
    publication = result.document.raw_metadata["source_capture"]["publication"]
    assert publication["source"] == "labour-visible-publication-date"


@pytest.mark.parametrize("change", [
    ('<p class="news-publication-date my-3">07 September 2026</p>',
     '<p class="news-publication-date my-3">07 September 2026</p>'
     '<p class="news-publication-date my-3">08 September 2026</p>'),
    ('<div class="news-page">', '<div class="news-page"></div><div class="news-page">'),
    ('<h2 class="page-title">Release: A fixture announcement</h2>', ""),
])
def test_an_ambiguous_labour_news_page_has_no_article_date(change):
    result, _ = _hydrate(_labour_page().replace(*change), _document("labour"))
    assert result.document.published_at is None


def _media_document(source_key, path):
    from beehive.connectors.civic_sources import PARTY_MEDIA_CATALOG

    entry = PARTY_MEDIA_CATALOG.get(source_key)
    url = f"https://{next(iter(entry.hosts))}{path}"
    return CivicDocument(
        source_key=source_key, external_key=url, canonical_url=url,
        title="Fixture title from a feed", tier=entry.tier, publisher=entry.publisher,
        first_seen_at=NOW, updated_at=LASTMOD)


def _local_publication(result):
    return result.document.published_at.astimezone(ZoneInfo("Pacific/Auckland"))


def test_te_ao_story_is_dated_by_its_own_arc_content_object():
    page = civic_fixture("te_ao_article.html").decode()
    result, _ = _hydrate(page, _media_document("te-ao", "/2026/09/28/fixture-story/"))
    assert result.status is CivicHydrationStatus.HYDRATED
    assert has_current_article_capture(result.document)
    assert _local_publication(result).isoformat() == "2026-09-28T18:00:00+13:00"
    publication = result.document.raw_metadata["source_capture"]["publication"]
    assert publication["source"] == "te-ao-arc-first-publish-date"


@pytest.mark.parametrize("change", [
    ('"type":"story","canonical_url":"/2026/09/28/fixture-story/"',
     '"type":"story","canonical_url":"/2026/09/28/another-story/"'),
    ('"_id":"FIXTURESTORYID","type":"story"', '"_id":"FIXTURESTORYID","type":"gallery"'),
    ('Fusion.globalContentConfig=', 'Fusion.globalContent={"type":"story"};Fusion.globalContentConfig='),
    ('"first_publish_date":"2026-09-28T05:00:00Z",', '"first_publish_date":"2026-09-28 05:00",'),
])
def test_an_unbound_te_ao_content_object_gives_no_date(change):
    page = civic_fixture("te_ao_article.html").decode().replace(*change)
    result, _ = _hydrate(page, _media_document("te-ao", "/2026/09/28/fixture-story/"))
    assert result.document.published_at is None


def test_interest_story_is_dated_by_its_own_by_line():
    page = civic_fixture("interest_article.html").decode()
    result, _ = _hydrate(page, _media_document("interest", "/business/140443/fixture-policy-story"))
    assert result.status is CivicHydrationStatus.HYDRATED
    assert has_current_article_capture(result.document)
    assert _local_publication(result).isoformat() == "2026-09-28T13:33:00+13:00"
    publication = result.document.raw_metadata["source_capture"]["publication"]
    assert publication["source"] == "interest-by-line-published-at"
    assert publication["raw_value"] == "28th Sep 26, 1:33pm"


@pytest.mark.parametrize("change", [
    ('<span class="published-at">28th Sep 26, 1:33pm</span>',
     '<span class="published-at">28th Sep 26, 1:33pm</span>'
     '<span class="published-at">29th Sep 26, 8:00am</span>'),
    ('<span class="published-at">28th Sep 26, 1:33pm</span>',
     '<span class="published-at">Sept 28</span>'),
    ('<div id="story-wrapper">', '<div id="story-wrapper-old">'),
])
def test_an_ambiguous_interest_by_line_gives_no_date(change):
    page = civic_fixture("interest_article.html").decode().replace(*change)
    result, _ = _hydrate(page, _media_document("interest", "/business/140443/fixture-policy-story"))
    assert result.document.published_at is None


_OTHER_RELEASE = (
    '<h2 class="page-title">Release: Another release</h2>'
    '<p class="news-publication-date my-3">01 September 2026</p>')


@pytest.mark.parametrize("change", [
    pytest.param(
        lambda page: page.replace(
            '<h2 class="page-title">Release: A fixture announcement</h2>',
            f'<div class="related-posts"><div class="card">{_OTHER_RELEASE}</div></div>',
        ).replace('<p class="news-publication-date my-3">07 September 2026</p>', ""),
        id="marked-related-card"),
    pytest.param(
        lambda page: page.replace(
            '<h2 class="page-title">Release: A fixture announcement</h2>',
            f'<div class="news-list"><div class="card">{_OTHER_RELEASE}</div></div>',
        ).replace('<p class="news-publication-date my-3">07 September 2026</p>', ""),
        id="unmarked-nested-card"),
    pytest.param(
        lambda page: page.replace(
            '<p class="news-publication-date my-3">07 September 2026</p>', "",
        ).replace(
            '<h4 class="h4-header">',
            '<p class="news-publication-date my-3">07 September 2026</p><h4 class="h4-header">'),
        id="date-not-beside-the-heading"),
])
def test_labour_date_and_title_never_come_from_another_release(change):
    result, _ = _hydrate(change(_labour_page()), _document("labour"))
    assert result.document.published_at is None
    assert result.document.title != "Release: Another release"


@pytest.mark.parametrize("sidebar_after", ['<div id="story-wrapper">', '<div id="content-column">'])
def test_an_interest_sidebar_never_dates_the_story(sidebar_after):
    page = civic_fixture("interest_article.html").decode().replace(
        '<span class="published-at">28th Sep 26, 1:33pm</span>', "",
    ).replace(sidebar_after, sidebar_after + (
        '<div class="sidebar"><div class="by-line">'
        '<span class="published-at">27th Sep 26, 9:00am</span></div></div>'))
    result, _ = _hydrate(page, _media_document("interest", "/business/140443/fixture-policy-story"))
    assert result.document.published_at is None


@pytest.mark.parametrize("change", [
    pytest.param(
        lambda page: page.replace("<main>", '<main><div class="related-story-body">')
        .replace("</main>", "</div></main>"),
        id="body-class-mentions-related"),
    pytest.param(
        lambda page: page.replace("<body>", '<body class="single has-related-posts">'),
        id="page-wrapper-flags-related-posts"),
])
def test_a_related_class_token_never_removes_the_article_itself(change):
    result, _ = _hydrate(change(_national_page()))
    assert result.status is CivicHydrationStatus.HYDRATED
    assert "This is the end of the fixture release." in result.document.excerpt
    assert result.document.title == "A dated fixture announcement"
    assert result.document.published_at == datetime(2026, 9, 2, 23, 58, 38, 731000, tzinfo=timezone.utc)


@pytest.mark.parametrize("change", [
    pytest.param(lambda page: page.replace("<body>", '<body class="single has-related-posts">'),
                 id="body-flag"),
    pytest.param(lambda page: page.replace(
        '<div class="news-page">', '<div class="news-page has-related-posts">'), id="page-flag"),
    pytest.param(lambda page: page.replace(
        '<div class="news-page">', '<div class="news-page page--with-related">'), id="bem-flag"),
])
def test_a_related_posts_page_flag_never_removes_an_h2_only_release(change):
    result, _ = _hydrate(change(_labour_page()), _document("labour"))
    assert result.status is CivicHydrationStatus.HYDRATED
    assert "This is the end of the fixture release." in result.document.excerpt
    assert result.document.title == "Release: A fixture announcement"
    assert _local_publication(result).date().isoformat() == "2026-09-07"
