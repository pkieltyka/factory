---
created: 2026-11-23T22:20:00Z
author: operator
source: "#139 and #134 live probes; filed as #144"
status: verified
kind: learning
---

A scratch or test operator running `/factory up` follows SKILL.md's setup step and runs `herdr integration install <harness>`. That rewrites global files every other factory on the machine shares, such as `~/.claude/hooks/herdr-agent-state.sh` and `~/.config/opencode/plugins/herdr-agent-state.js`. For live tests, call the exported `factory.py up` directly, or tell the scratch operator not to install or repair integrations and deny the prompt if one appears. Snapshot those files before a probe.
