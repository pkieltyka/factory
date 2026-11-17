---
created: 2026-11-17T20:39:00Z
author: operator
source: "Owner request 2026-11-17; findings from #18, #22, #23, #24, #26 (evidence /tmp/ateam-qa23-p4tdlsaj, /tmp/ateam24-gates-g5vqyp3t, /tmp/ateam-wake-elwbr4ps); herdr 0.9.5"
status: provisional
---

# Proposal for herdr: deliver a message to an agent without typing into its composer

Draft for a future herdr issue or PR. Not posted. Related: [[20261117-160113-herdr-trailing-mention-upstream-draft]] (the #18 report) and the learning note notes/activity/20261117-191817-operator-inbox-per-harness.md.

## The problem

`herdr agent prompt` types text into the agent's composer and presses Enter. When a person is also typing in that pane (the usual case for a lead agent that both a human and other agents talk to), the two collide: the person's half-written text is cut off or merged with the incoming message. A message ending in an @ or $ word can also stall in harnesses whose composer completes it (#18).

herdr 0.9.5 has no non-typing send, no queue, no composer state, and no event when the person types (checked: CLI help, the herdr skill, the API schema; events.subscribe carries pane.output_matched, pane.agent_status_changed, pane.scroll_changed only).

## How a-team works around it today

An inbox file per team (`ateam.py inbox send`), read by `ateam.py wait`, which the operator runs in one of two ways per harness:

| Harness | Wakes on a finished background command | a-team mode |
|---|---|---|
| Claude Code | yes | background wait |
| OpenCode | yes | background wait |
| OMP | continues its own turn | background wait |
| Codex | no | foreground wait, 60 s, rerun while members work |
| pi | no background commands | foreground wait, 60 s |

Foreground mode works everywhere but keeps the operator in a turn while members work (about one model call a minute), and the person's message reaches it when the current wait returns. Native ways in that need no configuration, as found:

- Codex 0.169.0: `codex queue --thread <session id> --message <text>` starts a turn in an idle TUI on that session, draft untouched, picked up in 5-7 s; queued during a turn it arrives at turn end; queued while closed it arrives on resume. It fails (exit 1) when run inside the workspace-write sandbox, because it writes under ~/.codex; a sandboxed TUI still receives messages queued from outside.
- OpenCode 2.2.4: the TUI is a client of a local server with an API (`opencode api`, `opencode run --session`); not probed for delivery into a live session.
- pi 1.5.2: RPC exists only for pi started in RPC mode as a subprocess; no way into a running interactive session.
- OMP 18.20.0: a pi derivative; not found either.
- Claude Code: background-command events wake it, but only from inside its own process; no external send was found.

## Proposals

**1. `herdr agent message <name> <text>` (the main ask).** A queued message that herdr delivers by the best route for the agent's harness, never typing while the person is mid-composition:

- Native route where the harness has one, run by the herdr server process (outside the agent's sandbox): Codex `codex queue --thread <session>`; OpenCode its server API; others as they add one. herdr already knows the harness and session per pane (it reports them), so it can pick the route. This alone would wake sandboxed Codex agents, which a-team cannot do from inside the sandbox.
- Typed fallback otherwise, but held until it is safe (see 2), and always terminated so completion cannot swallow Enter (#18).
- Returns delivered / queued / failed honestly; `agent_prompted` today means "typed", not "submitted".

**2. Know when the person is typing.** herdr is the terminal multiplexer, so it sees every keystroke sent to a pane. Expose it:

- an event (e.g. pane.input) or a field on agent/pane get: time of the last keystroke from a client, and whether keys have been typed since the last Enter (a draft is probably pending);
- a typed delivery option that waits until the pane has had no client input for N seconds and no pending draft, or until the pane loses focus.

This is a heuristic (it cannot see what the harness did with the keys) but it is harness-independent and needs no screen reading.

**3. Queue per agent, delivered in order.** `herdr agent message` appends to a queue in the herdr server; delivery drains it in order when the agent is idle or the route allows. A queue with `list` / `clear` would let tools like a-team stop keeping their own inbox file.

**4. Optional, later: herdr as an ACP host.** herdr could run an agent over ACP behind a pane and still render it, giving structured messages both ways. Large; only native in OMP and OpenCode today. Mentioned for completeness.

## What a-team would do with each

- With 1 for Codex: Codex moves from foreground to background-style waking with no permission caveat.
- With 1 + 2: `ateam.py inbox send` could become a thin wrapper over `herdr agent message`, and the inbox file could go away for harnesses herdr can reach.
- Until then: a-team keeps the inbox, background for Claude Code / OpenCode / OMP, foreground for Codex / pi, and `[operator] messages = "typed"` as an opt-out (#25).

## Open questions before filing

- Probe OpenCode's server API for delivery into a live TUI session.
- Confirm `codex queue` behaviour on newer Codex versions and whether a narrower grant than full permissions lets it run sandboxed.
- Ask herdr maintainers whether keystroke metadata (2) is acceptable from a privacy standpoint: timing only, never contents.
