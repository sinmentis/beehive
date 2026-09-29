---
name: Beehive Datasheet
description: "The Owner's datasheet, set dark. Records the design system in src/beehive/web/static/admin.css, shared by three shells: the admin, the Owner's workspace (/research, /watchlist) and the reading pages, which so far are only the home page (/). The channel pages, archive, search and deep-read briefs still use base.html and beehive.css and are not described here until they move."
colors:
  desk: "#0A0C0E"
  sheet: "#111417"
  band: "#191D21"
  hover: "#161A1E"
  field: "#0C0E10"
  rule: "#262B30"
  rule-2: "#3A4046"
  rule-ink: "#C9CFD6"
  ctl: "#666E76"
  ctl-danger: "#B04A42"
  ink: "#E7EAED"
  ink-2: "#A7AFB7"
  ink-3: "#808891"
  on-fill: "#0A0C0E"
  link: "#7DB6FF"
  link-2: "#A9CEFF"
  caution: "#F0B44C"
  danger: "#FF6E61"
typography:
  headline:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "0"
  title:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 700
    lineHeight: 1.35
  subhead:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 700
    lineHeight: 1.35
  lead:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 500
    lineHeight: 1.75
  body:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.55
  reading:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.75
  data:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.45
  note:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.6
  button:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 600
    lineHeight: 1.2
  label:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.78rem"
    fontWeight: 600
    lineHeight: 1.3
    fontVariation: "'wdth' 85"
  caption:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.78rem"
    fontWeight: 400
  tag:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.7rem"
    fontWeight: 700
    lineHeight: "15px"
  numeric:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: "'tnum'"
    fontVariation: "'wdth' 88"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Cascadia Mono, DejaVu Sans Mono, Menlo, Consolas, monospace"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.75
rounded:
  square: "0px"
  control: "2px"
spacing:
  gap: "8px"
  cell: "10px"
  row: "14px"
  desk-margin: "16px"
  gutter: "24px"
  sheet-inset: "28px"
  section: "32px"
components:
  sheet:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.square}"
    padding: "16px 28px 36px"
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.on-fill}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "32px"
  button-primary-hover:
    backgroundColor: "#FFFFFF"
    textColor: "{colors.on-fill}"
  button-primary-active:
    backgroundColor: "{colors.rule-ink}"
    textColor: "{colors.on-fill}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "32px"
  button-secondary-hover:
    backgroundColor: "{colors.hover}"
    textColor: "{colors.ink}"
  button-secondary-active:
    backgroundColor: "{colors.band}"
  button-danger:
    backgroundColor: "transparent"
    textColor: "{colors.danger}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "32px"
  button-danger-hover:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.on-fill}"
  button-small:
    padding: "0 9px"
    height: "26px"
  button-disabled:
    backgroundColor: "transparent"
    textColor: "{colors.ink-3}"
  link:
    textColor: "{colors.link}"
  link-hover:
    textColor: "{colors.link-2}"
  link-danger:
    textColor: "{colors.danger}"
  input:
    backgroundColor: "{colors.field}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "6px 10px"
    height: "34px"
  input-line:
    backgroundColor: "{colors.field}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "6px 10px"
    height: "34px"
  rail:
    backgroundColor: "{colors.sheet}"
    width: "216px"
    padding: "18px 12px 16px"
  rail-link:
    textColor: "{colors.ink-2}"
    rounded: "{rounded.control}"
    padding: "4px 8px"
    height: "32px"
  rail-link-hover:
    backgroundColor: "{colors.hover}"
    textColor: "{colors.ink}"
  rail-link-current:
    backgroundColor: "{colors.band}"
    textColor: "{colors.ink}"
  rail-count:
    textColor: "{colors.caution}"
    rounded: "{rounded.control}"
    padding: "0 4px"
    height: "18px"
  rail-count-info:
    textColor: "{colors.link}"
    rounded: "{rounded.control}"
    padding: "0 4px"
    height: "18px"
  rail-sublink-current:
    backgroundColor: "{colors.band}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    height: "28px"
  running-head:
    textColor: "{colors.ink-2}"
    typography: "{typography.caption}"
  params-head:
    backgroundColor: "{colors.band}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    padding: "6px 10px"
  param-row:
    typography: "{typography.data}"
    padding: "14px 10px"
  param-note:
    textColor: "{colors.ink-2}"
    typography: "{typography.note}"
  tag-modified:
    textColor: "{colors.link}"
    typography: "{typography.tag}"
    rounded: "{rounded.control}"
    padding: "0 5px"
  tag-unread:
    textColor: "{colors.link}"
    typography: "{typography.tag}"
    rounded: "{rounded.control}"
    padding: "0 5px"
  savebar:
    backgroundColor: "{colors.sheet}"
    padding: "12px 10px"
  table-head:
    backgroundColor: "{colors.band}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    padding: "7px 10px"
  table-cell:
    typography: "{typography.data}"
    padding: "{spacing.cell}"
  table-row-hover:
    backgroundColor: "{colors.hover}"
  note-tag-info:
    backgroundColor: "{colors.ink-2}"
    textColor: "{colors.on-fill}"
    padding: "6px 10px"
  note-tag-caution:
    backgroundColor: "{colors.caution}"
    textColor: "{colors.on-fill}"
    padding: "6px 10px"
  note-tag-danger:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.on-fill}"
    padding: "6px 10px"
  note-tag-done:
    backgroundColor: "{colors.rule-ink}"
    textColor: "{colors.on-fill}"
    padding: "6px 10px"
  note-body:
    typography: "{typography.data}"
    padding: "10px 14px"
  confirm-panel:
    backgroundColor: "{colors.field}"
    padding: "12px 14px"
  popover:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.square}"
    width: "460px"
  popover-head:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.on-fill}"
    padding: "10px 14px"
  empty-state:
    textColor: "{colors.ink-2}"
    padding: "18px 14px"
  segmented-filter:
    textColor: "{colors.ink-2}"
    rounded: "{rounded.control}"
    padding: "0 11px"
    height: "32px"
  segmented-filter-hover:
    backgroundColor: "{colors.hover}"
    textColor: "{colors.ink}"
  segmented-filter-current:
    backgroundColor: "{colors.band}"
    textColor: "{colors.ink}"
  session-tabs:
    textColor: "{colors.ink-2}"
    rounded: "{rounded.control}"
    padding: "4px 8px"
    height: "38px"
  session-tabs-current:
    backgroundColor: "{colors.band}"
    textColor: "{colors.ink}"
  lot-photo:
    backgroundColor: "{colors.field}"
    rounded: "{rounded.square}"
    width: "80px"
    height: "60px"
  lot-where:
    textColor: "{colors.ink-3}"
    typography: "{typography.caption}"
  price-facts:
    textColor: "{colors.ink-2}"
    typography: "{typography.caption}"
  reading-column:
    textColor: "{colors.ink}"
    typography: "{typography.reading}"
    width: "72ch"
  answer-lead:
    textColor: "{colors.ink}"
    typography: "{typography.lead}"
  citation:
    textColor: "{colors.link}"
  citation-hover:
    textColor: "{colors.link-2}"
  turn:
    padding: "16px 0"
  turn-label:
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    width: "104px"
  notebook-side:
    width: "300px"
  side-facts-row:
    padding: "7px 0"
  evidence-snippet:
    textColor: "{colors.ink-2}"
    typography: "{typography.note}"
  evidence-group-row:
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    padding: "14px 10px 4px"
  evidence-row-excluded:
    textColor: "{colors.ink-3}"
  run-reason:
    textColor: "{colors.danger}"
    typography: "{typography.data}"
  plan-latest:
    padding: "12px 10px 4px"
  skeleton-line:
    backgroundColor: "{colors.band}"
    height: "10px"
  hint:
    textColor: "{colors.ink-3}"
  pager-field:
    width: "80px"
    height: "28px"
  rail-tally:
    textColor: "{colors.ink-3}"
  rail-tally-current:
    textColor: "{colors.ink-2}"
  meta-link:
    textColor: "{colors.ink-2}"
  meta-link-hover:
    textColor: "{colors.ink}"
  head-search:
    backgroundColor: "{colors.field}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "6px 10px"
    width: "260px"
    height: "32px"
  desk-state:
    textColor: "{colors.ink-2}"
  read-toggle:
    rounded: "{rounded.control}"
    size: "22px"
  read-toggle-hover:
    backgroundColor: "{colors.band}"
  unread-mark:
    backgroundColor: "{colors.link}"
    size: "8px"
  unread-mark-hover:
    backgroundColor: "{colors.link-2}"
  score:
    textColor: "{colors.ink-2}"
  score-high:
    textColor: "{colors.ink}"
  score-low:
    textColor: "{colors.ink-3}"
  story-summary:
    textColor: "{colors.ink}"
  story-summary-hover:
    textColor: "{colors.link-2}"
  story-summary-read:
    textColor: "{colors.ink-2}"
  deep-read-pending:
    textColor: "{colors.ink-2}"
  comment-disclosure:
    textColor: "{colors.link}"
    typography: "{typography.caption}"
  price-now:
    textColor: "{colors.ink}"
  price-was:
    textColor: "{colors.ink-3}"
  price-change:
    textColor: "{colors.link}"
  table-row-selected:
    backgroundColor: "{colors.band}"
  keycap:
    backgroundColor: "{colors.field}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.control}"
    padding: "0 4px"
