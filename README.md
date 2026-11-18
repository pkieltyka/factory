# Factory

### An owner-directed software factory on herdr

`/factory` is an agent skill that forms a team, assigns work, gets it reviewed and brings it back after a restart. You direct the operator; members work in their own panes using Claude Code, Codex, pi, omp or OpenCode. Keep the built-in workflow or add tools through short project sections.

[Get started](docs/getting-started.md) · [The factory on one page](docs/README.md)

## Quickstart

Install herdr, Python 3.11+ and the harnesses you will use. From a clone of this repo, **copy or link `skill/`** into your harness's skills directory as `factory`. For Claude Code, with no existing factory installation:

```bash
mkdir -p ~/.claude/skills
cp -R skill ~/.claude/skills/factory
```

Or link it instead of copying: `ln -s "$PWD/skill" ~/.claude/skills/factory`.

In your project, start a harness inside herdr:

```text
/factory setup      # choose the team and project tools
/factory up         # launch the members, then give the operator a task
/factory continue   # recover the team when you return
```

## Commands

| Command | What it does |
|---|---|
| `/factory` | Reads the current state and chooses the next step. |
| `/factory setup` | Forms or changes the team and project brief. |
| `/factory up` | Checks, launches and briefs the members. |
| `/factory continue` | Resumes the team and reconciles unfinished work. |
| `/factory status` | Shows each member's state and the message route. |
| `/factory wait` | Returns when a message or member needs the operator. |
| `/factory brief` | Previews or resends the team brief. |
| `/factory say` | Sends an operator message to members. |

The [skill references](skill/references/) hold the operating rules.

## Repo setup

Setup writes `FACTORY.md`, the shared project brief, and `factory.toml`, your uncommitted formation. `factory.toml.sample` is the shared suggestion. Start with `TODO.md` and the reviewer's own review; add only the [optional choices](docs/README.md) the project needs. One team runs per herdr session; parallel developers can use Git worktrees.

By default, when the configured operator harness matches its live pane, members' messages go to an inbox. An explicit typed choice, an unset or mismatched harness, or an unusable inbox uses typed messages instead. See [leading the team](skill/references/leading.md).

## What else is in this repo

- [skill/](skill/) — what you install: instructions, roles, templates, recipes and `scripts/factory.py`.
- [docs/](docs/README.md) — the map and getting-started guide; [docs/plans/](docs/plans/) records development plans.
- [tests/](tests/) — fake harness/herdr tests, real Git checks and the Gate 1 fixture; run `python3 tests/test_factory.py`. `FACTORY_LIVE=1 python3 tests/test_factory.py LiveFlags` checks installed harness flags.

To add a harness, add its table (with `operator_wait`) in `skill/harnesses.toml`, its code block and a test fake.

## Coming from a-team

Coming from a-team: install the skill as `factory`. The first command in a project with the earlier names prints the renames from `TEAM.md`, `team.toml` and `team.toml.sample` to `FACTORY.md`, `factory.toml` and `factory.toml.sample`. Follow them, then run `factory.py init` to update `.gitignore`.

A project that ran under a-team keeps its cache under `a-team`; new projects use `factory-skill`. Moving a project's cache is optional and done by a person, with no team running on that project and only if its destination does not exist. Under the cache base (`$XDG_CACHE_HOME`, or `~/.cache` when unset or empty), create `factory-skill` with `mkdir -p -m 700`, then `mv` the project's directory from `a-team` into it.
