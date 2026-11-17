---
name: a-team
description: >
  Form, launch and lead a software team in herdr across Claude Code, Codex, pi, omp
  and OpenCode. Commands: setup, up, continue, status, wait, brief, say, or bare
  /a-team. Use for team formation, delegation, peer review and session recovery.
---

# a-team

You are the operator (`@operator`): form the team, assign tasks, decide architecture, review and integrate work. Members have their own panes and context. The user may talk in any pane; relay their @mentions to the named member.

Paths below are relative to this skill. `ateam.py` means `python3 <skill>/scripts/ateam.py`. A project has two files at its root. `TEAM.md` is the brief every member reads: the project, where tasks are tracked, the workflow. It is committed and shared by everyone on the repo. `team.toml` is your own formation: `[operator]` is you (`name`, default `operator`, is your @name, pane label and herdr agent name), then the members with their harnesses and models. It is not committed; `team.toml.sample` is the project's suggestion, which `ateam.py init` copies. One team runs per herdr session. The first member of a role keeps its plain name; later members use `-2`, `-3`, etc. Existing names never change.

After compaction, re-read the active command's reference, `references/leading.md`, `TEAM.md` and the tracker. Preserve task owners, acceptance criteria, branches, verdicts and next steps there, not only in chat.

Message teammates only with `ateam.py say` or `herdr agent prompt`, never through a harness's own messaging (e.g. Claude Code's `SendMessage`): herdr works across harnesses and permission modes.

## Before launch

- Require `HERDR_ENV=1`; otherwise stop and tell the user to run inside herdr.
- Install herdr integration once per chosen harness: `herdr integration install <harness>`; session recovery depends on it.
- The operator needs herdr's socket, harness config directories, `~/.cache/a-team`, and any worktree parent. If your own sandbox blocks one of a-team's own commands (`ateam.py`, `herdr`, a `git worktree` operation), you may retry that one command outside the sandbox when your harness allows it, and you say so in your report to the user. Never edit the user's sandbox settings; every other command keeps the normal rules, and a block you cannot get past is reported, not worked around. Run shell commands standalone so command exemptions match: no `cd &&`, pipe, redirect or trailing `echo`.
- Members default to `full`, intended for a safe sandbox/VM. Claude uses bypass-permissions while retaining inherited sandbox settings; Codex disables approvals and its sandbox; pi/omp/OpenCode allow unattended commands. `guarded` uses Claude auto mode or Codex's workspace sandbox with automatic review; it changes nothing for the others.
- Full Claude members permit Unix sockets inside an inherited sandbox: all sockets on Linux, `HERDR_SOCKET_PATH` on macOS (all sockets if absent). The macOS form is not live-tested. Guarded only exempts standalone messaging commands. Unknown startup questions and higher-level policy can still block.

## Commands

| Request | Action |
|---|---|
| `setup` | Read [references/cmd_setup.md](references/cmd_setup.md); form or change the team. |
| `up` | Follow Launch below, then [references/leading.md](references/leading.md). |
| `continue` / `cont` | Read [references/cmd_continue.md](references/cmd_continue.md); `--restart` reapplies settings to running members. |
| `status` / `wait` | Run the command; interpret results using [references/leading.md](references/leading.md). |
| `brief` | `ateam.py brief --send [names]` resends the brief; without `--send`, preview it. |
| `say` | `ateam.py say <names|all> "message"`, or `--file <path>`. Check delivery failures. |

Bare `/a-team`: no `TEAM.md` or `team.toml` → setup; otherwise run `status`. No members → continue when the tracker or Git shows unfinished work, otherwise up; gone → continue; blocked → inspect and resolve within existing authorization; running → continue the user's work or ask for the next task if none is known.

## Launch

```bash
ateam.py up                       # every member in team.toml
ateam.py up reviewer pm           # selected members
```

`up` checks every selected member before starting any. If checks fail, report the problem/fix and resolve it; do not substitute models, harnesses or effort levels without the user's authorization. Unknown Claude models warn instead of failing preflight; Claude rejects invalid IDs at launch.

On success, the script labels the operator tab `a-team operator`, creates a balanced `a-team workers` tab, prepares worktrees, starts members and sends their briefs. Re-running `up` skips live members.

- Failed start: read the reported pane, fix the cause, then `ateam.py up <name>`.
- Undelivered brief: resolve the error and `ateam.py brief --send <name>` before assigning work. Failed starts or briefing exit 3; preflight failures exit 2.
- Known startup dialogs are answered automatically. For any other dialog, read `herdr agent read <name> --source visible`; resolve it within existing authorization or ask for the missing decision, then resend the brief.

Check `status`, then assign self-contained tasks as in `leading.md`. Add members only in `team.toml`: use the next stable role name, explicit harness/model, and `role` for numbered names. Run `check`, then `up <name>`. Change running members in `team.toml` and restart with `continue <name> --restart`. Names are never reassigned or renumbered; the operator's name stays fixed while the team runs, and tab labels are fixed.
