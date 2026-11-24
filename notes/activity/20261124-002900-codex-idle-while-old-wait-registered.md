---
created: 2026-11-24T00:29:00Z
author: operator
source: "#148, lead-dev's live acceptance on herdr 0.9.5 / Codex 0.171.0 (PR #151)"
status: verified
kind: learning
---

After an Enter steer, herdr 0.9.5 can report a Codex 0.171.0 operator as idle while its previous foreground `wait` process still holds the watcher registration. When diagnosing a missed handoff after an owner reply, check the process and the original tool handle, not just herdr's status.
