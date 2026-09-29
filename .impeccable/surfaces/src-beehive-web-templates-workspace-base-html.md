---
version: 1
slug: "src-beehive-web-templates-workspace-base-html"
primary_target: "src/beehive/web/templates/workspace_base.html"
related_targets: ["src/beehive/web/templates/research_detail.html","src/beehive/web/templates/watchlist.html","src/beehive/web/static/admin.css"]
---

## Scope and mode

The Owner's workspace: `/research` (session list, the four pages of a session, the new-research form, the run-budget preview, the source form) and `/watchlist`. Operate mode. It shares the admin datasheet (`admin.css`, `datasheet_base.html`) but has its own shell, `workspace_base.html`.

## Audience, job and task

The Owner only. The watch list is opened often, mostly on a phone: see which reminder failed, retry it, see what closes next, stop watching a lot. Research is used on a desktop: read the conclusion, check the source behind a citation, exclude bad evidence, ask a follow-up, change the sources and refresh, and see why a run failed.

## Structure

Two numbered chapters in the rail, 1 研究 (blue count of finished runs not yet opened) and 2 关注列表 (amber count of failed reminders); the footer links back to reading and to the admin. A session has four pages in the rail under its chapter: 1.1 结论与追问, 1.2 证据, 1.3 信源与计划, 1.4 运行记录. The watch list has 2.1 需要注意 (every failed reminder, whatever the filter) above 2.2 关注的拍品.

## Direction

Datasheet, dark, notebook structure (seed f5d11c5a, dealt 6, 3, 5; the Owner chose A, the notebook, from HTML mocks built on production data). The conclusion is the first answer in a transcript, follow-ups continue below it and the composer comes last; a sticky side column holds the cited source and the session facts. No chat bubbles, no cards.

## Memorable moment

Clicking a citation number on a wide screen shows that source in the side column (publisher, quality, excerpt or a note that none was saved, exclude, open the source) and outlines every copy of the number in the text. Stopping a watch re-renders the list, the head counts and the rail count together, moves focus to the next lot and announces the change.

## Constraints

Everything in the admin brief applies: no overflow at 1440, 1024 or 390 px in Chinese or English, whole units in tables (`.nw`, `ui.units`, `ui.keep`; `.c-words` keeps CJK words whole), `ui.line_field` for free-length values, CSP with no inline script or style. AI text sits at a 72ch measure. At 1180 px and narrower the side column moves under the text and citations open the source in a new tab. Times use the shared `short_time` filter in Auckland time. Research session delete and clearing closed watches cannot be undone and ask once, in words, without a typed confirmation.

## Open decisions

None recorded.
