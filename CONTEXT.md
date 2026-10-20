# Beehive

Beehive helps a reader turn selected information sources into concise, evidence-grounded understanding.

## Language

**Research Session**:
A one-time investigation built from a chosen Research Question and set of Research Sources, whose collected evidence supports an ongoing conversation but never becomes recurring monitoring.
_Avoid_: Search session, chat session, ad hoc channel

**Source**:
A recurring collection configuration attached to a Channel.
_Avoid_: Provider, research source

**Editorial Channel**:
A recurring information feed whose ranked items use read state, Home and Archive aggregation, feedback, Deep Read, and regular email delivery.
_Avoid_: News mode, feed type

**Monitor Channel**:
A recurring mutable catalogue whose ranked listings remain available as active inventory or permanent history and can emit discovery, price-drop, and back-in-stock events.
_Avoid_: Shopping list, Tracker

**Tracker Channel**:
A recurring mutable set of time- or condition-bound listings. It sends regular email for new matches and lets the Owner select individual items for adapter-defined follow-up reminders.
_Avoid_: Auction Channel, Monitor

**Research Source**:
A connector and its source-specific input chosen by the Owner or added automatically by the Research Plan for one Research Session.
_Avoid_: Source, provider

**Research Question**:
The reader's natural-language statement of what they want to understand, used to guide retrieval, evidence selection, synthesis, and conversation.
_Avoid_: Keyword, prompt, channel profile

**Research Plan**:
The visible set of source-specific queries and selections generated from a Research Question for an explicit search or refresh action.
_Avoid_: Query plan, prompt, source configuration

**Evidence Snapshot**:
An immutable body of source material captured by one explicit search or refresh action within a Research Session.
_Avoid_: Search results, context window, knowledge base

**Evidence Item**:
One individual source result preserved inside an Evidence Snapshot and available for citation.
_Avoid_: Item, result card, document

**Evidence Cluster**:
A group of Evidence Items that describe the same underlying event or substantially duplicate one another while preserving their distinct publishers.
_Avoid_: Duplicate, topic, source

**Evidence Citation**:
A reference from a generated claim to the specific Evidence Item that supports it.
_Avoid_: Source list, link

**Research Synthesis**:
A versioned, citation-backed answer to the Research Question generated from the Research Session's active evidence.
_Avoid_: Summary, report, chat answer

**Evidence Sufficiency**:
The state in which the Research Question's material sub-questions are covered, important claims are independently supported or tied to a primary source, and unresolved contradictions are visible.
_Avoid_: Enough results, confidence score, search completion

**Conversation Memory**:
An AI-maintained, hidden compression of earlier conversation used to continue a long Research Session after the full message history no longer fits in context.
_Avoid_: Chat history, Evidence Snapshot, user notes

**Civic Watch**:
A recurring, Owner-approved commitment to monitor one civic subject on a fixed daily slot, producing at most one Civic Run per slot.
_Avoid_: Channel, Research Session, Tracker Watch

**PartyMap Global Watch**:
The sole active Civic Watch used for PartyMap publication. Its subject is exactly `partymap`; candidate party identity comes from application-owned source profiles and aliases, never this label.
_Avoid_: Party Watch, one Watch per party, party slug

**Party Alias Registry**:
The closed application-owned mapping from phrase-safe party names to the 17 catalogued party slugs, used only when no party-primary source already identifies the subject.
_Avoid_: Remote slug, fuzzy entity extraction, Watch subject

**Civic Vocabulary**:
An immutable, versioned set of Civic Terms that a Civic Watch's screening, drafting, and verification must all resolve against, so a published statement can only ever name entities the Owner already approved.
_Avoid_: Keyword list, prompt, glossary

**Civic Term**:
One approved name inside a Civic Vocabulary, carrying the role that decides whether matching material is required, optional, or excluded.
_Avoid_: Keyword, tag, entity

**Civic Run**:
One durable execution of a Civic Watch for exactly one slot, owned by a single worker lease from gathering through publishing.
_Avoid_: Fetch cycle, Research Run, job

**Civic Batch**:
The immutable, content-hashed envelope of a Civic Run's publishable Civic Candidates, sealed once and then delivered exactly once downstream.
_Avoid_: Payload, digest, Evidence Snapshot

**Civic Candidate**:
One proposed civic statement produced inside a Civic Batch, which may only be published after independent verification against its own Civic Evidence.
_Avoid_: Item, story, claim

**Civic Evidence**:
One source record captured for a Civic Candidate, carrying the tier that decides how much corroboration the Candidate still needs.
_Avoid_: Source, link, Evidence Item

