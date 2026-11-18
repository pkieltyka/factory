---
created: 2026-11-18T16:09:00Z
author: operator
source: "#21, lead-dev's run of the recipe's read command; gh issue view --help"
status: verified
kind: learning
---

When its output is captured or piped, `gh issue view <n> --comments` prints only the comments, not the title or body. An agent reading an issue uses `gh issue view <n> --json title,body,comments`.
