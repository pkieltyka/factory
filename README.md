# a-team

An agent skill that makes the current coding agent the lead (CTO / orchestrator) of a software team running in [herdr](https://herdr.dev). Every member has its own role, harness, model and thinking level: Claude Code, Codex, pi, omp or OpenCode v2. Members, and the user, talk to each other with `@mentions` through `herdr agent prompt`.

Commands: `/a-team setup`, `up`, `continue`, `status`, `wait`, `cost`, `brief`, `say`, or bare `/a-team`.

The team is one file at the repo root, `team-brief.md`: TOML front matter (lead, members, permissions, tracker, teams) plus prose (role details, workflow, a short project section). It is sent to every member with a roster built from the settings. herdr holds the rest, one team per herdr session: the lead's tab (`a-team driver`), and one `a-team workers` tab with a pane per member, each labeled with its @name. So herdr knows the team, its panes and their sessions, which `continue` resumes and `cost` reads. Members that work in parallel can each have a git worktree.

This repo:

- `SKILL.md`: the router; `references/cmd_*.md` hold each command's steps; `references/leading.md` covers working with the team.
- `roles/*.md`: one file per role, each with the same four parts: what you own, what you output, when it is done, what you never do. Frontmatter: `description`, `thinking`, `readonly`. `{lead}` becomes the lead's @name.
- `templates/member-prompt.md`: the standing instructions every member gets around its role: how to talk to the team, the report form, the rules.
- `templates/team-brief.md`: the default formation that `ateam.py init` writes to the repo root.
- `harnesses.toml`: one table per harness with everything that is plain data: label, install hint, recommended models, thinking levels, how the member gets its instructions, the flags for each permission level, the start-up questions `up` answers by itself, where sessions live.
- `scripts/ateam.py`: the commands. Each harness has one block of code for what cannot be data: resolving a model, building the launch arguments, reading a session's usage.

Adding a model costs nothing on Codex, pi, omp and OpenCode (their lists are read live) and one line in `harnesses.toml` for Claude Code. Adding a harness is one table, one code block, and one fake in the tests.

```bash
python3 scripts/ateam.py harnesses               # what this machine can run
python3 scripts/ateam.py init                    # team-brief.md from the template
python3 scripts/ateam.py check --team full       # can every member launch?
python3 scripts/ateam.py up                      # inside herdr: launch and brief the default team
python3 -m unittest discover -s scripts          # fake herdr and harnesses; nothing is launched
ATEAM_LIVE=1 python3 -m unittest discover -s scripts   # also check each installed harness still has the flags used
```

Install by linking this directory (the `herdr-a-team` repo) into your harness's skills folder as `a-team`, e.g. `ln -s $PWD ~/.claude/skills/a-team`.
