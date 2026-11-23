---
created: 2026-11-23T22:22:00Z
author: operator
source: "#139, lead-dev's live probe on herdr 0.9.5"
status: verified
kind: learning
---

For an isolated Factory live test on herdr 0.9.5, set `XDG_CACHE_HOME` when you start the scratch herdr server, not only in the workspace. An override applied only to the workspace reaches the operator but not the members Factory launches in its new team tab, so their `say` looks for a different inbox. See also [[20261123-222000-scratch-operator-rewrites-global-integrations]].
