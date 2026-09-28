---
name: Beehive Admin
description: "The Owner's datasheet, set dark. Records the admin design system in src/beehive/web/static/admin.css. Public reading pages use beehive.css and are not described here."
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
  body:
    fontFamily: "Archivo, PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, Hiragino Kaku Gothic ProN, Noto Sans CJK JP, Apple SD Gothic Neo, Noto Sans CJK KR, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.55
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
---

# Design System: Beehive Admin

> **Scope.** This file records the admin design system only. That means every page under `/admin/` plus the admin login, styled by `src/beehive/web/static/admin.css`, with shared behaviour in `src/beehive/web/static/beehive.js`. The public reading pages (dashboard, channel pages, archive, watch list, research) use `src/beehive/web/static/beehive.css`, which is a different visual world and is not described here. Don't carry tokens from `beehive.css` into admin, or from this file into public pages.

## Overview

**Creative North Star: "The Owner's Datasheet"**

Admin reads like a technical datasheet for one person's pipeline, set dark. A graphite sheet lies on a darker desk. A contents rail on the left numbers the chapters. A running head gives the location and the Auckland clock that every schedule is read against, and each section carries a number that matches the rail. Every setting is a parameter row with its value and a note. Every list is a banded table under a heavy ink rule. The density is working density, with 15px body text, 32px controls and hairlines between rows. Nothing is padded out for show.

Colour is a signal. Ink on graphite carries everything that is fine. Amber appears only where something waits on the Owner, red only for failure or a destructive action, and blue for links, focus and unsaved edits. State is always a shape plus a word. Controls have 2px corners and everything else is square. Depth comes from tone, and the one drop shadow belongs to the removal popover.

The system refuses the card-grid SaaS console, so there are no tiles, no dashboards of boxes and no decorative colour. The Owner visits rarely, so every page says what it controls and what state it is in. Unsaved rows are marked and counted, and destructive rows open in place into a typed confirmation.

**Key Characteristics:**
- Graphite sheet on a darker desk, flat and tonal.
- Every form is a parameter table (参数 | 设置 | 说明) and every list is a banded table under a heavy ink rule.
- Boxed notes tagged 说明 / 注意 / 警告 / 完成.
- Colour only where the Owner is needed. Amber for attention, red for failure, blue for links and focus.
- State is a square mark plus a word.
- Archivo throughout, condensed only in column heads and numbers, with tabular figures wherever numbers change.
- 2px corners on controls, square containers.
- Nothing overflows at 1440, 1024 or 390px, and units never split.

## Colors

A restrained graphite palette with three functional signals, and each signal has one meaning.

