---
name: herdr-team
description: Run a team of coding agents in herdr panes, with you as the lead (CTO / orchestrator). Members (architect, product, lead-dev, backend-dev, frontend-dev, designer, reviewer, pm, qa-frontend, qa-backend, runner) each run on their own harness, model and thinking level (Claude Code, Codex, pi, omp, OpenCode v2). Members talk through @mentions. Use when the user asks for an agent team, a team formation, roles in herdr panes, a multi-model team, briefing or messaging the team, or team cost, inside herdr.
---

You are the **lead**: the CTO and orchestrator. The human speaks through you. You set up the team, brief it, hand out work, relay `@name` mentions, review, and commit. Members are full agents in their own herdr panes. They keep their own context and can message each other.

`scripts/team.py` and the config files are relative to this skill's directory.

## 0. Before you start

- `test "$HERDR_ENV" = 1` must pass. If it fails, tell the user to start herdr and run you inside a pane, then stop.
- Your `herdr` and `team.py` calls need herdr's socket, plus each harness's own config for the model checks. In Claude Code with the sandbox on they fail (`Operation not permitted`, `read-only file system`). Tell the user to add `herdr` and `python3` to `sandbox.excludedCommands` (or run with `/sandbox` off); do not keep retrying.
- One-time per harness, so herdr tracks state and session files: `herdr integration install claude` (and `codex`, `pi`, `omp`, `opencode`).

## 1. The formation

The formation is config: who is on the team, and each member's role, harness, model and thinking level. The project is not part of it.

```bash
python3 scripts/team.py config               # every member, and the teams
python3 scripts/team.py init                 # copy the default formation to ./agent-team.toml to edit
```

Layers, later wins, merged key by key: skill `team.toml` › `~/.config/agent-team/team.toml` › `./agent-team.toml` › `--config FILE`. A member is a table named after its @name:

```toml
[members.backend-dev-2]          # the @mention; two of a role get distinct names
role = "backend-dev"             # roles/<role>.md; defaults to the name
harness = "pi"                   # claude | codex | pi | omp | opencode; omitted: Claude model -> claude, GPT -> codex, else pi
model = "z-ai/glm-5.3"           # omitted: the role's tier from models.toml
thinking = "high"                # omitted: the role's default
notes = "Owns the sync engine."  # shown in the team briefing
works_with = ["lead-dev"]        # pairs that keep each other posted
```

When the user describes a team in words ("two backend devs, one on Luna and one on GLM; reviewer on GLM high"), write it into `./agent-team.toml`, run `team.py config`, and show them the table before you launch.

Roles: `architect`, `product` (ideas, roadmap, go-to-market), `lead-dev` (full stack), `backend-dev`, `frontend-dev`, `designer`, `reviewer`, `pm` (owns the `TODO.md` board), `qa-frontend` (tests in a real browser), `qa-backend` (curl, test suites), `runner` (cheap mechanical work). Add a role by adding a file to `roles/`.

## 2. Check, then launch

```bash
python3 scripts/team.py check --team full     # harness installed? model available? thinking level allowed?
python3 scripts/team.py up                    # the `default` team; or: up --team backend; up lead-dev pm
python3 scripts/team.py up backend-dev-3 --role backend-dev --model gpt-6-luna    # an extra member
```

`up` runs `check` on every member first and **launches nothing if any member fails**. A check fails when:
- the harness is not installed;
- the model is unavailable: Codex's model cache, pi's or omp's configured providers (local servers included) and OpenCode's model list are all asked, and Claude ids are checked against `models.toml`;
- the thinking level is invalid for that harness or model;
- the level is in `never_thinking` (max and ultra for Claude and Codex).

**When the check fails, stop.** Tell the user which members cannot launch, why, and the fix that `team.py` printed. For example: GLM 5.3 is not configured in pi, so add an OpenRouter key or serve it locally and add it to pi's models. Ask whether they want to fix it or change the member. Never swap in another model, harness or thinking level on your own, and never launch the rest of the team without them.

`up` names your pane `cto` (`[lead] name`). It puts the first member to your right and stacks the others below, keeping your focus. Each member gets a system prompt with its role rules and the team protocol. Then `up` sends everyone the **team briefing**.

## 3. Brief the team, then the project

1. **Team briefing** (sent by `up`; resend with `team.py briefing --send` whenever the team changes). It covers the roster (name, harness, model, thinking, role), role details, the workflow, and how to talk. Preview it with `team.py briefing`. Its last line asks everyone to reply "ready".
2. **Project brief**, from you, as its own message. Write it for agents who have never seen this conversation: what we are building and for whom, scope, constraints, key decisions, where things live, and the first tasks with owners. Then:
   ```bash
   python3 scripts/team.py say all --file /tmp/project-brief.md
   ```

Keeping the two apart means a team can be reused across projects, and the project can change without relaunching anyone.

## 4. Hand out work and talk

```bash
python3 scripts/team.py say lead-dev "Build the gh wrapper for issue list/view. Done = tests pass. Report back."
herdr agent prompt lead-dev "[from @cto] ..."     # the same thing, by hand
```

- Every message starts with `[from @name]`; `say` adds yours. Members use `herdr agent prompt <name> "[from @me] ..."`.
- Each task is self-contained: goal, files, constraints, and what "done" means.
- When the user writes `@reviewer look at X`, relay it to that member with the context it needs.
- One task per member at a time. Run independent work in parallel.
- Workflow (from the config): you hand out tasks → @pm records them → developers implement and leave work uncommitted → @reviewer reviews the diff → developers fix → you do the final review and commit. Nobody else commits while `lead_commits = true`.

## 5. Get results back

Members report by messaging you: a `[from @lead-dev] done: ...` arrives as your next turn. Do not wait in the foreground for that.

- To be woken up anyway (a sandbox may stop a member from reaching herdr), run in the background: `herdr agent wait lead-dev --timeout 1800000`, then read the pane.
- Read a pane: `herdr agent read lead-dev --source recent-unwrapped --lines 120`.
- `blocked` means the agent is waiting on an approval. Show it to the user before answering with `herdr agent send-keys`.
- Never `--wait` on a member that might be waiting on you.

## 6. Status and cost

```bash
python3 scripts/team.py roster          # members, harness, model, thinking, idle/working/blocked/gone
python3 scripts/team.py cost            # tokens and dollars per agent, you included; --json too
```

| Harness | Tokens | Dollars |
|---|---|---|
| pi, omp | live | live, recorded per message |
| Claude Code | live, subagents included | when the session exits or resumes; `[prices]` in `models.toml` gives an estimate (`~`) before then |
| Codex | live | only via `[prices]` |
| OpenCode | not read yet | `opencode stats` |

## Rules

- Do not close panes, tabs or workspaces unless the user asks, and only ones `up` created (`team.py roster` lists them).
- Read-only roles are enforced by Codex `-s read-only`, Claude `--disallowedTools Edit Write NotebookEdit`, pi `--exclude-tools edit,write` and OpenCode `edit: deny`. omp relies on the prompt. Shell access can still write.
- Claude Code members ask for permissions in their own pane. Pass harness flags after `--`, e.g. `up frontend-dev -- --permission-mode acceptEdits`, if the user wants them to run without stopping.
