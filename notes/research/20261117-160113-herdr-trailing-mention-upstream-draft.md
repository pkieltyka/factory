---
created: 2026-11-17T16:01:13Z
author: lead-dev (copied by operator)
source: "#18 investigation, /tmp/ateam-18-uoub2noe/upstream-draft.md; owner asked to keep it"
status: provisional
---

Draft upstream report for herdr, not posted; posting is the owner's call. Findings and probe recipe are on #18.

# agent prompt: trailing mention completion mutates payload and consumes Enter

Herdr client/server 0.9.5, protocol 22, Linux. Reproduced in Codex 0.169.0, OMP 18.20.0, OpenCode 2.2.4+45898d0. Not reproduced in Claude Code 2.1.325 or pi 1.5.2 with the same scratch file and prompt.

1. In a scratch directory, create `roles/pm.md` containing harmless text.
2. Start an idle scratch harness in a Herdr pane and name it `probe`.
3. Run `herdr agent prompt probe 'Reply only ACK. Do not use tools. Final addressee @pm.'`.
4. Herdr returns `agent_prompted`, but no turn starts. The composer contains the unsent request with `@pm.` replaced by `roles/pm.md` (Codex) or `@roles/pm.md` (OMP/OpenCode). State/completion sequence stays unchanged.

Expected: a programmatic literal prompt reaches the harness without file-completion mutation or Enter being consumed by its picker. Actual: the harness accepts a completion instead of submitting. This report does not assume that `agent_prompted` promises model-turn acknowledgement; the documented text-plus-Enter transport is insufficient to prevent this completion interaction.

An otherwise identical payload ending with a literal newline prevents the problem in all three affected harnesses. Codex and OpenCode reply; OMP reaches model dispatch with the literal mention intact, but this machine lacks credentials for its selected provider. Codex also stalls on a trailing `$herdr` skill token; a newline prevents that too.

A second prompt appends to the altered draft and submits both, so blind retries can combine distinct instructions. OMP restores the submitted draft after a provider error, making draft visibility alone insufficient evidence of non-submission. Please consider a transport-level way to terminate completion without changing the intended message or blindly adding another Enter/retrying. a-team can append a newline locally, but direct callers remain affected.
