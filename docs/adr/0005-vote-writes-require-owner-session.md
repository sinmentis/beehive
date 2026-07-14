# Vote/read-state writes require an owner session (long-lived cookie), not per-action re-auth

An early security review of the original design found that it put
Vote and read/unread-marking controls directly on the fully public Dashboard/Channel-detail pages
(per ADR-0003) with no protection on their write endpoints. Since Votes feed directly into the AI's
few-shot ranking prompt (ADR-0001) and are mutable (latest overwrites), an unauthenticated public
write endpoint would let anyone — or a bot scanning the internet-reachable host — cast or overwrite
Votes and silently corrupt the owner's ranking signal. This is an integrity attack on the product's
core mechanism, not a cosmetic issue.

Two ways to close this were considered: (a) require the same admin session as the config panel for
every vote/read-toggle, or (b) the same, but with a long-lived session so the owner doesn't have to
re-enter the password every time they want to vote during normal daily reading. Chose (b): the
owner authenticates once (the same password login ADR-0003 already established for the admin
panel), and that session persists across normal browsing/voting for an extended period, rather than
expiring aggressively. This keeps daily-use friction close to zero while still requiring that
*mutations* — unlike *views* — come from an authenticated owner session.

This does not reopen ADR-0003: the Dashboard and Channel drill-down remain fully public for
viewing. It only extends "protect mutations, not views" from the
config panel to cover Votes and read-state as well, since both are mutations by the same owner-only
logic — there is exactly one legitimate voter.
