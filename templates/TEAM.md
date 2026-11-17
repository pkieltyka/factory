# Team

<!-- The brief every member of the team reads. Everyone who works on this repo shares it, so write it by
     role, not for one person's line-up: who is on a team, and the operator's @name, come from each person's
     own team.toml, and each member is told them. Write "the operator", not an @name.
     Comments like this one are not sent to members. -->

## Project

<!-- What we are building, for whom, and the stack. Detailed tasks come from the operator. -->

## Tasks

Tasks, owners, status and bugs are tracked in `TODO.md` at the repo root. An item marked `ready` may be started without asking; only a person marks it, never the team.

<!-- Say where and with what tool, in words a member can act on. For example:
     "GitHub Issues on owner/repo, with `gh issue`" / "Linear team ENG, with the Linear MCP tools" / "the `todo` CLI".
     A file like TODO.md suits one person. With several people on the repo, use a shared tool, and put the
     person's name next to the member's in an item's owner (peter/@lead-dev).
     Say what marks an item ready to start (a status, label or board column) and who sets it. -->

## Workflow

1. The operator assigns tasks with acceptance criteria. When the team has a @pm, it records owner, criteria, branch or commit, verdict and next step in the tracker, and only @pm updates it; otherwise the operator does.
2. Developers agree shared interfaces before parallel work, implement, then ask @reviewer with the task, criteria and exact diff or commit. They fix findings until approved; after three rounds on one task the operator decides how to go on.
3. The operator reviews the result, commits shared-checkout work and merges reviewed branches.
4. One feature or issue is in progress at a time. Its tasks may run in parallel when each writer has its own worktree; in a shared checkout only one task writes at a time.

## Notes

Disabled.

<!-- To keep notes, replace "Disabled." with the location and the folders in use, for example:
     "Enabled in notes/: activity/ (the team's working notes), research/ (findings the owner asked for),
     discussions/ (records of meetings and their decisions). Follow the a-team notes convention; search with rg."
     Name a search tool there if the project has one. -->

<!-- Optional sections, shaped like Tasks: where, which tool, how to learn it, and one cheap command that
     shows it works on this machine. Add only those the project uses:
     ## Specs    how specifications are written and where they live
     ## Browser  the browser tool for UI work and QA
     ## Review   a tool the reviewer runs on committed changes, and its limits (depth, time, publishes nothing) -->
