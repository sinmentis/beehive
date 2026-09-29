---
version: 1
slug: "src-beehive-web-templates-reading-base-html"
primary_target: "src/beehive/web/templates/reading_base.html"
related_targets: ["src/beehive/web/templates/dashboard.html","src/beehive/web/templates/channel_editorial.html","src/beehive/web/templates/archive.html","src/beehive/web/templates/search.html","src/beehive/web/templates/deep_read_brief.html","src/beehive/web/templates/_reading_macros.html","src/beehive/web/static/admin.css"]
---

## Scope and mode

Every public reading page, set in the datasheet: the home page (`/`, 精选, with its channel desk and its ranked list `/?view=…`), the channel pages (`/channels/{id}` for news, store monitors and auction trackers), the archive (`/archive`), search (`/search`) and the deep-read briefs (`/items/{id}/brief`). `/channels/` redirects to the home page. Read mode, with the Owner's triage on top. Error and 404 pages are a single sheet without the rail. The old `base.html` and `beehive.css` are gone.

## Audience, job and task

The Owner, on desktop screens from laptop to 4K, and anonymous readers. The Owner scans where anything new is, triages stories (open, mark read, rate relevant or not, deep read), browses a channel's catalogue or open lots with filters, watches lots, looks things up in the archive or search, and reads a brief before the article. A reader gets the same pages without read state or owner controls.

## Structure

The rail lists 1 精选 (a quiet unread number for the Owner), one chapter per Channel and the Archive; the current page's numbered sections appear under its chapter, each heading carrying its state or count. A channel numbers its parts (2.1 重点内容, 2.2 其余内容); the archive numbers the Auckland days on the page, each with that day's story total; search sorts the page's hits into one section per Channel in rail order (1.1, 1.2 …), each with the Channel's total hits and a link into the Channel filtered by the query; a finished brief numbers 结论, 关键发现, 为什么重要, 重要数据 and 来源 under the chapter it was opened from. Every page opens with the same head: h1, a meta line of counts, freshness and fetch stats, then search and page actions at right. Owner-only facts sit in boxed notes (说明 for a channel's match criteria, 注意 for brief warnings, 警告 for failures). Lists are banded tables with one row part per kind, shared by every page: story rows (unread square, score, summary over the original title, source, time, deep read, relevance toggles), listing rows and lot rows (photo, title, vendor or auction, price or bids, deadline, score, watch and relevance toggles). Catalogue pages have a filter bar of labelled fields; history sits closed under a disclosure. A brief is a reading column with its figures and source in a side column. A page past the end of its list says so above its pager rather than going blank.

## Direction

Datasheet, dark, channel desk (seed ba4c87a4). The channel, archive, search and brief pages extend it with the same parts rather than a new concept. Unread is a small blue square that is also the read toggle; a score is a tabular numeral, 90 and above in full ink, under 60 faint. Two-state controls are words with a square that fills when on. Every cell wraps; nothing is cut with an ellipsis.

## Memorable moment

On a wide screen the sheet fills the window. Independent sections flow side by side: the home desk shows two or three channels per row, a news channel its top stories beside the rest, an auction channel its watched, ending and upcoming lots, a search its Channels. A long table, such as an archive day, a lone search section or a store's catalogue, sets its rows in two to four newspaper columns like a printed catalogue, each row's source, time, price and actions under its title. Marking a story read, rating it or watching a lot updates that row or section in place and announces it.

## Constraints

Everything in the admin brief applies: no overflow at 390, 1024, 1440, 2560 or 3840 px in Chinese or English, whole units (`.nw`), CSP with no inline script or style. The sheet has no maximum width; prose keeps its measure (72ch), form fields a sane width, sections flow into `.cols` columns of at least 60rem, and a reading table whose own box fits two 56rem columns and their gap (115.5rem, 1848px) flows into newspaper columns, order running down each column so reading and keyboard order stay as listed. Pages never build more than they show: the home desk picks rows in SQL, and the archive's day totals come from one grouped count. Times are Auckland time, including the archive's days and its date filter.

## Open decisions

The home page has no stored "last visit", so it cannot mark what arrived since the Owner last looked.
