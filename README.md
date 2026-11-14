# herdr-team

An agent skill that makes the current coding agent the lead (CTO / orchestrator) of a team running in [herdr](https://herdr.dev) panes. Every member has its own role, harness, model and thinking level: Claude Code, Codex, pi, omp or OpenCode v2. Members talk with `@mentions` through `herdr agent prompt`.

- `SKILL.md`: the lead's playbook: formation, check, launch, team briefing, project brief, work, cost.
- `roles/*.md`: role rules. Frontmatter: `description`, `tier`, `thinking`, `readonly`.
- `team.toml`: the default formation (lead, members, workflow, teams). Override in `./agent-team.toml` or `~/.config/agent-team/team.toml`; see `examples/agent-team.toml`.
- `models.toml`: known Claude ids, thinking levels per harness, fallback models by tier, optional `[prices]`.
- `scripts/team.py`: `config`, `check`, `up`, `briefing`, `say`, `roster`, `cost`, `brief`, `init`.

```bash
python3 scripts/team.py config                 # the formation
python3 scripts/team.py check --team full      # can every member launch?
python3 scripts/team.py up                     # inside herdr: launch the default team and brief it
python3 scripts/team.py say all --file brief.md
python3 scripts/team.py cost
python3 -m unittest discover -s scripts        # fake herdr and harnesses; nothing is launched
```

Install as a skill by linking this directory into your harness's skills folder, e.g. `~/.claude/skills/herdr-team`.
