# Your first factory

## Install and sign in

Install [herdr](https://herdr.dev), Python 3.11+ and at least one supported coding harness: Claude Code, Codex, pi, omp or OpenCode v2. The [harness catalog](../skill/harnesses.toml) names their installation sources. Start the harness once and sign in or configure its provider. Git is needed for worktrees and the GitHub recipes. You install these tools; factory does not install or upgrade them.

## Install the skill

Clone this repository, then copy or link its `skill/` directory into your harness's skills directory as `factory`. For example, from this checkout, choose one method for Claude Code when that destination does not already exist:

```bash
mkdir -p ~/.claude/skills
cp -R skill ~/.claude/skills/factory
```

Or use `ln -s "$PWD/skill" ~/.claude/skills/factory` instead of the copy. Use your harness's own skills directory for another harness. [The root README](../README.md) has the command map and migration guidance.

## Form and launch the team

Open herdr in your project and start the signed-in harness there. In its conversation, run `/factory setup`. Tell it the project goal and ask for a team using only the harnesses you installed; one harness can run every member. Choose optional tools from the [factory map](README.md). Setup writes the shared `FACTORY.md`, your private `factory.toml` and the suggested `factory.toml.sample`; the [setup reference](../skill/references/cmd_setup.md) owns the details.

Run `/factory up`. The current agent becomes the operator and starts the members in their panes. Launch requirements, including herdr integrations and permission choices, live in [SKILL.md](../skill/SKILL.md#before-launch).

## Give it one task

Ask the operator for a small change with a visible result, for example: “Add a test for this bug, fix it, and have the reviewer check the change.” Follow its report in the operator pane. The [leading reference](../skill/references/leading.md) describes assignment, review and integration; the [PR reference](../skill/references/pull-requests.md) applies if you enabled pull requests.

## Come back later

Open the project in herdr and run `/factory continue`. The operator recovers from the brief, tracker and Git, and resumes the noted member sessions. Use the [continue reference](../skill/references/cmd_continue.md) for changed settings or sessions that cannot resume. All operating rules live in [the skill references](../skill/references/).
