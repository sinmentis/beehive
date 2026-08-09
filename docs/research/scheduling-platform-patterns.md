# Scheduling Platform Patterns

Primary-source research on user-configurable recurring jobs, with recommendations for Beehive
Channel fetching.

Date: 2026-08-09

## Executive summary

Mainstream schedulers separate three concerns:

1. **Schedule intent**: the calendar slot or stable interval when work should start.
2. **Execution policy**: retry, catch-up, overlap, pause, and failure handling.
3. **Execution state**: last scheduled slot, last attempt, and last successful completion.

Calendar schedules such as "daily at 07:00 Pacific/Auckland" are generally anchored to wall-clock
slots. Interval schedules are anchored to a stable start time or phase. The next schedule is not
normally calculated from the previous successful completion because that makes latency and failures
change the schedule.

Timing is still best-effort. Mature systems publish a tolerance, use at-least-once delivery where
appropriate, and require idempotent jobs. They also make missed-run and overlap behavior explicit.

Beehive should reuse the calendar scheduling model already used by Email Groups, keep interval mode
for frequent monitoring, and stop using `last_fetch_at` as the schedule anchor.

## Platform comparison

| Platform | User-facing schedule | Missed-run policy | Overlap and retry | Timing semantics |
|---|---|---|---|---|
| AWS EventBridge Scheduler | Calendar cron, fixed rate, and one-time schedules; per-schedule IANA timezone | Retry age and attempt limits rather than implicit catch-up | Bounded exponential retries; optional flexible execution window | 60-second precision; at-least-once delivery |
| Google Cloud Scheduler | Cron or human-readable schedules such as daily at a time; per-job timezone | Failed runs follow an explicit retry configuration | No simultaneous outstanding executions; bounded backoff | At-least-once delivery; duplicate execution is possible |
| Azure Logic Apps | Interval/frequency plus explicit days, hours, and minutes | Recurrence drops missed slots; Sliding Window catches them up | Workflow and connector retry policies | Microsoft explicitly warns that last-run-based recurrence can drift |
| Azure Functions Timer Trigger | Cron-like calendar expression or duration | Persisted occurrence monitoring can detect late runs | Function retry policies | Exposes previous, next, and past-due schedule state |
| Kubernetes CronJob | Cron schedule with explicit timezone | `startingDeadlineSeconds` bounds late starts | `Allow`, `Forbid`, or `Replace` concurrency | Approximate execution; duplicate or missed Jobs remain possible |
| Apache Airflow | Cron, duration, presets, or custom Timetables | `catchup` and Backfill are explicit | Active-run limits and task retries | Calendar schedules are based on data intervals, not last success |
| Temporal Schedules | Calendar and epoch-aligned interval specifications | Bounded Catchup Window and explicit Backfill | Rich overlap policies and workflow retries | Actions generally start close to the intended slot |
| GitHub Actions | Cron with optional timezone | No catch-up; delayed or dropped runs are possible | Workflow concurrency and job-level retry logic | Explicitly best-effort under load |
| Zapier | Every hour/day/week/month plus time-of-day controls | Not exposed as a catch-up model | Platform-managed | Runs within a few minutes rather than at an exact minute |
| n8n | Friendly interval controls, clock offsets, multiple rules, or cron | No documented catch-up contract | Workflow-level behavior | Interval schedules can re-anchor when a workflow is published |

## Common design patterns

### Calendar first, interval second

Infrastructure platforms expose both calendar and interval schedules. End-user automation products
usually present them as:

- Every day at a chosen time.
- Selected weekdays at a chosen time.
- Every N hours, optionally at a chosen minute.
- An advanced cron expression only when necessary.

Azure Logic Apps documents the problem with a recurrence based only on the previous run: execution
latency can make future start times drift. Its recommended remedy is to specify the intended hours
and minutes.

### Schedule slots are separate from successful completion

Mature schedulers preserve the intended scheduled timestamp independently of execution outcome:

- Google Cloud Scheduler sends the original scheduled time with the request.
- Kubernetes annotates Jobs with their intended CronJob slot.
- Airflow assigns each run a logical date and data interval.
- Temporal exposes the most recent successful completion separately from the Schedule specification.

This separation lets a failed or slow run affect status and retry behavior without moving every
future calendar slot.

### Polling is an implementation detail

Some schedulers poll internally, but the polling interval is much finer than the smallest
user-configurable schedule:

- Kubernetes checks CronJobs frequently.
- EventBridge documents minute-level precision.
- systemd timers default to a one-minute accuracy window.

A three-hour polling loop for a three-hour minimum schedule exposes the implementation granularity
directly to the product. A near miss can delay execution by a full period.

### Missed runs need an explicit policy

Mainstream policies include:

- Skip missed slots.
- Run only the latest missed slot.
- Catch up every missed slot.
- Catch up only within a bounded window.
- Refuse a large backlog and raise an operational error.

For a feed snapshot, replaying every missed slot provides little value because historical snapshots
cannot be reconstructed. Latest-slot-only catch-up is the more appropriate default.

### Retry must not mutate the schedule

Retry configuration is normally independent of recurrence:

- Maximum attempts.
- Maximum event age.
- Exponential backoff.
- Dead-letter handling or pause-on-failure.

Retries should stop when they would collide with the next regular slot. The next calendar slot
remains unchanged.

### Overlap policy is first-class

Kubernetes, Temporal, Airflow, and Google Cloud all define what happens when a previous run is still
active. Common choices are:

- Allow concurrent runs.
- Skip the new run.
- Queue one or all runs.
- Replace or cancel the previous run.

Beehive's mutable snapshot reconciliation should use a serial policy such as **Skip/Forbid**.
Concurrent snapshots could incorrectly mark listings inactive.

