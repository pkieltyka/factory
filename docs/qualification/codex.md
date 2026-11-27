# Codex qualification (Phase 8, #173)

**Status: not qualified yet.** Codex is under test as operator and as member; see "Unresolved" in each role's table.

- **Tested:** codex-cli 0.172.0, model gpt-6.2-astra, on hc 0.1.20 and rodeo 0.2.1, Linux, 2026-11-27. The partner in each team is Claude Code.
- **Configured mode:** the full preset (`--dangerously-bypass-approvals-and-sandbox`). Case 9 uses the guarded preset.
- **Session strategy:** `session_env = CODEX_THREAD_ID`, `session_follow = false`. LC-12 shows the child's thread ID differs from the parent's, so following stays off.
- **Mid-turn, history:** before 0ace3a9 a message to a working Codex member steered its current turn: case 4 mid-turn's steering pass at a65a1fe, and the two-senders failure at a65a1fe, where one of two reports in one turn was ignored. As operator, the steering pass at 4954e2f is kept as history too. Factory now types to a working Codex only between turns, in both roles: from 539acb1 as operator, from 0ace3a9 as member. The between-turns member runs pass: case 4 at 5fb3fb9 (both rows) and two-senders at 5092e01. A Claude Code member control at 5fb3fb9 still took its message in the working turn.

## Findings

- **Case 8 dead-member found a keeper bug, fixed in 13e684d.** A message queued `gone` for a member whose terminal ended was claimed for the replacement after its brief. hc refused it with "t4 is still starting: its output has not settled", and the keeper recorded `unconfirmed`, so the message was never typed.
  - hc 0.1.20 settles before it types; @lead-dev read this in its `input.rs`. The keeper now re-queues exactly that refusal as `waiting` and sends the message again.
  - @lead-dev's traced reproduction and the 4954e2f rerun both pass: sending, queued waiting, sending, delivered to t4, acted on once.
- **Blocked (case 5, member): not run.** A native Codex in the full preset never reported `blocked`.
- **member-switch at 670dd76 did not switch.** `/new` went in a say. From 4954e2f the owner types it through rodeo.
- **Installed skill (the owner's rule):** QA agents get every HERDR_* variable dropped, a failing herdr stub first on PATH, and the scratch FACTORY.md naming the candidate script. Where a harness has a launch switch, user skill discovery is off: --no-skills for pi and omp, --disable-slash-commands for Claude Code (except in case 6, which needs /clear), and a per-path skills.config disable for Codex. For those harnesses a run fails when an installed factory.py is run: a shell call in the run's transcript that has a result invokes it, as `python3 PATH` or as the program itself. A path given only as an argument (ls, cat), seen in a call's output (a search, say), in a call with no result, or on a QA screen is recorded as an observation (@operator, from pi member case 8 at a198b9c). A run is only recorded for OpenCode and agy, which have no switch, and for Claude Code in case 6, which starts without its switch so that /clear works (@operator approved, pending the owner's confirmation).
- **Between turns, now in both roles (keeper.md §9):** @operator extended the rule to Codex members after case 7 two-senders at a65a1fe: two reports submitted into one working turn of a Codex member got one action, though Factory delivered both. The earlier member results (that two-senders failure, case 4 mid-turn's steering pass at a65a1fe, and owner-interrupt's not_run) stay below as the record of the old behaviour. On the changed build (@lead-dev):
  - **Case 4 at 5fb3fb9, both rows pass.** In mid-turn, #4 and #5 stayed off-screen in t3's working turn 2. After the original task finished, they were delivered in turns 3 and 4 and acted on once each, in committed order. In both Esc trials #4 was queued busy and off-screen just before Esc. It acted once, 6.25 s after Esc by itself, or 17.51 s after Esc behind the owner's exact line.
  - **Two-senders at 5092e01 passes.** Operator setup #5 was delivered, with the member done and the receipt empty. Operator #6 started turn 3. Reviewer #7 waited queued busy, then started turn 4. Each was acted on once, in committed order.
  - **The steering control at 5fb3fb9 passes.** A working Claude Code member (app claude-code) got #4 in its original receipt turn 1, bypassing the Codex rule. It finished the original action, then acted on #4 once.
