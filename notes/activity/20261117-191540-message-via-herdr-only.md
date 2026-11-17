---
created: 2026-11-17T19:15:38Z
author: operator
source: "#3; owner instruction 2026-11-17"
status: verified
kind: decision
---

Team messages go only through herdr (ateam.py say, herdr agent prompt, or ateam.py inbox send to an inbox operator), never a harness's own messaging such as Claude Code's SendMessage. SendMessage was held by a permission-mode mismatch and the message was lost. Recorded in SKILL.md and the member prompt.