### Timezone and daylight-saving behavior must be visible

Calendar schedules use an IANA timezone and require a documented daylight-saving time policy.
Interval schedules are durations and should not be presented as equivalent to wall-clock "daily."

Common rules are:

- A repeated fall-back time runs once.
- A non-existent spring-forward time is skipped or advanced to the next valid time.
- UTC remains available for schedules that must avoid daylight-saving changes.

### Scheduling needs product surfaces

Mature products show:

- The interpreted schedule and timezone.
- The next intended run.
- The last intended slot and actual start time.
- Last success and last failure.
- Recent run history.
- Pause/resume state.
- Manual run and, where meaningful, backfill controls.

## Assessment of Beehive

Beehive currently has two scheduling layers:

- `deploy/quadlet/beehive-fetch.timer` starts the collector every three hours.
- `source_is_due()` in `src/beehive/scheduling.py` compares
  `last_fetch_at + fetch_interval_hours` with the current time.

The Channel editor exposes 3-hour, 6-hour, and daily intervals. Because `last_fetch_at` is written
only after a successful fetch, it currently acts as both:

- The last-success freshness watermark.
- The anchor for the next scheduled attempt.

This creates several problems:

- "Daily" means 24 hours after the previous success, not daily at a chosen clock time.
- Execution latency can move future runs.
- Manual runs can move the next automatic run.
- Failed fetches implicitly retry on the next three-hour collector wake-up.
- The five-minute grace period reduces near misses but does not remove the underlying coupling.

Beehive already has the better model for Email Groups: interval/calendar modes, timezone, weekdays,
and time-of-day calculations. Channel fetching can reuse this scheduling vocabulary and its
latest-calendar-slot evaluation.

## Recommended Beehive model

### Owner-facing schedule

Offer two explicit modes:

**Calendar**

- Daily at `HH:MM`.
- Selected weekdays at `HH:MM`.
- Optional multiple times per day.
- IANA timezone, defaulting to `Pacific/Auckland`.

**Interval**

- Every 1, 3, 6, 12, or 24 hours.
- A stable start anchor or phase.
- Clear copy that this is elapsed-duration scheduling, not a wall-clock daily schedule.

Do not expose raw cron syntax in the default UI.

### Slot-based due calculation

Use the intended slot as the schedule anchor:

```text
due = latest_scheduled_slot(now, schedule) > last_dispatched_slot
```

Do not calculate the next slot from the previous successful completion.

### Separate timestamps

Keep distinct state:

| State | Meaning |
|---|---|
| `last_scheduled_slot_at` | Intended slot most recently dispatched, regardless of outcome |
| `last_attempt_at` | Actual most recent attempt time |
| `last_fetch_at` | Most recent successful completion and freshness watermark |
| `last_fetch_status` | Current outcome |
| `last_fetch_error` | Current failure detail |

### Scheduler substrate

- Run a lightweight due-check every 5–15 minutes, or use a long-running scheduler that sleeps until
  the next due slot.
- Keep `Persistent=true` so host downtime can be detected.
- Add a small stable random offset to avoid every source hitting upstream sites exactly on the hour.
- Dispatch only Channels that are due; the frequent check should not perform network work itself.

### Execution policies

- **Catch-up:** run only the latest missed slot, within a bounded window.
- **Retry:** bounded exponential backoff, independent of the next regular slot.
- **Overlap:** skip a slot while the same Channel is still running.
- **Pause:** do not replay slots missed while paused.
- **Manual run:** run immediately without changing future calendar slots.

### UI and observability

Show:

- `Daily at 07:00 Pacific/Auckland`.
- `Next run: 10 Aug 2026, 07:00 NZST`.
- Last scheduled slot, actual start, duration, status, and item counts.
- A clear delayed/missed indicator.
- Recent successful and failed runs.

## Suggested implementation order

1. Add calendar schedule fields to Channels by reusing the Email Group schema and helpers.
2. Add `last_scheduled_slot_at` while preserving `last_fetch_at`.
3. Change due evaluation to slot-based scheduling.
4. Reduce the systemd timer to 15 minutes and add a stable randomized offset.
5. Add explicit catch-up, retry, and overlap policies.
6. Update the Channel editor and next-run display.

## Primary sources

- [AWS EventBridge Scheduler schedule types, timezones, and DST](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html)
- [AWS EventBridge Scheduler flexible time windows](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-schedule-flexible-time-windows.html)
- [AWS EventBridge Scheduler overview and delivery semantics](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html)
- [Google Cloud Scheduler cron, human-readable schedules, timezone, and DST](https://cloud.google.com/scheduler/docs/configuring/cron-job-schedules)
- [Google Cloud Scheduler overview and delivery guarantees](https://cloud.google.com/scheduler/docs/overview)
- [Google Cloud Scheduler retry configuration](https://cloud.google.com/scheduler/docs/configuring/retry-jobs)
- [Azure Logic Apps recurring schedule behavior](https://learn.microsoft.com/en-us/azure/logic-apps/concepts-schedule-automated-recurring-tasks-workflows)
- [Azure Functions Timer Trigger](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-timer)
- [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/)
- [Apache Airflow cron and time intervals](https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/cron.html)
- [Apache Airflow Dag runs, catch-up, and backfill](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dag-run.html)
- [Apache Airflow timezones and DST](https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/timezone.html)
- [Temporal Schedules](https://docs.temporal.io/schedule)
- [GitHub Actions scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [Zapier scheduled workflows](https://help.zapier.com/hc/en-us/articles/8496288648461-Schedule-Zap-workflows-to-run-at-specific-intervals)
- [n8n Schedule Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/)
- [systemd timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)
