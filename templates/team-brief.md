+++
# team-brief.md: the whole team in one file at the repo root. Settings in this block, prose below the
# closing +++ (role details, workflow, and a short project section at the end). ateam.py launches members
# from the settings; every member receives the brief with a roster table built from them, so nothing drifts.
# One team runs per herdr session.
#
# Member fields (the table key is the @name: lowercase, digits, - or _):
#   role         a role in the skill's roles/ (defaults to the name)
#   harness      claude | codex | pi | omp | opencode (v2+). Omitted: Claude model -> claude, GPT -> codex, else pi
#   model        exact model id; `ateam.py harnesses` lists what this machine can use
#   thinking     thinking level. Omitted: the role's default
#   notes        what this member owns on this team; the Role column of the roster
#   permissions  full | guarded, for this member only (see below)
#   worktree     true: its own git worktree on branch a-team/<name>. "some/branch": a worktree of that branch;
#                members that name the same branch share it. Omitted: the main checkout, shared with the team
#   args         extra native flags for its harness, e.g. args = ["--search"]
# Two of the same role get distinct names: [members.backend-dev-2] role = "backend-dev".
# Top-level keys must come before the first [table].

# Where @pm tracks tasks, owners, status and bugs, in words members can act on. Leave it out for no tracker.
#   "TODO.md at the repo root"
#   "GitHub Issues on owner/repo, via `gh issue` (board: GitHub Project 'Roadmap')"
#   "Linear team ENG, via the Linear MCP tools"
#   "the `todo` CLI (`todo --help`)"
tracker = "TODO.md at the repo root"
lead_commits = true   # only the lead runs git commit, merge and push

# Members never stop to ask for approval: every harness runs with no sandbox and no permission prompts
# ("full", the default). That is meant for a machine that is itself a sandbox or a VM.
# On a machine that is not, "guarded" keeps Claude Code and Codex members behind their own guard (auto mode;
# a sandbox with automatic review). They still run unattended but can stop and ask. pi, omp and OpenCode
# have no such guard, so for them it changes nothing.
# permissions = "guarded"

# worktree_dir = "../<repo>.worktrees"   # where members' worktrees go; this is the default
# [tabs]                                 # herdr tab labels; these are the defaults
# lead = "a-team driver"                 #   your own tab
# team = "a-team workers"                #   the one tab that holds every member's pane
# [never_thinking]                       # thinking levels this team never uses; the default is in harnesses.toml
# claude = ["max"]
# codex = ["max", "ultra"]

[lead]
name = "cto"
harness = "claude"
model = "claude-fable-5-2"
thinking = "medium"
notes = "Orchestrator and lead architect. Owns architecture and the big decisions, hands out work, does the final review, and is the only one who commits. The human talks to @cto, and can also talk to any member in its pane."

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

# Rosters. `ateam.py up` with no names launches `default`.
[teams]
default = ["lead-dev", "frontend-dev", "reviewer", "pm"]
planning = ["product", "architect", "pm"]
backend = ["lead-dev", "backend-dev", "qa-backend", "reviewer", "pm"]
frontend = ["frontend-dev", "designer", "qa-frontend", "reviewer", "pm"]
full = ["architect", "product", "lead-dev", "backend-dev", "frontend-dev", "designer", "reviewer", "pm", "qa-frontend", "qa-backend"]
+++
## Role details

- **@lead-dev** and **@frontend-dev** agree on the interface between the core and the UI before either builds against it, and keep each other posted when it changes.
- **@reviewer** reviews every task before {lead} sees it. A review request carries the task and its acceptance criteria.
- **@pm** owns the tracker: short, current, ordered by priority, with an owner and status on every item. Only @pm updates it; teammates ask @pm to add or change items.

## Workflow

1. {lead} sets direction and hands out tasks, each with its acceptance criteria; @pm records them in the tracker.
2. Developers implement. In the main checkout they leave their work uncommitted; in a worktree of their own they commit on their branch.
3. Developers ask @reviewer directly to review their diff or branch, and fix what it finds until it says `approve`.
4. {lead} does the final review, commits what is in the main checkout, and merges each reviewed branch. Nobody else merges or pushes.

## Project

<!-- One to three sentences: what we are building, for whom, and the stack. Details come from the lead. -->
