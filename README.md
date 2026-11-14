# a-team

An agent skill that makes the current coding agent the lead (CTO / orchestrator) of a software team running in [herdr](https://herdr.dev) panes. Every member has its own role, harness, model and thinking level: Claude Code, Codex, pi, omp or OpenCode v2. Members talk with `@mentions` through `herdr agent prompt`.

Commands: `/a-team setup`, `up`, `continue`, `status`, `cost`, `brief`, `say`, or bare `/a-team`.

The team is one file at the repo root, `team-brief.md`: TOML front matter (lead, members, tracker, teams) plus prose (role details, workflow, a short project section). It is sent to every member with a roster built from the settings. There is no other state: each member's herdr pane is labeled with its @name, so herdr knows the team, its panes and their sessions, which `continue` resumes and `cost` reads.

This repo:

- `SKILL.md`: the router; `references/cmd_*.md` hold each command's steps; `references/leading.md` covers working with the team.
- `roles/*.md`: role rules. Frontmatter: `description`, `tier`, `thinking`, `readonly`.
- `templates/team-brief.md`: the default formation that `ateam.py init` writes to the repo root.
- `models.toml`: known Claude ids, thinking levels per harness, fallback models by tier, optional `[prices]`.
- `scripts/ateam.py`: `init`, `harnesses`, `show`, `check`, `up`, `continue`, `status`, `brief`, `say`, `cost`, `prompt`.

```bash
python3 scripts/ateam.py harnesses               # what this machine can run
python3 scripts/ateam.py init                    # team-brief.md from the template
python3 scripts/ateam.py check --team full       # can every member launch?
python3 scripts/ateam.py up                      # inside herdr: launch and brief the default team
python3 -m unittest discover -s scripts          # fake herdr and harnesses; nothing is launched
```

Install by linking this directory (the `herdr-a-team` repo) into your harness's skills folder as `a-team`, e.g. `ln -s $PWD ~/.claude/skills/a-team`.
