# devteam

An agent skill that makes the current coding agent the lead (CTO / orchestrator) of a software team running in [herdr](https://herdr.dev) panes. Every member has its own role, harness, model and thinking level: Claude Code, Codex, pi, omp or OpenCode v2. Members talk with `@mentions` through `herdr agent prompt`.

Commands: `/devteam setup`, `up`, `continue`, `status`, `cost`, `brief`, `say`, or bare `/devteam`.

The team lives in the repo:

- `.devteam/team-brief.md`: TOML front matter (lead, members, teams) plus prose (role details, workflow). The single source of truth, sent to every member with a roster built from the settings.
- `.devteam/project-brief.md`: what the team is building.
- `.devteam/state/`: git-ignored panes and session ids, so `continue` can resume members in their old sessions.

This repo:

- `SKILL.md`: the router; `references/cmd_*.md` hold each command's steps; `references/leading.md` covers working with the team.
- `roles/*.md`: role rules. Frontmatter: `description`, `tier`, `thinking`, `readonly`.
- `templates/team-brief.md`: the default formation that `devteam.py init` writes.
- `models.toml`: known Claude ids, thinking levels per harness, fallback models by tier, optional `[prices]`.
- `scripts/devteam.py`: `init`, `harnesses`, `show`, `check`, `up`, `continue`, `status`, `brief`, `say`, `cost`, `prompt`.

```bash
python3 scripts/devteam.py harnesses             # what this machine can run
python3 scripts/devteam.py init                  # .devteam/team-brief.md from the template
python3 scripts/devteam.py check --team full     # can every member launch?
python3 scripts/devteam.py up                    # inside herdr: launch and brief the default team
python3 -m unittest discover -s scripts          # fake herdr and harnesses; nothing is launched
```

Install by linking this directory into your harness's skills folder as `devteam`, e.g. `ln -s $PWD ~/.claude/skills/devteam`.
