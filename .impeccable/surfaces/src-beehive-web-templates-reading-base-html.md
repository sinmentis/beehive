---
version: 1
slug: "src-beehive-web-templates-reading-base-html"
primary_target: "src/beehive/web/templates/reading_base.html"
related_targets: ["src/beehive/web/templates/dashboard.html","src/beehive/web/templates/channel_editorial.html","src/beehive/web/templates/channel_monitor.html","src/beehive/web/templates/channel_tracker.html","src/beehive/web/templates/archive.html","src/beehive/web/templates/search.html","src/beehive/web/templates/deep_read_brief.html","src/beehive/web/templates/_reading_macros.html","src/beehive/web/templates/_reading_action_notice.html","src/beehive/digest/templates/digest_email.html","src/beehive/web/static/admin.css"]
---

## Scope and mode

Every public reading page, set in the datasheet: the home page (`/`, 精选, with its channel desk and its ranked list `/?view=…`), the channel pages (`/channels/{id}` for news, store monitors and auction trackers), the archive (`/archive`), search (`/search`) and the deep-read briefs (`/items/{id}/brief`). `/channels/` redirects to the home page. Read mode, with the Owner's triage on top. Error and 404 pages are a single sheet without the rail. The old `base.html` and `beehive.css` are gone.

## Audience, job and task

The Owner, on desktop screens from laptop to 4K, and anonymous readers. The Owner scans where anything new is, triages stories (open, mark read, rate relevant or not, deep read), browses a channel's catalogue or open lots with filters, watches lots, looks things up in the archive or search, and reads a brief before the article. A reader gets the same pages without read state or owner controls.

## Structure

The rail lists Featured, one chapter per Channel and the Archive. Numbered section headings carry state or counts; search groups hits by Channel and preserves their read/score scope when opening it. Every page opens with its h1, useful counts and freshness. Channel source/fetch facts and scoring rules live in Channel details; catalogs place those facts, secondary filters and page-size choices in Filters and details. Primary search and sort/status remain outside it. Shared banded story/listing/lot rows and ruled gallery plates retain their workflows and wide-screen lanes. Brief warnings and failures remain boxed notes. Individual read/unwatch recovery uses a visible action notice. History is closed unless requested, paged or matched by search. A brief remains a reading column with figures and source beside it, and an exhausted page keeps a useful pager.

## Direction

Datasheet, dark, channel desk (seed ba4c87a4). The channel, archive, search and brief pages extend it with the same parts rather than a new concept. Unread is a small blue square that is also the read toggle; a score is a tabular numeral, 90 and above in full ink, under 60 faint. Two-state controls are words with a square that fills when on. Every cell wraps; nothing is cut with an ellipsis.

## Memorable moment

On a wide screen the sheet fills the window. Independent sections flow side by side: the home desk shows two or three channels per row, a news channel its top stories beside the rest, an auction channel its watched, ending and upcoming lots, a search its Channels. A long list, such as an archive day, the ranked list, a lone search section or a store's catalogue, splits into two lanes, and sections side by side (a news channel's two parts, an auction's lots, search's Channels) become lanes too. Each lane fits the window and scrolls on its own, so the top of the next one is always in view and nobody scrolls back up. Paging never jumps the page on a wide sheet. The reader can hide any side-by-side section but the last, which then shrinks to its numbered heading above the rest, and the rest take the width; the page remembers it. A store opens as a gallery of ruled plates with whole photos, each led by the item's name, and the Owner can switch it to the list and pick 24, 48 or 96 rows a page; both choices are remembered. Marking a story read, rating it or watching a lot updates that row or section in place and announces it, opening a story reads it in place, and the Owner's keys (m, + and -, w) press the selected row's own controls.

## Constraints

The phone catalog is content-first: search and sort/status remain visible, while secondary filters, page size, source/fetch facts and scoring rules share Filters and details. It starts closed on narrow screens, retains applied-filter counts and has an Apply action at its end. Unsupported gender filters are omitted. Gallery titles and prices precede photos on phones without changing the square, uncropped image. The active channel is revealed in the horizontal rail. Watch List and Research remain direct footer links; Admin and Sign out use a phone-only More disclosure. Coarse-pointer controls have 44px targets.

A regenerating or failed replacement keeps the last successful brief, its generation time and limitations visible; only successful replacement swaps it. Initial generation still uses a labelled status/skeleton. Owner-only reminder failures appear in the reading footer, Tracker desk heading and affected watch controls, linking to the Watch List. Counts include failed watches beyond the home shortlist and update when a watch is removed. Individual read/unread and unwatch actions add a visible, dismissible recovery notice. Its ten-minute undo preserves prior reminder state and is not offered for an in-flight delivery.

Search-to-channel navigation keeps read and score scope; matching counts and history results stay understandable. Date errors retain their inputs. Empty home sections explain collection, time-window, ranking or criteria state from actual counts and offer a useful route. Merchant reference prices and observed changes remain distinct, with no live-price or historical-low claim.

The related digest remains its incumbent light, 600px, inline-styled email document rather than adopting the web sheet. Its explicit website setting supplies site/channel/saved-brief links, configured caps stay unchanged, and deadlines use the group's local zone. Preview navigation opens a no-opener window while scripts and same-origin privileges remain disabled.

Everything in the admin brief applies: no overflow at 390, 1024, 1440, 2560 or 3840 px in Chinese or English, whole units (`.nw`), CSP with no inline script or style. The sheet has no maximum width; prose keeps its measure (72ch), form fields a sane width, sections flow into `.cols` columns of at least 60rem, and a long list whose own table box fits two 56rem lanes and their gutter (115.5rem, 1848px) splits into two lanes that scroll on their own, order running down the first lane and then the second so reading and keyboard order stay as listed. The gallery is the one grid of boxes, kept flat and ruled. Pages never build more than they show: the home desk picks rows in SQL, and the archive's day totals come from one grouped count. Times are Auckland time, including the archive's days and its date filter.

## Open decisions

The home page has no stored "last visit", so it cannot mark what arrived since the Owner last looked.
