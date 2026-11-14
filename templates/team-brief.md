+++
# The team brief: one file holds the team's settings (this block) and its prose (below the closing +++).
# devteam.py reads the settings to launch members; every member receives the whole brief, with a
# roster table built from these settings when it is sent, so the two cannot drift apart.
#
# Member fields (the table key is the @name: lowercase, digits, - or _):
#   role      a role in the skill's roles/ (defaults to the name)
#   harness   claude | codex | pi | omp | opencode (v2+). Omitted: Claude model -> claude, GPT -> codex, else pi
#   model     exact model id. Omitted: the role's tier from the skill's models.toml
#   tier      top | mid | fast, used only without a model
#   thinking  thinking level. Omitted: the role's default
#   notes     what this member owns on this team; the Role column of the roster
# Two of the same role get distinct names: [members.backend-dev-2] role = "backend-dev".
# Top-level keys must come before the first [table].

board = "TODO.md"     # the PM's board of todos, owners, status, bugs and decisions
lead_commits = true   # only the lead runs git commit / git push

[lead]
name = "cto"
harness = "claude"
model = "claude-fable-5-2"
thinking = "medium"
notes = "Orchestrator and lead architect. Owns architecture and the big decisions, hands out work, does the final review, and is the only one who commits. The human speaks through @cto."

[never_thinking]      # levels never used, even if a member asks for them
claude = ["max", "ultra"]
codex = ["max", "ultra"]

[members.architect]
harness = "claude"
model = "claude-fable-5-2"
thinking = "high"

[members.product]
harness = "claude"
model = "claude-opus-5-5"
thinking = "medium"

[members.lead-dev]
harness = "codex"
model = "gpt-6.1-sol"
thinking = "high"

[members.backend-dev]
harness = "pi"
model = "z-ai/glm-5.3"
thinking = "medium"

[members.frontend-dev]
harness = "claude"
model = "claude-opus-5-5"
thinking = "medium"

[members.designer]
harness = "claude"
model = "claude-sonnet-5-5"
thinking = "high"

[members.reviewer]
harness = "codex"
model = "gpt-6-astra"
thinking = "high"

[members.pm]
harness = "pi"
model = "deepseek/deepseek-v4.1-flash"
thinking = "low"

[members.qa-frontend]
harness = "claude"
model = "claude-sonnet-5-5"
thinking = "medium"

[members.qa-backend]
harness = "pi"
model = "z-ai/glm-5.3-flash"
thinking = "medium"

# Rosters. `devteam.py up` with no names launches `default`.
[teams]
default = ["lead-dev", "frontend-dev", "reviewer", "pm"]
planning = ["product", "architect", "pm"]
backend = ["lead-dev", "backend-dev", "qa-backend", "reviewer", "pm"]
frontend = ["frontend-dev", "designer", "qa-frontend", "reviewer", "pm"]
full = ["architect", "product", "lead-dev", "backend-dev", "frontend-dev", "designer", "reviewer", "pm", "qa-frontend", "qa-backend"]
+++
## Role details

- **@lead-dev** and **@frontend-dev** agree on the interface between the core and the UI before either builds against it, and keep each other posted when it changes.
- **@reviewer**: when @cto asks for a review, read the diff (`git status`, `git diff`, new untracked files), build and test where that helps, and report findings ordered by severity, each with `file:line` and a concrete failure scenario. Read-only: you report, the developers fix.
- **@pm** owns `TODO.md`: short, current, ordered by priority, with an owner and status on every item. Only @pm edits it; teammates ask @pm to add or update items.

## Workflow

1. @cto sets direction and hands out tasks; @pm records them in `TODO.md`.
2. Developers implement and leave their work uncommitted.
3. @reviewer does the first-pass review of the diff; the developers fix what it finds.
4. @cto does the final review and commits. Nobody else runs `git commit` or `git push`.
