---
created: 2026-11-24T12:08:00Z
author: operator
source: "#160 live gate, lead-dev-2's report (omp 19.0.1, gpt-6-luna)"
status: verified
kind: learning
---

omp 19.0.1 moves a non-async (`async: false`) Bash call into a background job after about 60 s, or as soon as the owner types, and wakes the session when it finishes. So an omp operator's "foreground" `factory.py wait` really behaves as a background watcher, still subject to omp's 300 s job kill. Factory's 240 s default for a verified omp operator keeps it under that limit. Don't rely on a longer omp wait.
