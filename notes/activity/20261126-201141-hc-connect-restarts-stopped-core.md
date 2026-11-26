---
created: 2026-11-26T20:12:00Z
author: operator (from @lead-dev-2's report)
source: "#165, PR #176"
status: verified
kind: learning
---

# hc connect restarts a stopped core; only a dropped client cancels a held send

- `hc connect`, and anything else that sends `hello`, starts a stopped core again on the same socket with the same core id. To confirm a core is stopped, make a raw `AF_UNIX` connect to its socket; never ask `hc`.
- A `terminal.send` that `hc` is holding for `draft_wait` is cancelled only when its client connection drops (for example SIGKILL of the `hc connect` process). EOF on the client's stdin does not cancel it: the message is still typed when the hold ends.

Evidence: `docs/qualification/hc-checks.md` (LC-1), `tests/live.py` `Core.stop`.
