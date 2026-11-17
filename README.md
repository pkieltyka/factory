# a-team

An agent skill for running a software team in [herdr](https://herdr.dev). The current agent becomes the operator, `@operator`: the lead agent. You, the user, are the owner who directs it; members use Claude Code, Codex, pi, omp or OpenCode v2 and message each other directly. One team per herdr session, with an `a-team operator` tab and an `a-team workers` tab. Parallel developers can use separate Git worktrees.

Requires Python 3.11+, herdr, and the chosen harnesses; Git is required for worktrees. Link this repo into your harness's skills directory, for example:

```bash
ln -s "$PWD" ~/.claude/skills/a-team
```

Run `/a-team setup`, then `/a-team up`. Setup writes two files at the project root: `TEAM.md`, the brief every member reads (project, where tasks are tracked, workflow), committed and shared; and `team.toml`, your own formation (operator, members, harnesses, models), which is not committed. `team.toml.sample` is committed as the project's suggestion, so each person on a shared repo can run a different formation. herdr holds the panes. `/a-team continue` brings the team back, resuming noted sessions, and works out where the project stands from `TEAM.md`, the tracker and Git.

```bash
python3 scripts/ateam.py harnesses      # available harnesses and recommended models
python3 scripts/ateam.py check          # validate the formation
python3 scripts/ateam.py up             # launch and brief, inside herdr
python3 scripts/test_ateam.py           # fake harnesses/herdr, real Git and API schema checks
ATEAM_LIVE=1 python3 scripts/test_ateam.py LiveFlags  # installed harness flags
```

Unknown top-level keys in `team.toml` are rejected. Only `user`, `permissions`, `worktree_dir`, `[operator]` and `[members]` belong at the top level; operator fields such as `harness`, `model`, `thinking` and `notes` go under `[operator]`.

`SKILL.md` routes the operator; `references/` covers setup, orchestration and recovery. `roles/` defines each role in four parts (ownership, output, completion, prohibitions); `templates/` holds `TEAM.md`, `team.toml` and the common member instructions. `harnesses.toml` holds harness data; each harness's model resolution and launch code lives in `scripts/ateam.py`.

Codex models come from its local cache; pi, omp and OpenCode list them through their CLIs. Unknown Claude model IDs produce a warning; Claude validates them at launch. To add a harness, add its table, code block and test fake.