### Primary
- **Datasheet Blue** (#7DB6FF): the only interactive colour. It marks links, the focus outline and field ring, the 已修改 tag on changed rows, the unsaved count in the save bar, the research unread counter, and in-progress state marks. It is never a fill.
- **Pale Datasheet Blue** (#A9CEFF): link hover, and hover on names in tables.

### Secondary
- **Attention Amber** (#F0B44C): something waits on the Owner. It marks paused sources, warnings, caution notes (注意) and the counts on the contents rail. A healthy page doesn't show it.

### Tertiary
- **Fault Red** (#FF6E61): failure and destruction. It marks failed fetches, error lines, danger notes (警告), danger buttons and links, the removal popover, invalid fields and field errors.
- **Danger Stroke** (#B04A42): the resting boundary of danger buttons. It is darker than Fault Red so a danger button stays quiet until hovered, and it holds at least 3.15:1 on every admin surface.

### Neutral
- **Desk Black** (#0A0C0E): the desk under the sheet, and the page background behind the shell.
- **Graphite Sheet** (#111417): the sheet, the contents rail, the save bar and the popover body.
- **Header Band** (#191D21): table and parameter head rows, the current rail entry, checked selection rows, prefix cells and highlighted rows.
- **Hover Wash** (#161A1E): hover on table rows, rail entries and buttons.
- **Recessed Field** (#0C0E10): input wells, confirmation panels, raw errors and the plain-text preview. It sits darker than the sheet, so fields read as sunk.
- **Hairline** (#262B30): row dividers, the sheet and rail edges, facts dividers, raw-error boxes, and the faded border of disabled buttons.
- **Rule Stroke** (#3A4046): structural lines only. It draws the line under table heads and the parameter head row, the save bar's top edge, the confirmation panel's top edge, the plain-text preview border, dashed empty-state boxes and the scrollbar thumb. It is never a control boundary.
- **Control Stroke** (#666E76): the resting boundary of every enabled control, meaning buttons, fields (inputs, selects, textareas and one-line fields), prefix cells and weekday chips. It holds at least 3.27:1 on every admin surface.
- **Ink Rule** (#C9CFD6): the heavy rules over tables and parameter tables, the running-head rule, the filled "ok" mark and the 完成 note.
- **Sheet Ink** (#E7EAED): primary text, and the fill of the primary button.
- **Secondary Ink** (#A7AFB7): notes, meta lines, column heads and the 说明 note.
- **Faint Ink** (#808891): chapter and section numbers, placeholders, stacked-row labels, empty values, disabled text, and the border of a hovered field. It holds at least 4.7:1 on every admin surface.
- **Fill Ink** (#0A0C0E): text on solid fills (primary button, note tags, danger hover, popover head). It has the same value as the desk but is its own role.

A few component states use fixed values that are deliberately not tokens: primary hover #FFFFFF, danger link hover #FF9A90, text selection #28507F under white text, the popover backdrop `rgba(5,6,7,.6)`, and link underlines at 40% of their link colour. Reuse them only in those states.

### Named Rules
**The Owner-Needed Rule.** Colour appears only where something needs the Owner. Amber means attention, red means failure or a destructive action, and blue means a link, focus or an unsaved edit. Everything else is ink on graphite, so a healthy page shows no amber and no red outside its danger rows.

**The Solid Ink Rule.** The primary action is a solid block of ink with dark text. Blue never fills a control, and amber never fills a button.

**The Shape-Plus-Word Rule.** Every state is a 9px square plus its word. Settled states are filled (ok in ink rule, error in red). Waiting states are hollow (paused and warning in amber, never fetched in faint ink, in progress in blue). Colour never carries a state alone.

**The Visible Control Rule.** Every outlined control the stylesheet draws (buttons, fields, prefix cells, weekday chips) rests on the control stroke, or the danger stroke on danger buttons, and both hold at least 3:1 on every admin surface. The rule stroke draws structure only and never outlines a control. States only strengthen the boundary (secondary ink on a hovered button or a checked chip, faint ink on a hovered field, blue on focus, red when invalid). Only disabled buttons fade to the hairline.

## Typography

**Display Font:** Archivo, self-hosted variable (weight 100–900, width 62–125%), falling back to the platform CJK sans (PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Microsoft YaHei, then the Japanese and Korean equivalents) and system-ui
**Body Font:** the same stack
**Label/Mono Font:** the ui-monospace stack (SF Mono, Cascadia Mono, DejaVu Sans Mono, Menlo, Consolas), for raw errors and the plain-text email preview only

**Character:** One grotesque in several widths and weights, set like a spec sheet. Bold full-width heads name things, and condensed semibold column heads and numbers pack the data.

Archivo ships as two woff2 subsets (latin and latin-ext) in `static/fonts/`, with `font-display: swap`, and the latin file is preloaded by the admin shell. The CSP (`default-src 'self'`) rules out third-party font hosts. `font-synthesis: none` stops the browser from faking bold or italic in fallback faces.

Archivo covers Latin only. In the zh-CN interface, Chinese text in headings, labels and column heads renders in the platform CJK sans, which has no width axis. The condensed heads therefore show only on Latin runs such as channel names, numbers and times. The direction's "Archivo with condensed headers" lands fully only in Latin locales.

### Hierarchy
- **Headline** (700, 1.5rem/24px, 1.25): the page h1, with a 14px meta line of counts under it in secondary ink. It wraps balanced and breaks anywhere rather than overflow.
- **Title** (700, 1rem/16px, 1.35): numbered section heads. The number sits in faint ink at 600 in a 2.4em column, and section tools (counts, small buttons) align right on the same line.
- **Body** (400, 0.9375rem/15px, 1.55): base text. Field values use the same size at 1.4. Ledes stop at 72ch.
- **Data** (400, 0.875rem/14px, 1.45): table cells, note bodies and meta lines. Parameter labels use it at 600.
- **Note** (400, 0.8125rem/13px, 1.6): the note column of parameter rows, secondary lines in forms and popovers, and confirmation labels. Key values inside a note go bold in ink.
- **Button** (600, 0.8125rem/13px, 1.2): button labels. Small buttons drop to 0.78rem.
- **Label** (600, 0.78rem/12.5px, 1.3, 85% width): column heads, the parameter head row and fact labels.
- **Caption** (400, 0.78rem/12.5px): secondary lines under table values, the running head, footnote strips, stacked-row labels, and error and caution lines.
- **Numeric** (400, 0.875rem/14px, tabular, 88% width): numeric table columns, right-aligned.
- **Mono** (400, 0.8125rem/13px, 1.75): the plain-text email preview. Raw error text uses 0.75rem at 1.55.
- Counters and tags sit at 0.7–0.72rem and 700 (rail counts, the 已修改 tag). Footnote references use 0.68rem.

### Named Rules
**The Condensed Head Rule.** Width marks data. Column heads and fact labels set at 85% width and numeric columns at 88%. Headings and body text stay at full width.

**The Tabular Figures Rule.** Every number that changes (counts, the clock, chapter and section numbers, numeric columns, facts) uses tabular figures, so columns and counters never jitter.

## Layout

The shell has two columns, a 216px contents rail and a fluid column that holds one sheet. The rail is sticky, runs the full viewport height and scrolls on its own. Its footer (back to the site, watch list, research, sign out) sits at the bottom above a hairline. The sheet sits on the desk with a 16px margin, is at most 1320px wide, and has a 16px top, 28px side and 36px bottom inset inside a 1px hairline edge.

Every sheet opens with the running head. The location sits on the left (chapter number and name, then crumbs separated by ›) and the Auckland clock on the right, with a 2px ink rule under both and 20px of space below. The page head comes next, with the h1 and its meta line on the left and actions on the right, wrapping under the title when space runs out. Sections follow as numbered heads (N.M, matching the rail's sub-entries), with 32px above and 10px below. A chapter's attention notes come before the list they concern.

Parameter tables use three columns: label at 140–190px, control fluid, and note at 200–290px. The gutter is 24px, the side inset 10px, and each row has 14px of padding above and below. Controls in a row sit 8px apart and wrap when they run out of room.

Breakpoints are container queries on the `adm` container (the body), not media queries.
- **Mid (1180px and below):** the desk margin drops to 8px and the sheet inset to 14px 18px 32px. Table cells take 8px at the sides, no-wrap cells relax, and operation columns wrap. Parameter columns become 120–160px, fluid and 180–240px, with an 18px gutter.
- **Narrow (880px and below):** the rail becomes a wrapping top bar, with chapters in a row, sub-sections hidden and footer links at the right. The sheet runs edge to edge without a border at 14px 14px 28px. Parameter rows stack label, control and note, and the head row hides. Banded tables become labelled stacked rows, and notes move their tag above the body.

### Named Rules
**The No-Overflow Rule.** No text leaves its box at 1440, 1024 or 390px. Free-length values wrap (one-line fields, break-anywhere cells), and bounded values ellipsize. No layout depends on sideways scrolling. The table wrapper's scroll is a guard, not a layout.

**The Whole Unit Rule.** In a table a unit never splits, whether it is a count with its word, a day with its time or an email address. Lines break between units, and URLs break only before a slash. At the narrow rule these holds relax so stacked rows can wrap.

**The Stacked Row Rule.** At 880px and below, every multi-column list becomes labelled rows. Each value is led by its column name in a 76px faint-ink label column, the row checkbox moves to the top right, and operations drop to the bottom.

## Elevation & Depth

The system is flat. Depth is three steps of graphite. The desk sits lowest, the sheet lies on it behind a hairline edge, and header bands sit one step lighter than the sheet. Fields sit one step darker than the sheet, so they read as wells. Hover is a tone change, never a lift. The only drop shadow belongs to the source-removal popover, which opens in the top layer over a 60% near-black backdrop.

### Shadow Vocabulary
- **Popover lift** (`box-shadow: 0 18px 40px -8px rgba(0,0,0,.65)`): the removal popover only.
- **Field focus ring** (`box-shadow: 0 0 0 1px var(--link)`): doubles the blue border of a focused field. It is a ring, not elevation.
- **Hollow mark** (`box-shadow: inset 0 0 0 1.5px <tone>`): draws the hollow state squares.

### Named Rules
**The Flat Sheet Rule.** Depth is tone. The desk sits below the sheet, bands sit above it and fields sink into it. Nothing floats except the removal popover.

## Shapes

Everything is a rectangle, ruled like a datasheet. Controls and counters take a 2px corner. That covers buttons, fields, rail entries, weekday chips, prefix cells, rail counts and the 已修改 tag. The sheet, tables, notes, the popover, facts strips, empty states and code blocks are square. Lines do the structural work. A 2px ink rule sits under the running head. A 1.5px ink rule opens every table, parameter table, selection table and facts strip. Notes and the popover take 1.5px borders in their tone, and 1px lines in the hairline and rule-stroke tones do everything else. Controls are outlined apart from all of this, in 1px of the control stroke. Empty states use a dashed rule-stroke border. State marks are 9px squares. The one non-rectangular form is the product's hexagon mark from `favicon.svg`, drawn as an 18px stroked outline beside the product name.

### Named Rules
**The Two Corner Rule.** Controls and counters get 2px, and containers get none.

**The Heavy Ink Rule.** A 1.5px ink rule opens every table and parameter table. Hairlines do everything else.

## Components

### Buttons
Quiet rectangles, labelled in words.
- **Shape:** 2px corners, 32px tall, 12px sides, never wrapping.
- **Primary:** a solid block of sheet ink with fill-ink text, brightening to pure white on hover and dimming to the ink rule tone while pressed, by mouse or keyboard. It is reserved for the page's main commitment, such as create, save, sign in or send a test.
- **Hover / Focus:** colour-only transitions at 150ms ease-out. Focus is the global 2px blue outline at a 2px offset.
- **Secondary (default):** transparent, with a 1px control-stroke border and ink text. On hover the border moves to secondary ink and the hover wash fills it. Active uses the band.
- **Danger:** transparent, with red text and a danger-stroke border. On hover it fills solid red with fill-ink text. It appears only in danger rows and the removal popover.
- **Small and block:** small buttons are 26px tall with 9px sides, used for note actions and section tools. The block button is full width at 38px, used for sign-in.
- **Disabled and loading:** disabled buttons take a hairline border, faint ink and a not-allowed cursor. Loading shows a wait cursor at 70% opacity.

### Links
- Links are datasheet blue with a 40% blue underline at a 3px offset. On hover they turn pale blue with a full underline. Danger links are red.
- Row operations in tables are links, even when they post a form, so five operations (test, edit, pause, copy, remove) stay one quiet line.

### State Marks
- **Style:** a 9px square and a word, 7px apart, never wrapping.
- **States:** ok is a filled ink-rule square with normal text. Paused and warning are a hollow amber square with amber semibold text. Error is a filled red square with red semibold text. Never fetched is a hollow faint square with faint text. In progress is a hollow blue square with blue text.
- The channel and source lists carry a legend of the marks in the footnote strip below them.

### Inputs / Fields
- **Style:** 34px minimum height, 6px 10px padding, a 1px control-stroke border, 2px corners, the recessed field fill, and 15px text at 1.4. Placeholders use faint ink. On hover the border moves to faint ink. Selects, textareas and one-line fields share the same border.
- **Focus:** the border turns blue and a 1px blue ring doubles it. There is no outline.
- **Error:** an invalid field takes a red border, and the message sits in the row's note column in red 13px semibold.
- **Bounded and free-length values:** names, numbers, times and selects are inputs, and input text ellipsizes. Values of any length (URLs, queries, email addresses, subject templates, source names) use the one-line field (`ui.line_field`). It is a one-row textarea that wraps and grows with its content (`field-sizing: content`, with a script fallback). Enter submits the form and pasted line breaks are dropped. Literal values turn off spellcheck, autocapitalize and autocorrect.
- **Multi-line:** the channel profile is a textarea at least 132px tall at 1.65 line height, growing with its content.
- **Sizes and variants:** number fields are 96px, time fields 132px, and mid selects up to 260px. Growing fields flex from 220px. Selects draw a 12px chevron in secondary ink. A prefix cell (such as `r/`) joins its field in band fill under the same control-stroke border. Checkboxes and radios are 16px native controls tinted ink. Weekday chips are 30px labels with a control-stroke border, and a checked chip takes a secondary-ink border and band fill.

### Navigation
- **Contents rail:** a brand line (the hexagon mark, the product name at 700, and 管理 in secondary ink at 500), then numbered chapters as 32px rows with a 22px column of faint tabular numbers. Entries sit in secondary ink, take the hover wash and ink on hover, and the current chapter takes the band, ink and 600. The current chapter lists its N.M sections as 28px sub-rows at 13px.
- **Attention counts:** a chapter that needs the Owner shows an 18px amber-outlined count with a full sentence for screen readers. Each problem is counted once, in the chapter where it is fixed.
- **Rail footer:** back to the site, watch list, research (with a blue-outlined unread count) and sign out, at 13px.
- **Running head:** 12.5px secondary ink. Crumbs are links that underline on hover, and the clock uses tabular figures.
- **Mobile:** at the narrow rule the rail becomes a wrapping top bar, with chapters in one row and footer links at the right.

### Parameter Table
The signature form, 参数 | 设置 | 说明 (Parameter | Setting | Notes).
- A 1.5px ink rule sits on top, then an optional band head row in label type over a rule-stroke line. The head row is hidden from assistive tech because every row labels itself.
- Each row has a 14px semibold label (with an optional 可选 line in faint caption), a control column with 10px between stacked controls, and a 13px secondary-ink note. Fixed values, such as a channel's type, sit in the control column as plain text. A hairline closes each row.
- A changed row shows the 已修改 tag after its label, a blue outlined 11px bold tag with 2px corners. Radio and checkbox groups carry the tag in their section head instead.
- The sticky save bar closes the form on the sheet tone above a rule-stroke line. It holds the primary save, then 放弃修改 once something has changed, an optional cancel, and the blue count "N 项修改还没保存". After a rejected save the bar stays unsaved and says so, and discard reloads the saved page.
- Single-setting forms (language, model, featured window, default recipient) put their own secondary button in the row instead of a save bar.

### Banded Table
The signature list.
- A 1.5px ink rule sits over a band head in label type that never wraps, with a rule-stroke line under the head. The body is 14px at 1.45 with 10px cells, a hairline under every row and the hover wash on hover. A highlighted row takes the band.
- **Columns:** the name column is semibold, at least 12em wide, wraps balanced, and its links turn pale blue on hover. Numeric columns right-align in tabular figures at 88% width. Unit columns never wrap, and word columns keep CJK words whole. Wide columns (details, sources) start at 16em and break anywhere. The operations column is as narrow as its links, right-aligned, with 12px between links.
- **Cell holds:** cells set `text-wrap: pretty`, which resets wrapping and outranks a single-class `white-space: nowrap`. Scope any cell-level hold under the table, as the build's `.tbl .c-nw` and `.tbl .c-num` rules do, or the unit breaks anyway.
- **Lines under a value:** a caption line in secondary ink, a failure line in red, or a caution line in amber. A raw error folds into a disclosure, set in 12px mono on the field fill.
- **Footnote strip:** below the table, in caption type. It holds the mark legend, notes, and numbered footnotes referenced by a faint superscript "(1)".
- **Selection tables:** source type, channel kind and group members use the same ink rule. Each row holds a radio or checkbox, a semibold name, a description and a side note. The checked row takes the band, keyboard focus draws a blue outline inside the row, and unavailable rows go faint.

### Boxed Notes
The datasheet's callouts.
- **Style:** a square box with a 1.5px border in its tone. A solid tag cell at least 64px wide carries the tone word in 13px bold fill ink, tracked 0.02em (0.12em in CJK). The body is 14px with 10px 14px padding, and actions sit in a cell on the right.
- **Tones:** 说明 Note (secondary ink), 注意 Caution (amber), 警告 Warning (red) and 完成 Done (ink rule). Confirmations after an action use Done. Failures use Warning and are announced as alerts.
- Notes stack 8px apart. At the narrow rule the tag becomes a strip above the body.

### Danger Row and Removal Popover
- **Danger row:** a Warning note built on a disclosure. Closed, it shows the bold title, what will go with its counts, the 7-day undo hint and a small danger button. Open, the button becomes 取消 and a confirmation panel unfolds below on the field fill, under a rule-stroke line. The panel reads "输入“name”以确认。" and holds a growing input and a danger submit.
- **Removal popover:** removing a source uses the native popover, so the table row stays in place. It is up to 460px wide, with a 1.5px red border and a red head strip with the title in 14px bold fill ink. Below come the source name, what goes with it, the undo hint and the same typed confirmation, with a cancel.

### Containers
- **Sheet:** see Layout.
- **Facts strip:** auto-fit cells of at least 150px between a 1.5px ink rule and a hairline, split by hairlines. A condensed label sits on top and a 17px semibold tabular value below.
- **Empty state:** a dashed rule-stroke box with 18px 14px padding, a bold ink first line over secondary-ink text, and the primary action below.
- **Plain-text preview:** 13px mono at 1.75 on the field fill with a rule-stroke border, at most 560px tall and scrolling inside.

### Named Rules
**The Parameter Row Rule.** Every form is a parameter table. The label names the parameter, the control holds the value, and the note says what it does, what is in effect now and what went wrong.

**The Counted Save Rule.** A changed row is tagged 已修改 in blue and counted in the sticky save bar. After a rejected save the bar stays unsaved and says so.

**The Typed Confirmation Rule.** A destructive action opens in place (or, for a source, in a popover) into a confirmation that names what goes, gives the counts, says it can be undone for 7 days, and asks the Owner to type the name.

**The Boxed Note Rule.** Callouts are square boxes in their tone, with a solid tag cell that says the tone in words (说明, 注意, 警告, 完成).

## Do's and Don'ts

### Do:
- **Do** build every form as a parameter table (参数 | 设置 | 说明) with `ui.param`, and put hints, the value in effect and errors in the note column.
- **Do** use the one-line field (`ui.line_field`) for any value without a length limit, such as URLs, queries, email addresses, subject templates and source names. Keep bounded values as inputs.
- **Do** keep units whole in table cells with `ui.units`, `ui.keep` and `.nw`, and break URL paths with `ui.url_path`. Mark multi-column list tables to stack at the narrow rule (`.tbl-stack`, with `data-label` on each cell).
- **Do** pair every state with a square mark and a word, and add amber or red only when the Owner has to act.
- **Do** give each destructive action its own Warning row (`ui.danger`) that opens in place into a typed confirmation naming what goes and the 7-day undo.
- **Do** use tabular figures for every changing number, and condense only column heads, fact labels and numeric columns.
- **Do** keep all admin styling in `admin.css`. Templates carry no inline `style=` (a test enforces this), scripts load only from the site (`script-src 'self'`), and fonts are self-hosted.
- **Do** write 频道 and 信源 in Chinese copy. English capitalises Channel and Source as product terms.

### Don't:
- **Don't** build card grids, tiles or dashboards of boxes. Lists are banded tables and settings are parameter rows, and a row of figures is a ruled facts strip.
- **Don't** fill a control, tag or mark with blue. Blue is for links, focus, unsaved edits and work in progress, and the primary action is solid ink.
- **Don't** use amber or red for emphasis or decoration.
- **Don't** round containers, or give any control more than 2px.
- **Don't** lift things off the sheet with drop shadows. Change the tone instead. The removal popover is the only shadow.
- **Don't** set free-length text on one unbreakable line, or let a layout depend on sideways scrolling at 1440, 1024 or 390px.
- **Don't** write "Channel" in Chinese copy.
- **Don't** outline a control with the rule stroke. Outlined controls take the control stroke (`--ctl`), or the danger stroke (`--ctl-danger`) on danger buttons, and both hold at least 3:1 on every admin surface.
- **Don't** borrow from `beehive.css`. The public site's gold accent and serif heads belong to the public world.
