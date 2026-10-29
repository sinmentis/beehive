---
version: 1
slug: "src-beehive-web-templates-admin-base-html"
primary_target: "src/beehive/web/templates/admin_base.html"
related_targets: ["src/beehive/web/templates/admin_settings.html","src/beehive/web/templates/admin_new_channel.html","src/beehive/web/templates/admin_edit_channel.html","src/beehive/web/templates/admin_add_source.html","src/beehive/web/templates/admin_source_test.html","src/beehive/web/templates/_admin_ranking_result.html","src/beehive/web/templates/_admin_group_fields.html","src/beehive/web/templates/_admin_delivery_status.html","src/beehive/web/templates/admin_new_email_group.html","src/beehive/web/templates/admin_edit_email_group.html","src/beehive/web/templates/admin_email_group_preview.html","src/beehive/web/static/admin.css","src/beehive/web/static/beehive.js"]
---

## Scope and mode

Operate mode. The surface covers signed-in `/admin/` and the admin login. This record updates the finished local Channel, Source, rules and Email tasks; it does not assert that every legacy admin route was reviewed or redesigned. Public reading and the research/watchlist workspace retain their existing design authority.

## Audience, job and task

The Owner visits infrequently to check collection and delivery, connect or repair a Source, refine rules and arrange Email. Complete one task without remembering a configuration maze. Desktop is primary; all five Channel tasks and the current commitment remain available on a 390px phone.

## Structure

The compact rail retains four chapters: Channels, Email groups, Global settings and System, with attention counts beside the place to act. The shared running head retains location and Auckland time.

- **Channel workspace:** heading and operational state, header actions, then persistent Overview, Sources, Organization rules, Email and Settings links. Task navigation sticks at the top; saves stay in the current form's sticky footer. Sources are not below a profile form.
- **Creation:** type and name only, then paused Source → Rules → Email → Review/Start setup. No collection before explicit Start. Review shows source count/pending state, schedule, Email choice, saved rule summary, cutoff and highlight cap. Start/Continue is in the header on desktop and phone.
- **Forms:** stacked task fields with nearby hints/errors. `.page-admin .param` supplies the stack; `.page-admin .params` and task forms stop at 60rem. Existing parameter markup, single-setting saves, selection tables and nested schedule rows remain; the shared legacy three-column parameter layout is not retired.
- **Sources:** preflight tests unsaved connection values without persisting. Results follow Test connection before optional display name; focus/scroll goes to the result unless an invalid field needs attention. Source rows stack into label/value records at viewport 1100px; coarse-pointer operations have 44px targets.
- **Rules:** profile saves affect future ranking. Preview up to five eligible existing items without changing saved rules, items or delivery; compare before/after summaries with integer display scores, rationale and cutoff sample count. Keep Edit rules, Rules used for this run, Save current rules and stale-profile feedback explicit. Python normalizes separate display fields; raw scores drive cutoff and templates do not round.
- **Reprocessing:** a separate disclosure and explicit confirmation apply saved rules to unread Editorial items/current listings. Unsaved-rule notice and disabled action synchronize, including after polling. Cancellation retains applied results; history and delivery exclusions remain.
- **Recovery:** section-local saves preserve unrelated settings. Session-local drafts restore on normal navigation when the baseline matches; conflicts offer restore/discard. Modified rows/counts, before-unload protection and input clearance above sticky footers remain.
- **Email:** website-only, existing shared group or contextual new daily email. Shared-group scope is stated. Live first/next-due forecasts repeat in the save bar. Real HTML preview is sandboxed and non-consuming, with plain text, explicit test-send and delivery diagnostics.
- **System health:** separate Research run and reply rows inspect the latest work in each unarchived session. Older and archived failures stay in history without raising attention. Expired leases outrank current failures; each recovery link opens the affected run history or conversation. Both rows contribute to the System chapter's attention count.

## Direction

CODE-LED extension of the existing dark datasheet; preserved seed `9e52a516`. No approved comp or decision comp and no new seed roll. The built source and final captures are authority.

