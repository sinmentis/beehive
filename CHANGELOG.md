# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Changed

- New ranking summaries state the strongest evidence-supported conclusion in one sentence instead
  of only describing the article topic.
- Failed article briefs now identify the failing stage, explain whether the LLM ran, and provide a
  safe next step without exposing internal error details.
- Reddit deep reads fall back to stored self-post text when Reddit blocks automated page access,
  with an explicit warning about missing comments, edits, links, and truncated long posts.
- Featured now ranks only content published during a configurable Auckland calendar-day window,
  defaulting to three days and falling back to fetch time when publication time is unavailable.
- Featured table columns can be resized with pointer dragging or the keyboard.

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
