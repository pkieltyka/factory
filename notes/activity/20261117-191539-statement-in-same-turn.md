---
created: 2026-11-17T19:15:38Z
author: operator
source: "#7, Gate 1 runs (fixtures /tmp/ateam-gate1.*), commits 4837a42 (reverted) and d18056d"
status: verified
kind: learning
---

A Claude Code (Opus 5.5) operator doing a cold recovery sends its first messages to members and writes its summary to the user at the end of the same turn. Three runs did this, with and without a cmd_continue.md sentence telling it to state the position first, so wording does not change the order.

- Gate 1 criterion 2 therefore asks for the statement in the same turn, before any member reply is read, and nothing unexplained or contradicted acted on until the user answers.
- Don't spend wording on ordering within one harness turn; test for the outcome instead.
