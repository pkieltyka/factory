---
created: 2026-11-23T19:21:00Z
author: operator
source: "#132, lead-dev's live acceptance on herdr 0.9.5 (PR #135)"
status: verified
kind: learning
---

A watcher that reads a harness's composer to tell "busy, healthy" from "unrecognised" must know every busy rendering, or it wrongly degrades a healthy wait.
- Codex 0.171.0: during an empty poll of a yielded shell command, it shows `• Waiting for background terminal` with a terminal preview (`└ …`), not `• Working (…)`.
- pi 1.6.0: its Bash timer switches format, from seconds to minutes at 60 s and later to hours (`Elapsed 1h 2m 3s`).
Test long-running and resumed states, not only the first seconds of a command.