**Civic Evidence Capture**:
An immutable version of a Civic Evidence record's source text, dates and full extraction metadata. An unsealed source refresh appends a capture and invalidates the active AI decision atomically, without rewriting original evidence or published history.
_Avoid_: Edited evidence, overwritten snapshot, source-date fallback

**Civic Hydration Reservation**:
The durable authorization for detail requests against a Civic Run's shared fresh-ingestion and carryover-refresh budget. Failed or abandoned requests stay charged; a completed result can be replayed without another fetch.
_Avoid_: In-memory counter, refunded retry, separate carryover budget

**Civic Research Attempt**:
One bounded, recorded extra pass that tries to resolve a Civic Candidate whose existing Civic Evidence is not yet sufficient.
_Avoid_: Retry, Research Session, enrichment

**Civic Verification**:
One independent recorded judgment on whether a Civic Candidate's statement is supported by its own Civic Evidence.
_Avoid_: Review, score, sufficiency

**Civic Notification**:
An Owner-facing message about a Civic Watch's own operation, delivered on its own retry budget and never blocking a Civic Batch.
_Avoid_: Digest, alert, Tracker Reminder

**Civic Delivery Receipt**:
The immutable proof that one Civic Batch was accepted downstream under a specific idempotency key, legacy Candidate-content hash, and canonical publication-payload hash, which makes a replayed delivery a no-op instead of a duplicate publication.
_Avoid_: Acknowledgement, log entry, delivery status

**Civic Publication Envelope**:
The versioned, canonical JSON projection validated and frozen atomically with a new Civic Batch's seal, then written unchanged to the local inbox spool. Its payload hash omits only its own `batch.payload_hash` field, and writing it is not delivery. Historical Batches replay their original bound artifacts rather than rebuilding them under current content policy.
_Avoid_: Civic Batch, receipt, API request

**Civic Event Identity**:
A conservative, source-grounded description of the development in a final generated entry, distinct from its article URL or pre-generation cluster fingerprint. Publication compares recognized identities against recent sealed outputs and same-Batch admitted items without rewriting history or changing AI snapshot hashes.
_Avoid_: Topic, source URL, cluster key, universal semantic deduplication

**Civic Publication Outcome**:
The whole-Batch accepted or rejected result written back by the destination. Only an accepted result can consume a delivery attempt and create a Civic Delivery Receipt.
_Avoid_: Partial result, inbox marker, delivery acknowledgement

**Processed Civic Envelope**:
A settled inbox envelope atomically archived under `processed/accepted` or `processed/rejected`; its outcome file remains separately durable in `outcomes`.
_Avoid_: Outcome, receipt, deleted inbox

**Destination Scope**:
The production boundary that narrows Beehive's broader evidence policy to shapes the current publication contract can represent.
_Avoid_: Evidence rule, coercion, fallback

**Civic Document Cluster**:
The deterministic group of normalized source documents that report one development, which becomes exactly one Civic Candidate however many sources covered it.
_Avoid_: Story, topic, bucket, dedupe group

**Civic Evidence Hydration**:
The bounded pre-ingest step that turns a selected metadata-only party document into a correctly titled citation with citable text and a trustworthy publication date, or defers its whole cluster unconsumed for a later Run.
_Avoid_: Unbounded crawl, best-effort scrape, post-ingest enrichment

**Consumed Civic Item**:
One source item permanently bound to the Civic Candidate that was built from it, so the same article can never produce a second Candidate on the same Civic Watch.
_Avoid_: Seen item, processed item, watermark

**Civic Qualification**:
The recorded decision of the pure evidence policy on one Civic Candidate, stored with a hash of the inputs that produced it so a replay is distinguishable from a changed decision.
_Avoid_: Screening result, verdict, approval

**Capacity Deferral**:
The record that a verified Civic Candidate was held out of one Civic Batch only because the day's publication cap was full, keeping it adoptable by a later Civic Run rather than rejected.
_Avoid_: Backlog, queue overflow, postponed candidate

**Owner**:
The authenticated person who administers Beehive and has access to private, cost-incurring workflows.
_Avoid_: Admin user, account

**Tracker Watch**:
The Owner's saved interest in one Tracker item, kept until the Owner removes it.
_Avoid_: Channel monitor, favourite

**Tracker Reminder**:
An adapter-defined follow-up email for a Tracker Watch. The auction adapter sends it when the current closing time enters the one-hour reminder window and may send again after a genuine extension.
_Avoid_: Digest, regular Channel email
