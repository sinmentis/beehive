# PartyMap Civic automation

Civic runs outside the jobs worker, as two timer-started units with their own locked image
(ADR-0013). How to install and operate them is in
[deploy/README.md](../deploy/README.md#civic-partymap-worker-adr-0013); `CIVIC_SPOOL_ROOT` and
`CIVIC_SPOOL_SHADOW` are in [Configuration](configuration.md#environment-variables).

Beehive can run Owner-approved Civic Watches against fixed New Zealand official, party-primary,
and licensed media catalogs. A daily Civic Run gathers and clusters documents, persists their
provenance as Civic Evidence, applies the deterministic evidence policy, spends at most one
bounded research pass, generates four citation-equivalent locales, verifies them independently,
and seals one immutable Civic Batch. Production narrows the broader policy to publication
contract v1: `timeline` + `add`, with `party-timeline-event`, `legislation`,
`official-finding`, or `electoral-administration`.

PartyMap publication uses exactly one active Civic Watch whose subject is `partymap`, whose
schedule is `06:30` in `Pacific/Auckland`, and that has one published Civic Vocabulary. The
worker schedules nothing unless all three are true. The Watch label is never a party identity.
Party-primary catalog entries provide their own canonical party slug; official and media
clusters are matched against a closed alias registry for the 17 catalogued parties. Zero or
multiple matches settle that Candidate as unsupported before any AI call, while the rest of the
Run continues.

Before scheduling or consuming candidates, the command checks the live Copilot account model
list for both pinned models. Missing or disabled models fail explicitly and trigger the unit's
existing failure notification. There is no automatic downgrade or same-family verifier fallback.

After correcting an operational failure, an operator can authorize one catch-up slot:

```bash
python -m scripts.run_collector --mode civic-run \
  --civic-catch-up-at "2026-10-20T07:00:00+00:00"
```

Use the actual recovery time, with a timezone, no later than the current time. The current daily
Run must have failed before that time. Reuse the exact timestamp when retrying the command.
This creates a separately audited Run with the normal evidence gates and publication cap, rather
than resetting the failed Run's deadline or rewriting its history. An active Run blocks another
slot. Deferred candidates are adopted by the normal worker; rejected content is not approved or
republished by this operation.

Metadata-only party sitemaps and listings are discovery surfaces, not evidence bodies. After the
deterministic 25-cluster cap selects a Run's work, Beehive fetches only those selected on-host
detail pages through the same host allow-list, crawl pacer, content-type check and response-size
cap. It extracts the article heading, bounded article text and a publication date before
Candidate creation. Template furniture is removed at that point rather than stored: a leading
back-link and section label are dropped only when the article's own heading is what follows
them, and a trailing promoter statement only when it is the short trailing sentence the
Electoral Act requires. Both fail closed, because this text is the record's own proof and is
also the closed set of numbers generated text may contain. The
pre-hydration cluster plan remains authoritative even when the recovered date is older than the
listing record. If one selected detail page cannot supply usable text or a trustworthy date, the
whole cluster stays unconsumed and is retried on a later Run; it is never split, guessed into
today's date, or settled as a permanent `snapshot_unusable` rejection. A malformed or non-HTTPS
detail URL is recorded as `detail_url_invalid` and defers only that cluster instead of terminating
the Run. English number words in
source evidence are normalized only when building the evidence snapshot, so four-locale output
may use the required digits without weakening cross-locale number parity.

Plan the Owner setup first, then apply it explicitly:

```bash
.venv/bin/python -m scripts.setup_partymap_civic --db-path "$DB_PATH"
.venv/bin/python -m scripts.setup_partymap_civic --db-path "$DB_PATH" --apply
```

The default invocation writes no Watch or Vocabulary. `--apply` atomically creates the global
Watch and publishes required PARTY terms derived from the checked-in alias registry. Replaying
the exact setup is a no-op; another active Watch, an incompatible `partymap` Watch, an Owner draft,
or a different published Vocabulary is a conflict and is never replaced.

Delivery is a local two-phase filesystem seam:

Before sealing, the shared publication preflight checks every verified Candidate's current
qualification, exact generation/verifier provenance, current content rules, actual chosen
citations, and complete v1 item. Deployment drift starts bounded requalification, regeneration
and independent reverification under a new receipt revision; it does not discard otherwise
publishable carryovers. Genuine current content failures receive explicit rejections. This gate
is mandatory for the `partymap` Watch even through low-level seal APIs. Other Civic Watches retain
their generic, non-publication Batch API.

The seal commits membership, both hashes, and canonical envelope bytes in one transaction.
Spooling emits those frozen bytes without rebuilding snapshots under newer generation policy.

1. Beehive writes canonical `civic-publication-v1` JSON to
   `$CIVIC_SPOOL_ROOT/inbox`. The write is atomic and uses the SHA-256 payload hash bound at seal;
   it does not claim delivery or record a receipt.
2. The destination writes a whole-Batch `civic-publication-outcome-v1` JSON file to
   `$CIVIC_SPOOL_ROOT/outcomes`.
3. An accepted outcome claims the sealed Batch and writes one SPOOL receipt bound to both the
   legacy Batch content hash and the payload hash. A rejected outcome quarantines the Batch.
   With no outcome, the Batch remains sealed and no delivery attempt is consumed. Outside shadow
   mode, `accepted` with `proposal: null` is valid only for an empty envelope.
4. After durable acceptance or a matching terminal rejection, Beehive atomically moves the inbox
   envelope to `processed/accepted` or `processed/rejected`. Outcome JSON remains in `outcomes`
   as the cross-seam audit fact. Shadow mode never archives.

Legacy Batches are never backfilled or reinterpreted. See
[Civic publication reliability](civic-publication-reliability.md) for stale-v3 handling,
historical artifact recovery, semantic limits, and the locked Civic worker image.

Set `CIVIC_SPOOL_SHADOW=1` to write and validate envelopes/outcomes without consuming accepted
outcomes or creating receipts. The schemas shipped in `src/beehive/civic/schemas/` are the
versioned interface; generated claims, entities, and policy status remain local audit data and
are not included in the envelope. Because the destination has no uncertainty rendering, any
generated locale set with `uncertainty: true` is rejected before verification and Batch seal.

Civic jobs also stream newline-delimited JSON (NDJSON) events to stdout. Every record has
`schema_version: "partymap.civic.event.v1"`, an `event` name, a UTC `timestamp`, and one
invocation-scoped `correlation_id`. Each event is flushed immediately and carries IDs for the
Watch, Run, Batch, Candidate, source, and spool envelope when those identities exist. Event
fields are allow-listed; article text, URLs, prompts, model responses, credentials, headers,
cookies, and environment values are never emitted. The command's existing final JSON summary
remains the last output document.

Inspect recent state without changing SQLite or reading item contents:

```bash
.venv/bin/python -m scripts.diagnose_partymap_civic \
  --db-path "$DB_PATH" --spool-root "$CIVIC_SPOOL_ROOT" --since-hours 48 --pretty
```

The diagnostic opens SQLite with `mode=ro` and `query_only`, reports only bounded operational
metadata, validates spool hashes/correlation, and lists recovery anomalies. It is safer to share
than the database or spool payloads, but its IDs, hashes, and sanitized provider references can
still identify internal work. Review them before sharing outside the operations team.

## Notices and source holds

Civic Runs queue operational notices: source failures, clusters whose detail pages could not be
fetched, and cluster overflow. The jobs worker emails them to the default recipient as one
combined message, at most once every six hours. Notices older than 48 hours are suppressed
instead of sent.

A Candidate whose source articles have not been captured is held and retried on later Runs for
up to 14 days. One whose only evidence comes from outlets whose articles are never captured
(Stuff, The Post) is rejected at once as `source_uncapturable`, because no later Run can date it.
When a party site refuses a detail page (HTTP 401/403), the rest of that Run skips the site, so
it cannot spend the hydration budget other sources need.
