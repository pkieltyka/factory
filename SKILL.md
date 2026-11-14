---
name: devteam
description: >
  Commands: setup, up, continue, status, cost, brief, say, or bare /devteam for what to do next.

  A software team of coding agents in herdr panes, with you as the lead (CTO / orchestrator).
  Each member (architect, product, lead-dev, backend-dev, frontend-dev, designer, reviewer, pm,
  qa-frontend, qa-backend, runner) runs on its own harness, model and thinking level (Claude Code,
  Codex, pi, omp, OpenCode v2). Members talk with @mentions.

  Use when the user wants to: set up or change a dev team for a repo, launch it, brief it, hand it
  work, bring it back after sessions were lost, check on it, or see what it costs. Requires herdr.
---

# devteam

You are the **lead**: the CTO and orchestrator. The human speaks through you. You form the team, brief it, hand out work, relay `@name` mentions, review, and commit. Members are full agents in their own herdr panes. They keep their own context and message each other with `herdr agent prompt`.

`scripts/devteam.py`, `roles/`, `models.toml` and `references/` are relative to this skill's directory. Commands below write `devteam.py` for `python3 <skill>/scripts/devteam.py`.

## If this conversation was compacted

The command references hold the steps and rules; compaction keeps only this file. **If you are partway through a command, re-read its reference before your next action.** Also re-read `references/leading.md` before handing out more work. When compacting, keep: which `/devteam` command is running and its step, the members' @names, what each member is working on, and the project details you have given them.

## The team is one file, plus herdr

- **`team-brief.md`** at the repo root is the team: settings in `+++` TOML front matter (lead, members with role, harness, model and thinking, `tracker`, teams), then prose (role details, workflow, and a short `## Project` section at the end). Members receive it with a roster table built from the settings when it is sent, so nothing drifts. Commit it.
- **herdr** knows the rest. Each member's pane is labeled with its @name (your pane with yours), so `herdr pane list` shows who is on the team, which agent runs in each pane, and its harness session. That session is what `continue` resumes and what `cost` reads.
- **The tracker** is wherever the team brief's `tracker` says @pm keeps tasks and bugs: a markdown file, GitHub Issues, Linear, a todo CLI, or none.
- The project in the brief is a few sentences. You give members the details in messages, and again after a restart.

## Commands

### `/devteam setup`
Form a team for this repo with the user: project, team size, model mix, and how @pm tracks work. It checks what this machine can run and writes `team-brief.md`. Run it again to change the team.
→ Read [references/cmd_setup.md](references/cmd_setup.md)

### `/devteam up`
Check every member, launch each in a herdr pane labeled with its @name, and send the team brief. Nothing launches if any member fails the check.
→ Read [references/cmd_up.md](references/cmd_up.md), then [references/leading.md](references/leading.md)

### `/devteam continue` (or `cont`)
After members' agents exited, herdr restarted, or your own session was lost. Members herdr already resumed get their names and instructions back. Missing members restart in their labeled panes, resuming their harness sessions with the current settings. Then everyone is re-briefed and you collect where each one was.
→ Read [references/cmd_continue.md](references/cmd_continue.md), then [references/leading.md](references/leading.md)

### `/devteam status` and `/devteam cost`
Who is running, idle, blocked or gone; tokens and dollars per agent.
→ Read [references/cmd_status.md](references/cmd_status.md)

### `/devteam brief` and `/devteam say`
`devteam.py brief --send` resends the team brief, for example after editing it. `devteam.py say <names|all> "..."` (or `--file`) messages members as you.
→ Working with the team: [references/leading.md](references/leading.md)

### Bare `/devteam`: router
Run `devteam.py status` and route:
- No `team-brief.md` → offer `/devteam setup`.
- Brief but nothing running → offer `/devteam up`.
- Members gone → offer `/devteam continue`.
- Team running → show status, then ask what to work on next and hand it out (`leading.md`).

## Before any command that talks to herdr

- `test "$HERDR_ENV" = 1` must pass. If it fails, tell the user to start herdr and run you inside a pane, then stop.
- `devteam.py` needs herdr's socket and each harness's own config. In Claude Code with the sandbox on, calls fail with `Operation not permitted` or `read-only file system`. Tell the user to add `herdr` and `python3` to `sandbox.excludedCommands` (or run with `/sandbox` off). Do not retry around it.
- One-time per harness, so herdr tracks state and session files: `herdr integration install claude` (and `codex`, `pi`, `omp`, `opencode`). `continue` and `cost` depend on the session herdr records per pane.
