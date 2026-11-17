---
created: 2026-11-17T19:15:38Z
author: operator
source: "#18, commit 7abc8e9; probes on herdr 0.9.5"
status: verified
kind: learning
---

A herdr agent prompt whose text ends on a word starting with @ or $ (even "@pm.") can stay unsent while herdr reports agent_prompted. Codex 0.169.0, OMP 18.20.0 and OpenCode 2.2.4 complete the word to a file path or skill and the Enter goes to the picker; Claude Code 2.1.325 and pi 1.5.2 submit normally.

- ateam.py deliver() adds a final newline, which prevents it for everything a-team sends.
- Direct herdr agent prompt sends rely on the rule: never end a message on an @ or $ word.
- A second prompt into a stuck composer is appended to the mangled draft. Never clear a member's composer with keys: Ctrl+C exits the harness (it killed @reviewer once).
