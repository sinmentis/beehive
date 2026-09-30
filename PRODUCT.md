# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One Owner: the person who self-hosts Beehive. Only the Owner signs in. Admin is password-only and has no other roles (docs/adr/0003-public-read-surfaces-password-only-admin.md).

The Owner uses admin mostly from a desktop browser, and opens it rarely, only when a configuration needs to change. Every admin page has to be understandable without remembering how it worked last time. Phone use must still work but is secondary.

The Owner also has a workspace for their own research and watched lots. The Watch List is opened often, mostly on a phone, to see which reminder failed and which lot closes next. Research is used on a desktop to read a conclusion, check the sources behind it, exclude bad evidence, ask follow-up questions and see why a run failed.

## Product Purpose

Beehive is a self-hosted AI briefing system. It collects items from chosen sources, ranks each item against a channel-specific interest profile, and delivers conclusion-first summaries through a personal dashboard and scheduled email. Monitor channels watch shopping catalogues for new listings, price drops and restocks. Tracker channels follow time-bound listings such as auctions.

Admin exists so the Owner can keep this running: see whether collection and delivery are healthy, and change channels, sources, email groups, AI and language settings. Success means a change takes one visit, the current state is obvious, and nothing gets misconfigured by accident.

## Positioning

A single-owner, self-hosted briefing pipeline on SQLite, with per-channel AI ranking and three immutable channel workflows (editorial, monitor, tracker). Admin is its control room, not a multi-tenant SaaS console.

## Operating Context

- All of these are confirmed reasons to open admin: check fetch and source health; add, edit, test, pause and remove sources; tune a channel's profile, fetch schedule, highlight count and minimum AI score; manage email groups and preview or test-send a digest; switch the LLM model or interface language; review the audit log and undo a destructive action within seven days.
- Visits are infrequent and task-driven, on desktop.
- Deployment is rootless Podman on one host, served at news.shunlyu.com behind a proxy. Schedules run in Pacific/Auckland time.

## Capabilities and Constraints

- Server-rendered FastAPI and Jinja templates with no client framework. Every page (the reading pages, the admin and the Owner's workspace at `/research` and `/watchlist`) uses the one stylesheet `src/beehive/web/static/admin.css` and the dark datasheet it describes; the sheet fills the screen at any width. All share `beehive.js`. Forms post with CSRF tokens. The CSP allows no inline script and no third-party fonts, so the Archivo font is self-hosted.
- Interface copy lives in `src/beehive/translations/`: `web.py` for what every page shares, `web_admin.py`, `web_reading.py` and `web_workspace.py` for each area, in seven locales (en, zh-CN, ja, ko, es, fr, de). Parity tests require every key in every locale. The current interface language is Simplified Chinese.
- Content is long and user-supplied: Chinese and English channel names, full source URLs, multi-paragraph channel profiles, fetch error messages, timezone names. Layouts must hold all of it without overflowing.
- Domain terms come from CONTEXT.md: Owner, Channel (Editorial, Monitor, Tracker), Source, Email Group, Research Session, Tracker Watch.
- A channel's workflow is chosen at creation and never changes. Each connector declares which workflows it supports.

## Brand Commitments

- Name: Beehive, 蜂巢 in Chinese. Mark: the hexagon in `src/beehive/web/static/favicon.svg`.

## Evidence on Hand

- The production database holds real channels, sources with long URLs, mixed Chinese and English names, email groups and audit history. Use a copy of it for layout testing.
- `docs/assets/*.png` are synthetic previews.
- No testimonials, customers or usage metrics exist. Do not invent them.

## Product Principles

1. Clarity first. The Owner visits rarely, so each page says what it controls and what state it is in, in plain words.
2. State sits next to the thing it describes: schedule, last result and failures appear beside the channel or source they belong to.
3. Real content fits. Names, URLs, profiles and errors wrap or truncate on purpose and never overflow their container.
4. Destructive actions are separated, explained, and undoable where the system supports it.
5. One vocabulary. The same control, label and term mean the same thing on every page, in the Owner's language.
