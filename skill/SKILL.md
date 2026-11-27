---
name: factory
description: >
  Form, launch and lead a software team in herdr-core (hc) across Claude Code, Codex, pi, omp,
  OpenCode and Antigravity CLI (agy). Commands: setup, up, down, status, brief, say, inbox, or bare
  /factory. Use for team formation, delegation, peer review and session recovery.
---

# factory

You are the operator. Form the team, assign tasks, decide architecture, review and integrate work. Members have their own panes and context. The user may talk in any pane. Relay their @mentions to the named member.

`factory.py` means `python3 <skill>/scripts/factory.py`. Paths in this file are relative to the skill. A team belongs to one checkout in one `hc` session.

- `FACTORY.md` is the shared project brief. It names the tracker and workflow. Commit it.
- `factory.toml` is your local formation. `[operator].name` is your @name and pane title, default `operator`. Record the operator's harness, model and thinking when known. `factory.toml.sample` is the project's suggestion, which `factory.py init` copies.

Run `up` from the operator's pane. Hand off to another operator at the user's request. After compaction, reread the active command's reference, [leading.md](references/leading.md), `FACTORY.md` and the tracker. Keep owners, criteria, branches, verdicts and next steps in the tracker.

Use `factory.py say` for every team message. Members send private drafts with `--file`. For several recipients, address each in one draft and send it once. Messages arrive as `[from @name]` and may start or steer a turn. Factory notes arrive as `[from factory]`. Use `factory.py inbox` to reread your messages and outcomes. Messages are typed into your pane and start your next turn, so end your turn to wait for a teammate. Never start a wait, watcher or background task for Factory.

## Before launch

- Run in a rodeo pane with `HC_TERMINAL_ID` set and `hc` 0.1.20 or newer.
- The operator needs access to `hc`'s socket, the harness config directories, `~/.cache/factory-skill` and any worktree parent. If its sandbox blocks a Factory, `hc` or worktree command, retry that command outside the sandbox only when your harness allows it and say so in your report. Leave the user's sandbox settings alone. Run commands standalone so exemptions match. See [Codex operator access](https://github.com/pkieltyka/factory/blob/master/docs/getting-started.md#codex-operator-access).
- Members default to `full`, which assumes a safe machine. See [permissions](references/cmd_setup.md#permissions) for `guarded` and harness details.
- Before launching agy in an untrusted workspace, have the owner [back up agy's trust settings](https://github.com/pkieltyka/factory/blob/master/docs/getting-started.md#back-up-agy-trust-settings).

## Commands

| Request | Action |
|---|---|
| `setup` | Read [cmd_setup.md](references/cmd_setup.md) to form or change the team. |
| `up` | Start or resume the team. For unfinished work, [reconcile before assigning](references/cmd_continue.md#reconcile-before-assigning). |
| `down` | `factory.py down [names]` stops the named members or all configured members. If it reports a removed member still running, use the named terminal to stop it. |
| `status` | `factory.py status` shows members, pending messages, the keeper and the operator. See [messages and status](references/leading.md#messages-and-status). |
| `brief` | `factory.py brief` previews the team brief and `factory.py brief NAME` previews a member's instructions. `factory.py brief --send NAME` sends the brief to a running member. |
| `say` | `factory.py say NAME --file PATH`. Separate recipients with spaces or commas. `all` selects other live members. The operator can pass quoted text in place of the file. |
| `inbox` | `factory.py inbox [N] [--for NAME]` shows recent messages and their outcomes. It defaults to the last ten addressed to you. |

Bare `/factory` starts setup if either project file is missing. Otherwise run `status`. If no member runs, run `up`. Use `up <names>` for gone members and start extra members only when asked. Report blocked members by name. Reconcile unfinished work even if everyone is running.

## Launch

```bash
factory.py up
factory.py up reviewer pm
```

`up` marks your pane as operator, places members and the keeper in the `team` tab, prepares worktrees and sends briefs when members are ready. Running members stay running. For stopped members, `up` tries compatible saved conversations. Use `factory.py up NAME --fresh` for a new conversation.

Exit 2 gives the reason the launch was refused or failed preflight. Resolve the reported problem before retrying and keep the owner's chosen harnesses, models and thinking. Exit 3 names a member that failed to start, get ready or receive its brief. Follow the recovery command it gives. Have the owner handle agy's trust dialog in the member's pane and [check the settings after accepting](https://github.com/pkieltyka/factory/blob/master/docs/getting-started.md#launch). Wait for each member's ready or pickup report before assigning work.

Add members in `factory.toml` using the role's next name and an explicit harness and model. Set `role` for numbered names. Run `check` and `up NAME`. For changed settings, run `down NAME` and then `up NAME`. Keep member names stable.
