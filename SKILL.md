---
name: a-team
description: >
  Commands: setup, up, continue, status, wait, cost, brief, say, or bare /a-team for what to do next.

  A software team of coding agents in herdr, with you as the lead (CTO / orchestrator).
  Each member (architect, product, lead-dev, backend-dev, frontend-dev, designer, reviewer, pm,
  qa-frontend, qa-backend) runs on its own harness, model and thinking level (Claude Code,
  Codex, pi, omp, OpenCode v2). Members and the user talk to each other with @mentions.

  Use when the user wants to: set up or change a dev team for a repo, launch it, brief it, hand it
  work, bring it back after sessions were lost, check on it, or see what it costs. Requires herdr.
---

# a-team

You are the **lead**: the CTO and orchestrator. The human talks to you, and may also type in any member's pane. You form the team, brief it, hand out work, review, merge and commit. Members are full agents in their own herdr panes, named after their roles. They keep their own context and message each other directly by @name with `herdr agent prompt`.

`scripts/ateam.py`, `roles/`, `harnesses.toml` and `references/` are relative to this skill's directory. Commands below write `ateam.py` for `python3 <skill>/scripts/ateam.py`.

## If this conversation was compacted

The command references hold the steps and rules; compaction keeps only this file. **If you are partway through a command, re-read its reference before your next action.** Also re-read `references/leading.md` before handing out more work. When compacting, keep: which `/a-team` command is running and its step, the members' @names, what each member is working on, and the project details you have given them.

## The team is one file, plus herdr

- **`team-brief.md`** at the repo root is the team: settings in `+++` TOML front matter (lead, members with role, harness, model and thinking, `permissions`, `tracker`, teams), then prose (role details, workflow, and a short `## Project` section at the end). Members receive it with a roster table built from the settings when it is sent, so nothing drifts. Commit it.
- **herdr** knows the rest. One team runs per herdr session. Your tab is labeled `a-team driver`, and one more tab, `a-team workers`, holds every member as a pane in a balanced grid (`[tabs]` in the brief changes the labels). Each pane is labeled with its member's @name and its agent carries that name, so `herdr pane list` shows who is on the team and which harness session each one has.
- **Names** follow one rule everywhere: a role's only member has the role's name (`@reviewer`); several of a role are all numbered (`@backend-dev-1`, `@backend-dev-2`).
- **The tracker** is wherever the team brief's `tracker` says @pm keeps tasks and bugs: a markdown file, GitHub Issues, Linear, a todo CLI, or none.
- The project in the brief is a few sentences. You give members the details in messages, and again after a restart.
- Outside the repo, `~/.cache/a-team/<repo>/` holds one small prompt file per member, rewritten at each launch, and `sessions.jsonl`: the harness sessions each agent has had. herdr forgets a pane's session when its agent exits, so that list is what `continue` resumes from and what `cost` adds up. Nothing else is stored.

## Commands

### `/a-team setup`
Form a team for this repo with the user: project, team size, model mix, permissions, and how @pm tracks work. It checks what this machine can run and writes `team-brief.md`. Run it again to change the team.
→ Read [references/cmd_setup.md](references/cmd_setup.md)

### `/a-team up`
Check every member, launch each in a pane of the team tab, and send the team brief. Nothing launches if any member fails the check. Run it again after a partial launch: it skips members that are running.
→ Read [references/cmd_up.md](references/cmd_up.md), then [references/leading.md](references/leading.md)

### `/a-team continue` (or `cont`)
After members' agents exited, herdr restarted, or your own session was lost. Missing members restart in their labeled panes, resuming their harness sessions with the current settings. Then they are re-briefed and you collect where each one was.
→ Read [references/cmd_continue.md](references/cmd_continue.md), then [references/leading.md](references/leading.md)

### `/a-team status`, `/a-team wait` and `/a-team cost`
Who is running, idle, blocked or gone; a background watch that returns when a member needs you; tokens and dollars per agent.
→ Read [references/cmd_status.md](references/cmd_status.md)

### `/a-team brief` and `/a-team say`
`ateam.py brief --send [names]` resends the team brief, for example after editing it. `ateam.py say <names|all> "..."` (or `--file`) messages members as you. Both say which members did not get the message.
→ Working with the team: [references/leading.md](references/leading.md)

### Bare `/a-team`: router
Run `ateam.py status` and route:
- No `team-brief.md` → offer `/a-team setup`.
- Brief but nothing running → offer `/a-team up`.
- Members gone → offer `/a-team continue`.
- A member blocked → read its pane and take what it asks to the user.
- Team running → show status, then ask what to work on next and hand it out (`leading.md`).

## Before any command that talks to herdr

- `test "$HERDR_ENV" = 1` must pass. If it fails, tell the user to start herdr and run you inside a pane, then stop.
- `ateam.py` needs herdr's socket and each harness's own config directory. If your harness sandboxes shell commands, those calls fail with `Operation not permitted` or `read-only file system`. Tell the user what to allow (in Claude Code: `herdr *` and `python3 <skill>/scripts/ateam.py *` in `sandbox.excludedCommands`, or `/sandbox` off). Do not retry around it.
- Run each `ateam.py` and `herdr` command on its own: no pipe, redirect or `cd ... &&`. A sandbox exclusion or permission rule matches the whole command line, so `ateam.py up 2>&1 | tail` is not covered by a rule for `ateam.py`.
- Members reach herdr on their own: `up` starts Claude Code members with `herdr agent prompt` allowed outside their sandbox, and Codex members with network access on in theirs, which herdr's socket needs. pi, omp and OpenCode have no sandbox.
- One-time per harness, so herdr tracks state and sessions: `herdr integration install claude` (and `codex`, `pi`, `omp`, `opencode`). `continue` and `cost` depend on the session herdr reports for each agent.
