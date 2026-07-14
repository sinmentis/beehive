# Vote feedback feeds the ranking prompt as few-shot examples, not a learned summary

Votes (thumbs up/down + optional one-line reason) need to improve future AI ranking accuracy per
Channel over time. Considered three options: (1) inject recent Votes directly into each ranking
prompt as few-shot examples, (2) have the AI periodically distil Vote history into a standalone
"learned preference" summary consumed alongside the owner-written profile, (3) treat Votes as
informational only, left for the owner to notice patterns and manually edit their profile.

Chose (1) for v1: it's the simplest to build and reason about, and every new Vote is available to
the very next ranking cycle rather than waiting on a periodic summarization job.

Accepted limitation: the few-shot list will need a recency cap or representative-sampling strategy
once Votes accumulate over months — option (2) is the natural next step if/when that becomes a
real problem, not attempted now (YAGNI).
