# Civic publication reliability

## Preflight and atomic seal

Every unsealed verified PartyMap Candidate, including a capacity carryover, passes
`publication.preflight_candidate` before capacity selection. It checks the latest publishable
qualification; generation and verification versions, provider, allowed models, output hashes,
snapshot bindings and rendered requests; a matching supporting verifier judgment; current
`check_candidate_content` rules; and the complete actual publication-v1 item and chosen citations.
An arbitrary HTTPS evidence row or successful locale deserialization is not publication proof.
For new seals, the verifier must also equal the current pinned independent-verifier choice for
the recorded drafting model. An obsolete allowed verifier or an unpinned automatic drafting
route requests bounded refresh; neither is an immediate negative judgment on the news.

Genuinely invalid current content receives an explicit rejection without blocking valid
neighbors. Stale deployment provenance is not a negative news judgment: it requests a bounded
refresh instead. One SQLite transaction commits seal-time decisions, membership, both hashes,
and canonical bytes in `civic_batch_publications`. Corrupt frozen publication/stage artifacts,
failed writes, or an expired lease/deadline roll back the seal, including any source holds made
during preflight.

Source freshness is checked before qualification or AI-receipt drift at publication preflight.
`CivicSourceRefreshBlocked` is handled by the seal's locked source-hold path, separately from
`CivicPublicationRefreshRequired`; it cannot spend an AI-only revision or reject the news.
Qualification and direct generation use the same guard scoped to the `partymap` Watch, so
generic Civic fixtures keep their existing semantics. A read-only snapshot is not publication
authorization; the authoritative source check always runs again under the seal transaction.

## Bounded refresh of unsealed work

Prompt v4 still uses AI schema 1. Parsing schema 1, or merely matching a version label, does not
upgrade cached generation or verification. Stale versions, provider/model identities, snapshots
or rendered requests start a new `ai_revision` for an unsealed Candidate. The worker requalifies
its captured evidence, genuinely regenerates all locales, and obtains a new independent verifier
decision. A fresh negative decision can reject the Candidate; old deployment drift alone cannot.

Qualification and AI-stage readers use only the active revision. Original successful receipts
and judgments remain auditable, with their original versions, hashes, outputs and models.
Superseded open reservations close as failed and cannot later record a success. Source-item
ownership is retained; no Candidate or article is recreated to evade deduplication.

The fixed limits remain real: 30 reserved AI calls per Run, two attempts per stage/revision/Run,
at most three refresh revisions and three AI carryover deferrals per Candidate. All revisions'
reservations count toward their Run's budget. Budget exhaustion or interrupted refreshes use
`DEFERRED_AI`; a later slot resumes qualification, research or the missing active AI stage.
Research allowance is never reset. Exhaustion has an explicit resource-limit reason, not a false
content verdict. Drift found during final seal is deferred rather than published.

Source-capture and prompt/verifier upgrade causes can share an already pending revision only
while it has no qualification or AI reservation. Capture updates must be applied under that
same unsealed Run claim before requalification. Once qualification or AI work has begun, changed
inputs must not reuse its decision or receipts. This keeps a coordinated upgrade to one revision
without weakening the provenance fence.
The source plan's original expected revision remains authoritative even when a policy refresh
opens its pending successor before hydration reservation. Reservation and apply must not replace
that expectation with a fresh read of the Candidate's revision. If qualification or any AI
reservation starts in the successor while HTTP is pending, the old source callback is stale;
it must not consume another revision or supersede that work.

## Immutable source captures

Article publication witnesses are distinct from sitemap modification and collection times.
National article-bound JSON-LD and local bylines are checked before related-story dates;
Conservative releases are recovered from the already-delivered, identity-bound Wix body.
RSS summary text alone does not establish a complete article. Interior background references
in a complete newsletter are not terminal continuation links; partial, paywalled and clipped
previews remain unavailable. Initial and research evidence retain each document's own date
rather than inheriting the Candidate or cluster date.

