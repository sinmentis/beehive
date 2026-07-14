# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
