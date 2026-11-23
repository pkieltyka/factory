---
created: 2026-11-23T20:12:00Z
author: operator
source: "#134, lead-dev-2's operator gate on herdr 0.9.5"
status: verified
kind: learning
---

Claude Code and Codex load a user-level skill (`~/.claude/skills/factory`, the Codex equivalent) in preference to a project copy with the same name. pi, omp and OpenCode prefer the project copy (`.pi/.omp/.opencode/skills/factory`). To live-test a branch build on Claude Code or Codex, rename the copy, e.g. folder `factory-<sha>` with `name: factory-<sha>` in its SKILL.md, and invoke `/factory-<sha>`. Otherwise the test silently runs the installed version.
