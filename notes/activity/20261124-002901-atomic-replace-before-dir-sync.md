---
created: 2026-11-24T00:29:01Z
author: operator
source: "#148, reviewer's r3 finding and the claim-sync fault test (PR #151)"
status: verified
kind: learning
---

An atomic state-file replacement (`os.replace`) makes the new state visible before the directory fsync that makes it durable. If that sync fails, the state is already published. Never treat the error as "nothing written". Under the same mutex, read back the exact token and sync again. If it fails again, clear only that token, so later writers are not silently blocked by a stale reservation. Inject faults after the replace, not only before it.
