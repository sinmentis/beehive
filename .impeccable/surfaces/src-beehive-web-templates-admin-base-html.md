---
version: 1
slug: "src-beehive-web-templates-admin-base-html"
primary_target: "src/beehive/web/templates/admin_base.html"
related_targets: ["src/beehive/web/templates/admin_settings.html","src/beehive/web/templates/admin_edit_channel.html","src/beehive/web/static/admin.css"]
---

## Scope and mode

Every signed-in page under `/admin/` plus the admin login. Operate mode.

## Audience, job and task

The Owner, on desktop, visiting rarely and only when something needs changing: check that fetching and delivery are healthy, add or fix a source, tune a channel, manage email groups, switch model or language, review or undo recent actions. Each page must read correctly without remembering how it worked last time. Phone use must work but is secondary.

## Structure

Four numbered chapters in a left contents rail: 1 频道 (channels), 2 邮件组 (email groups, including the default recipient), 3 全局设置 (language, model, featured window), 4 系统 (health, activity log). Old tab names `ai` and `delivery` still resolve. Amber counts on the rail mark where attention is needed; each problem is counted once, in the chapter where it is fixed.

## Direction

Datasheet, dark (seed 9e52a516, the model's pick, chosen by the Owner with "make it dark"). Forms are parameter tables (参数 | 设置 | 说明), lists are banded tables under a heavy ink rule, state is a shape plus a word, callouts are boxed 说明 / 注意 / 警告 notes. Colour only where the Owner is needed.

## Memorable moment

Unsaved parameter rows are tagged 已修改 and counted in the sticky save bar; destructive rows open in place into a typed confirmation that names what will be deleted and that it can be undone for 7 days.

## Constraints

No text may overflow its box at 1440, 1024 or 390 px. Fields for values of any length (URLs, queries, addresses, subjects) use `ui.line_field`, a one-line textarea that wraps and grows; short fields stay inputs. In tables a unit (a count with its word, a day with its time, an address) never splits: use `.nw`, `ui.units` or `ui.keep`, and `ui.url_path` so URLs break before a slash. Chinese UI says 频道 and 信源, never Channel. CSP is `default-src 'self'`, so fonts are self-hosted and there is no inline script; templates carry no inline styles.

## Open decisions

None recorded.
