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
