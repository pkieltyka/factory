# a-team

An agent skill for running a software team in [herdr](https://herdr.dev). The current agent becomes the orchestrator (@cto); members use Claude Code, Codex, pi, omp or OpenCode v2 and message each other directly. One team per herdr session, with an `a-team orchestrator` tab and an `a-team workers` tab. Parallel developers can use separate Git worktrees.

Requires Python 3.11+, herdr, and the chosen harnesses; Git is required for worktrees. Link this repo into your harness's skills directory, for example:

```bash
ln -s "$PWD" ~/.claude/skills/a-team
```

Run `/a-team setup` to write `team-brief.md`, then `/a-team up`. The brief holds the formation and project workflow; herdr holds the panes. `/a-team continue` resumes noted sessions.

```bash
python3 scripts/ateam.py harnesses      # available harnesses and recommended models
python3 scripts/ateam.py check          # validate the formation
python3 scripts/ateam.py up             # launch and brief, inside herdr
python3 scripts/test_ateam.py           # fake harnesses/herdr, real Git and API schema checks
ATEAM_LIVE=1 python3 scripts/test_ateam.py LiveFlags  # installed harness flags
```

`SKILL.md` routes the orchestrator; `references/` covers setup, orchestration and recovery. `roles/` defines each role in four parts (ownership, output, completion, prohibitions); `templates/` holds the brief and common member instructions. `harnesses.toml` holds harness data; each harness's model resolution and launch code lives in `scripts/ateam.py`.

Codex models come from its local cache; pi, omp and OpenCode list them through their CLIs. Unknown Claude model IDs produce a warning; Claude validates them at launch. To add a harness, add its table, code block and test fake.
