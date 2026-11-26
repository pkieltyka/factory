---
status: draft
---

# Implementation Plan: Factory v2

Each phase is one ticket on the board, built on its own branch, reviewed (fast lane plus `codex review`) and merged through the `FACTORY.md` workflow. Details live in the spec and architecture; this list is the order.

## Phases

- [ ] Phase 1: Live checks. Run LC-1 to LC-12 (architecture.md §10) on a separate `hc` session viewed in rodeo; record each answer and apply its fallback in the architecture, `harnesses.toml` and tests before the code that depends on it is written.
- [ ] Phase 2: Foundation. `hc` client (hc_client.md), shared constants, config with the three new optional keys, the fake `hc` and its contract tests against `hc_fixtures.json`; CI runs the suite (with a pinned `hc` if LC-9 allows). v1 back-port: accept and ignore the new `factory.toml` keys on the `v1` branch.
- [ ] Phase 3: Message log. `Log`, `Box`, append and commit, full validation and `log.check`, damage handling, outcomes, message files (message_log.md, spec §8.6–8.7), with the fault tests.
- [ ] Phase 4: Keeper delivery and `say`. `decide`, per-recipient delivery and send-result mapping, `say`, `inbox`, `ensure_keeper`, heartbeat, single instance, re-exec on version change, the pane log and title (keeper.md §1–4, §7–8); crash and restart tests.
- [ ] Phase 5: Launch, layout and team commands. Harness adapters and session capture (`session_env`, launch flags, resume), readiness and startup dialogs, labels, the `herdr-app` doc with compare-and-set and the team tab with the keeper pane, `check_operator`, and `up`, `down`, `status`, `brief`, `check` (launch_layout.md, cli_skill.md).
- [ ] Phase 6: Notes and autonomy. Member notes by observed episode, the team-quiet rule, the ready check (keeper.md §5–6), with the fake OSC 7501 agent and controlled-clock tests for every keeper row of spec §8a.3.
- [ ] Phase 7: Skill text and cleanup. `SKILL.md`, references, roles, templates (including the workflow in the `FACTORY.md` template and the member prompt), README and docs; remove v1's herdr-only code, docs and tests; installed-copy and docs checks (cli_skill.md).
- [ ] Phase 8: Qualification, first set. Spec §10 cases 1–10 with Claude Code and Codex as operator and all six harnesses as members, plus the cross-team cases; results recorded with versions.
- [ ] Phase 9: End-to-end autonomy. The local run (bare remote, file tracker, injected faults), then the private `pkieltyka/factory-test` run with its board, pull requests and CI; the empty-board run.
- [ ] Phase 10: Qualification, second set, and release. pi, omp, OpenCode and agy as operator; fix what they find; tag v2, and point skills-sync back at `master`.
