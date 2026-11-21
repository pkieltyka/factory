---
name: factory
description: >
  Form, launch and lead a software team in herdr across Claude Code, Codex, pi, omp,
  OpenCode and Antigravity CLI (agy). Commands: setup, up, down, status, wait, brief, say, or bare
  /factory. Use for team formation, delegation, peer review and session recovery.
---

# factory

You are the operator (`@operator`): form the team, assign tasks, decide architecture, review and integrate work. Members have their own panes and context. The user may talk in any pane; relay their @mentions to the named member.

Paths below are relative to this skill. `factory.py` means `python3 <skill>/scripts/factory.py`. A project has two files at its root. `FACTORY.md` is the brief every member reads: the project, where tasks are tracked, the workflow. It is committed and shared by everyone on the repo. `factory.toml` is your own formation: `[operator]` is you (`name`, default `operator`, is your @name, pane label and herdr agent name), then the members with their harnesses and models. It is not committed; `factory.toml.sample` is the project's suggestion, which `factory.py init` copies. One team runs per herdr session.

The session receiving the request is the operator. Run `up` from its pane. Record any known operator harness, model and thinking level for the roster. The live operator harness determines the wait style. Roster choices apply to members. Hand off to another operator session only on the user's explicit request.

After compaction, re-read the active command's reference, `references/leading.md`, `FACTORY.md` and the tracker. Preserve task owners, acceptance criteria, branches, verdicts and next steps there, not only in chat.

Use `factory.py say` to message teammates, never a harness's own messaging (e.g. Claude Code's `SendMessage`). Members send private drafts with `--file`, including reports to the operator. For several recipients, address each with `@name` in one draft and send it once. `wait` prints reports queued in your inbox. See `references/leading.md`.

## Before launch

- Require `HERDR_ENV=1`; otherwise stop and tell the user to run inside herdr.
- Install herdr integration once per chosen harness: `herdr integration install <harness>` (use `antigravity-cli` for `agy`). Session recovery depends on it.
- The operator needs herdr's socket, harness config directories, `~/.cache/factory-skill`, and any worktree parent. If your own sandbox blocks one of factory's own commands (`factory.py`, `herdr`, a `git worktree` operation), you may retry that one command outside the sandbox when your harness allows it, and you say so in your report to the user. On Linux, start each Claude Code background `wait` outside the sandbox through its normal permission check (see [references/leading.md](references/leading.md#receive-and-inspect)). Never edit the user's sandbox settings; every other command keeps the normal rules, and a block you cannot get past is reported, not worked around. Run shell commands standalone so command exemptions match: no `cd &&`, pipe, redirect or trailing `echo`.
- Members default to `full` permissions, which assume a safe machine (a sandbox or VM); `guarded` is the alternative. What each means per harness: [references/cmd_setup.md](references/cmd_setup.md#permissions). Unknown startup questions and higher-level policy can still block a member.

## Commands

| Request | Action |
|---|---|
| `setup` | Read [references/cmd_setup.md](references/cmd_setup.md); form or change the team. |
| `up` | Start or resume the team as under Launch. When returning to unfinished work, [reconcile before assigning](references/cmd_continue.md#reconcile-before-assigning). |
| `down` | `factory.py down [names]` closes running member panes after recording their sessions for `up`. Omit names to stop all members. |
| `status` / `wait` | Run the command; interpret results using [references/leading.md](references/leading.md). |
| `brief` | `factory.py brief --send [all|names]` resends the brief; without `--send`, preview it. |
| `say` | `factory.py say NAME [NAME ...] --file <path>`. Separate names with spaces or commas. `all` selects members only. The operator can pass quoted text instead of `--file`. |

Bare `/factory`: if either project file is missing, run setup. Otherwise run `status`. If no member is running, run `up`. If some are gone, run `up <names>` for those members. While others run, start never-started members only if the user asks. Inspect blocked members and resolve within existing authorization. When the tracker or Git shows unfinished work, reconcile as above, even if everyone is running. Carry on with the user's work or ask for the next task if none is known.

## Launch

```bash
factory.py up                       # every member in factory.toml
factory.py up reviewer pm           # selected members
```

`up` checks every member it will start or restart and checks for name conflicts before changing anything in herdr. Reading the team can record observed sessions in the local cache. If checks fail, report the problem/fix and resolve it; do not substitute models, harnesses or effort levels without the user's authorization. Unknown Claude models warn instead of failing preflight; Claude rejects invalid IDs at launch.

On success, the script labels the operator tab `factory operator`, creates a balanced `factory team` tab, prepares worktrees, starts members and sends their briefs. Re-running `up` leaves live members alone and resumes matching sessions for stopped members. Use `--fresh` for new sessions and `--restart` to relaunch running members too.

- Failed start: read the reported pane, fix the cause, then `factory.py up <name>`.
- Undelivered brief: resolve the error and `factory.py brief --send <name>` before assigning work. Failed starts or briefing exit 3; preflight failures exit 2.
- Known startup dialogs are answered automatically. For any other dialog, read `herdr agent read <name> --source visible`; resolve it within existing authorization or ask for the missing decision, then resend the brief.

Check `status`, then assign self-contained tasks as in [leading.md](references/leading.md). Add members only in `factory.toml`: use the role's next name (`reviewer`, then `reviewer-2`, `reviewer-3`), explicit harness/model, and `role` for numbered names. Run `check`, then `up` or `up <name>`. Change running members in `factory.toml` and restart with `up <name> --restart`. Names are never reassigned or renumbered; the operator's name stays fixed while the team runs, and tab labels are fixed.
