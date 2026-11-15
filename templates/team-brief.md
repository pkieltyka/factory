+++
# Formation: keep chosen members; all roles remain available in the skill's roles/.
# Each member needs harness + model; thinking defaults to its role. Optional fields:
# role (for numbered names), notes (project ownership), permissions (full/guarded),
# args (native flags), worktree (true => a-team/<name>; a branch string => that checkout).
# The first member keeps the plain role name; later ones use -2, -3, etc. Never rename.
# Top-level settings precede tables. Omit tracker for none. Full permissions assume a safe machine;
# guarded adds a guard only on Claude/Codex. See SKILL.md for sandbox limits.
tracker = "TODO.md at the repo root"
# permissions = "guarded"
# worktree_dir = "../<repo>.worktrees"

[orchestrator]
name = "cto"   # the orchestrator's @name, pane label and herdr agent name. It never changes while the team
               # runs. If you change it, change @cto in the prose below too.
harness = "claude"
model = "claude-fable-5-2"
thinking = "medium"
notes = "Owns architecture, assigns tasks and integrates reviewed work."

[members.lead-dev]
harness = "codex"
model = "gpt-6-astra"
thinking = "high"

[members.reviewer]
harness = "codex"
model = "gpt-6-luna"
thinking = "high"

[members.pm]
harness = "pi"
model = "deepseek/deepseek-v4.1-flash"
thinking = "low"

# Uncomment for a UI project.
# [members.frontend-dev]
# harness = "claude"
# model = "claude-opus-5-5"
# thinking = "medium"
+++
## Workflow

1. @cto assigns tasks with acceptance criteria; @pm records owners, criteria, branch/commit, verdict and next step in the tracker. Only @pm updates it.
2. Developers agree shared interfaces before parallel work, implement, then request @reviewer with the task, criteria and exact diff/commit. Fix findings until approved.
3. @cto reviews the result, commits shared-checkout work and merges reviewed branches.

## Project

<!-- What we are building, for whom, and the stack. Detailed tasks come from the orchestrator (@cto). -->
