# Your first factory

## Before you start

Install [herdr](https://herdr.dev), Python 3.11+, Git and one supported coding harness. Start the harness and sign in or configure its provider. One harness can run the operator and every member. Run `herdr integration install <harness>` once for each harness you will use, with `claude`, `codex`, `pi`, `omp` or `opencode` as the argument. [Harness installation sources](../skill/harnesses.toml)

**Choose where members will run before launch.** The default `full` setting bypasses permission prompts in Claude Code, disables approvals and sandboxing in Codex, and runs pi, omp and OpenCode unattended. Use a sandbox or VM for full permissions. If you want Claude Code or Codex to keep its own guard, ask for `guarded` during setup. That setting only changes those two harnesses. [Permission details](../skill/references/cmd_setup.md#permissions)

## Install the skill

Only the operator's harness needs the skill installed. At launch the operator writes each member's instructions and delivers them through its harness. Clone the skill somewhere you can keep it, separate from the project your team will change.

```bash
git clone https://github.com/pkieltyka/factory
cd factory
```

### Operator harnesses

| Operator harness | Install the skill at | Enter in its conversation |
|---|---|---|
| Claude Code | `~/.claude/skills/factory` | `/factory setup` |
| Codex | `~/.agents/skills/factory` | `$factory setup` |
| pi | `~/.pi/agent/skills/factory` | `/skill:factory setup` |
| omp | `~/.omp/agent/skills/factory` | `/skill:factory setup` with skill commands enabled |
| [OpenCode v2](https://opencode.ai/docs/skills/#loading) | `~/.config/opencode/skills/factory` | `@factory setup` |

The Claude Code invocation has been tried live. The other entries were checked against the harness documentation or source. The commands below use Claude Code's spelling. In another harness, keep its prefix and replace `setup` with the command you need.

For a fresh Claude Code installation, run these commands from the factory clone. For another operator, substitute its directory from the table.

```bash
mkdir -p ~/.claude/skills
cp -R skill ~/.claude/skills/factory
```

To link instead, create the parent directory and use `ln -s "$PWD/skill" ~/.claude/skills/factory` in place of the copy command. To update either installation, run `git pull` in the factory clone. A link follows the update. For a copy, refresh the contents with `cp -R skill/. ~/.claude/skills/factory/`. To give running members the updated instructions, finish their current work and have the operator run `/factory up --restart`.

## Form the team

From your project directory, start a dedicated session with `herdr --session first-factory`. In its shell, check that you are in the project and start your signed-in harness (for example, `claude`). Enter `/factory setup`. Tell the operator your project goal, which harnesses are installed, and what work is ready. It proposes roles, models and project tools. For a first team, you can ask for one harness throughout, the built-in `TODO.md` tracker and the reviewer's own review. [Default team](README.md#the-team)

To turn off prompt suggestions for your Claude Code operator as factory does for members, set `"promptSuggestionEnabled": false` in your own `.claude/settings.local.json` in this project.

Setup writes the shared brief in `FACTORY.md` and the suggested formation in `factory.toml.sample`. Commit both so teammates can use them. Your personal formation goes in `factory.toml`, which setup adds to `.gitignore`.

## Launch

Enter `/factory up`. After checking the formation, the operator renames its tab **factory operator** and opens **factory team** with one pane per member, labelled with the member's name. Enter `/factory status` to see who is running. You can speak in any pane. To add a configured member or finish a partial launch, run `/factory up` again, or `/factory up <name>` for one member.

## Give it one task

Ask the operator for a small change with a visible result. For example, “Add a test for this bug, fix it, and have the reviewer check the change.” Read the result in the operator pane.

## Ready items and autopilot

With `TODO.md`, put the items the operator may assign under `## Ready` in the order you want them done. Only you or another owner puts work there. To use autopilot, tell the operator to keep going on a goal and give it bounds. It works through ready items and stops when it needs your decision, nothing is ready, or a block prevents progress.

Pushing, deploying, destructive actions, spending and sending anything outside the team each need your authorization. A project's Pull requests section can give standing permission for two things separately: pushing ticket branches and opening or updating PRs, and merging ready PRs.

## Stop and come back

After members report, quit each member's harness in its pane to stop the members. To stop the whole dedicated session, run `herdr session stop first-factory` from another terminal. This ends every process in that session, including the operator. To leave work running instead, detach with **Ctrl+B**, then **q**. [How herdr sessions persist](https://herdr.dev/tui/docs/persistence-remote/)

From the project, return with `herdr --session first-factory`. If the operator pane opens at a shell, start your harness there. If you left the session running, enter `/factory up`. It leaves live members alone and brings back the rest, including closed panes, resuming recorded sessions of the same harness when available. Use `/factory up --fresh` to start stopped members in new sessions. To keep a member out, name the others or remove it from `factory.toml`.

After `herdr session stop first-factory` or a full server restart, herdr restores the panes and can reopen the operator's previous conversation. Use that conversation, or quit the restored harness and start it again in the same pane for a fresh one. Enter `/factory up --restart` to reapply member launch settings.

## Where the rules live

The skill references address the operator agent. “You” in those pages means the operator. [Setup](../skill/references/cmd_setup.md) · [Leading the team](../skill/references/leading.md) · [Pull requests](../skill/references/pull-requests.md) · [Returning to the team](../skill/references/cmd_continue.md)