`civic_evidence_captures` stores append-only versions of an existing Evidence identity. Each
version retains its predecessor, Run, AI revision, complete source metadata and content hash.
The original evidence row, its ID, source ownership, qualifications, receipts and judgments
remain intact. For legacy evidence, that original row remains the initial historical record;
`list_evidence_capture_history` returns the subsequently appended versions without backfilling
or duplicating the original. Current evidence readers select the whole latest capture, including an explicit
unknown publication or event date. They never revive a legacy lastmod-derived date with
`COALESCE`. A malformed current capture hash remains an explicit integrity error and requires
operator attention. Its unsealed Candidate receives a technical source hold; it is never
silently repaired or called bad news, and cannot wedge unrelated future updates indefinitely.
Date sufficiency uses current source publication/event witnesses, not a date copied from Candidate
context. A complete undated corroborator can accompany a dated source; it stays in capture
history but cannot supply dated qualification/model evidence. A wholly undated Candidate is
held without inventing a date.

New evidence writers pass `source_metadata=document.raw_metadata`, including official event
chronology outside `source_capture`. Repeating an existing evidence identity returns its current
projection; it does not install new metadata. Published, sealed, rejected and quarantined work
cannot receive replacement captures. Official records can be current with an explicit source
event and unknown publication; they must not invent HTML-extraction provenance.

`apply_evidence_refresh_if_claimed` atomically checks the stored reservation, claim, lease,
deadline, BUILDING membership, active evidence fingerprint, expected revision and complete
prepared capture set. It appends all captures and invalidates unsealed AI decisions together.
Both whole-set and replacement-specific fingerprints are recomputed from current capture inputs
under that lock using the shared `capture_fingerprint` helper, never a separate field list or
mutable publication history.
Any write failure or end-of-transaction deadline expiry rolls back both. An identical completed
apply is an explicit replay; a changed callback cannot overwrite it.

A legacy DRAFTED or RESEARCHING Candidate can refresh without losing its source items. A genuine
replacement starts one bounded AI revision, or shares an already pending revision with no
qualification or AI reservation. A paid, unfinished research attempt stays pending across refresh,
Run failure and adoption; its global allowance is not reset. Starting a source fetch marks that
work as unresolved before I/O, so a crash before the first AI revision cannot discard the news.
Successful capture installation clears this unresolved-source marker. Later AI interruptions
then spend the normal AI carryover allowance rather than receiving unlimited free deferrals.

### Shared hydration authorization

`civic_hydration_attempts` durably reserves a single limit of 25 detail requests per Run across
fresh ingestion and carryover refresh. Reserve before network work. Failed, abandoned and
partially used reservations stay charged, including after restart. Authorization fields and
settled results are immutable; deleting a record cannot refund the budget. A new claim cannot
complete an old claim's reservation.

Fresh requests bind the original document identities to a stable work key. Completed documents
can be restored with `hydration_documents` after restart without another fetch. Replacement
requests additionally bind the complete source plan and active evidence fingerprint.

An unchanged Candidate/capture fingerprint gets at most three replacement-fetch reservations
across Runs. Unsupported source routes, exhausted source retries, unavailable dates and revision
limits receive explicit source holds, not negative news judgments. Source holds do not spend
another AI revision or the AI deferral allowance. Unsupported routes and exhausted source retries
need operator attention; they do not retry without bound or silently trust the old metadata.

A hold is bounded as well. Once the attempts on a Candidate's current capture fingerprint span
`SOURCE_HOLD_LIFETIME` (14 days), the next hold settles the active Candidate `rejected` with
`source_hold_expired` and keeps the last hold reason in `ai_refresh_reason`, instead of being
re-adopted and held by every daily Run. Evidence from an outlet whose article pages are
client-rendered app shells (`article_capture=False`, currently Stuff and The Post) is never a
refresh target, so it spends no budget and never blocks a capturable neighbour. Its listing date
also cannot satisfy the date gate below; only a current capture can witness a date.

### Worker integration contract

The DB APIs do not perform network work or AI calls. The PartyMap production caller must use this
order; installing the additive tables alone does not activate the source gate:

1. Adopt carryovers into the claimed BUILDING Batch before fresh detail hydration can spend
   the shared budget. Use `adopt_capacity_deferrals(..., now_fn=clock)` so lease/deadline
   checks run under its lock and again before commit; expiry rolls back adoption and deferral
   history together. The later ingest adoption is idempotent. Run `candidate_source_plan` for every
   DRAFTED, RESEARCHING, VERIFYING and VERIFIED Candidate before screening or qualification,
   including capacity carryovers and partially generated work.
