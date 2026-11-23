---
created: 2026-11-23T22:50:00Z
author: operator
source: "#140, lead-dev's reading of herdr 0.9.5 src/integration/registry.rs and the #134 F5 capture"
status: verified
kind: learning
---

herdr 0.9.5 can report OpenCode as `needs repair (v13)` even when its main plugin file is already at v13, because `herdr integration status` also checks the companion TUI plugin and its registration. Read the status line; a version check on the main plugin file misses this failure.
