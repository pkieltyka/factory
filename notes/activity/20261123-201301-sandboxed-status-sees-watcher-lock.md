---
created: 2026-11-23T20:13:01Z
author: operator
source: "#136, lead-dev's live acceptance on Claude Code 2.1.330, Linux"
status: verified
kind: learning
---

On Linux, a Claude Code watcher started outside the Bash sandbox is invisible to sandboxed `pgrep` (PID namespace), but a sandboxed `factory.py status` sees its flock on the shared cache directory. The same inode is visible from both sides. Use `status`, not process searches, to check an operator watcher.
