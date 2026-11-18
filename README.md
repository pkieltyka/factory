# factory

An agent skill for running a software team in [herdr](https://herdr.dev). The current agent becomes the operator, `@operator`: the lead agent. You, the user, are the owner who directs it; members use Claude Code, Codex, pi, omp or OpenCode v2 and message each other directly. By default, when the configured operator harness matches its live pane, members' messages to the operator go to an inbox that `factory.py wait` reads, so nothing is typed into the pane you type in. An explicit typed choice, an unset or mismatched harness, or a damaged or unavailable inbox, uses typed messages instead. One team per herdr session, with a `factory operator` tab and a `factory team` tab. Parallel developers can use separate Git worktrees.

Requires Python 3.11+, herdr, and the chosen harnesses; Git is required for worktrees. Link this repo's `skill/` directory into your harness's skills directory as `factory`, for example:

```bash
ln -s "$PWD/skill" ~/.claude/skills/factory
```

Run `/factory setup`, then `/factory up`. Setup writes two files at the project root: `FACTORY.md`, the brief every member reads (project, where tasks are tracked, workflow), committed and shared; and `factory.toml`, your own formation (operator, members, harnesses, models), which is not committed. `factory.toml.sample` is committed as the project's suggestion, so each person on a shared repo can run a different formation. herdr holds the panes. `/factory continue` brings the team back, resuming noted sessions, and works out where the project stands from `FACTORY.md`, the tracker and Git.

```bash
python3 skill/scripts/factory.py harnesses      # available harnesses and recommended models
python3 skill/scripts/factory.py check          # validate the formation
python3 skill/scripts/factory.py up             # launch and brief, inside herdr
python3 tests/test_factory.py           # fake harnesses/herdr, real Git and API schema checks
FACTORY_LIVE=1 python3 tests/test_factory.py LiveFlags  # installed harness flags
```

Unknown top-level keys in `factory.toml` are rejected. Only `user`, `permissions`, `worktree_dir`, `[operator]` and `[members]` belong at the top level; operator fields such as `harness`, `model`, `thinking`, `messages` and `notes` go under `[operator]`.

[skill/SKILL.md](skill/SKILL.md) routes the operator; [skill/references/](skill/references/) covers setup, orchestration, recovery, the notes convention and the optional pull-request workflow. [skill/roles/](skill/roles/) defines each role in four parts (ownership, output, completion, prohibitions); [skill/templates/](skill/templates/) holds `FACTORY.md`, `factory.toml` and the common member instructions. [skill/harnesses.toml](skill/harnesses.toml) holds harness data; each harness's model resolution and launch code lives in [skill/scripts/factory.py](skill/scripts/factory.py). Tests and the Gate 1 fixture builder live in [tests/](tests/), outside the installed skill.

Codex models come from its local cache; pi, omp and OpenCode list them through their CLIs. Unknown Claude model IDs produce a warning; Claude validates them at launch. To add a harness, add its table (with `operator_wait`), code block and test fake.

Coming from a-team: install the skill as `factory`. The first command in a project with the earlier names prints the renames from `TEAM.md`, `team.toml` and `team.toml.sample` to `FACTORY.md`, `factory.toml` and `factory.toml.sample`. Follow them, then run `factory.py init` to update `.gitignore`.

A project that ran under a-team keeps its cache under `a-team`; new projects use `factory-skill`. Moving a project's cache is optional and done by a person, with no team running on that project and only if its destination does not exist. Under the cache base (`$XDG_CACHE_HOME`, or `~/.cache` when unset or empty), create `factory-skill` with `mkdir -p -m 700`, then `mv` the project's directory from `a-team` into it.
