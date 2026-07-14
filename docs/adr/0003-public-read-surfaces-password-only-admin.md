# Beehive's read surfaces are public; only the admin panel is password-protected

The Dashboard and Channel drill-down pages are fully public, with no identity gateway in front of
them, because this content (AI-summarized public Reddit/news posts) is not sensitive enough to
justify per-request identity gating. Read surfaces may be externally reachable.

Only the admin/config surface (Channel and Source CRUD, editing a Channel's profile text) and the
write endpoints are protected — and only by a simple app-level password, not full user accounts,
since there is exactly one operator. Every admin login attempt, successful or failed, is logged
with timestamp, source IP, and resolved geolocation, retained as full history (not just the most
recent), so brute-force attempts against the password are visible after the fact even though the
UI only surfaces the latest by default.

This is a conscious, content-based choice: the read surfaces stay open while the app itself gates
every mutation behind the password login. ADR-0005 extends the same gate to Votes and read-state.