2. A CURRENT plan can continue. For PLANNED work, call
   `reserve_evidence_refresh_if_claimed` before `prepare_evidence_refresh`; keep the Run lease
   alive during preparation, then call `apply_evidence_refresh_if_claimed`. Reload the Candidate
   and its source plan after apply before using a claim or snapshot.
3. For a deferred plan, a preparation failure, `CivicHydrationBudgetError` or
   `CivicSourceRefreshBlocked`, call `defer_evidence_refresh_if_claimed` with the explicit reason.
   Include the reservation when a fetch was attempted. Do not turn these into content rejection
   or invoke AI on the old source capture.
4. Fresh detail hydration uses the same budget through `reserve_hydration_if_claimed`, with
   `units` and the original `documents`. Reuse a completed reservation through
   `hydration_documents`; otherwise record success with `complete_hydration_if_claimed` or failure
   with `fail_hydration_if_claimed`. Persist the entire resulting metadata with initial evidence.
5. Call `require_current_source_capture` before qualification/AI and again at final admission.
   A final source hold must settle only that unsealed Candidate, preserving healthy neighbors
   and capacity. Claimed API wrappers start their own transactions; do not call them inside the
   seal transaction. Historical spool replay must never run this new-source guard.

A `None` claimed result means the Run/Candidate authorization no longer holds or identical work
is already in progress, not permission to fetch or publish. A late source failure cannot replace
an existing AI deferral or reset its bounded lifetime. A stale/conflicting plan requires rereading
current state. Integrity errors must remain visible rather than being relabeled as an expected
source hold.

The DB wrapper `candidate_source_plan`, unlike the pure preparation planner, rejects a wholly
undated Candidate even when its article captures are complete. Installation returns
`status="deferred", reason="source_refresh_date_unavailable"` and persists its source hold
when no current publication or official-event witness establishes a date. The worker must skip
claim construction and AI for that result, rather than turn it into a Run failure. A mixed
dated/undated Candidate and an official event-only source remain eligible.

Current AI receipts do not certify source capture. The
[source-contract lifecycle regression](../tests/db/test_civic_source_contract_lifecycle.py)
starts five revision-1 `DEFERRED_CAPACITY` Candidates in a sealed prior Batch, with
`sealed_into_batch_at` still NULL. Their model, prompt, schema, snapshot and rendered-request
bindings are fully current, but source metadata is empty and dates came from September 4 lastmod.
Public adoption moves them into the claimed BUILDING Batch before any source refresh. Seal holds
those Candidates without spending AI revisions and admits a healthy neighbor.
Only real offline article extraction, atomic capture installation and fresh generation plus
independent verification permit publication at the witnessed September 2/3 dates in revision 2.
Original evidence and approval rows remain unchanged, and concurrent seal/fingerprint changes
cannot install stale captures. This is independent of a prompt or source-version bump.

## Final-event novelty

After current-output preflight, seal compares the generated English event and its actual cited
material against recent immutable outputs and items admitted to the same Batch. This supplements,
not replaces, source-item and pre-generation fingerprint deduplication. Distinct source URLs can
describe one development; shared party/topic/numbers alone are insufficient.

The conservative classifier recognizes specific formal bill identities with distinct introduction,
first/second/final reading, assent and commencement stages. Conditional/future effects are not
completed milestones. It also recognizes some repeated policy proposals using at least two exact
counted targets, the same relationship, subject, additional topic anchor and scope. Related explicit
reiteration cannot be promoted into a new announcement. Real stages, material follow-ups and
unclassified events are not blindly merged.

`publication_duplicate_event` and `publication_unsupported_newness` settle only the current
unsealed Candidate, with an auditable reason. Rejections refill capacity; an unpublished overflow
item cannot suppress another deferred peer. The final comparison is inside the seal transaction,
without AI calls, so a publication that appeared after generation is still considered.

