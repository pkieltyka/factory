---
created: 2026-11-18T18:43:00Z
author: operator
source: "#58, lead-dev's reproducer and reviewer's verification"
status: verified
kind: learning
---

Patching an attribute on a shared module (`patch.object(factory.time, "sleep")`) changes it for every importer: `subprocess.Popen._wait` also calls `time.sleep` while polling for a child's exit, so a test clock callback fires early. To isolate a clock, replace the module binding in the code under test (`factory.time`) with an object holding the real `monotonic` and the test's `sleep`.
