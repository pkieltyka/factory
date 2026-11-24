---
created: 2026-11-24T04:29:00Z
author: operator
source: "#157 live gate incident (lead-dev-2 message 486); tracked as #158"
status: verified
kind: learning
---

An unsandboxed operator told to "stop your watcher" may run `pkill -f "factory.py wait"`. The pattern matches every factory watcher on the machine, so it kills other teams' watchers too, the live team's included. Stop a watcher only by its own task handle or PID. Gate scripts must never ask a scratch operator to stop its watcher.
