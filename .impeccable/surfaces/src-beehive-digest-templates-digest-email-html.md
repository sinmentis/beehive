---
version: 1
slug: "src-beehive-digest-templates-digest-email-html"
primary_target: "src/beehive/digest/templates/digest_email.html"
related_targets: ["src/beehive/digest/compose.py"]
---

## Scope and mode

Read mode, CODE-LED, evolved from editorial seed `0f190027`. The surface is the HTML digest email (`digest_email.html`). `compose.py` and `translations/background.py` supply kind-aware facts, escaped detail markup, hostname references and locale punctuation. Plain-text content and delivery semantics are retained, with terminal punctuation added to English and Chinese omission notes. The web sheet and admin authority are unchanged.

## Audience, job and task

The Owner scans a daily digest in an inbox, often on a phone. For each event, they read the full summary, check its price or deadline, then open the source or saved brief.

## Structure

- No visible masthead, contents list or section headings. A hidden, localized preheader carries the digest name, date and channel/item counts.
- Per channel, in order: source warnings, one bounded block per event, then the channel line. That line has an optional omission note punctuated in English and Chinese, followed by the localized channel link.
- In each block: the full summary, a decision fact (bold new price or a grouped local deadline with its zone), then references. These are the hostname source link and, only when a saved brief exists, its link.
- Footer: per-channel health statuses, the snapshot note and the site link.

## Direction

- **THESIS:** One complete event per visible block, not a continuous newsletter.
- **OWN-WORLD:** Dark slate, soft white text, blue underlined links, outlined entry surfaces.
- **STORY:** Read the summary, inspect the price or deadline, open the source or saved brief.
- **FIRST VIEWPORT:** Content begins immediately: a fluid 640px column, 8px gutters and gaps, 10px by 12px block padding, 16px/24px body and 14px/24px references. No masthead or rules.
- **FORM:** User-approved dark entry blocks, evolved from editorial direction 1, seed `0f190027`.

Approval: the user rejected several mockups and explicitly required bounded entry blocks. They approved light v6, asked for a dark version, approved v7 and requested implementation. This supersedes the earlier no-box and light-email refinement assumptions. The v7 mock is at `/tmp/ui-design-compare-2136532-1793326426/content/email-dark-blocks-v7.html`, which is temporary and outside the repo.

## Memorable moment

Each event is a self-contained block with its decision fact in it. The price or deadline is never pushed into small metadata.

## Constraints

The canvas is `#10151c`. Entry blocks are `#19232f` with a 1px `#62768c` stroke, 5px radius and 10px by 12px padding, with 8px gaps. Text is `#e7eaed`, links `#a9ceff`, references `#a7b8cc`, the footer `#a7afb7` and warnings `#f0b44c`. Primary and decision text is 16px/24px, underlined links 14px/24px and the health footer 13px/20px. The system stack, including Arial and Chinese fallbacks, is for email compatibility, not new web typography. The layout is a fluid, single-column table with a 640px maximum, 8px outer gutters, inline styles as the baseline and a conditional 640px Outlook wrapper. Metadata declares dark. Long links wrap. Existing localized labels are retained, and the field separator is localized. None of these values becomes a web token, and the block is not a web card.

## Evidence and finish

Captures are `.impeccable/review/email/{en-desktop,en-mobile,zh-desktop,zh-mobile}.png`, plus the approved mock captures `approved-{desktop,mobile}.png`. The full finish review found fidelity and the craft floor matching, except for two punctuation fixes. Both were applied, and an independent verdict scored them resolved: **ship** at that two-fix scope, not a new blanket review. No raster assets ship.

The reported checks are 146 focused tests and a full Ruff pass. Browser previews at 320, 390 and 640px, in English and Chinese, with 50 long items, a long channel name and enlarged text, showed no overflow. The 50 representative full summaries render to 55,323 bytes; total size for arbitrary digests is not guaranteed. This evidence covers local rendering, not deployment. Gmail, Apple Mail and Outlook have not been tested natively, and no native-client validation is claimed.

## Open decisions

Native Gmail and Apple Mail checks on phones remain before release, including dark mode, larger text and the largest expected digest.