---

# Design System: Beehive Datasheet

> **Scope.** This file records one datasheet system with three shells. The admin is every page under `/admin/` plus the admin login. The Owner's workspace is `/research` (the session list, a session's four pages, the new-research form, the run-budget preview and the source form) and `/watchlist`. The reading shell holds the public reading pages that have moved into the datasheet. So far that is only the home page (`/`, 精选), with its channel desk and its ranked list. All three are styled only by `src/beehive/web/static/admin.css`, all three render through the shared document `templates/datasheet_base.html` (extended by `admin_base.html`, `workspace_base.html` and `reading_base.html`, whose body class is `page-reading`), and all three share behaviour in `src/beehive/web/static/beehive.js`. The other public reading pages (channel pages, archive, search and deep-read briefs) still render through `base.html` with `src/beehive/web/static/beehive.css`, a different visual world that is not described here until those pages move into the reading shell. Don't carry tokens from `beehive.css` into the datasheet, or from this file into pages still on `beehive.css`.

## Overview

**Creative North Star: "The Owner's Datasheet"**

The admin and the Owner's workspace read like a technical datasheet for one person's pipeline, set dark. A graphite sheet lies on a darker desk. A contents rail on the left numbers the chapters. A running head gives the location and the Auckland clock that every schedule is read against, and each section carries a number that matches the rail. Every setting is a parameter row with its value and a note. Every list is a banded table under a heavy ink rule. The density is working density, with 15px body text, 32px controls and hairlines between rows. Nothing is padded out for show.

The workspace is the same sheet with its own rail of two chapters, 1 研究 and 2 关注列表. A research session reads like a notebook. The conclusion is the first turn of a transcript, follow-up questions continue below it, and on a desktop window wider than 1180px a sticky side column shows the source behind a citation. Long AI text sits at a 72ch reading measure, and citations are small blue numbers. The watch list is a banded table of lots by deadline. Each lot's reminder state sits under its deadline, and failed reminders are boxed above the list.

The reading shell is the same sheet again, for the Owner and for anonymous readers. Its rail opens with 1 精选, gives every Channel a chapter of its own and ends with the archive. The home page is a channel desk. Each Channel gets a numbered section (1.1 on) whose heading says the Channel's state, with its best few rows beneath as stories, live listings or open lots. A story leads with its AI summary, and a score is a bare tabular numeral. For the Owner, a small blue square marks a story unread and is also the switch that marks it read, in place. Nothing on a reading page is cut short.

Colour is a signal. Ink on graphite carries everything that is fine. Amber appears only where something waits on the Owner, red only for failure or a destructive action, and blue for links, focus, unsaved edits, unread results and stories, and a listing's latest change. State is always a shape plus a word. Controls have 2px corners and everything else is square. Depth comes from tone, and the one drop shadow belongs to the removal popover.

The system refuses the card-grid SaaS console and the chat window, so there are no tiles, no dashboards of boxes, no chat bubbles and no decorative colour. The Owner visits admin rarely, so every page says what it controls and what state it is in. Unsaved rows are marked and counted, and destructive rows open in place into a confirmation that says what goes and whether it can be undone.

**Key Characteristics:**
- Graphite sheet on a darker desk, flat and tonal.
- One document, three shells: the admin (管理), the Owner's workspace (工作区) and the reading pages (阅读), each with its own numbered rail and footer.
- Every form is a parameter table (参数 | 设置 | 说明) and every list is a banded table under a heavy ink rule.
- A research conversation is a transcript with a label column, conclusion first, beside a sticky side column for the cited source.
- The home page is a channel desk: a numbered section per Channel, its state in the heading and its best rows beneath.
- A story leads with its AI summary. A score is a bare tabular numeral, and unread is a small blue square that is also the read toggle.
- Long AI text at a 72ch measure, with small blue citation numbers.
- Boxed notes tagged 说明 / 注意 / 警告 / 完成.
- Colour only where the Owner is needed. Amber for attention, red for failure, blue for links, focus, unread results and stories, and a listing's latest change.
- State is a square mark plus a word.
- Archivo throughout, condensed only in column heads, labels and numbers, with tabular figures wherever numbers change.
- 2px corners on controls, square containers.
- Nothing overflows at 1440, 1024 or 390px, units never split, and nothing on a reading page is cut short.

## Colors

A restrained graphite palette with three functional signals, and each signal has one meaning.

