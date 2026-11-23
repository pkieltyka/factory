---
created: 2026-11-23T21:51:00Z
author: operator
source: "#138, lead-dev-2's live acceptance on Codex 0.171.0 (PR #142)"
status: verified
kind: learning
---

Between its own polls, Codex 0.171.0 briefly shows its last message with no `• Working` line. A pane-screen check that reads a Codex operator once can land on that frame, so one unrecognized read must not change behaviour. Act only on a state that persists. #138 made the composer fallback non-sticky for this reason. See also [[20261123-192100-composer-busy-renderings]].
