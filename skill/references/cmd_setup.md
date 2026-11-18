# Setup

- **The project already has `FACTORY.md`:** someone set the team up. Run `factory.py init`; it copies `factory.toml.sample` to your `factory.toml`. Ask only what the user wants different for themselves (other models or harnesses, one model for everything, fewer members) and edit `factory.toml`. Check `user`, a top-level key above `[operator]`: it is the first part of this person's worktree branches. `init` prints it when the sample sets one. Leave `FACTORY.md` alone unless they ask.
- **Neither file exists:** follow the steps below.
- **Both exist:** change only what the user requested.

1. Inspect the repo's stack, UI and existing tracker. Run `factory.py harnesses`; recommend available models and report missing-harness fixes.
2. Propose the default below. Infer known facts; ask in one round only for missing project scope, UI needs, parallel work/worktrees, where tasks are tracked, what marks one ready to start, whether to keep notes and a private notes folder, any spec, browser or review tool the project uses, and whether work reaches the main branch through pull requests. Custom formations, model mixes and trackers remain available. The skill's `recipes/` hold tested choices for the optional sections: propose one when its check passes on this machine, name the alternative, and keep a tracker or tool the project already uses. For tasks, propose `gh` when the remote is GitHub, its check passes and the owner confirms issue-write permission, otherwise `TODO.md`; `todos` is the board choice.
3. Show name, role, harness, model, thinking and project responsibility. Adjust until agreed; keep the current operator session unless the user requests a change.

## Default formation

The default is [templates/factory.toml](../templates/factory.toml): @lead-dev, @reviewer and @pm, plus @frontend-dev only for a UI. `init` seeds a new project's `factory.toml.sample` from it; where the project already has a sample, `init` copies that to your `factory.toml`. Keep the current operator session; Claude Code Opus or Fable is recommended.

- Add catalog roles only for concrete work; an architect is optional for larger systems.
- Tip for custom teams: a reviewer from a different model family can catch different mistakes. The template's reviewer stays the default.
- Frontend, designer and frontend QA need a real browser tool. Verify availability or ask before assigning browser work.
- Use the role's thinking default unless the task warrants another supported level. The highest Claude/Codex levels are refused by `harnesses.toml`.
- Separate developers' worktrees when parallel changes could collide. Otherwise they share the main checkout.
- Default permissions are full: explain the safe-machine assumption (Permissions, below). Use guarded if the user requests it.

## Write and validate

Run `factory.py init`. It writes what is missing (`FACTORY.md`, `factory.toml.sample`, your `factory.toml`), replaces nothing, and adds `factory.toml` to `.gitignore`.

Edit `factory.toml`, your formation:

- Keep the chosen members, each with explicit harness, model and project-specific `notes`. `up` starts every member in it; there are no named rosters.
- Use `worktree = true` for developers who work in parallel: each gets branch `<user>/<member>` (for example `peter/lead-dev`), so two people's branches do not collide when their `user` differs. `user` is a top-level key above `[operator]` and defaults to the system user name; set it when that name is generic (`root`, `ubuntu`).
- Match the operator's model/thinking to the current session, not the template defaults.

Edit `FACTORY.md`, the shared brief, as plain Markdown without comments. Paste the chosen recipe's section and fill the names in capitals; names in angle brackets stay for members. Write it by role ("the operator", never an @name), so it holds for any person's formation:

- `## Project`: what, for whom, stack. Detailed tasks go in messages.
- `## Tasks`: where tasks and bugs are tracked and with what tool, in words a member can act on (a repo file, GitHub Issues with `gh`, Linear, another CLI). A file suits one person; several people on the repo need a shared tool, with the person's name next to the member's in an item's owner (`peter/@lead-dev`). @pm owns the tracker when present; otherwise you track tasks. Say what marks an item ready to start and who sets it. With `TODO.md` as the tracker and none in the repo, copy the skill's `templates/TODO.md` to the repo root.
- `## Workflow`: short. Review and integration rules. Who commits follows from each member's checkout and is in its standing instructions: do not restate it, and where the project's workflow has to name it, as with pull requests, never contradict it. Keep or change its defaults: one feature at a time, one writing task per shared checkout, three review rounds.
- `## Notes`: `Disabled.`, or the location and the folders the user chose, for example "Enabled in `notes/`: `activity/` (the team's working notes), `research/` (findings the owner asked for), `discussions/` (records of meetings and their decisions). Follow the factory notes convention; search with rg." Name the project's search tool instead of rg if it has one. You may recommend notes; never enable them unasked, and on a rerun keep what is there. With notes enabled, ask whether the person keeps private notes for their team to read. If so, add to the section "`notes/private/` holds each person's own notes, ignored by Git. That person's team may read and search it, under the private-folder rule of the notes convention."; add `/notes/private/` to `.gitignore`; and check that `git check-ignore -q notes/private/x` succeeds and `git ls-files notes/private` prints nothing.
- `## Review`: only when the project uses a review CLI for the deep lane. `## Pull requests`: only when work reaches the integration branch through pull requests. Each stands alone: a project may have either without the other. After its check passes, follow the recipe ([recipes/review-codegenie.md](../recipes/review-codegenie.md), [recipes/pull-requests-github.md](../recipes/pull-requests-github.md)): its setup directions as well as its pasteable section. For another tool or forge, write the same section in its own words, with a counterpart for every direction and line of the recipe and one cheap check command. The two permission lines of `## Pull requests` are the user's: write what they said and leave out what they have not decided. Then make the Workflow section's integration rule match.
- `## Specs` (how specifications are written and where they live) and `## Browser` (the browser tool for UI work and QA): only for a tool the project uses, in that tool's own words: where, which tool, how to learn it, one cheap check command. Run the check before you write the section.

When the formation should be the project's suggestion, copy `factory.toml` to `factory.toml.sample` and make the copy portable: remove `user` and `worktree_dir`, and any worktree branch or path that carries a person's name.

Run `factory.py check`. Resolve each failure without substituting unapproved models or harnesses; unknown Claude IDs only warn. Preview with `factory.py brief`, show the result, and commit `FACTORY.md` and `factory.toml.sample` when authorized; never `factory.toml`. Next: `/factory up`.

## Permissions

- Members default to `full`, intended for a safe sandbox/VM. Claude uses bypass-permissions while retaining inherited sandbox settings; Codex disables approvals and its sandbox; pi/omp/OpenCode allow unattended commands. `guarded` uses Claude auto mode or Codex's workspace sandbox with automatic review; it changes nothing for the others.
- Full Claude members permit Unix sockets inside an inherited sandbox: all sockets on Linux, `HERDR_SOCKET_PATH` on macOS (all sockets if absent). The macOS form is not live-tested. Guarded only exempts standalone messaging commands, the inbox command included. Unknown startup questions and higher-level policy can still block.