- **Member two-senders at 5fb3fb9 (@lead-dev): fail, recorded. Delivery between turns is proven; the model declined an unassigned task.** Reviewer #5 was delivered to t3 in turn 2 (started). Operator #6 was queued busy, then delivered to t3 in turn 3 (started). Both tagged inputs are whole in the native transcript, in separate turns. For #5 the member replied "The receipt edit needs an assignment from @operator. I'll let @reviewer know." and took no receipt action. #6's action ran once. @architect-2 and @operator: no product or assignment-rule change. From 898220d, the trial's setup has @operator assign both receipt steps first, without their tokens, and waits for that message to be delivered and settled with the receipt empty. The two independent sends and the once-each, committed-order and turn checks are unchanged. The rerun at 5092e01 passes (above).
- **Between turns as operator (keeper.md §9, from 539acb1):** in two upgrade trials a report typed into a working Codex operator's turn was submitted whole and ignored. Factory now holds messages to a working Codex operator as queued (busy) and types them when its turn ends, using hc's if: idle guard when it is idle. omp's case 7 upgrade at d1033a1 passes on this path. As operator in busy mode, case 4 passes both rows at 9cf44d9 (@lead-dev). In mid-turn, #4 was busy and #5 waiting, neither was on screen in working turn 2, and both were delivered to t1 in receipt turns 3 and 4, each acted on once. In both Esc trials, the snapshot before Esc shows t1 working, app codex, #4 queued (busy) and its marker absent. Case 3 passes both rows at 5caa350.
- **Case 8 as operator:** operator-gone and same-pane pass at db85120. quit-during-hold, measurement only: the first Ctrl+C clears the owner's draft, which ends hc's hold, and the held message is then typed, so a keyboard quit cannot happen during a hold.
- **Case 7 as operator:** at 9cf44d9, suffix, multiline, two-senders and held-quiet pass. The upgrade trial has the requeue history (#3 sending → queued (busy) → sending → delivered in turn 3, started), but that run predates the delivery facts the judge now needs. The rerun at 5caa350 passes. pending-restart is not_run: a Codex operator could not be made to report blocked, the same limit as case 5.
- **Open rows:** operator 7 pending-restart, member 5 blocked and member 7 pending-restart. In the full preset, Codex was never made to report `blocked`, which all three need. Every other row in both roles has a current pass.

<!-- qualify report: generated, do not edit -->

Generated by `tests/live.py report` from 111 real runs. A result marked unproved passed under an older driver's weaker check and needs a run at a current head.

## As operator

- Harness: codex-cli 0.172.0, model gpt-6.2-astra
- Runs: 068b30d (Factory 31d3e84d827b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 0c020a5 (Factory 1d42b23d7c6b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 3321bfc (Factory 6aea043fab51, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 5caa350 (Factory 1d42b23d7c6b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 9cf44d9 (Factory 1d42b23d7c6b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); c09143b (Factory 31d3e84d827b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); db85120 (Factory 31d3e84d827b, hc 0.1.20, rodeo 0.2.1, 2026-11-27)

| Case | Subcase | Result | Observed or reason | Run |
|---|---|---|---|---|
| 1 | states | pass |  | 068b30d |
| 1 | startup-dialog | pass | "Trust and continue" was shown, answered and the start finished | 068b30d |
| 2 | idle-report | pass | token appended by the operator; partner claude | c09143b |
| 2 | operator-commands | pass | partner claude | c09143b |
| 3 | draft | pass | partner claude | 5caa350 |
| 3 | draft-cleared | pass | report acted on 14.4 s after the draft was deleted; partner claude | 5caa350 |
| 4 | mid-turn | pass | held by Factory until the turn ended, then both acted on once, in order; partner claude | 9cf44d9 |
| 4 | owner-interrupt | pass | the held message ran by itself / the owner's Enter; original task cancelled by the interrupt; held message acted 7.0 s / 18.76 s after Esc | 9cf44d9 |
| 5 | blocked | pass | partner claude | 068b30d |
| 6 | operator-switch | pass |  | 0c020a5 |
| 7 | suffix | pass |  | 5caa350 |
| 7 | multiline | pass |  | 5caa350 |
| 7 | two-senders | pass | partner claude | 5caa350 |
| 7 | pending-restart | not_run | the operator never reported blocked (last done) | 5caa350 |
| 7 | held-quiet | pass | the held note ended dropped; partner claude | 5caa350 |
| 7 | upgrade | pass | partner claude | 5caa350 |
| 8 | operator-gone | pass |  | db85120 |
| 8 | same-pane | pass |  | db85120 |
| 8 | quit-during-hold | not_run | the harness was not seen to exit; partner claude | 3321bfc |
| 10 | idle-latency | pass | median 1.09 s of 3 warm says; 0 over SAY_WAIT (10 s) | 068b30d |
| 10 | idle-latency-times | measured | [1.09, 1.3, 0.69] | 068b30d |
| 10 | held-sender-exit | pass | say returned after 10.09 s with the send sending; partner claude | 068b30d |
| 10 | crash | pass | 1 claim(s), final unconfirmed, the held text was acted on 0 time(s); partner claude | 068b30d |

Unresolved as operator: 7 pending-restart.

Superseded results as operator:

- 1 states: fail at ac07dd0, superseded by the pass above
- 2 operator-commands: fail at ac07dd0, superseded by the pass above
- 4 owner-interrupt: fail at 4954e2f, superseded by the pass above

## As member

- Harness: codex-cli 0.172.0, model gpt-6.2-astra
- Runs: 0c020a5 (Factory 1d42b23d7c6b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 5092e01 (Factory 29f7cecc24d1, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 5fb3fb9 (Factory 29f7cecc24d1, hc 0.1.20, rodeo 0.2.1, 2026-11-27); a65a1fe (Factory 1d42b23d7c6b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); cf4ec8f (Factory 20f55e56f380, hc 0.1.20, rodeo 0.2.1, 2026-11-27)

| Case | Subcase | Result | Observed or reason | Run |
|---|---|---|---|---|
| 1 | states | pass |  | a65a1fe |
| 1 | startup-dialog | pass | "Trust and continue" was shown, answered and the start finished | a65a1fe |
| 2 | idle-report | pass | token appended by the operator; partner claude | a65a1fe |
| 3 | draft | pass | partner claude | a65a1fe |
| 4 | mid-turn | pass | held by Factory until the turn ended, then both acted on once, in order; partner claude | 5fb3fb9 |
| 4 | owner-interrupt | pass | the held message ran by itself / the owner's Enter; original task cancelled by the interrupt; held message acted 6.25 s / 17.51 s after Esc | 5fb3fb9 |
| 5 | blocked | not_run | @lead-dev never reported blocked (last working); a prompt could not be elicited in this mode | a65a1fe |
| 6 | resume | pass | recalled the word | 0c020a5 |
| 6 | member-switch | pass | recalled the first word | 0c020a5 |
| 7 | suffix | pass |  | a65a1fe |
| 7 | multiline | pass |  | a65a1fe |
| 7 | two-senders | pass | partner claude | 5092e01 |
| 7 | pending-restart | not_run | the lead-dev never reported blocked (last done) | a65a1fe |
| 7 | held-quiet | pass | the held note ended delivered; partner claude | a65a1fe |
| 7 | upgrade | pass | partner claude | a65a1fe |
| 8 | dead-member | pass |  | cf4ec8f |
| 8 | unconfirmed-brief | pass | partner claude | cf4ec8f |
| 8 | quit-during-hold | not_run | a member's pane closes when its harness ends: there is no sink to read | cf4ec8f |
| 9 | guarded | pass | partner claude | cf4ec8f |
| 10 | idle-latency | pass | median 0.89 s of 3 warm says; 0 over SAY_WAIT (10 s) | cf4ec8f |
| 10 | idle-latency-times | measured | [1.09, 0.89, 0.49] | cf4ec8f |
| 10 | held-sender-exit | pass | say returned after 10.08 s with the send sending; partner claude | cf4ec8f |
| 10 | crash | pass | 1 claim(s), final unconfirmed, the held text was acted on 0 time(s); partner claude | cf4ec8f |

Unresolved as member: 5 blocked, 7 pending-restart.

Superseded results as member:

- 3 draft: fail at 670dd76, superseded by the pass above
- 6 resume: fail at 50faa5f, superseded by the pass above
- 6 member-switch: fail at 4954e2f, superseded by the pass above
- 6 member-switch: fail at 670dd76, superseded by the pass above
- 7 two-senders: fail at a65a1fe, superseded by the pass above
- 7 two-senders: fail at 5fb3fb9, superseded by the pass above
- 8 dead-member: fail at 670dd76, superseded by the pass above

<!-- end of generated report -->
