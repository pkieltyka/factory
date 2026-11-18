# Setup

A project has `TEAM.md` (the shared brief, committed) and `team.toml` (your own formation, not committed). `team.toml.sample` is the project's suggested formation.

- **The project already has `TEAM.md`:** someone set the team up. Run `ateam.py init`; it copies `team.toml.sample` to your `team.toml`. Ask only what the user wants different for themselves (other models or harnesses, one model for everything, fewer members) and edit `team.toml`. Check `user`, a top-level key above `[operator]`: it is the first part of this person's worktree branches. `init` prints it when the sample sets one. Leave `TEAM.md` alone unless they ask.
- **Only an old `team-brief.md` exists:** `ateam.py` stops and prints how to split it by hand into `team.toml` and `TEAM.md`. Do that; never run `init` to get past it.
- **Neither file exists:** follow the steps below.
- **Both exist:** change only what the user requested.

1. Inspect the repo's stack, UI and existing tracker. Run `ateam.py harnesses`; recommend available models and report missing-harness fixes.
2. Propose the default below. Infer known facts; ask in one round only for missing project scope, UI needs, parallel work/worktrees, where tasks are tracked, what marks one ready to start, whether to keep notes and a private notes folder, any spec, browser or review tool the project uses, and whether work reaches the main branch through pull requests. Custom formations, model mixes and trackers remain available.
3. Show name, role, harness, model, thinking and project responsibility. Adjust until agreed; keep the current operator session unless the user requests a change.

## Default formation

| Member | Harness/model |
|---|---|
| @operator | Current operator; Claude Code Opus or Fable recommended |
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

Run `ateam.py init`. It writes what is missing (`TEAM.md`, `team.toml.sample`, your `team.toml`), replaces nothing, and adds `team.toml` to `.gitignore`.

Edit `team.toml`, your formation:

- Keep the chosen members, each with explicit harness, model and project-specific `notes`. `up` starts every member in it; there are no named rosters.
- Use `worktree = true` for developers who work in parallel: each gets branch `<user>/<member>` (for example `peter/lead-dev`), so two people's branches do not collide when their `user` differs. `user` is a top-level key above `[operator]` and defaults to the system user name; set it when that name is generic (`root`, `ubuntu`).
- Match the operator's model/thinking to the current session, not the template defaults.

Edit `TEAM.md`, the shared brief, as plain Markdown without comments. Write it by role ("the operator", never an @name), so it holds for any person's formation:

- `## Project`: what, for whom, stack. Detailed tasks go in messages.
- `## Tasks`: where tasks and bugs are tracked and with what tool, in words a member can act on (a repo file, GitHub Issues with `gh`, Linear, another CLI). A file suits one person; several people on the repo need a shared tool, with the person's name next to the member's in an item's owner (`peter/@lead-dev`). @pm owns the tracker when present; otherwise you track tasks. Say what marks an item ready to start and who sets it. With `TODO.md` as the tracker and none in the repo, copy the skill's `templates/TODO.md` to the repo root.
- `## Workflow`: short. Review and integration rules, and who commits: shared-checkout members leave commits to you; worktree members commit their own branches. It must not contradict the standing instructions. Keep or change its defaults: one feature at a time, one writing task per shared checkout, three review rounds.
- `## Notes`: `Disabled.`, or the location and the folders the user chose, for example "Enabled in `notes/`: `activity/` (the team's working notes), `research/` (findings the owner asked for), `discussions/` (records of meetings and their decisions). Follow the a-team notes convention; search with rg." Name the project's search tool instead of rg if it has one. You may recommend notes; never enable them unasked, and on a rerun keep what is there. With notes enabled, ask whether the person keeps private notes for their team to read. If so, add to the section "`notes/private/` holds each person's own notes, ignored by Git. That person's team may read and search it, under the private-folder rule of the notes convention."; add `/notes/private/` to `.gitignore`; and check that `git check-ignore -q notes/private/x` succeeds and `git ls-files notes/private` prints nothing.
- `## Specs` (how specifications are written and where they live), `## Browser` (the browser tool for UI work and QA), `## Review` (only when the project uses a review CLI for the deep lane: which lane a review starts in, fast or deep, and when the other runs, for example deep at pull-request readiness; what the tool can review, a commit range or also uncommitted work; its limits: depth, time, publishes nothing): only for a tool the project uses, in that tool's own words: where, which tool, how to learn it, one cheap check command. Run the check before you write the section.
- `## Pull requests`: only when the project integrates work through pull requests. It says: the integration branch; the ticket-branch pattern (`<user>/<ticket>`); that the team merges only by squash merge; the standing permissions, each on its own line: whether the operator may push ticket branches and open or update pull requests, and whether it may merge (otherwise the user merges); whether the project has CI; that the task record links the pull request and a ticket closes only after its merge is confirmed; and the forge's commands, in its own words, to open a pull request, wait for its checks with a time limit, read its head commit, checks, review threads and mergeability, and merge at an exact head commit, with one cheap check command. The two permission lines are the user's: write what they said and leave out what they have not decided. Update the Workflow rule on committing and integration to match.

When the formation should be the project's suggestion, copy `team.toml` to `team.toml.sample` and make the copy portable: remove `user` and `worktree_dir`, and any worktree branch or path that carries a person's name.

Run `ateam.py check`. Resolve each failure without substituting unapproved models or harnesses; unknown Claude IDs only warn. Preview with `ateam.py brief`, show the result, and commit `TEAM.md` and `team.toml.sample` when authorized; never `team.toml`. Next: `/a-team up`.