### Primary
- **Datasheet Blue** (#7DB6FF): the only interactive colour. It marks links, the focus outline and field ring, the 已修改 tag on changed rows, the unsaved count in the save bar, the research unread count (in the admin and reading footers, on the workspace rail and as the unread tag on a session), citation numbers and the outline of the one in view, and in-progress state marks. On the reading pages it also marks an unread story (the 8px square that is also its read toggle), the change line under a listing's price (降价, 重新有货), the best-comment disclosure, and a lot the Owner watches (已关注, as the in-progress mark). It is never a fill, except for that one 8px unread square.
- **Pale Datasheet Blue** (#A9CEFF): link hover, and hover on names in tables, lot titles, citation numbers and cited titles. On the reading pages it is also the hover on a story's summary, the channel column, the unread square and the best-comment disclosure.

### Secondary
- **Attention Amber** (#F0B44C): something waits on the Owner. It marks paused sources, warnings, caution notes (注意) and the attention counts on the contents rail, such as failed reminders on 2 关注列表. On the watch list it also marks the deadline of a lot that closes within the hour. A healthy page doesn't show it.

### Tertiary
- **Fault Red** (#FF6E61): failure and destruction. It marks failed fetches, failed runs and reminders, a run's failure reason, error lines, danger notes (警告), danger buttons and links, the removal popover, invalid fields and field errors.
- **Danger Stroke** (#B04A42): the resting boundary of danger buttons. It is darker than Fault Red so a danger button stays quiet until hovered, and it holds at least 3.15:1 on every admin surface.

### Neutral
- **Desk Black** (#0A0C0E): the desk under the sheet, and the page background behind the shell.
- **Graphite Sheet** (#111417): the sheet, the contents rail, the save bar and the popover body.
- **Header Band** (#191D21): table and parameter head rows, the current rail entry and rail sub-page, the current segment of a segmented filter and the current session tab, checked selection rows, prefix cells, highlighted rows and skeleton lines. On the reading pages it also sits behind a hovered read toggle and under a story row selected from the keyboard.
- **Hover Wash** (#161A1E): hover on table rows, rail entries, filter segments and buttons.
- **Recessed Field** (#0C0E10): input wells, confirmation panels, raw errors, the plain-text preview, the well behind a lot photo, and keycaps. It sits darker than the sheet, so fields read as sunk.
- **Hairline** (#262B30): row and turn dividers, side-facts rows, the line between a deadline and its reminder or watch state, the sheet and rail edges, facts dividers, raw-error boxes, lot photo edges, and the faded border of disabled buttons.
- **Rule Stroke** (#3A4046): structural lines only. It draws the line under table heads and the parameter head row, the save bar's top edge, the confirmation panel's top edge, the plain-text preview border, the dividers inside a segmented filter and the session tabs, the group bar on grouped evidence rows, the line beside an older plan's source list, the underline of a cited title, of the home page's count links and of a running deep read, the outline of a keycap, dashed empty-state boxes and the scrollbar thumb. It is never a control boundary.
- **Control Stroke** (#666E76): the resting boundary of every enabled control, meaning buttons, fields (inputs, selects, textareas and one-line fields), prefix cells, weekday chips and the empty square of a read story's toggle. It holds at least 3.27:1 on every admin surface.
- **Ink Rule** (#C9CFD6): the heavy rules over tables, parameter tables, the transcript, side-column boxes and the newest plan, the running-head rule, the filled "ok" mark and the 完成 note.
- **Sheet Ink** (#E7EAED): primary text, and the fill of the primary button. On the reading pages it sets a story's summary, a listing's current price and scores of 90 and above.
- **Secondary Ink** (#A7AFB7): notes, meta lines, column heads, transcript labels, side-box heads, snippets, price-fact lines, closed and archived rows, and the 说明 note. On the reading pages it also sets the state items in a desk section's heading, the home page's count links, scores from 60 to 89, a read story's summary, the original title under a summary, a story's byline and age on phones, an opened best comment, the rail tally on the current chapter and keycap text.
- **Faint Ink** (#808891): chapter and section numbers, placeholders, stacked-row labels, empty values, disabled text, hints, times under a transcript label, the auction line under a lot, list markers in AI text, excluded evidence, and the border of a hovered field. On the reading pages it also sets the rail's quiet tally, scores under 60 and unranked dashes, a listing's vendor line and former price, and the keyboard hint. It holds at least 4.7:1 on every admin surface.
- **Fill Ink** (#0A0C0E): text on solid fills (primary button, note tags, danger hover, popover head). It has the same value as the desk but is its own role.

A few component states use fixed values that are deliberately not tokens: primary hover #FFFFFF, danger link hover #FF9A90, text selection #28507F under white text, the popover backdrop `rgba(5,6,7,.6)`, link underlines at 40% of their link colour, a closed lot's photo in grayscale at 65% opacity, and a read toggle at 60% opacity while its request runs. Reuse them only in those states.

### Named Rules
**The Owner-Needed Rule.** Colour appears only where something needs the Owner. Amber means attention, red means failure or a destructive action, and blue means a link, focus, an unsaved edit, an unread result or story, a listing's latest change or work in progress. Everything else is ink on graphite, so a healthy page shows no amber and no red outside its danger rows.

**The Solid Ink Rule.** The primary action is a solid block of ink with dark text. Blue never fills a control, and amber never fills a button.

**The Shape-Plus-Word Rule.** Every state is a 9px square plus its word. Settled states are filled (ok in ink rule, error in red). Waiting states are hollow (paused and warning in amber, never fetched, scheduled or excluded in faint ink, in progress or watched in blue). Colour never carries a state alone. The 8px unread square on a story row is the one mark without a word beside it, because it is also the read toggle. Its word is its tooltip and accessible name (标为已读 or 标为未读), and the story's own weight repeats the state.

**The Visible Control Rule.** Every outlined control the stylesheet draws (buttons, fields, prefix cells, weekday chips, segmented filters, session tabs, and the empty square of a read story's toggle) rests on the control stroke, or the danger stroke on danger buttons, and both hold at least 3:1 on every admin surface. The rule stroke draws structure only and never outlines a control. States only strengthen the boundary (secondary ink on a hovered button, a checked chip or a hovered read square, faint ink on a hovered field, blue on focus, red when invalid). Only disabled buttons fade to the hairline.

## Typography

**Display Font:** Archivo, self-hosted variable (weight 100–900, width 62–125%), falling back to the platform CJK sans (PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Microsoft YaHei, then the Japanese and Korean equivalents) and system-ui
**Body Font:** the same stack
**Label/Mono Font:** the ui-monospace stack (SF Mono, Cascadia Mono, DejaVu Sans Mono, Menlo, Consolas), for raw errors and the plain-text email preview only

**Character:** One grotesque in several widths and weights, set like a spec sheet. Bold full-width heads name things, and condensed semibold column heads and numbers pack the data.

Archivo ships as two woff2 subsets (latin and latin-ext) in `static/fonts/`, with `font-display: swap`, and the latin file is preloaded by the shared shell document. The CSP (`default-src 'self'`) rules out third-party font hosts. `font-synthesis: none` stops the browser from faking bold or italic in fallback faces.

Archivo covers Latin only. In the zh-CN interface, Chinese text in headings, labels and column heads renders in the platform CJK sans, which has no width axis. The condensed heads therefore show only on Latin runs such as channel names, numbers and times. The direction's "Archivo with condensed headers" lands fully only in Latin locales.

### Hierarchy
- **Headline** (700, 1.5rem/24px, 1.25): the page h1, with a 14px meta line of counts under it in secondary ink. It wraps balanced and breaks anywhere rather than overflow. A research question set as the h1 also keeps CJK words whole. On the home page's desk the counts are quiet links into the ranked list.
- **Title** (700, 1rem/16px, 1.35): numbered section heads. The number sits in faint ink at 600 in a 2.4em column, and section tools (counts, small buttons) align right on the same line. On the channel desk the tools carry the Channel's state in 13px secondary ink, its fetch time and 打开频道.
- **Subhead** (700, 0.9375rem/15px, 1.35): an unnumbered head inside a numbered section, such as AI 计划 under 1.3, with 30px above and 6px below. The heads inside AI text (主要发现, 来源一致之处) use the same size and weight with 22px above and 8px below.
- **Lead** (500, 1.0625rem/17px, 1.75): the bottom line of a research conclusion, the first answer in the notebook. Facts-strip values share the size at 600 with tabular figures.
- **Body** (400, 0.9375rem/15px, 1.55): base text. Field values use the same size at 1.4. Ledes stop at 72ch.
- **Reading** (400, 0.9375rem/15px, 1.75): long AI text (conclusions, replies, plan summaries) at the 72ch measure. Paragraphs sit 10px apart and list items 9px apart, indented 1.35em. Ordered lists take decimal markers and unordered lists square ones, in faint ink with tabular figures. The Owner's own question in a turn is ink at 600 and 1.7.
- **Data** (400, 0.875rem/14px, 1.45): table cells, note bodies and meta lines. Parameter labels use it at 600, and so does a story's summary, which drops to 500 once read.
- **Note** (400, 0.8125rem/13px, 1.6): the note column of parameter rows, secondary lines in forms and popovers, and confirmation labels. Key values inside a note go bold in ink.
- **Button** (600, 0.8125rem/13px, 1.2): button labels. Small buttons drop to 0.78rem.
- **Label** (600, 0.78rem/12.5px, 1.3, 85% width): column heads, the parameter head row, fact labels, side-column box heads and evidence group rows. The transcript's label column uses it at 1.4.
- **Caption** (400, 0.78rem/12.5px): secondary lines under table values, the running head, footnote strips, stacked-row labels, error and caution lines, the auction line under a lot and price-fact lines. On the reading pages it also sets the original title under a story's summary, the change line under a price (at 600), the best-comment disclosure, and a story's byline and age on phones.
- **Tag** (700, 0.7rem/11.2px, 15px line): the outlined tags beside a label, 已修改 on a changed row and the unread tag on a session.
- **Numeric** (400, 0.875rem/14px, tabular, 88% width): numeric table columns, right-aligned. Scores use it at 600, at 700 for 90 and above and at 400 under 60. Prices and discounts keep full width with tabular figures.
- **Mono** (400, 0.8125rem/13px, 1.75): the plain-text email preview. Raw error text uses 0.75rem at 1.55.
- Rail counts sit at 0.72rem and 700. The rail's quiet tally sits at 0.78rem and 400, and keycaps at 0.72rem and 600. Citation numbers are 0.75rem at 600 with tabular figures, and 0.8125rem where one leads the citation card's title. Footnote references use 0.68rem.

### Named Rules
**The Condensed Head Rule.** Width marks data. Column heads, fact labels, transcript labels, side-column box heads and group rows set at 85% width, and numeric columns and scores at 88%. Headings and body text stay at full width.

**The Tabular Figures Rule.** Every number that changes (counts, the clock, chapter and section numbers, numeric columns, facts, citation numbers, filter counts, list markers, scores, prices and the rail tally) uses tabular figures, so columns and counters never jitter.

**The Reading Measure Rule.** Long AI text sits at 72ch with 1.75 line height, and a conclusion's bottom line leads at 17px. Ledes stop at the same 72ch, and so does an opened best comment. Tables, forms and notes keep working density.

## Layout

All three shells use one document (`datasheet_base.html`) with two columns, a 216px contents rail and a fluid column that holds one sheet. The rail is sticky, runs the full viewport height and scrolls on its own. Its footer sits at the bottom above a hairline. The admin's footer holds back to the site, watch list, research and sign out, and the workspace's holds 返回阅读, 管理后台 and 退出登录. The reading shell's holds 关注列表, 研究, 管理 and 退出登录 for the Owner, and only 登录 for a reader. The sheet sits on the desk with a 16px margin, is at most 1320px wide, and has a 16px top, 28px side and 36px bottom inset inside a 1px hairline edge.

Every sheet opens with the running head. The location sits on the left (chapter number and name, then crumbs separated by ›) and the Auckland clock on the right, with a 2px ink rule under both and 20px of space below. The page head comes next, with the h1 and its meta line on the left and actions on the right, wrapping under the title when space runs out. Sections follow as numbered heads (N.M, matching the rail's sub-entries), with 32px above and 10px below. A chapter's attention notes come before the list they concern.

Parameter tables use three columns: label at 140–190px, control fluid, and note at 200–290px. The gutter is 24px, the side inset 10px, and each row has 14px of padding above and below. Controls in a row sit 8px apart and wrap when they run out of room.

A research session's conclusion page is a notebook. The reading column is fluid, and a 300px side column sits 36px to its right, sticky 16px from the top, with its boxes 18px apart. Inside the reading column the transcript runs as turns, each with a 104px label column and the words 20px beside it, and 16px above and below (20px above the conclusion). A session's four parts (1.1 conclusion and follow-ups, 1.2 evidence, 1.3 sources and plan, 1.4 runs) are separate pages under the same head, listed in the rail as sub-pages, or as tabs under the head at the narrow rule. The watch list puts its attention notes (2.1) above the list (2.2). Irreversible clean-ups (delete the session, clear closed watches) sit at the foot of the page, 36px below the content.

The home page's head holds the h1 精选 and its counts on the left, and search on the right with 全部标记为已读 beside it for the Owner. The channel desk follows as sections 1.1 on, one per Channel in the rail's order, each a heading over a short banded table of 4 stories, 5 listings or 5 lots. The sections are numbered under chapter 1 but not listed in the rail, since the rail already gives every Channel its own chapter. The ranked list replaces the desk under the same head, with its segmented filter 18px below the head, then the table and the pager. A page-level empty state sits 24px below the head, and the keyboard hint closes the page 28px below the last table.

Breakpoints are container queries on the `adm` container (the body), not media queries.
- **Mid (1180px and below):** the desk margin drops to 8px and the sheet inset to 14px 18px 32px. Table cells take 8px at the sides, no-wrap cells relax, and operation columns wrap. Parameter columns become 120–160px, fluid and 180–240px, with an 18px gutter. The notebook drops to one column. The side column follows the transcript as a static block 28px below it, and its citation box hides, because a citation opens its source in a new tab at this width. On the reading pages a story's summary column drops its minimum width from 20em to 14em, and the channel column from 7em to 5em.
- **Narrow (880px and below):** the rail becomes a wrapping top bar, with chapters in a row, sub-sections hidden and footer links at the right. The sheet runs edge to edge without a border at 14px 14px 28px. Parameter rows stack label, control and note, and the head row hides. Banded tables become labelled stacked rows, and notes move their tag above the body. Transcript turns stack, and the label column becomes a wrapping line above the words. A lot photo grows to 88×66px, the lot cell leaves 28px on the right for the row checkbox, and the filter form takes a full line under the segments. A research session lists its four pages as a 2×2 tab grid under the page head, and the watch list's select-all moves into the section tools, because the table head is hidden. The reading rail keeps its chapters on one line that scrolls sideways inside the bar. The home page's search takes a full line, its field flexing beside the button, and the keyboard hint hides. Story tables become headline rows rather than labelled rows (see Story Row), while the desk's listing and lot tables stack as labelled rows.

### Named Rules
**The No-Overflow Rule.** No text leaves its box at 1440, 1024 or 390px. Free-length values wrap (one-line fields, break-anywhere cells), and bounded values ellipsize. No layout depends on sideways scrolling. The table wrapper's scroll is a guard, not a layout. The one sideways scroll drawn on purpose is the reading rail's chapter line at the narrow rule, which scrolls inside its bar and never widens the page.

**The Uncut Reading Rule.** Nothing on a reading page is cut short. Every cell wraps in full, with no ellipsis and no line clamp. Where the workspace clamps a line, the reading pages undo it under `.page-reading`, as they do for a lot's auction line (`.page-reading .lot-where`). Units still hold whole.

**The Whole Unit Rule.** In a table a unit never splits, whether it is a count with its word, a day with its time or an email address. Lines break between units, and URLs break only before a slash. At the narrow rule these holds relax so stacked rows can wrap.

**The Stacked Row Rule.** At 880px and below, every multi-column list becomes labelled rows. Each value is led by its column name in a 76px faint-ink label column, the row checkbox moves to the top right, and operations drop to the bottom. Story tables are the exception. A story reads as a headline row, summary first, with its byline, age and deep read beneath, because column labels would outweigh the story. Its toggle and score hang in a lead on the left, and everything else wraps inside the indent, so no wrapped line starts left of the summary.

**The Side Column Rule.** On a desktop window wider than 1180px, a citation shows its source in the side column beside the text, so checking it never leaves the page. At 1180px and narrower, the side column follows the transcript, its citation box hides, and a citation opens the source in a new tab.

## Elevation & Depth

The system is flat. Depth is three steps of graphite. The desk sits lowest, the sheet lies on it behind a hairline edge, and header bands sit one step lighter than the sheet. Fields sit one step darker than the sheet, so they read as wells. Hover is a tone change, never a lift, and so is a story row selected from the keyboard, which takes the band. The sticky save bar and the notebook's sticky side column hold their place without a shadow. The only drop shadow belongs to the source-removal popover, which opens in the top layer over a 60% near-black backdrop.

### Shadow Vocabulary
- **Popover lift** (`box-shadow: 0 18px 40px -8px rgba(0,0,0,.65)`): the removal popover only.
- **Field focus ring** (`box-shadow: 0 0 0 1px var(--link)`): doubles the blue border of a focused field. It is a ring, not elevation.
- **Hollow mark** (`box-shadow: inset 0 0 0 1.5px <tone>`): draws the hollow state squares, and the empty square a read story keeps in its read toggle, in the control stroke.
- **Group bar** (`box-shadow: inset 2px 0 0 var(--rule-2)`): the 2px rule-stroke bar on the first cell of evidence rows that belong to a duplicate group. It is a rule, not elevation, and it never takes a tone.

### Named Rules
**The Flat Sheet Rule.** Depth is tone. The desk sits below the sheet, bands sit above it and fields sink into it. Nothing floats except the removal popover.

## Shapes

Everything is a rectangle, ruled like a datasheet. Controls and counters take a 2px corner. That covers buttons, fields, rail entries, weekday chips, prefix cells, segmented filters, session tabs, the read toggle, rail counts, keycaps, and the 已修改 and unread tags. The rail's quiet tally is a bare numeral with no box. The sheet, tables, notes, the popover, facts strips, side-column boxes, lot photos, empty states, skeleton lines and code blocks are square. Lines do the structural work. A 2px ink rule sits under the running head. A 1.5px ink rule opens every table, parameter table, selection table, facts strip, transcript and side-column box, and the newest plan. Notes and the popover take 1.5px borders in their tone, and 1px lines in the hairline and rule-stroke tones do everything else. Two 2px rule-stroke bars group things, one on the first cell of grouped evidence rows and one beside an older plan's source list. Controls are outlined apart from all of this, in 1px of the control stroke. Empty states use a dashed rule-stroke border. State marks are 9px squares, the unread square on a story row is 8px, and the citation number in view takes a 1px blue outline at a 1px offset. The one non-rectangular form is the product's hexagon mark from `favicon.svg`, drawn as an 18px stroked outline beside the product name.

### Named Rules
**The Two Corner Rule.** Controls, counters, tags and keycaps get 2px, and containers get none.

**The Heavy Ink Rule.** A 1.5px ink rule opens every table, parameter table, transcript and side-column box. Hairlines do everything else.

## Components

### Buttons
Quiet rectangles, labelled in words.
- **Shape:** 2px corners, 32px tall, 12px sides, never wrapping.
- **Primary:** a solid block of sheet ink with fill-ink text, brightening to pure white on hover and dimming to the ink rule tone while pressed, by mouse or keyboard. It is reserved for the page's main commitment, such as create, save, sign in, send a test, preview or start a research run, refresh a session, or send a follow-up.
- **Hover / Focus:** colour-only transitions at 150ms ease-out. Focus is the global 2px blue outline at a 2px offset.
- **Secondary (default):** transparent, with a 1px control-stroke border and ink text. On hover the border moves to secondary ink and the hover wash fills it. Active uses the band.
- **Danger:** transparent, with red text and a danger-stroke border. On hover it fills solid red with fill-ink text. It appears only in danger rows and the removal popover.
- **Small and block:** small buttons are 26px tall with 9px sides, used for note actions and section tools, such as 打开频道 on the channel desk. The block button is full width at 38px, used for sign-in.
- **Disabled and loading:** disabled buttons take a hairline border, faint ink and a not-allowed cursor. Loading shows a wait cursor at 70% opacity.

### Links
- Links are datasheet blue with a 40% blue underline at a 3px offset. On hover they turn pale blue with a full underline. Danger links are red.
- Row operations in tables are links, even when they post a form, so five operations (test, edit, pause, copy, remove) stay one quiet line. The workspace does the same for exclude and restore, stop watching and retry a reminder, and sets its older-plan disclosure as a link.
- **Title links:** a story's summary is an ink link, like a name in a table. It turns pale blue with an underline on hover and opens the story in a new tab. The ranked list's channel column links to the Channel the same way.
- **Quiet links:** the home page's counts are links in secondary ink with a rule-stroke underline at a 3px offset, the device a cited title uses. On hover they turn ink with a full underline.
- **Deep read:** a plain datasheet link in a story row's last cell. 深度阅读 starts one (a form button set as a link) and 打开简报 opens the brief. A running one reads 生成中… in secondary ink with a rule-stroke underline, and a failed one offers a red 重试.

### State Marks
- **Style:** a 9px square and a word, 7px apart, never wrapping.
- **In a sentence:** `.st-line` puts the same square before a whole sentence in a note column, for example a refresh in progress or a failure reason. The square stays on the first line and the words wrap, so a long status never overflows.
- **States:** ok is a filled ink-rule square with normal text. Paused and warning are a hollow amber square with amber semibold text. Error is a filled red square with red semibold text. Never fetched is a hollow faint square with faint text. In progress is a hollow blue square with blue text.
- **In the workspace:** runs mark completed as ok, failed as error, cancelled as never and queued or running as in progress. Reminders mark 已发送 as ok, 发送失败 as error and 正在发送 as in progress, and 已安排 and 没有提醒 take the hollow faint square. Excluded evidence carries a hollow faint 已排除. A lot that closes within the hour marks its deadline as a warning.
- **On the reading pages:** a lot the Owner watches carries 已关注 under its deadline, below a hairline, as the hollow blue in-progress mark, because the watch is running.
- The channel and source lists carry a legend of the marks in the footnote strip below them.

### Inputs / Fields
- **Style:** 34px minimum height, 6px 10px padding, a 1px control-stroke border, 2px corners, the recessed field fill, and 15px text at 1.4. Placeholders use faint ink. On hover the border moves to faint ink. Selects, textareas and one-line fields share the same border.
- **Focus:** the border turns blue and a 1px blue ring doubles it. There is no outline.
- **Error:** an invalid field takes a red border, and the message sits in the row's note column in red 13px semibold.
- **Bounded and free-length values:** names, numbers, times and selects are inputs, and input text ellipsizes. Values of any length (URLs, queries, email addresses, subject templates, source names) use the one-line field (`ui.line_field`). It is a one-row textarea that wraps and grows with its content (`field-sizing: content`, with a script fallback). Enter submits the form and pasted line breaks are dropped. Literal values turn off spellcheck, autocapitalize and autocorrect.
- **Multi-line:** the channel profile is a textarea at least 132px tall at 1.65 line height, growing with its content.
- **Sizes and variants:** number fields are 96px, time fields 132px, and mid selects up to 260px. Growing fields flex from 220px. Selects draw a 12px chevron in secondary ink. A prefix cell (such as `r/`) joins its field in band fill under the same control-stroke border. Checkboxes and radios are 16px native controls tinted ink. Weekday chips are 30px labels with a control-stroke border, and a checked chip takes a secondary-ink border and band fill.
- **Workspace sizes:** filter fields drop to 32px to sit level with the segments and buttons. The composer's textarea starts at 92px. The pager's page field is 80px wide and 28px tall.
- **Reading sizes:** the home page's search field is 260px wide at 32px, level with its 搜索 button, and flexes to fill its line at the narrow rule.

### Navigation
- **Contents rail:** a brand line (the hexagon mark, the product name at 700, and the shell's name in secondary ink at 500, 管理, 工作区 or 阅读, linking to that shell's home), then numbered chapters as 32px rows with a 22px column of faint tabular numbers. Entries sit in secondary ink, take the hover wash and ink on hover, and the current chapter takes the band, ink and 600. The current chapter lists its N.M sections as 28px sub-rows at 13px. When those sections are separate pages, as a research session's four parts are, the open one takes the band and ink at regular weight and the chapter stays marked.
- **Attention counts:** a chapter that needs the Owner shows an 18px amber-outlined count with a full sentence for screen readers. Each problem is counted once, in the chapter where it is fixed.
- **Unread count:** in the workspace, 1 研究 counts finished results the Owner hasn't opened, in the same count outlined in blue, while 2 关注列表 counts failed reminders in amber. A chapter shows one count at most, and attention wins.
- **Reading rail:** 1 精选 comes first, then one chapter per Channel under its own name, numbered from 2, and the archive (归档) last. Long Channel names wrap inside the rail. The desk's sections get no sub-rows, since each Channel already has its chapter.
- **Quiet tally:** 1 精选 carries the Owner's unread featured stories as a bare number in 12.5px regular faint ink with tabular figures, secondary ink on the current chapter, and a full sentence in its tooltip and for screen readers (精选中未读 228 条). It shows 0 once everything is read, and nothing for an anonymous reader. A chapter's count slot holds one thing at most, in this order: attention, then an unread count, then the tally.
- **Rail footer:** the admin's holds back to the site, watch list, research (with a blue-outlined unread count) and sign out. The workspace's holds 返回阅读, 管理后台 and 退出登录. The reading shell's holds 关注列表, 研究 (with the same blue unread count), 管理 and 退出登录 for the Owner, and only 登录 for a reader. All are 13px.
- **Running head:** 12.5px secondary ink. Crumbs are links that underline on hover, and the clock uses tabular figures. A research question is cut to 40 characters in the crumbs. On the ranked list the crumb names the view (全部, 未读, 已读 or N 分以上).
- **Mobile:** at the narrow rule the rail becomes a wrapping top bar, with chapters in one row and footer links at the right. The reading rail's chapters stay on one line that scrolls sideways inside the bar, with 4px below for the scrollbar, because a chapter per Channel would otherwise stack several rows above the page.
- **Session tabs:** at the narrow rule, where the rail hides its sub-rows, a research session lists its four pages 14px under the page head as a 2×2 grid of 38px cells, with the rail's sub-row labels. The grid takes a control-stroke outline with 2px corners and rule-stroke dividers. Labels are 13px semibold in secondary ink, the current page takes the band and ink, and focus draws inside the cell. Wider screens hide the grid.

### Segmented Filter
One outlined control that switches a list's view, such as the watch list's 进行中 / 已结束 / 全部.
- **Style:** a control-stroke outline with 2px corners holding 32px link segments with 11px sides at 13px semibold in secondary ink. Rule-stroke lines divide the segments, and each segment ends in its count in faint regular tabular figures.
- **States:** hover takes the hover wash and ink. The current view takes the band and ink. Focus draws the blue outline 2px inside the segment.
- **Filter row:** the segments come first, then a search field and a select at 32px, a secondary apply button, and a clear link once a filter is set. The row wraps and sits 12px above its table.
- **Ranked list:** 频道总览 comes first, without a count, and leads back to the desk. Then come 全部, and for the Owner 未读 and 已读, each with its count, and last 90 分以上 with its count, or the score filter in use (N 分以上) as the current segment. The segments stand alone, 18px under the head, with no search form beside them.

### Parameter Table
The signature form, 参数 | 设置 | 说明 (Parameter | Setting | Notes).
- A 1.5px ink rule sits on top, then an optional band head row in label type over a rule-stroke line. The head row is hidden from assistive tech because every row labels itself.
- Each row has a 14px semibold label (with an optional 可选 line in faint caption), a control column with 10px between stacked controls, and a 13px secondary-ink note. Fixed values, such as a channel's type, sit in the control column as plain text. A hairline closes each row.
- A changed row shows the 已修改 tag after its label, a blue outlined 11px bold tag with 2px corners. Radio and checkbox groups carry the tag in their section head instead.
- The sticky save bar closes the form on the sheet tone above a rule-stroke line. It holds the primary save, then 放弃修改 once something has changed, an optional cancel, and the blue count "N 项修改还没保存". After a rejected save the bar stays unsaved and says so, and discard reloads the saved page.
- Single-setting forms (language, model, featured window, default recipient) put their own secondary button in the row instead of a save bar.
- Workspace forms (new research, source form) use the table without change tracking. There are no 已修改 tags, and the bar holds the primary action, a cancel and a faint hint. The new-research form puts its sources in a selection table between two parameter tables.

### Banded Table
The signature list.
- A 1.5px ink rule sits over a band head in label type that never wraps, with a rule-stroke line under the head. The body is 14px at 1.45 with 10px cells, a hairline under every row and the hover wash on hover. A highlighted row takes the band.
- **Columns:** the name column is semibold, at least 12em wide, wraps balanced, and its links turn pale blue on hover. Numeric columns right-align in tabular figures at 88% width. Unit columns never wrap, and word columns keep CJK words whole. Wide columns (details, sources) start at 16em and break anywhere. The operations column is as narrow as its links, right-aligned, with 12px between links.
- **Cell holds:** cells set `text-wrap: pretty`, which resets wrapping and outranks a single-class `white-space: nowrap`. Scope any cell-level hold under the table, as the build's `.tbl .c-nw` and `.tbl .c-num` rules do, or the unit breaks anyway.
- **Lines under a value:** a caption line in secondary ink, a failure line in red, or a caution line in amber. A raw error folds into a disclosure, set in 12px mono on the field fill.
- **Footnote strip:** below the table, in caption type. It holds the mark legend, notes, and numbered footnotes referenced by a faint superscript "(1)".
- **Selection tables:** source type, channel kind, group members and a new research's sources use the same ink rule. Each row holds a radio or checkbox, a semibold name, a description and a side note. The checked row takes the band, keyboard focus draws a blue outline inside the row, and unavailable rows go faint.
- **Unread and archived rows:** a session with a finished result the Owner hasn't opened carries the blue unread tag after its question. Archived sessions and closed lots set their rows in secondary ink.
- **Times:** every short time uses one label in Auckland time (今天 14:21, 明天 09:00, 昨天 22:10, or 2026-10-02 10:02 further out), from the `short_time` filter all three shells share.

### Watched Lot Row
The watch list's banded table holds a checkbox, the lot, 截止与提醒, 出价, and the 取消关注 link.
- **Lot cell:** an 80×60px photo, cover-cropped inside a hairline edge on the field fill (an empty well when there is no photo), beside the title in semibold ink and the auction line in faint caption type, clamped to two lines. The title opens the lot in a new tab and turns pale blue with an underline on hover.
- **Deadline and reminder cell:** at least 9.5em wide. The relative deadline (2 天后截止) comes first with its exact time as a caption line. Below a hairline with 6px either side, the reminder state follows as a state mark with its time or hint on a caption line, and a failed reminder adds a 立即重试提醒 link. A closed lot shows 已结束 with its closing time, and a lot without a deadline says so in faint ink.
- **Price facts:** at least 11.5em wide. The first fact (the current bid, or that there is none) leads at 14px semibold in ink, and the rest (the estimate with buyer's premium, the RRP, the sold price) follow as caption lines in secondary ink. A lot without prices shows a faint dash.
- **Closed rows:** text drops to secondary ink and the photo to grayscale at 65% opacity.
- **Failed reminders first:** 2.1 需要注意 boxes every failed reminder as a 警告 note above the list, whatever the filter, with its deadline, the folded raw error, a small retry button and a link to delivery health. When nothing failed it shows a 完成 note, and a missing reminder address is a 注意 note.
- **Removing:** row checkboxes belong to a form outside the table. The head checkbox selects all and shows a partial pick as indeterminate, and the section tools show 已选 N 个 beside a small danger button that stays disabled until something is picked. At the narrow rule, where the table head is hidden, a semibold 全选 checkbox joins the section tools before the count, and both select-alls stay in step. Stopping one watch re-renders the list, the head counts and the rail count from the server, moves focus to the next lot's title (else the previous one, else the settings button) and announces the change in a polite live region.

### Notebook Transcript
The conclusion page of a research session (1.1). It is a transcript, not a chat.
- **Turns:** rows under one 1.5px ink rule, each closed by a hairline. The label column says who and when in label type, with 综合结论 and its version, time and sufficiency, 你 for the Owner, and 回答 for a reply. Versions and times sit on caption lines in faint ink with tabular figures.
- **Conclusion:** always the first turn. The bottom line is the lead, in ink at 500. Key findings, agreements, conflicts, unknowns and evidence coverage follow under in-text heads in reading type. General model knowledge comes last in a 说明 note inside the reading column, as a 14px list in secondary ink with no citations. When the conclusion can be rewritten, a small 重新生成结论 button and a faint hint sit 18px below.
- **Citation numbers:** a bracketed number right after the claim, in 12px semibold blue with tabular figures, 3px from the text, raised 0.1em and never wrapping. Hover turns it pale blue with an underline. The number shown in the side column, and every copy of it in the text, takes a 1px blue outline.
- **Follow-ups:** the Owner's question is ink at 600 and 1.7. A reply is reading type that keeps its line breaks. A pending reply is a 回答 turn holding skeleton lines, and a reply that failed is a faint hint. A failure to send is a 警告 note under the transcript.
- **Composer:** last. A semibold label, a textarea from 92px, then a row with a faint hint on the left and the primary 发送 on the right.
- **No conclusion yet:** the first turn is muted and holds an empty state.

### Side Column
On a desktop window wider than 1180px the side column sits beside the transcript, sticky 16px from the top.
- **Boxes:** each opens with the 1.5px ink rule and 10px of space, headed in label type in secondary ink.
- **Citation box (引用):** a faint hint until a number is clicked, then that source's card, which leads with its title. The title line starts with the citation number in 13px blue tabular figures, then the linked title in semibold ink with a rule-stroke underline that turns pale blue on hover. Under it sit the publisher and quality as a faint caption line (with a hollow faint 已排除 mark once excluded), then the saved excerpt at 13px in secondary ink at 1.65, or a note in the same style that no excerpt was saved. Last come a row with a small 排除这条证据 button (restore once excluded) and an 打开原文 link, and a faint hint that the conclusion has to be rewritten after excluding.
- **Session facts (这次研究):** rows split by hairlines, 7px above and below, with the label in secondary ink on the left and the value right-aligned in tabular figures. Evidence, sources and runs link to their pages, and the model is plain text.

### Evidence Table
1.2 lists every evidence item in the latest snapshot, a page at a time.
- **Columns:** the citation number, right-aligned in tabular figures; the title with its snippet; the publisher, at least 6.5em; the quality; and an exclude or restore link.
- **Snippet:** under the semibold title at 13px in secondary ink at 1.6, clamped to three lines on purpose. The title opens the full text in a new tab.
- **Duplicate groups:** a group row in label type and secondary ink, with 14px above, 4px below and no rule, says how many reports of one story follow. Each grouped row carries the group bar on its first cell.
- **Excluded rows:** text drops to faint ink, the title to secondary ink struck through in faint ink, and the quality cell adds a hollow faint 已排除 mark. When everything is excluded, a 注意 note says so above the table.

### Sources and Plan
1.3 holds what the next run searches and what the AI planned on each run.
- **Sources:** a banded table of source, who added it, and edit and remove links (remove in red, while more than one source is left), with a small 添加信源 button in the section tools. While a run is going, a 说明 note says the sources are locked.
- **Newest plan:** under an unnumbered subhead (AI 计划) and a lede, the newest plan shows in full as a block under a 1.5px ink rule, with 12px 10px 4px padding. Its run and version sit in bold with the time in faint ink, then the summary in reading type, then a banded table of the planned sources and their rationale.
- **Older plans:** one disclosure, set as a blue link (看其余 N 个计划版本), opens a table of run and version, time, and summary. Each summary carries its own disclosure, 信源与理由（N）, in 13px blue text with the native marker. Open, it lists every source in semibold ink with its reason below in secondary ink, indented 12px behind a 2px rule-stroke line.

### Run History
1.4 has one row per run.
- **Columns:** the run number in bold with its kind as a caption line, a state mark, the start time, the duration, the work (deep reads, with plan versions and attempts as a caption line), and the outcome.
- **Outcome:** up to three lines, each on its own and each shown only when it applies. The failure reason comes first as a red sentence at the data size. The snapshot the run sealed (快照 N：X 条证据) and the conclusion it wrote (结论第 N 版 · sufficiency) follow in ink at the data size, 4px below a reason. A failed run that sealed evidence shows both its reason and its snapshot, and a conclusion is credited to the run that wrote it, including a retry that only rewrote the conclusion. The stored technical detail folds under 原始错误 after the lines, in mono on the field fill. A run with none of these shows a faint in-progress or no-outcome note.

### Running Work
- **Status note:** while a run is queued or running, a 说明 note heads every session page with the run and its state in bold, the phase, a hint and three skeleton lines. It refreshes every three seconds. A failed run leaves a 警告 note with the reason and the folded raw error. The control beside the title is a cancel button while a run is going and the primary 刷新研究 otherwise.
- **Skeleton lines:** three 10px bars in the band tone at full, 75% and 50% width, 8px apart, pulsing to 45% opacity over 1.6s. They hold still under reduced motion. There is never a spinner.

### Reading Page Head
The home page's head, over the desk and the ranked list alike.
- **Counts:** under the h1 精选, the meta line gives the window (最近 3 天, or 今天) in plain text, then the story count, the Owner's unread count and the count at 90 or above, split by middots. On the desk each count is a quiet link into the ranked list. In the ranked list they are plain text, because the filter does that job.
- **Search (`.head-search`):** on the right, a 260px search field at 32px and a secondary 搜索 button, 8px apart. It searches through `/search`, which is still on `beehive.css`.
- **Mark all read:** for the Owner, and only while something is unread, a secondary 全部标记为已读 follows the search. It is a plain form post that reloads the page.
- At the narrow rule the search takes a full line and 全部标记为已读 wraps below it.

### Channel Desk
The home page's default view: one numbered section per Channel (`.desk-sec`), its state in the heading and its best rows beneath.
- **Section head:** the number (1.1 on) and the Channel's name, then the tools on the right. The state items (`.desk-state`) are 13px secondary ink and never wrap inside themselves. The fetch time (14 小时前抓取) carries the exact time, the next fetch and the last fetch's numbers in its tooltip. A small 打开频道 button closes the line and names the Channel to screen readers.
- **State by kind:** an Editorial Channel says how many stories its window holds (3 天内 6 条) and, for the Owner, how many are unread (未读 5). A Monitor Channel says how many listings are live (在售 3,767 件). A Tracker Channel says how many lots are open at its score floor (80 分以上在拍 16 件), where the floor is 80 or the Channel's own minimum when that is higher, and, for the Owner, how many it watches (关注 0 个).
- **Editorial rows:** its top 4 featured stories in the window, unread first for the Owner, as story rows.
- **Monitor rows:** its 5 best live listings in a banded table of 商品 | 价格 | 折扣 | 分数. The listing cell is the watched lot's cell (`.lot`): the photo well, the title opening the listing in a new tab, and the vendor as the faint line under it. Prices follow (see Prices), and the score closes the row.
- **Tracker rows:** 5 open lots at the floor, soonest deadline first, in 拍品 | 截止 | 出价 | 分数, reusing the watched lot's cells (`.lot`, `.kv`). The auction line wraps in full, the relative deadline sits over its exact time, 已关注 follows under a hairline when the Owner watches the lot, and the price facts lead with the current bid.
- **Empty sections:** a section with nothing to show keeps its head and says so in one dashed empty-state line in secondary ink, per kind: no featured stories, no live listings, or no open lots at the floor. With no Channels at all, the desk is one empty state 24px under the head, with its bold line, the explanation and, for the Owner, the primary 新建频道.
- Listing and lot tables stack as labelled rows at the narrow rule. Story tables become headline rows (see Story Row).

### Story Row
One story, in a desk section or the ranked list (`.tbl-feed`).
- **Columns:** the read toggle (Owner only), the score, the summary, the channel (ranked list only), the source, the age and the deep read. The toggle and deep-read columns have no visible head.
- **Read toggle (`.rd`):** a 22px button with 2px corners and no border. An unread story shows an 8px solid blue square, which turns pale blue over a band fill on hover. A read story keeps an empty square in the same place, outlined 1.5px in the control stroke, so the toggle never moves and stays visible. Hover strengthens that outline to secondary ink. The tooltip and accessible name say the action (标为已读 or 标为未读, with the story's title). While the request runs the toggle shows a wait cursor at 60% opacity.
- **Score (`.score`):** a bare numeral, right-aligned in a narrow column, in tabular figures at 88% width. 90 and above is ink at 700 (`.is-high`), under 60 is faint ink at 400 (`.is-low`), and the rest are secondary ink at 600. An unranked story shows a faint dash.
- **Summary (`.c-sum`):** at least 20em wide, breaking anywhere. The AI summary is the link, in ink at 600, and the original title follows under it as a caption line in secondary ink (`<small>`). A read story's summary drops to secondary ink at 500. A story without a summary shows its title as the link.
- **Byline and age:** the source cell (`.c-src`, at least 6.5em) joins the byline's parts with middots (HN · 热门 · 299分 94评论). The age (`.c-age`, 1 天前) never wraps and carries the exact time in its tooltip, or shows a faint dash.
- **Best comment (`.cmt`):** when a story has one, a 热评 disclosure sits 6px under the summary in 12.5px blue with the native marker. Open, the comment reads in secondary ink at 1.6, up to 72ch.
- **Phones:** at the narrow rule the row becomes a headline row with no head and no labels. The toggle (22px) and the score (a 2.1rem column) hang in a lead on the left, 12px apart and 12px before the text, with the score level with the summary's first line. The summary takes the full width beside them, and the channel, source, age and deep read follow beneath in caption type, 12px apart, in secondary ink. When that line wraps, it wraps inside the same indent, so nothing starts left of the summary.
- An anonymous reader gets the same row without the toggle and without read state.

### Prices
A listing's price, as the Monitor rows show it.
- **Current price (`.price-now`):** ink at 600 in tabular figures, in a cell at least 6.5em wide that never wraps.
- **Former price (`.price-was`):** struck through in faint ink with tabular figures, as a caption line under the current price. Screen readers hear it as 原价.
- **Discount (`.price-off`):** the percentage off with a true minus sign (−39%), in ink at 600 with tabular figures, in its own column. No discount is a faint dash.
- **Change (`small.chg`):** the listing's latest change, 降价 or 重新有货, as a blue caption line at 600, 4px under the price.
- A discount or a drop takes no green or red. The change line is the only colour in a price, because it is news.
- Lot prices on the desk are the watched lot's price facts. The first fact leads in ink at 600 and the rest follow as caption lines.

### Ranked List
Every featured story in the window, ranked by score, a page at a time (`/?view=`, `&minimum_score=`, `&page=`). The head's counts lead here.
- It replaces the desk under the same head. The running head's crumb names the view, the head's counts turn to plain text, and the segmented filter sits 18px below (see Segmented Filter).
- The table is the story row's, with a channel column (`.c-chan`, at least 7em, breaking between words) after the summary. The channel links to its page in ink, with a pale-blue underline on hover.
- The workspace pager closes the list, labelled 精选分页, with small previous and next buttons and the page count. An empty view is one empty state, which also takes focus when the view's last row is marked read.

### Read Toggle in Place
Marking a story read or unread changes the page in place, with htmx, never by reload.
- The toggle posts, and the page swaps back only what changed: its own region (the desk section with its heading, or the whole ranked list), plus the head's meta line, the rail's count slot (`#toc-count-featured`) and, in the ranked list, the filter's counts.
- Focus returns to the toggle in the same row slot, which may now hold the next story. With no row left it moves to the region's first link or its empty state, so it never drops to the page.
- A polite live region says 已标为已读 or 已标为未读.
- Without script the toggle is still a form that posts and comes back to the same view. Only the Owner's pages load htmx.
- Nothing moves. The only feedback in flight is the toggle's wait cursor and 60% opacity.

### Keyboard Reading
- `j` and `k` walk the story rows down the whole page, across desk sections and in the ranked list. `o`, or Enter on the selected row, opens the selected story in a new tab, and `/` focuses search. Keys do nothing while a field has focus.
- The selected row (`.kb-row.is-selected`) takes the band, and keyboard focus adds a 1.5px blue outline inside the row. Rows keep a roving tabindex, so only the selected row joins the tab order.
- A polite live region announces the selection with its Channel, AI score and summary.
- The hint closes the page in 13px faint type: `j` `k` 移动 · `o` 打开 · `/` 搜索. Each key is a keycap (`kbd`), at least 1.6em wide with 4px sides, outlined in 1px rule stroke with 2px corners on the field fill, in secondary ink at 0.72rem and 600. A keycap is type, not a control. The hint hides at the narrow rule.

### Boxed Notes
The datasheet's callouts.
- **Style:** a square box with a 1.5px border in its tone. A solid tag cell at least 64px wide carries the tone word in 13px bold fill ink, tracked 0.02em (0.12em in CJK). The body is 14px with 10px 14px padding, and actions sit in a cell on the right.
- **Tones:** 说明 Note (secondary ink), 注意 Caution (amber), 警告 Warning (red) and 完成 Done (ink rule). Confirmations after an action use Done. Failures use Warning and are announced as alerts.
- Notes stack 8px apart. At the narrow rule the tag becomes a strip above the body.
- Notes also sit inside the reading column (the model-knowledge 说明 note, 22px below the text) and in a session's status region (a running or failed run).

### Danger Row and Removal Popover
- **Danger row:** a Warning note built on a disclosure. Closed, it shows the bold title, what will go with its counts, an undo hint and a small danger button. Open, the button becomes 取消 and a confirmation panel unfolds below on the field fill, under a rule-stroke line. A deletion is typed. In the admin the hint is the 7-day undo, and the panel reads "输入“name”以确认。" with a growing input and a danger submit. Deleting a research session can't be undone, so its hint says 不能撤销。 and its panel asks for the word 删除 (delete in each locale) the same way. A list cleanup that keeps the records behind it, such as clearing closed watches (the lots and their history stay), also says 不能撤销。 but asks once. Its panel states what goes in one sentence above the danger submit, with no typed field.
- **Removal popover:** removing a source uses the native popover, so the table row stays in place. It is up to 460px wide, with a 1.5px red border and a red head strip with the title in 14px bold fill ink. Below come the source name, what goes with it, the undo hint and the same typed confirmation, with a cancel.

### Containers
- **Sheet:** see Layout.
- **Facts strip:** auto-fit cells of at least 150px between a 1.5px ink rule and a hairline, split by hairlines. A condensed label sits on top and a 17px semibold tabular value below. The run-budget preview sets a run's limits in one.
- **Empty state:** a dashed rule-stroke box with 18px 14px padding, a bold ink first line over secondary-ink text, and the primary action below.
- **Plain-text preview:** 13px mono at 1.75 on the field fill with a rule-stroke border, at most 560px tall and scrolling inside.
- **Pager:** 10px below a table, small previous and next buttons (a disabled one takes the hairline border and faint ink), the page count in 13px secondary ink, and on long lists a page field with a small jump button. Screen readers hear each pager named for the list it pages, such as 精选分页 on the ranked list.
- **Empty states** also sit inside a turn and under each session section. Only an empty research list offers the primary action (a new session) in the workspace. An empty watch list offers a secondary button to all lots or back to the site. On the reading pages, an empty desk offers the Owner the primary 新建频道, while an empty desk section or ranked view says so in one line with no action.

### Named Rules
**The Parameter Row Rule.** Every form is a parameter table. The label names the parameter, the control holds the value, and the note says what it does, what is in effect now and what went wrong.

**The Counted Save Rule.** A changed row is tagged 已修改 in blue and counted in the sticky save bar. After a rejected save the bar stays unsaved and says so.

**The In-Place Confirmation Rule.** A destructive action opens in place (or, for a source, in a popover) into a confirmation that names what goes, gives the counts and says whether it can be undone. Deleting data is typed. Admin removals, which can be undone for 7 days, ask for the name, and deleting a research session, which can't, asks for the word 删除. A list cleanup that keeps the records behind it, such as clearing closed watches, asks once, in words.

**The Boxed Note Rule.** Callouts are square boxes in their tone, with a solid tag cell that says the tone in words (说明, 注意, 警告, 完成).

**The Notebook Rule.** A research conversation is a transcript. Who and when sit in a label column with the words beside them. The conclusion is the first turn, follow-ups continue below it and the composer comes last. Never chat bubbles, never cards.

**The Citation Number Rule.** A citation is a small blue number in brackets right after the claim it supports. Its visible text is only the number, and the source's title and quality go to the tooltip and to screen readers.

**The Labelled Skeleton Rule.** Work in progress shows skeleton lines inside a note or turn that says what is running. Never a spinner.

**The Quiet Tally Rule.** A number that informs rather than asks is a bare faint numeral. The rail's unread tally on 1 精选 shows 0 when everything is read and nothing for a reader. Outlined counts stay for attention (amber) and unread research results (blue).

**The Score Numeral Rule.** A score is a bare tabular numeral: 90 and above in ink at 700, under 60 faint, the rest in secondary ink, and an unranked item a faint dash. Never a badge, a bar or a colour.

**The Toggle-in-Place Rule.** Marking a story read re-renders from the server only what changed: its section or list, the head's counts and the rail tally. Focus stays on the same slot, a live region says what happened, and nothing reloads or animates.

## Do's and Don'ts

### Do:
- **Do** build every form as a parameter table (参数 | 设置 | 说明) with `ui.param`, and put hints, the value in effect and errors in the note column.
- **Do** use the one-line field (`ui.line_field`) for any value without a length limit, such as URLs, queries, email addresses, subject templates and source names. Keep bounded values as inputs.
- **Do** keep units whole in table cells with `ui.units`, `ui.keep` and `.nw`, and break URL paths with `ui.url_path`. Mark multi-column list tables to stack at the narrow rule (`.tbl-stack`, with `data-label` on each cell), except story tables, which take `.tbl-feed` and read as headline rows.
- **Do** pair every state with a square mark and a word, and add amber or red only when the Owner has to act.
- **Do** give each destructive action its own Warning row (`ui.danger`) that opens in place into a confirmation naming what goes. Pass a `confirmation_value` for any deletion of data, keep the one-step worded confirmation for list cleanups, and pass a `hint` whenever the admin's 7-day undo doesn't apply, so the row never promises an undo that doesn't exist.
- **Do** use tabular figures for every changing number, and condense only label type (column heads, fact labels, transcript labels, box heads, group rows), numeric columns and scores.
- **Do** set long AI text in the reading column (`.read`, 72ch), lead a conclusion with its bottom line (`.answer`), and mark citations with `rx.cite`.
- **Do** format every short time with the shared `short_time` filter, so all three shells write today, tomorrow, yesterday and full dates the same way, in Auckland time.
- **Do** show running work as labelled skeleton lines, and mark closed lots and archived sessions with `.is-closed`.
- **Do** keep all admin, workspace and reading styling in `admin.css`, and render all three shells through `datasheet_base.html`. Templates carry no inline `style=` (a test enforces this), scripts load only from the site (`script-src 'self'`), and fonts are self-hosted.
- **Do** render every reading page through `render_reading` and `reading_base.html` (body class `page-reading`), so the rail, its tally, the footer and the clock are built in one place.
- **Do** show a score with the score numeral (`.score`, `.is-high` at 90 and above, `.is-low` under 60), and a listing's price with `.price-now`, `.price-was`, `.price-off` and `small.chg`.
- **Do** give every in-place toggle its own region with `data-refocus-slot`, an `hx-select` of that region, the head's meta and the rail's count slot (`#toc-count-…`) out of band, and a `data-feedback-message` for the live region.
- **Do** undo any workspace clamp under `.page-reading`, as `.lot-where` does, so a reading page cuts nothing short.
- **Do** write 频道 and 信源 in Chinese copy. English capitalises Channel and Source as product terms.

### Don't:
- **Don't** build card grids, tiles or dashboards of boxes. Lists are banded tables and settings are parameter rows, and a row of figures is a ruled facts strip.
- **Don't** set a conversation as chat bubbles or cards. Turns are rows with a label column.
- **Don't** fill a control, tag or mark with blue. The one blue fill is the 8px unread square on a story row. Blue is for links, focus, unsaved edits, unread results and stories, a listing's latest change and work in progress, and the primary action is solid ink.
- **Don't** use amber or red for emphasis or decoration.
- **Don't** round containers, or give any control more than 2px.
- **Don't** lift things off the sheet with drop shadows. Change the tone instead. The removal popover is the only shadow.
- **Don't** put a kicker or eyebrow above a heading or a title. Caption lines go under the value they describe. The running head and facts labels are structure, not kickers.
- **Don't** give a row, note or card a coloured side stripe. The only left bars are 2px rule-stroke lines that group things, on grouped evidence rows and beside an older plan's source list.
- **Don't** set free-length text on one unbreakable line, or let a layout depend on sideways scrolling at 1440, 1024 or 390px. The reading rail's chapter line at the narrow rule is the one deliberate sideways scroll.
- **Don't** cut text short on a reading page with an ellipsis or a line clamp.
- **Don't** give a score a badge, a bar or a colour, or colour a discount green or red.
- **Don't** write "Channel" in Chinese copy.
- **Don't** outline a control with the rule stroke. Outlined controls take the control stroke (`--ctl`), or the danger stroke (`--ctl-danger`) on danger buttons, and both hold at least 3:1 on every admin surface.
- **Don't** borrow from `beehive.css`. Its gold accent and serif heads belong to the pages still on it (channel pages, archive, search and deep-read briefs), and a page that moves into the reading shell leaves them behind.
