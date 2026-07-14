# Failure isolation is per-Source/per-Channel, not all-or-nothing per run

Beehive's collection cycle deliberately isolates failures at the Source and Channel level: a
single Source failing to fetch must not block its sibling Sources or other Channels in the same
cycle. The guiding principle is to ship what you can and not let one small failure take down
everything. A failed Source is just skipped for that cycle, with a warning line surfaced in the
next digest email. An AI/LLM call failure is treated as more severe (an immediate alert email) but
is still scoped to only the one Channel whose call failed — sibling Channels process and display
normally in the same cycle.

Beehive's Channels are independent by design, so a failure in one has no bearing on the
correctness or completeness of another. Finer-grained isolation is therefore both possible and
clearly better than aborting the whole run. Aborting an entire run only makes sense when the run
is a single, indivisible unit of output where partial results are meaningless, which is not the
case here.
