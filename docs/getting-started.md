# Your first factory

## Before you start

Install [herdr](https://herdr.dev), Python 3.11+, Git and one supported coding harness. Start the harness and sign in or configure its provider. Any operator harness below can also run members. Check `herdr integration status` for each harness you will use. For a missing, outdated, or broken integration, the operator asks for your approval before running `herdr integration install <harness>`. This changes files shared with other factories. Use `antigravity-cli` as the install target for `agy`. [Harness installation sources](../skill/harnesses.toml)

**Choose where members will run before launch.** The default `full` setting bypasses permission prompts in Claude Code, disables approvals and sandboxing in Codex, and runs pi, omp and OpenCode unattended. Antigravity CLI uses its session-only `always-proceed` preset, which still blocks denied commands. Use a sandbox or VM for full permissions. If you want Claude Code or Codex to keep its own guard, ask for `guarded` during setup. That setting only changes those two harnesses. [Permission details](../skill/references/cmd_setup.md#permissions)

## Install the skill

Only the operator's harness needs the skill installed. At launch the operator writes each member's instructions and delivers them through its harness.

The quickest way is the [skills CLI](https://github.com/vercel-labs/skills), which needs Node.js. It asks which harnesses to install to; add `-a claude-code` (or another agent name) to choose up front. `-g` installs for your user rather than one project:

```bash
npx skills add pkieltyka/factory -g
```

Update it later with `npx skills update factory`, then see the note on restarting members below.

To install by hand instead, clone the skill somewhere you can keep it, separate from the project your team will change.

```bash
git clone https://github.com/pkieltyka/factory
cd factory
```

### Operator harnesses

Claude Code, OpenCode and agy receive reports through native background task completion. Codex, pi and omp are supported with an active foreground wait and are not qualified for unattended idle delivery. Re-run the foreground wait while members work. Idle reports stay in the inbox until you return. The session where you run setup stays the operator when you run `up`.

| Operator harness | Install the skill at | Enter in its conversation |
|---|---|---|
| Claude Code | `~/.claude/skills/factory` | `/factory setup` |
| Codex | `~/.agents/skills/factory` | `$factory setup` |
| pi | `~/.pi/agent/skills/factory` | `/skill:factory setup` |
| omp | `~/.omp/agent/skills/factory` | `/skill:factory setup` with skill commands enabled |
| [OpenCode v2](https://opencode.ai/docs/skills/#loading) | `~/.config/opencode/skills/factory` | `@factory setup` |
| Antigravity CLI (agy) | `.agents/skills/factory` in the project or `~/.gemini/config/skills/factory` | `/factory setup` |

**agy workspace trust.** Before launching agy in an untrusted folder, back up `~/.gemini/antigravity-cli/settings.json`. Factory can accept agy's trust prompt at launch, and agy 1.5.13 can then rewrite `trustedWorkspaces` and drop unrelated entries. Check the file afterward and restore any dropped entries yourself.

The Claude Code invocation has been tried live. The other entries were checked against the harness documentation or source. The commands below use Claude Code's spelling. In another harness, keep its prefix and replace `setup` with the command you need.

For a fresh Claude Code installation, run these commands from the factory clone. For another operator, substitute its directory from the table.

```bash
mkdir -p ~/.claude/skills
cp -R skill ~/.claude/skills/factory
```

To link instead, create the parent directory and use `ln -s "$PWD/skill" ~/.claude/skills/factory` in place of the copy command. To update either installation, run `git pull` in the factory clone. A link follows the update. For a copy, refresh the contents with `cp -R skill/. ~/.claude/skills/factory/`. To give running members the updated instructions, finish their current work and have the operator run `/factory up --restart`.

### Codex operator access

If a sandboxed Codex operator is denied access to Herdr, you can approve a single run or save a reusable approval for a recurring operation. For a Factory command, ask the operator to request a prefix consisting of `python3`, the absolute path of the installed script, and the subcommand. Review it before saving and keep the script outside the sandbox's writable roots. A saved allow runs matching commands outside the sandbox. [Codex rules](https://developers.openai.com/codex/rules)

The prefix `["python3", "/absolute/trusted/install/scripts/factory.py", "wait"]` covers both of these calls.

```bash
python3 /absolute/trusted/install/scripts/factory.py wait --seen 12 --timeout 0
python3 /absolute/trusted/install/scripts/factory.py wait --seen 15 --timeout 0
```

Choose which other prefixes to save, one operation at a time. Common candidates are the same script with `status`, `inbox` or `say`, and `herdr integration status`. Keep approvals for team-changing commands such as `up` and `down` separate. Run each command standalone so Codex can match the prefix.

Writable roots for the Factory cache or external worktrees (`--add-dir`) and Unix socket access are separate permissions. The workspace setting `sandbox_workspace_write.network_access=true` grants broader network access. If Codex runs in an externally sandboxed VM, you can explicitly start the operator with `codex --dangerously-bypass-approvals-and-sandbox`, which disables both controls for that session. `-a never` alone keeps the configured sandbox. Factory's `full` and `guarded` settings apply to the members it launches. [Codex permissions](https://developers.openai.com/codex/permissions) · [Configuration reference](https://developers.openai.com/codex/config-reference) · [Member permissions](../skill/references/cmd_setup.md#permissions)

## Form the team

From your project directory, start a dedicated session with `herdr --session first-factory`. In its shell, check that you are in the project and start your signed-in harness (for example, `claude`). Enter `/factory setup`. Tell the operator your project goal, which harnesses are installed, and what work is ready. It proposes roles, models and project tools. For a first team, you can ask for one harness throughout, the built-in `TODO.md` tracker and the reviewer's own review. [Default team](README.md#the-team)

To turn off prompt suggestions for your Claude Code operator as factory does for members, set `"promptSuggestionEnabled": false` in your own `.claude/settings.local.json` in this project.

Setup writes the shared brief in `FACTORY.md` and the suggested formation in `factory.toml.sample`. Commit both so teammates can use them. Your personal formation goes in `factory.toml`, which setup adds to `.gitignore`.

## Launch

Enter `/factory up`. After checking the formation, the operator renames its tab **factory operator** and opens **factory team** with one pane per member, labelled with the member's name. Enter `/factory status` to see who is running. You can speak in any pane. To add a configured member or finish a partial launch, run `/factory up` again, or `/factory up <name>` for one member.

## Give it one task

Ask the operator for a small change with a visible result. For example, “Add a test for this bug, fix it, and have the reviewer check the change.”

## Ready items and autopilot

With `TODO.md`, put the items the operator may assign under `## Ready` in the order you want them done. Only you or another owner puts work there. To use autopilot, tell the operator to keep going on a goal and give it bounds. It works through ready items and stops when it needs your decision, nothing is ready, or a block prevents progress.

Pushing, deploying, destructive actions, spending and sending anything outside the team each need your authorization. A project's Pull requests section can give standing permission for two things separately: pushing ticket branches and opening or updating PRs, and merging ready PRs.

## Stop and come back

After members report, enter `/factory down` from the operator's pane. It records available member sessions and closes their panes, including any where work is still running. Use `/factory down <name>` to stop one member. To stop the whole dedicated session, run `herdr session stop first-factory` from another terminal. This ends every process in that session, including the operator. To leave work running, detach with **Ctrl+B**, then **q**. [How herdr sessions persist](https://herdr.dev/tui/docs/persistence-remote/)

Enter `/factory up` to bring members back. It leaves live members alone and resumes recorded sessions of the same harness when available. Use `/factory up --fresh` to start stopped members in new sessions. To keep a member out, name the others or remove it from `factory.toml`. If you left herdr, return from the project with `herdr --session first-factory` first. If the operator pane opens at a shell, start your harness there.

After `herdr session stop first-factory` or a full server restart, herdr restores the panes and can reopen the operator's previous conversation. Use that conversation, or quit the restored harness and start it again in the same pane for a fresh one. Run `/factory down` before stopping herdr so the next `/factory up` starts the members with current factory flags. Use `/factory up --restart` to reapply launch settings to members herdr restored.

## Messages from a v1 team

Use a retained Factory v1 checkout from the original herdr session. The first command shows unread messages without acknowledging them. The second shows acknowledged history. Factory v2 preserves those files and reads its own message log.

```bash
python3 <v1-checkout>/skill/scripts/factory.py wait --cwd <original-checkout> --timeout 0
python3 <v1-checkout>/skill/scripts/factory.py inbox 100 --cwd <original-checkout>
```

## Where the rules live

The skill references address the operator agent. “You” in those pages means the operator. [Setup](../skill/references/cmd_setup.md) · [Leading the team](../skill/references/leading.md) · [Pull requests](../skill/references/pull-requests.md) · [Returning to the team](../skill/references/cmd_continue.md)
