# Team

<!-- The brief every member of the team reads. Everyone who works on this repo shares it, so write it by
     role, not for one person's line-up: who is on a team, and the operator's @name, come from each person's
     own team.toml, and each member is told them. Write "the operator", not an @name.
     Comments like this one are not sent to members. -->

## Project

<!-- What we are building, for whom, and the stack. Detailed tasks come from the operator. -->

## Tasks

Tasks, owners, status and bugs are tracked in `TODO.md` at the repo root.

<!-- Say where and with what tool, in words a member can act on. For example:
     "GitHub Issues on owner/repo, with `gh issue`" / "Linear team ENG, with the Linear MCP tools" / "the `todo` CLI".
     A file like TODO.md suits one person. With several people on the repo, use a shared tool, and put the
     person's name next to the member's in an item's owner (peter/@lead-dev). -->

## Workflow

1. The operator assigns tasks with acceptance criteria. When the team has a @pm, it records owner, criteria, branch or commit, verdict and next step in the tracker, and only @pm updates it; otherwise the operator does.
2. Developers agree shared interfaces before parallel work, implement, then ask @reviewer with the task, criteria and exact diff or commit. They fix findings until approved.
3. The operator reviews the result, commits shared-checkout work and merges reviewed branches.
