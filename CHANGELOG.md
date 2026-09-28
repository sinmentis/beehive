# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Per-Channel fetch scheduling: each Channel now chooses either a fixed interval (every 3 or 6
  hours) or a daily wall-clock time in its own IANA timezone, defaulting to `Pacific/Auckland`. A
  daily schedule is anchored to its calendar slot instead of the last successful fetch, so a late
  run never pushes the next day's fetch later, and a manual "fetch now" does not move the
  automatic schedule. Existing Channels keep their current interval behavior on upgrade.
- Owner-only Research Sessions: a persistent, one-time question investigated with the existing
  credentialless connectors, a visible AI-proposed Research Plan, durable asynchronous evidence
  collection and clustering, a conclusion-first citation-backed Research Synthesis, stable
  per-session citation numbers, owner evidence curation (exclude or annotate an Evidence Item),
  and a durable long-running chat with versioned Conversation Memory. See
  [Research Sessions](README.md#research-sessions).
- An always-on durable Research worker and a periodic reconcile timer for Research Runs and
  Research Chat replies, deployed alongside the existing collector, digest, and deep-read
  workers. See [`deploy/README.md`](deploy/README.md#research-worker-adr-0009).
- Global English-default localization for the web interface, email delivery, alerts, and
  language-aware AI output, with Simplified Chinese, Japanese, Korean, Spanish, French, and
  German support.
- Owner-triggered, asynchronous full-article AI briefs with cached results, regeneration,
  partial-content warnings, and a dedicated responsive reading page.
- A safe, resumable, and reversible migration for rewriting existing unread summaries.
- An admin model selector that applies one validated Copilot model to future rankings, comment
  summaries, summary rewrites, and article briefs.
- Per-channel highlight-count and minimum-score settings for Channel pages and daily digests.
- Home-dashboard read/unread views, counts, and owner voting controls.
- All About Auctions upcoming-lot monitoring with public bids, descriptions, estimates,
  seller-stated RRP, fee-inclusive cost, stable-lot refreshes, and Tracker AI ranking.
- Immutable Editorial, Monitor, and Tracker Channel workflows with definition-driven ranking,
  persistence, lifecycle, events, navigation, and notification behavior.
- Dedicated Channel panels: Editorial reading and feedback, Monitor catalogue search and changes,
  and Tracker watched, ending-soon, upcoming, and permanent-history sections.
- Strict Source compatibility declarations enforced by admin forms, persistence, collection, and
  startup contract tests.
- Generic Tracker Watches and follow-up reminders, with All About Auctions as the first adapter and
  compatibility-preserving auction reminder storage and deployment names.
- Owner administration safety and operations: typed destructive confirmations, seven-day
  transactional recovery, recent activity, bounded non-persisting Source test samples, Channel
  onboarding, delivery history, and cross-worker System Health.
- Email Group calendar schedules with local weekdays, times, and IANA timezones, plus non-mutating
  email previews and test sends.
- Research Source management, enforced work-budget previews, run/snapshot/synthesis history,
  synthesis-only retries, unread completion state, and localized completion emails.
- International designer-clearance monitoring for THE OUTNET, Mytheresa, END., and YOOX, with
  verified markdown thresholds and listing-currency preservation.
- Every Channel in a digest email carries a status line (sources OK, listings tracked, or no
  changes), and a capped Channel says how many more events are waiting.
- A nightly host-side SQLite backup that restores each archive into a scratch file to verify it
  before keeping it, and `deploy/release.sh` for SHA-tagged releases from clean commits.
- CI that runs Ruff and the test suite on every push.

### Changed

- Email Groups now consume actionable Channel events and may render mixed Editorial, Monitor, and
  Tracker sections without sending empty scheduled messages.
- The digest timer now evaluates Email Group schedules and Research completion notifications every
  15 minutes. Individual groups remain governed by their own interval or calendar schedule.
- The fetch timer now runs every 15 minutes as well. Individual Channels remain governed by their
  own interval or daily calendar schedule. A Source whose last automatic attempt failed waits an
  hour before its first retry, and the wait doubles with each further failure up to 24 hours.
- New ranking summaries state the strongest evidence-supported conclusion in one sentence instead
  of only describing the article topic.
- Failed article briefs now identify the failing stage, explain whether the LLM ran, and provide a
  safe next step without exposing internal error details.
- Reddit deep reads fall back to stored self-post text when Reddit blocks automated page access,
  with an explicit warning about missing comments, edits, links, and truncated long posts.
- Featured now ranks only content published during a configurable Auckland calendar-day window,
  defaulting to three days and falling back to fetch time when publication time is unavailable.
- Featured table columns can be resized with pointer dragging or the keyboard.
- The SQLite schema is versioned with `PRAGMA user_version`. `init_schema` only migrates a database
  that is behind and refuses one that a newer, incompatible release migrated; `--mode migrate` is
  the explicit release step.
- Digest events expire: Editorial news after 3 days, Monitor discoveries after 7 days, and price
  drops and restocks after 3 days, but never before the group's next scheduled email, so a weekly
  group still gets the whole week. Listings from a Source whose data is stale are held back from
  email until it recovers. A new catalogue Source's first snapshot is a baseline and sends no
  discovery emails.
- Every fetch outcome is logged to the journal, and digest warnings name the Source the way the
  admin UI does, say how long it has been failing, and flag a Source that silently stopped
  fetching.
- SQLite runs with `synchronous=NORMAL`, and each fetch is ingested in a single transaction.
- Container units log each line once and keep Podman lifecycle events out of the journal. The
  research-reconcile and deep-read backstop timers run hourly and every 30 minutes.

### Fixed

- Editorial digests permanently suppress repeated stories with the same publisher and exact
  headline, even when Google News republishes them under a new GUID on a later day.
- Digest subjects and channel delivery dates now use each Email Group's configured timezone
  instead of the UTC date.
- Failed manual fetches no longer delay the next automatic Channel slot, and paused Sources no
  longer distort the next-fetch preview.
- Shopify collections with more than 1,000 products are fetched completely instead of retiring
  every listing past the fourth page, and Land & Sea no longer truncates listings past 100 pages.
- A Monitor snapshot that would retire most listings at once is held until a second snapshot in a
  row confirms it, so a briefly empty storefront response no longer retires the catalogue.
- A discount change on a clearance listing reranks it again.

## [0.1.0] - 2026-07-14

Initial public alpha release.

### Added

- Self-hosted, single-user AI news aggregator with per-channel interests, fetch
  intervals, and email recipients.
- AI ranking, concise summaries, and optional comment summaries.
- Read/unread state and owner-only feedback controls.
- Scheduled and manual collection cycles.
- Daily email digests via Azure Communication Services.
- SQLite storage and rootless Podman Quadlet deployment units.
- Source adapters for six supported source families:
  - Reddit public subreddit Atom feeds
  - Google News search-query RSS feeds
  - Hacker News official Firebase API
  - Reserve Bank of New Zealand official RSS
  - New Zealand Government official RSS
  - Federal Reserve official RSS

[0.1.0]: https://github.com/sinmentis/beehive/releases/tag/v0.1.0