Comparison is bounded to 90 days and 1,024 sealed/in-flight/delivered Candidates per Watch; an
unexpected history overflow is an operational error, not a silent truncation. Frozen v1 outputs
are read directly. Legacy comparison uses recorded English generation text where available, never
reconstructing old alias mappings or snapshots; unclassifiable legacy text remains a coverage
limit. No historical rows or artifacts are modified. History is not part of an AI snapshot or
prompt hash, so unrelated daily publications cannot churn carryover revisions.

These are high-precision safeguards, not general semantic deduplication. Short/ambiguous bill
names, unrecognized policy targets, paraphrases and complex material changes can evade matching.
The matched generator/verifier novelty and language-fidelity instructions remain important; no
version label or deterministic match certifies all factual meaning.

## Whole-site source outages

A source whose listing and human-verified health URL (`OUTAGE_HEALTH_URLS`) both return an origin
HTTP status in 401, 403, 404, 410 or a server/CDN-origin 5xx is held, not failed: the collector
reports it under `source_holds` and exits 0. Missing responses (DNS, TLS, timeout), 429, other
4xx, 501, 505-511, and sources without a verified health URL still fail. If no source succeeded,
or more than max(1, 10%) are held at once, every hold fails as a suspected local cause.

`civic_source_outages` keeps each episode's clock under the Run's claim. A held observation at
least seven days after the episode began fails again, so an operator decides. Only a healthy
fetch at least seven days after the last held one ends the episode, and a row unobserved for more
than three days is discarded as stale. Holds are also listed in the per-run
`civic_source_failures` notification and in diagnostics `source_outages`.

## Historical records and rollout

New sealed Batches emit their frozen bytes without rebuilding snapshots or applying newer
content policy. Legacy Batches replay only their original canonical artifacts matched to a
previously bound payload hash. Missing legacy bytes raise `CivicSpoolLegacyProjectionError`;
they are not reconstructed or quarantined for policy drift. Missing or corrupt new frozen
artifacts fail closed. Migrations and policy updates never rewrite historical published content,
evidence, receipts, or sealed membership.
That includes sealed members of quarantined or otherwise unpublished Batches; site publication
counts are not an inventory of the immutable records. Unstamped capacity carryovers assigned to
a sealed prior Batch are different: adopt them into a new claimed BUILDING Batch before refresh.

Revision migration preserves existing values and issued-ID high-water marks. Both installed
Civic jobs must use revision- and capture-aware code together. Once refreshed decisions or source
captures exist, an older image that assumes one lifetime success per stage or reads only the
original evidence rows is not a safe rollback against that database. Keep
the pre-rollout database/spool snapshot, but do not restore it over newer accepted publications;
use a compatible image or a forward fix instead.

## Semantic limits

Both prompts carry source-bound language guidance. Mechanical regression fences cover known
financial/medical mistranslations and future events expressed with past-time wording, using the
publication dates of the actually cited sources rather than the earliest cluster date. Legitimate
transport benefits, accounting ledgers and later corroborating reports remain distinct cases.
Independent verification is explicitly pinned to Claude Opus 5 for non-Claude drafts and GPT-6
Astra for Claude drafts. An unpinned `auto` draft or a conflicting verifier override is refused
before the worker claims work; model identifiers are not ranked alphabetically.

Citation-local numbers, date witnesses, source roles, recurring-money periods and translation
guards reject known failure patterns. They and the independent model verifier do not prove every
claim true or every translation faithful. Matching hashes prove integrity, not semantic accuracy.
Long-term improvements belong in source extraction, evidence, prompts and future gates, not
in-place edits to published records.

## Reproducible runtime

Use the dedicated [`deploy/Containerfile.civic`](../deploy/Containerfile.civic) for Civic images.
It pins the Python and uv image digests and installs from `uv.lock` with
`uv sync --locked --no-dev --extra ai --extra email --no-editable`. Build from the repository root:

```bash
podman build -t localhost/beehive:civic -f deploy/Containerfile.civic .
```

Repository Civic Quadlets reference this staging tag. Installed units must instead use the same
unique release tag for both Civic jobs, retaining the previous release image for rollback.
Keep the current image and one rollback image after the replacement is healthy; never remove
database or spool data as image cleanup. See the
[Civic deployment instructions](../deploy/README.md#civic-spool).