- **THESIS:** Complete one pipeline task without navigating a configuration maze.
- **OWN-WORLD:** Preserve graphite, Archivo, restrained blue links, amber attention, red danger and the Beehive mark. Use readable forms and purposeful data tables, not decorative cards.
- **STORY:** Connect sources, inspect results, refine rules and arrange delivery. Changes affect future ranking unless the Owner explicitly requests reprocessing. Preserve history and recovery.
- **FIRST VIEWPORT:** Compact rail, channel heading, operational status, persistent task navigation and the current task's primary action. Sources never hide below a profile form.
- **FORM:** The approved channel workspace inside the existing dark identity; no new visual-world roll. Preview-before-apply is its signature interaction. Preserved seed `9e52a516`.
- **FINISH:** Review, verdict, design documentation and raster provenance, scoped as recorded below.

## Memorable moment

Preview before apply: inspect connection values or a rule sample without saving the Source or changing saved rules, items or delivery, then save future rules or explicitly confirm reprocessing. Modified rows remain counted. Destructive removal still names its impact, asks for typed confirmation and offers the supported seven-day undo.

## Constraints

Retain graphite `#0A0C0E`/`#111417`, self-hosted variable Archivo with CJK fallback, blue links/focus, amber attention, red danger and the SVG Beehive mark. State is shape plus word; legends can explain all states on a healthy page. No decorative cards or new raster assets. The embedded email document is output, not a new admin visual world.

Shared 24px headings, 16px task heads, 15px body/fields and 13px hints remain. Admin buttons are at least 36px (small 32px); phone header actions/task links and coarse-pointer controls use 44px. Facts and comparisons stack at 700px; the shared rail becomes a top bar at 880px. Keep long values wrapping, hints near controls and all five tasks visible at 390px. Free-length values use `ui.line_field`; the rule editor scrolls within 12–28rem. Keep Chinese product terms localized, self-hosted fonts, CSRF form posts and no inline scripts/styles.

## Evidence and finish

Final synthetic captures in `.impeccable/review/`, rewritten on 2026-10-28 at 15:02 and individually opened/validated:
- Sources: `desktop.png`, `mobile.png`.
- Overview: `overview-desktop.png`, `overview-mobile.png`.
- Compact creation: `new-channel-desktop.png`, `new-channel-mobile.png`.
- Paused review/start: `setup-review-desktop.png`, `setup-review-mobile.png`.
- Rules: `rules-preview-desktop.png`, `rules-preview-mobile.png`, `rules-actions-desktop.png`.
- Delivery: `delivery-desktop.png`, `email-preview-desktop.png`.
- Source validation/preflight: `source-error-mobile.png`, `source-preview-mobile.png`, `source-preview-focus-mobile.png`.

The last capture is the actual focused post-test viewport, not a full-page shot. Blue region outlines show keyboard/fragment focus. Full-page captures can place sticky footers mid-document; this is not a permanent obstruction or layout token. These are QA captures only; no new shipping raster assets or provenance obligations were introduced.

The supplied finish verdict is **ship**, with all eight material corrections resolved, not a blanket review of every admin route. Reported validation: 7,146 integration tests passed before final presentation fixes; 297 admin/localization tests plus Ruff, JS and whitespace checks passed afterward. One detector run emitted five inherited-Jinja line-0 colour advisories contradicted by rendered light-on-graphite headings; this is not a clean-scan claim and the detector was not rerun here.

This records built local source, not a live release. No production access, migration, deployment, commit or push occurred in this documentation pass.

## Open decisions

None for this record. No boundary blocker.

## Admin reliability patch, 2026-10-29

Scoped finish verdict: **ship** for the four admin audit findings. The footer wraps at the existing narrow breakpoint, and brand, chapter and footer controls have at least 44px width and height on coarse pointers. Fine-pointer desktop density and the reading shell's More menu are unchanged. Research health now distinguishes current run/reply problems from retained history.

The synthetic captures in `.impeccable/review/admin-fixes/` were opened and inspected. Component-level bounds cover German at 320px and 390px, English at 320px, Chinese at 390px, German landscape at 844px and desktop at 1440px, including fine/coarse pointer comparisons. All nine global actions remain visible without rail scrolling, keyboard focus reaches Sign out, and all five Channel tasks and Add source remain visible in the 320px German workspace. The expired-chat capture shows a healthy recovered run alongside an expired reply and its conversation link. The initial measurement also counted intentionally hidden section anchors; confirmation excluded only those anchors, not hidden global actions. No broader redesign or full-device compatibility claim is made.
