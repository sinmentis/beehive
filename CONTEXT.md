# Beehive

Beehive helps a reader turn selected information sources into concise, evidence-grounded understanding.

## Language

**Research Session**:
A one-time investigation built from a chosen Research Question and set of Research Sources, whose collected evidence supports an ongoing conversation but never becomes recurring monitoring.
_Avoid_: Search session, chat session, ad hoc channel

**Source**:
A recurring collection configuration attached to a Channel.
_Avoid_: Provider, research source

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

**Owner**:
The authenticated person who administers Beehive and has access to private, cost-incurring workflows.
_Avoid_: Admin user, account
