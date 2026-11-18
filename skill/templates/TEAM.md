# Team

## Project

## Tasks

Tasks and bugs are tracked in `TODO.md` at the repo root, one line each: `- [ ] <title> -- <description>`. `## Ready` holds, in the order they will be done, the items the operator may assign without asking the user; only a person puts an item there or reorders it, never the team. Members report discoveries to @pm, or the operator when there is no @pm, who records them in `## Backlog`. An assigned item moves to `## In progress` with its owner at the end of the line, `(@lead-dev)`. Integrated work moves to the top of `## Done` as `- [x]` with its commit. An item's criteria, verdict and next step go on indented lines under it.

## Workflow

1. The operator assigns tasks with acceptance criteria. When the team has a @pm, it records owner, criteria, branch or commit, verdict and next step in the tracker, and only @pm updates it; otherwise the operator does.
2. Developers agree shared interfaces before parallel work, implement, then ask @reviewer with the task, criteria and exact diff or commit. They fix findings until approved; after three rounds on one task the operator decides how to go on.
3. The operator reviews the result, commits shared-checkout work and merges reviewed branches.
4. One feature or issue is in progress at a time. Its tasks may run in parallel when each writer has its own worktree; in a shared checkout only one task writes at a time.

## Notes

Disabled.
