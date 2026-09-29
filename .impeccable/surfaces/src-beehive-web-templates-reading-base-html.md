---
version: 1
slug: "src-beehive-web-templates-reading-base-html"
primary_target: "src/beehive/web/templates/reading_base.html"
related_targets: ["src/beehive/web/templates/dashboard.html","src/beehive/web/static/admin.css"]
---

## Scope and mode

The reading pages set in the datasheet, starting with the home page (`/`, 精选). Read mode, with the Owner's triage on top. The page has two views: the channel desk (the default) and the ranked list of every featured story (`/?view=all|unread|read`, `&minimum_score=`, `&page=`). The channel pages, the archive and search still use `base.html` and `beehive.css`; they are meant to move into this shell next, with the same system.

## Audience, job and task

The Owner, mostly on a desktop, and anonymous readers. The Owner scans where anything new is, triages stories (open, mark read, deep read), sees the best live listings and the good auction lots still open, and opens a Channel to go further. A reader gets the same desk with no read state or owner controls.

## Structure

The rail lists 1 精选 (a quiet unread number for the Owner, not a badge), one chapter per Channel and the Archive; its footer holds the Owner's watch list, research, admin and log out, or 登录 for a reader. The head carries h1 精选, the window's counts (links into the ranked list), search and 全部标记为已读. Each Channel is a numbered section, 1.1 on, its state in the heading (window and unread counts, live listings, or open lots at the score floor and watches) with the fetch time and 打开频道, and a short table beneath: 4 stories, 5 listings or 5 lots.

## Direction

Datasheet, dark, channel desk (seed ba4c87a4, dealt 7, 3, 6; the Owner chose B, the channel desk, from HTML mocks built on production data). Unread is a small blue square that is also the read toggle; a score is a tabular numeral, 90 and above in full ink, under 60 faint. Every cell wraps; nothing is cut with an ellipsis.

## Memorable moment

Marking a story read re-renders its section in place: the next unread story slides into the same slot, focus stays on that slot's toggle, and the head, the section heading and the rail number all count down together. `j` and `k` walk the story rows, `o` opens one, `/` jumps to search.

## Constraints

Everything in the admin brief applies: no overflow at 1440, 1024 or 390 px in Chinese or English, whole units (`.nw`), CSP with no inline script or style. The desk never builds a whole Channel page: rows are picked in SQL and one grouped count serves every number on the page. On phones the rail is one scrolling line and a story row reads summary first, with source, age and deep read on a line beneath. Tracker lots on the desk start at score 80, or the Channel's own minimum when that is higher.

## Open decisions

The home page has no stored "last visit", so it cannot mark what arrived since the Owner last looked.
