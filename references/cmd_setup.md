# Setup

Start from the existing brief, if present; change only what the user requested.

1. Inspect the repo's stack, UI and existing tracker. Run `ateam.py harnesses`; recommend available models and report missing-harness fixes.
2. Propose the default below. Infer known facts; ask in one round only for missing project scope, UI needs, parallel work/worktrees and tracker choice. Custom formations, model mixes and trackers remain available.
3. Show name, role, harness, model, thinking and project responsibility. Adjust until agreed; keep the current orchestrator session unless the user requests a change.

## Default formation

| Member | Harness/model |
|---|---|
| @cto | Current orchestrator; Claude Code Opus or Fable recommended |
| @lead-dev | Codex gpt-6-astra |
| @reviewer | Codex gpt-6-luna |
| @pm | pi deepseek/deepseek-v4.1-flash |
| @frontend-dev, only for a UI | Claude Code claude-opus-5-5 |

- Add catalog roles only for concrete work; an architect is optional for larger systems.
- Tip for custom teams: a reviewer from a different model family can catch different mistakes. The default remains Luna.
- Frontend, designer and frontend QA need a real browser tool. Verify availability or ask before assigning browser work.
- Use the role's thinking default unless the task warrants another supported level. The highest Claude/Codex levels are refused by `harnesses.toml`.
- Separate developers' worktrees when parallel changes could collide. Otherwise they share the main checkout.
- Default permissions are full; explain the safe-machine assumption in SKILL.md. Use guarded if the user requests it.

## Write and validate

With no brief, run `ateam.py init`. Edit the brief:

- Keep the chosen members, each with explicit harness, model and project-specific `notes`. Use `worktree = true` for separate developers. `up` starts every member in the brief; there are no named rosters.
- Set `tracker` to actionable instructions (repo file, GitHub Issues, Linear or another tool), or omit it. @pm owns it when present; otherwise you track tasks.
- Keep the workflow short: project-specific responsibilities, review and integration. End with `## Project`: what, for whom, stack. Detailed tasks go in messages.
- Match the orchestrator's model/thinking to the current session, not the template defaults. Check the rendered brief against standing instructions: shared-checkout members leave commits to you; worktree members commit their own branches. Scope and ownership must agree throughout.

Run `ateam.py check`. Resolve each failure without substituting unapproved models or harnesses; unknown Claude IDs only warn. Preview with `ateam.py brief`, show the result, and commit when authorized. Next: `/a-team up`.
