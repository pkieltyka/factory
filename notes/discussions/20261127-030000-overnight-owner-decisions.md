---
created: 2026-11-27T03:00:00Z
author: operator
source: owner instruction before an overnight autonomous run (#164)
status: provisional
---

# Overnight run: decisions waiting for the owner

The owner asked the team to finish v2 autonomously overnight and to record here anything that needs the owner. The operator stops before the release step (tagging v2 and pointing skills-sync at master).

## Waiting for the owner

- (none yet)

## Decisions made overnight (FYI)

- #172: leading.md's Autonomous operation follows spec §8a.3 over v1's Autopilot text: a blocked item is recorded, the owner is asked once, and the operator carries on with other independent ready work (a project that wants 'stop at the first blocker' writes it in its FACTORY.md).
- @architect-3 (agy, Gemini 4 Neon) twice finished a turn without returning a requested plan review, stalling Phase 4 and Phase 6; reviews were moved to other architects. Consider swapping its model or harness.
- #173: authenticated qualification runs may write only what a normal harness launch writes for its own session; no global settings, trust lists, credentials or MCP/plugin config changes; throwaway working dirs. First PR may land with a partial real matrix.
- #173: Claude Code and Codex may add per-directory trust/session entries for QA scratch dirs (TMPDIR/factory-qa-*) in their own config; every added entry is listed in the qualification report so you can remove them.
- #174: each factory-test e2e run creates a user-level GitHub project board under pkieltyka, left in place and listed in the run manifest; members run at default 'full' permissions in the throwaway and factory-test repos only.
- Authenticated qualification runs moved from @lead-dev-3 (agy) to @lead-dev (Codex) after ~20 minutes with no results.
- #173 milestone: with Codex as operator, a member's report to an idle operator started a turn that acted on it with no human input (case 2), and the operator's own up/say/status/inbox worked; say to an idle recipient delivered in 0.7-1.3 s (case 10). Owner draft held through rodeo for both Claude Code and Codex operators (case 3).
- #173: mid-turn messages behave as the spec expected: Codex steers the current turn with them, Claude Code queues them for the next turn; both deliver exactly once (case 4).
- #173: Claude Code, owner presses Esc while a mid-turn message is queued: Claude Code keeps the message and runs it as its next turn about 5 s later (one trial; an earlier failed attempt is being explained, plus a check that the owner's typing right after Esc stays whole). No extra mode needed if that holds.
- #173: pi has no working resume (PI_SESSION_ID unset, nothing stored): pi members start fresh after down with a notice; a backlog ticket tracks pi's own session flags.
- #173: pi's 'no resume' limit was withdrawn: the cause was a Factory bug (say saved the unresolved model name), fixed by labelling each member terminal with its resolved model; pi case 6 is being rerun.
- #173: pi resume passes after the fix (case 6, both rows).
- #173: LC-7 passed: agy's session-only --settings trustedWorkspaces avoids its trust dialog without touching the global trust list, so Factory now passes it at every agy launch (spec §6.3).
- #174 milestone: first real end-to-end autonomous run (local leg): a real v2 team planned, approved, tested, integrated and closed all three tickets in ~27 minutes with no human input; a killed member was replaced and finished its item. Only the quiet-then-chase row wasn't exercised (the member reported on time).
- #173 part 4 (#223) merged: a working Codex operator now receives Factory's messages between turns (proved with omp and agy members and Codex operator cases 3, 4 and 7 under a stricter judge). QA installed-skill rule follows the owner's amendment (switch where available, else recorded observation). Remaining #173 reruns continue; Phase 10 (#175) build starting.
- #173: Claude Code member, owner presses Esc right after a mid-turn message was delivered: Claude Code drops the message with the interrupted turn (it stays in Factory's log). Decision: keep mid-turn steering for Claude Code members; leading.md tells the owner to check `inbox --for NAME` after Esc; recorded as a narrow exception for a deliberate owner interrupt, to be reopened if an uninterrupted turn ever loses a message.
- #173: Codex member case 7 showed the Codex model ignoring one of two messages submitted into the same working turn. Operator's decision (on @architect-2's recommendation): between-turns delivery for Codex in both roles; urgent corrections to a working Codex member wait for its turn to end (the owner can still type in its pane).
- #227/#229: v2 renamed to `factory2` on master so the owner can install it beside v1 for QA; [factory2] entry added (uncommitted) to ~/.nix-config/extras/skills.toml; rename back before tagging (#228).
- #175: omp as operator dropped independent reports that arrived while it was working (case 7 suffix and pending-restart). Operator's decision: between-turns delivery for omp in both roles, like Codex. pi keeps mid-turn steering.
- #173: Factory now prints runnable commands with the running script's absolute path (a keeper note's bare `factory.py up` sent an operator searching, and with factory and factory2 both installed a bare name can pick the wrong copy). The QA installed-skill check fails a run only on an actual run of an installed copy; a path seen in output is recorded.
- #175: a large message (29 KB) typed into an idle omp is pasted as an attachment and not submitted; Factory records it delivered with started=false. hc 0.1.20 can't tell Factory whose text sits in the input, so no automatic extra Enter. Operator's decision: the keeper notes an unstarted delivery to the operator (or shows it in status for an operator recipient) so a person presses Enter; omp's limit is documented; a longer paste gap before submit is asked of herdr-core (#178).
