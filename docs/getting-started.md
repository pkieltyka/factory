# Your first factory

## Before you start

Install [herdr-core](https://github.com/herdrdev/herdr-core) (`hc` 0.1.20 or newer), [rodeo](https://github.com/lukeed/rodeo), Python 3.11+, Git and a coding harness. Sign in to the harness or configure its provider. [Harness installation sources](../skill/harnesses.toml)

`hc` holds a message while you have an unsent draft in the pane it targets. Set `draft_wait = 600` or more in the `hc` config file named by `factory.py check`. Submit or clear unfinished drafts to let messages arrive. When the hold expires, `hc` can submit the draft together with the message.

The default `full` permissions assume a safe machine such as a sandbox or VM. Choose `guarded` to keep the harness guard for Claude Code and Codex. [Member permissions](../skill/references/cmd_setup.md#permissions)

## Install the skill

Install the skill in the harness that will run the operator. Factory supplies each member's instructions at launch. Clone this checkout somewhere separate from your project.

```bash
git clone https://github.com/pkieltyka/factory
cd factory
```

### Operator harnesses

The pane where you run setup becomes the operator when you run `up`. These are the skill paths and invocations for each harness.

| Harness | Install the skill at | Enter in its conversation |
|---|---|---|
| Claude Code | `~/.claude/skills/factory` | `/factory setup` |
| Codex | `~/.agents/skills/factory` | `$factory setup` |
| pi | `~/.pi/agent/skills/factory` | `/skill:factory setup` |
| omp | `~/.omp/agent/skills/factory` | `/skill:factory setup` with skill commands enabled |
| [OpenCode](https://opencode.ai/docs/skills/#loading) | `~/.config/opencode/skills/factory` | `@factory setup` |
| Antigravity CLI (agy) | `.agents/skills/factory` in the project or `~/.gemini/config/skills/factory` | `/factory setup` |

From the factory clone, copy `skill/` to the corresponding path. For example, for a fresh Claude Code install:

```bash
mkdir -p ~/.claude/skills
cp -R skill ~/.claude/skills/factory
```

You can symlink the skill directory instead. After pulling an update, refresh a copied installation from the checkout. To load changed instructions for a running member, finish its current work, stop it with `factory.py down NAME`, and start it with `factory.py up NAME`.

### Codex operator access

If the Codex sandbox blocks access to `hc`, you can approve a single run or save a reusable prefix for a recurring operation. Use `python3`, the absolute path to the installed script, and the subcommand in the prefix. Review it before saving and keep the script outside the sandbox's writable roots. A saved allow runs matching commands outside the sandbox. [Codex rules](https://developers.openai.com/codex/rules)

The prefix `["python3", "/absolute/trusted/install/scripts/factory.py", "inbox"]` covers both examples.

```bash
python3 /absolute/trusted/install/scripts/factory.py inbox 10
python3 /absolute/trusted/install/scripts/factory.py inbox 20 --for operator
```

Approve `status`, `inbox` or `say` separately as needed. Run each command standalone so the prefix matches. Keep team-changing operations such as `up` and `down` under separate approvals. Writable roots and Unix socket access are separate permissions. [Codex permissions](https://developers.openai.com/codex/permissions) · [Member permissions](../skill/references/cmd_setup.md#permissions)

## Form the team

From the project directory, run `rodeo --session first-factory`. Start your signed-in harness in its pane and enter `/factory setup` using the spelling for your harness. Give the operator your goal, the available harnesses and the work you want ready. For a first team, use the built-in `TODO.md` tracker and the reviewer's own review. [Default team](README.md#the-team)

To turn off prompt suggestions for your Claude Code operator, set `"promptSuggestionEnabled": false` in your own `.claude/settings.local.json` in this project.

Commit the shared brief `FACTORY.md` and suggested formation `factory.toml.sample`. Setup adds your local `factory.toml` to `.gitignore`.

## Launch

Enter `/factory up`. It marks your current pane as the operator and adds the members and a narrow keeper pane to the `team` tab. Your pane stays where it is. Check `/factory status` for members, pending messages and keeper state.

If agy asks whether you trust the workspace, accept the dialog in its pane. Then run `/factory brief --send NAME` for that member. Follow any other recovery instruction printed by `up`.

## Give it one task

Ask the operator for a small change with a visible result. For example, “Add a test for this bug, fix it, and have the reviewer check the change.”

## Ready items and autonomous work

Put work the operator can assign under `## Ready` in `TODO.md`, or mark it ready in your chosen tracker. The team carries that work through the brief's workflow. A quiet note prompts the operator to check for missing reports or ready work. When an owner decision is needed, the operator names it. For ready work added while the team is idle, either tell the operator or set a read-only `ready_check` in your local `factory.toml`. [Setup and ready-check recipes](../skill/references/cmd_setup.md#write-and-validate)

Pushing, deploying, destructive actions, spending and sending anything outside the team need your authorization. A project's Pull requests section can separately authorize pushing ticket branches and opening or updating PRs, and merging ready PRs.

## Stop and come back

From the operator's pane, `/factory down` stops configured members and keeps their saved conversations, worktrees and message log. Pass a name to stop one member. If it reports a live terminal for a removed member, use the command it gives to stop that terminal.

Detach rodeo with its prefix key then **q** to leave terminals running in `hc`. Reopen the same named session to return. After an `hc` restart, restart your operator harness if needed. Use `/factory up NAME` for the members you want back. Factory tries the saved compatible conversation and reports a fresh start. Use `/factory inbox` to reread messages.

## Limits

An unreachable operator's messages queue until `up` marks an operator again. If `hc` or the keeper stops while everyone is idle, a command is needed to detect it. Quiet detection cannot tell when teammates exchange messages without making progress, or when the operator works alone after a quiet note and stops without messaging anyone.

If a harness quits during a draft hold, `hc` can type into the shell left in its pane. Check the outcome and the recipient before retrying a send.

## Where the rules live

[Setup](../skill/references/cmd_setup.md) · [Leading the team](../skill/references/leading.md) · [Pull requests](../skill/references/pull-requests.md) · [Returning to the team](../skill/references/cmd_continue.md)
