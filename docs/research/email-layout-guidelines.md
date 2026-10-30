# Email layout guidelines

Primary-source findings for Beehive's mobile digest redesign, researched on
2026-10-30. These findings informed the initial comparison mockups. "Application to
Beehive" records that initial, spacing-only proposal; the later user-approved
direction is recorded under "Approved application".

## Sources

### General UI guidance, not examples of vendor emails

1. [Apple Human Interface Guidelines: Layout](https://developer.apple.com/design/human-interface-guidelines/layout)
   recommends prioritizing important content and grouping related items through
   negative space, containers, or separator lines.
2. [Material 3: Divider guidelines](https://m3.material.io/components/divider/guidelines)
   recommends using dividers sparingly. Repetitive list items can use margins
   instead of divider lines.
3. [WCAG 2.2: Target Size (Minimum)](https://www.w3.org/TR/WCAG22/#target-size-minimum)
   specifies 24 by 24 CSS pixel targets, with exceptions including adequately
   spaced targets and links inline in text.
   [Use of Color](https://www.w3.org/TR/WCAG22/#use-of-color) prohibits color-only
   cues; [Contrast (Minimum)](https://www.w3.org/TR/WCAG22/#contrast-minimum)
   requires at least 4.5:1 for normal-sized text.

### Email client documentation

4. [Gmail CSS support](https://developers.google.com/workspace/gmail/design/css)
   documents support for style blocks and width media queries, while warning
   that unsupported CSS may be ignored. Its supported property list includes
   `word-wrap`; do not rely solely on `overflow-wrap`.

### Actual email implementations

5. [GOV.UK digest builder](https://github.com/alphagov/email-alert-api/blob/636ff113c6a125d6f432b374fe108ba2f243958e/app/builders/digest_email_builder.rb#L28-L51)
   generates an opening line, a topic heading, items separated by horizontal
   rules, and a footer. There is no table of contents. This is an example of a
   shipped structure, not evidence that every digest needs headings or rules.
6. [GOV.UK Notify email wrapper](https://github.com/alphagov/notifications-utils/blob/19fb1303290242bdf1183979e01c0b6c62b6f3c3/notifications_utils/jinja_templates/email_template.jinja2#L201-L223)
   uses a fluid presentation table with `width: 100%`, `max-width: 580px`, 10px
   gutters, inline styles, and 19px text. The template's media query is for print.

## Application to Beehive

These are design recommendations derived from the sources, not vendor mandates.

### Stable item structure

Use the full summary or item title as the primary text. Put price changes and
closing dates immediately below it, at body size and with stronger weight. Keep
the time zone with the time. Finish with secondary source and saved-brief links.
Prices and deadlines are decision facts, not low-priority metadata.

### Grouping through proximity

Spacing inside an item should be smaller than spacing between items, which
should be smaller than spacing between channels. Keep a consistent left edge.
Remove per-item rules and decorative boxes. Check the structure at narrow widths:
if items blend together, restore their spacing rather than shrinking the text.

### Legible, identifiable links

Use blue underlined links with sufficient contrast. Prefer a publisher name or
domain over the generic label "Source". Keep source and saved-brief actions
together without adding a separate button for each.

Do not treat every text link as a large padded app button. Use appropriate line
height and spacing while checking target size and separation. Preserve meaningful
labels and safe link handling.

### Content-first email framing

Omit the visible masthead, contents block, top button, and decorative dividers
for this redesign. This is the requested product direction, not a pattern
universally followed by the sources. The email subject still identifies the digest.

Place a source-failure warning next to the affected channel's content, before the
items it qualifies. Routine source-health details can remain in the footer.

Use a fluid, single-column presentation-table baseline, modest gutters, and inline
styles. Media queries can refine the result but should not be required for a usable
layout. Preserve all selected entries, event facts, links, and the plain-text part.

## Approved application

After rejecting several spacing-only mockups, the user explicitly required a bounded
block for each entry. They approved a light block version (v6), asked for a dark
version, approved it (v7) and requested implementation. That decision supersedes
the initial advice above to remove per-item rules and decorative boxes. The sourced
findings are unchanged.

The local template now uses one outlined dark block per event inside the fluid
single-column table. It keeps the content-first framing, decision facts, hostname
links, warnings placed before the affected group, and the health footer. See
`DESIGN.md` (Digest Email) and
`.impeccable/surfaces/src-beehive-digest-templates-digest-email-html.md`. This change
is neither deployed nor committed, and the native-client checks under Limits are
still outstanding.

## Limits

Native-app and web accessibility guidance does not establish exact email font
sizes or guarantee email-client behavior. The mock's typography and spacing are
design choices, not prescribed values from Apple or Google.

Browser previews are not Gmail or Apple Mail compatibility tests. Before shipping,
check both clients on phones, including larger text, dark mode, long summaries,
long channel names, localized content, and the largest expected digest.
