---
created: 2026-11-20T13:12:41Z
author: operator
source: "Owner request on 2026-11-20 to reproduce #105 and #104; evidence from the Codex session logs of the factory session that #105 describes (2026-11-19, another project), and skill/scripts/factory.py at d6c4e75"
status: verified
---

# Member-to-member messaging failures (#105) and `brief --send all` (#104)

## What the logs show

In the #105 session, members on Codex sent 35 `herdr agent prompt` messages between 20:31 and 23:07 UTC. 32 were delivered; 3 failed, all from @reviewer to a developer:

| UTC | From → to | Result |
|---|---|---|
| 21:01:41 | @reviewer → @lead-dev | `bash: -c: line 1: unexpected EOF while looking for matching '` |
| 21:07:40 | @reviewer → @lead-dev-2 | same |
| 21:08:49 | @reviewer → @lead-dev | same |

All three were review verdicts of 513 to 802 characters. In each, the agent opened the message with `'` as the brief shows (`herdr agent prompt <name> '[from @you]: ...'`) but ended the command without the closing `'`. The message text itself held no apostrophe; one held double quotes, two did not. The cause is an omitted closing quote in a long one-line command, not a particular character. The same form also breaks on any apostrophe in the text: `bash -c "printf '%s' '[from @reviewer]: the fix doesn't hold'"` fails with the same error.

The shell error came back at once, but the reviewer did not resend any of the three. Each time it sent its copy to @pm (which succeeded) and moved on.

At 21:10:30 the reviewer tried `factory.py say lead-dev "..."` as a fallback. It failed with: `another agent is already named @operator (w1:p1). One team runs per herdr session: rename that agent, change [operator] name in factory.toml, or use another session.` It then told @pm and the operator through `inbox send` that the report to @lead-dev "remains pending".

From 21:10 to the end of the session, @reviewer sent 14 more messages, all to @pm and none to a developer. @pm relayed review results to @lead-dev-2.

## Why `say` refuses members

`cmd_say` (`skill/scripts/factory.py:1446`) calls `ensure_operator(cfg)` first. That treats the calling pane as the operator and fails when another pane already holds the operator's name, so `say` cannot run from a member pane. The member brief (`skill/templates/member-prompt.md:10`) states `factory.py say` is operator-only, so the reviewer used it against its instructions. Its message was in double quotes and would have been delivered had `say` allowed members.

## #104: `brief --send all`

`cmd_brief` (`skill/scripts/factory.py:1430`) checks each named member with `team.live(name)`, so `all` is treated as a member name and fails with `not running: all`. `cmd_say` expands `all` to every member (line 1455); `cmd_brief` has no such expansion.

## Tests

No test covers a member running `say`, a failed member-to-member delivery, or `brief --send all`. The existing `brief --send` tests name one member.

## Not established

Whether Claude Code, omp or agy members omit the closing quote at the same rate. The failures were all Codex. This repo's team has not hit it. Its peer messages have been shorter.
