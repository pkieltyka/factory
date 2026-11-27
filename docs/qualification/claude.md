# Claude Code qualification (Phase 8, #173)

**Status: not qualified yet.** Claude Code is under test as operator and as member. Rows are still running or not built; see "Unresolved" in each role's table.

- **Tested:** Claude Code 2.1.332, model claude-fable-5-2, on hc 0.1.20 and rodeo 0.2.1, Linux, 2026-11-27. The partner in each team is Codex.
- **Configured mode:** the full preset (`--permission-mode bypassPermissions`). Case 9 uses the guarded preset (`--permission-mode auto`).
- **Session strategy:** `session_env = CLAUDE_CODE_SESSION_ID`, `session_follow = true` (@operator's decision, spec §9.4). LC-3 passed on its rerun (assigned ID stored and resumed). LC-12 at db85120 shows a sub-agent's ID equals the parent's, so Factory follows a /clear from the member's next say. Case 6 needs a rerun on this build.
- **Mid-turn (case 4), per run:**
  - As operator: queued for the next turn (670dd76).
  - As member: queued for the next turn at 670dd76; steered the current turn at 4954e2f.

- **Installed skill (the owner's rule):** QA agents get every HERDR_* variable dropped, a failing herdr stub first on PATH, and the scratch FACTORY.md naming the candidate script. Where a harness has a launch switch, user skill discovery is off: --no-skills for pi and omp, --disable-slash-commands for Claude Code (except in case 6, which needs /clear), and a per-path skills.config disable for Codex. A sighting of an installed factory.py on a QA screen or in the run's transcript fails the run for those harnesses. It is only recorded for OpenCode and agy, which have no switch.
- **Case 6 at d1033a1 (member):** resume and member-switch pass. After /clear the conversation ID changed. The member's standalone say recorded it (session_follow on), and up resumed the switched conversation, which recalled the second word.
- **Case 8:** as member at 1819b5d, dead-member and unconfirmed-brief pass. brief --send now takes a replacement resting in done, which fixed the earlier fail. As operator at 97afeec, operator-gone and same-pane pass. quit-during-hold, measurement only, did not run: the operator was still working.
- **Case 9 (member):** rerun pending. At 3321bfc the driver's compound say ran inside the owner's sandbox and failed on a read-only log. A plain say from the same guarded member was delivered and restarted the keeper, so no permission change was needed (@architect).
- **Still to rerun at the current head:** the operator rows 2, 5, 6, 7 and 10, and the member rows 2, 4 and 7, which are unproved or not built in the older batches below.

## Findings

- **Owner Esc while a message is queued (case 4, operator), recorded as pass.** These were @lead-dev's focused runs at 670dd76, with a real Codex member saying M1 to a working Claude Code operator. After the owner's Esc through rodeo, Claude Code kept the queued message: it did not return it to the input and did not drop it. It then ran the message by itself as its next turn.
  - Three clean base trials: the action appeared 20.854, 18.353 and 7.350 s after Esc.
  - In a separate owner-draft trial, a line the owner typed straight after Esc stayed whole and unsubmitted while M1 ran, at 5.362 s. After one Enter its exact bytes arrived once.
  - @architect-2 verified the four result files; @operator confirmed pass with this behaviour and no skill-text or keeper change.
  - The held message's action was observed 5 to 21 s after the owner's Esc. These are times to the action, not to the start of the turn.
  - An earlier zero-action attempt (driver at 670dd76) stays unexplained: that driver kept neither the send ID nor the post-Esc screen. The driver now records both (4954e2f).
- **Member owner-interrupt fails at 4954e2f, in the owner-line trial only.** In both trials M1 (#4) was delivered while the member worked and was still pending at Esc.
  - next-delivery held: M1 acted once by itself, 3.0 s after Esc, and the later message once.
  - owner-line: the owner's exact line ran after Enter, but M1's action count stayed 0. @lead-dev's private screen shows M1's exact marker in a submitted, tagged prompt after Esc. So the text reached Claude Code and the model did not act on it; this is an action failure, not a lost delivery.
  - The 670dd76 attempt with zero actions stays unexplained; that driver kept neither the send ID nor the screen.
- **Operator case 8 at 7a90602: the driver's own setup failed, not Factory.** Fixed from the next head; both trials need a rerun.
  - same-pane failed: the second quit came while the operator was still working on the operator-gone messages. The restart command was typed before Claude Code had finished exiting, so it never started again. Its status stayed empty, and Factory queued the next say as "operator not running", as keeper.md specifies.
  - quit-during-hold did not run: the trial's message was delivered rather than held, and Ctrl+C then interrupted the working operator instead of quitting.
  - The driver now waits for a quiet team before each quit and for the shell prompt before a restart. A start that never comes back is recorded as not run.
- **member-switch at 670dd76 did not switch.** `/clear` went in a Factory say as "[from @operator] /clear". From 4954e2f the owner types it through rodeo.

<!-- qualify report: generated, do not edit -->

Generated by `tests/live.py report` from 73 real runs. A result marked unproved passed under an older driver's weaker check and needs a run at a current head.

## As operator

- Harness: 2.1.332 (Claude Code), model claude-fable-5-2
- Runs: 670dd76 (Factory a775c89ae322, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 97afeec (Factory 31d3e84d827b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); c09143b (Factory 31d3e84d827b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); e72f570 (Factory a775c89ae322, hc 0.1.20, rodeo 0.2.1, 2026-11-27)

| Case | Subcase | Result | Observed or reason | Run |
|---|---|---|---|---|
| 1 | states | pass |  | 670dd76 |
| 1 | startup-dialog | unproved (an older driver's weaker check) | declared: ['Yes, I trust this folder', 'Yes, I accept'] | 670dd76 |
| 2 | idle-report | fail | no action from the operator within 300 s; partner codex | c09143b |
| 2 | operator-commands | pass | partner codex | c09143b |
| 3 | draft | pass | partner codex | 670dd76 |
| 3 | draft-cleared | not_run | not built yet | 670dd76 |
| 4 | mid-turn | pass | queued for the next turn; partner codex | 670dd76 |
| 4 | owner-interrupt | pass | Esc left the composer empty; the queued message ran by itself as the next turn, once, in three base trials (20.854 s, 18.353 s, 7.35 s to the observed action after Esc); a line the owner typed straight after Esc stayed whole and arrived exactly once after one Enter (5.362 s to M1's action); partner codex | 670dd76 |
| 5 | blocked | not_run | @lead-dev never reported blocked (last working); a prompt could not be elicited in this mode | e72f570 |
| 6 | operator-switch | unproved (an older driver's weaker check) |  | e72f570 |
| 7 | suffix | pass |  | e72f570 |
| 7 | multiline | pass |  | e72f570 |
| 7 | two-senders | not_run | not built yet | e72f570 |
| 7 | pending-restart | not_run | not built yet | e72f570 |
| 7 | held-quiet | not_run | not built yet | e72f570 |
| 7 | upgrade | not_run | not built yet | e72f570 |
| 8 | operator-gone | pass |  | 97afeec |
| 8 | same-pane | pass |  | 97afeec |
| 8 | quit-during-hold | not_run | never idle after up: the team (not quiet for 8 s within 600 s) | 97afeec |
| 10 | idle-latency | unproved (an older driver's weaker check) |  | e72f570 |
| 10 | idle-latency-times | measured | [0.89, 0.69, 0.68] | e72f570 |
| 10 | held-sender-exit | not_run | not built yet | e72f570 |
| 10 | crash | not_run | not built yet | e72f570 |

Unresolved as operator: 1 startup-dialog, 2 idle-report, 3 draft-cleared, 5 blocked, 6 operator-switch, 7 two-senders, 7 pending-restart, 7 held-quiet, 7 upgrade, 10 idle-latency, 10 held-sender-exit, 10 crash.

Superseded results as operator:

- 1 states: fail at e72f570, superseded by the pass above
- 2 idle-report: pass at e72f570, superseded by the fail above
- 2 idle-report: pass at 670dd76, superseded by the fail above
- 3 draft: fail at e72f570, superseded by the pass above
- 4 owner-interrupt: fail at 670dd76, superseded by the pass above
- 8 same-pane: fail at 7a90602, superseded by the pass above

## As member

- Harness: 2.1.332 (Claude Code), model claude-fable-5-2
- Runs: 068b30d (Factory 31d3e84d827b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 1819b5d (Factory 6aea043fab51, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 4954e2f (Factory dff211090707, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 670dd76 (Factory a775c89ae322, hc 0.1.20, rodeo 0.2.1, 2026-11-27); d1033a1 (Factory 1d42b23d7c6b, hc 0.1.20, rodeo 0.2.1, 2026-11-27)

| Case | Subcase | Result | Observed or reason | Run |
|---|---|---|---|---|
| 1 | states | pass |  | 068b30d |
| 1 | startup-dialog | pass | "Yes, I trust this folder" was shown, answered and the start finished | 068b30d |
| 2 | idle-report | unproved (an older driver's weaker check) | token appended by the operator; partner codex | 670dd76 |
| 3 | draft | pass | partner codex | 670dd76 |
| 4 | mid-turn | pass | steered this turn; partner codex | 4954e2f |
| 4 | owner-interrupt | fail | the held message ran by itself / nothing; original task cancelled by the interrupt; held message acted 3.0 s / None s after Esc | 4954e2f |
| 5 | blocked | pass | partner codex | 068b30d |
| 6 | resume | pass | recalled the word | d1033a1 |
| 6 | member-switch | pass | recalled the second word | d1033a1 |
| 7 | suffix | pass |  | 670dd76 |
| 7 | multiline | pass |  | 670dd76 |
| 7 | two-senders | not_run | not built yet | 670dd76 |
| 7 | pending-restart | not_run | not built yet | 670dd76 |
| 7 | held-quiet | not_run | not built yet | 670dd76 |
| 7 | upgrade | not_run | not built yet | 670dd76 |
| 8 | dead-member | pass |  | 1819b5d |
| 8 | unconfirmed-brief | pass | partner codex | 1819b5d |
| 8 | quit-during-hold | not_run | a member's pane closes when its harness ends: there is no sink to read | 1819b5d |
| 9 | guarded | pass | partner codex | d1033a1 |
| 10 | idle-latency | pass | median 1.29 s of 3 warm says; 0 over SAY_WAIT (10 s) | 068b30d |
| 10 | idle-latency-times | measured | [1.29, 1.29, 0.48] | 068b30d |
| 10 | held-sender-exit | pass | say returned after 10.08 s with the send sending; partner codex | 068b30d |
| 10 | crash | pass | 1 claim(s), final unconfirmed, the held text was acted on 0 time(s); partner codex | 068b30d |

Unresolved as member: 2 idle-report, 4 owner-interrupt, 7 two-senders, 7 pending-restart, 7 held-quiet, 7 upgrade.

Superseded results as member:

- 6 resume: fail at 14ed74a, superseded by the pass above
- 6 member-switch: fail at 670dd76, superseded by the pass above
- 6 member-switch: fail at 14ed74a, superseded by the pass above
- 8 dead-member: fail at 670dd76, superseded by the pass above
- 8 unconfirmed-brief: fail at 97afeec, superseded by the pass above
- 9 guarded: fail at 3321bfc, superseded by the pass above
- 9 guarded: fail at 1819b5d, superseded by the pass above

<!-- end of generated report -->
