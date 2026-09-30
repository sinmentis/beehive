# The Owner can make the reading pages private

ADR-0003 made the reading pages public because the content was not sensitive. That still holds for
many installs, but a channel list can reveal what its Owner cares about, such as a job search, a
health topic or a purchase they are planning. Until now the only answer was to put access control
in front of the whole site, which most people running Beehive at home never set up.

The Owner can now choose, in Global settings, who can read the home page, channels, the archive,
search and deep-read briefs: anyone with the link (the default, so ADR-0003 is unchanged unless
the Owner acts), or only the Owner after signing in. Private reading uses the same password and
long-lived session as the admin (ADR-0003, ADR-0005). There are no reader accounts, because there
is still exactly one person reading.

When reading is private, every route on the reading router passes one gate, `require_reader`, so
a new reading page cannot forget it. A signed-out browser goes to the sign-in page with `next` set
to the page it asked for, and returns there after signing in, which keeps email links working. An
htmx request gets 401 with `HX-Redirect`, so the whole page moves to sign-in instead of a fragment
swapping in a form. Health checks, static files and the sign-in page stay reachable. Because
`next` now carries every reader's destination, the return-path check also refuses backslashes and
control characters, which browsers turn into `//another-site`.
