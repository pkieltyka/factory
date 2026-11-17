---
created: 2026-11-17T19:18:17Z
author: operator
source: "#23 and #24 QA runs in the qa workspace; evidence /tmp/ateam-qa23-p4tdlsaj/findings*.md and /tmp/ateam24-gates-g5vqyp3t/findings.md (lead-dev)"
status: verified
kind: learning
---

Every harness can receive members' messages through the operator inbox (ateam.py inbox send, ateam.py wait). harnesses.toml operator_wait says how each operator waits. Tested live on 2026-11-17, one fresh test operator per harness.

| Operator | Wakes on a finished background command | operator_wait | Notes |
|---|---|---|---|
| Claude Code 2.1.325 | yes | background | draft in the composer survives |
| OpenCode 2.2.4 | yes | background | two wakes from idle; draft survives |
| OMP 18.20.0 | continues its turn on its own | background | herdr shows it working while it waits; two cycles; accepted an 1810 s tool limit; full 1800 s not run; default OpenRouter model had no key, ran on the pax-vllm Qwen model |
| Codex 0.169.0 | no (a native sub-agent finishing does not wake the parent either) | foreground, 60 s | plain Enter (steering) reached the operator before the wait returned; give the command a long yield (e.g. 65 s) or Codex polls about every 10 s: one model call a minute instead of six |
| pi 1.5.2 | no background commands | foreground, 60 s | plain Enter reached the operator when the wait returned (40 s in the test); shows "Steering:" while pending; one model call per rerun |

- Foreground mode: the operator waits only while a member works; on an idle team wait says so and the operator ends its turn, so the owner is heard at once. While a member works the owner's Enter message waits at most until the current wait returns. Text queued with Tab (Codex) or Alt+Enter (pi) waits for the end of the whole turn: use plain Enter.
- herdr fires no event on the owner's typing; only the screen shows a pending message.
- Members of any harness send into the inbox fine.
- Cheap members (Codex gpt-6-luna, low) rewrite printed fallback commands; keep anything a member must copy short (fixed in ccd4ae8).
- A cheap member may start a ready task it sees in the brief before it is assigned.
