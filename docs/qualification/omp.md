# omp qualification (Phase 8, #173)

**Status: not qualified yet.** omp is covered as **member only** this phase.

- **Tested:** omp/19.2.1, model openai-codex/gpt-6-astra (FACTORY_QA_MODEL_OMP), on hc 0.1.20 and rodeo 0.2.1, Linux, 2026-11-27. The operator is Codex. Later runs use the real HOME (@operator's decision). Claude Code and Codex trust entries are listed.
- **Installed skill (the owner's rule):** QA agents get every HERDR_* variable dropped, a failing herdr stub first on PATH, and the scratch FACTORY.md naming the candidate script. Where a harness has a launch switch, user skill discovery is off: --no-skills for pi and omp, --disable-slash-commands for Claude Code (except in case 6, which needs /clear), and a per-path skills.config disable for Codex. For those harnesses a run fails when an installed factory.py is run: a shell call in the run's transcript that has a result invokes it, as `python3 PATH` or as the program itself. A path given only as an argument (ls, cat), seen in a call's output (a search, say), in a call with no result, or on a QA screen is recorded as an observation (@operator, from pi member case 8 at a198b9c). A run is only recorded for OpenCode and agy, which have no switch, and for Claude Code in case 6, which starts without its switch so that /clear works (@operator approved, pending the owner's confirmation).
- **Mid-turn, history:** before 8f40084, a message to a working omp steered its current turn, and the reopen condition was "a missed new message" (#173, the same wording as pi). Operator case 7 at 1be599e met that condition.
- **Between turns, in both roles (keeper.md §9, #175):** from this head Factory keeps messages to a working omp `queued (busy)` and types them when its turn ends (@operator: the reopen condition was met).
  - **Case 4 mid-turn as member at a198b9c (@lead-dev): fail, recorded.** Both tagged prompts reached omp. omp backgrounded the waiting command to handle #4, appended #4 once, then never did the original task's last append; the receipt has only [started, second].
  - **Case 7 as operator at 1be599e: suffix and pending-restart fail, recorded.** Factory delivered every message, but a report arriving while omp worked replaced an earlier, independent one, which was never acted on. In suffix, the first report (ZQALECVMUS) never reached the receipt, while the second and third did. In pending-restart, omp backgrounded its check for ZQGAMDWBPX "to handle an incoming message" and wrote only ZQBRNSGFSS: counts [0, 1]. Bounded traces from omp's own session files: /tmp/factory-qa-175-runs/omp-op-7-suffix-1be599e-trace.txt and omp-op-7-pending-restart-1be599e-trace.txt.
  - **Reruns at 8f40084:**
    - omp member case 4 passes both rows. #4 and #5 stayed off-screen in working turn 1, then went in at turns 2 and 3 on t3, once each in order. Esc: 6.25 s by itself, or 16.51 s behind the owner's line.
    - omp operator case 4 passes both rows.
    - In operator case 7, suffix, multiline, two-senders, pending-restart and held-quiet pass.
    - A Claude Code member control still took #4 in its working turn (between_turns false) and acted once.
  - **Case 7 upgrade as operator at 8f40084: fail, the #178 request 4 limit.** t1 reported `done` at the owner's first key, the claim and the release, so the held #3 was sent unguarded and submitted in the owner's turn 1. #4 came in turn 2. Both report actions ran once, in order, but omp backgrounded the owner's own task to handle #3 and never finished it (draft_exact false).
    - A guard for a `done` or `error` recipient is not possible on hc 0.1.20: `terminal.send`'s `if` accepts only `idle` or `working`. `if=done` and `if=error` return invalid_params, and `if=idle` on a `done` terminal returns status_mismatch, typing nothing (@architect-2, direct JSON-RPC on a private core).
    - Reopen when hc accepts a single-state `done` or `error` guard, rechecked after the draft hold. A `done` to `idle` mismatch could then requeue the same ID and retry.
    - omp's own session for this trial: /home/peter/.omp/agent/sessions/-tmp-factory-qa-nz_abac1-work/2026-11-27T23-23-31-488Z_01a21f52-3720-76e0-a6fb-b23cd1ca2dfb.jsonl.
- **Model:** the table default, openai/gpt-6-astra, resolves to openrouter/openai/gpt-6-astra, and this machine has no openrouter key. openai-codex was rate-limited at times.
- **Resume (case 6):** passes at 3321bfc. LC-6 at db85120 showed that --session-dir with --continue keeps and reopens each member's own conversation, and from 7cbd068 Factory resumes omp that way. member-switch is not_run: omp gives the tool no session variable, so a native switch cannot be proved.
- **Case 7 at d1033a1:** all six subcases pass, upgrade included. With the Codex operator taking Factory's messages between turns (keeper.md §9), #3 went from sending to queued (busy) while the owner's draft ran as a turn, then #3 and #4 each arrived as their own turn and were acted on (@lead-dev's pane trace). The earlier fails were the trial's timing (26f0611) and the Codex operator ignoring text typed into its working turn (db85120). The table shows the pass unproved: that run predates the between-turns facts the driver now records (the operator's state at the owner's key, the claim and the release, each ID's history, and both effects in order). The manual trace is the evidence until a rerun.
- **Still to rerun at the current head:**
  - case 9 guarded. Its fail at 26f0611 had no say from the member, the same compound-line cause as Claude Code's case 9, and the member's say now runs alone.
  - case 4 owner-interrupt. It was inconclusive at 26f0611: the message was not pending at Esc.
- Older batches (f4e5ea1, 26f0611) predate the stricter checks; a newer run's row supersedes theirs.

<!-- qualify report: generated, do not edit -->

Generated by `tests/live.py report` from 121 real runs. A result marked unproved passed under an older driver's weaker check and needs a run at a current head.

## As member

- Harness: omp/19.2.1, model openai-codex/gpt-6-astra
- Runs: 26f0611 (Factory 31d3e84d827b, hc 0.1.20, rodeo 0.2.1, 2026-11-27); 3321bfc (Factory 6aea043fab51, hc 0.1.20, rodeo 0.2.1, 2026-11-27); a198b9c (Factory 29f7cecc24d1, hc 0.1.20, rodeo 0.2.1, 2026-11-27); d1033a1 (Factory 1d42b23d7c6b, hc 0.1.20, rodeo 0.2.1, 2026-11-27)

| Case | Subcase | Result | Observed or reason | Run |
|---|---|---|---|---|
| 1 | states | pass |  | 26f0611 |
| 1 | startup-dialog | pass | no startup dialog declared; none of the known prompts appeared | 26f0611 |
| 2 | idle-report | pass | token appended by the operator; partner codex | 26f0611 |
| 3 | draft | pass | partner codex | 26f0611 |
| 4 | mid-turn | fail | lost or duplicated; partner codex | a198b9c |
| 4 | owner-interrupt | not_run | inconclusive: a trial did not run, or its message was not pending when Esc was pressed | a198b9c |
| 5 | blocked | pass | partner codex | 26f0611 |
| 6 | resume | pass | recalled the word | 3321bfc |
| 6 | member-switch | not_run | the session variable was not seen before and after | 3321bfc |
| 7 | suffix | pass |  | d1033a1 |
| 7 | multiline | pass |  | d1033a1 |
| 7 | two-senders | pass | partner codex | d1033a1 |
| 7 | pending-restart | pass | partner codex | d1033a1 |
| 7 | held-quiet | pass | the held note ended dropped; partner codex | d1033a1 |
| 7 | upgrade | unproved (not yet proved between turns, keeper.md §9) | partner codex | d1033a1 |
| 8 | dead-member | pass |  | 26f0611 |
| 8 | unconfirmed-brief | pass | partner codex | 26f0611 |
| 8 | quit-during-hold | not_run | a member's pane closes when its harness ends: there is no sink to read | 26f0611 |
| 9 | guarded | pass | partner codex | a198b9c |
| 10 | idle-latency | pass | median 0.89 s of 3 warm says; 0 over SAY_WAIT (10 s) | 26f0611 |
| 10 | idle-latency-times | measured | [1.09, 0.88, 0.89] | 26f0611 |
| 10 | held-sender-exit | pass | say returned after 10.09 s with the send sending; partner codex | 26f0611 |
| 10 | crash | pass | 1 claim(s), final unconfirmed, the held text was acted on 0 time(s); partner codex | 26f0611 |

Unresolved as member: 4 mid-turn, 4 owner-interrupt, 6 member-switch, 7 upgrade.

Superseded results as member:

- 2 idle-report: fail at f4e5ea1, superseded by the pass above
- 4 mid-turn: pass at f4e5ea1, superseded by the fail above
- 4 mid-turn: pass at 26f0611, superseded by the fail above
- 4 owner-interrupt: pass at f4e5ea1, superseded by the not_run above
- 6 resume: fail at f4e5ea1, superseded by the pass above
- 6 resume: fail at 26f0611, superseded by the pass above
- 7 suffix: fail at c09143b, superseded by the pass above
- 7 multiline: fail at c09143b, superseded by the pass above
- 7 upgrade: fail at 26f0611, superseded by the pass above
- 7 upgrade: fail at db85120, superseded by the pass above
- 9 guarded: fail at f4e5ea1, superseded by the pass above
- 9 guarded: fail at 26f0611, superseded by the pass above

<!-- end of generated report -->
