# Factory

## Project

## Tasks

Tasks and bugs are tracked in `TODO.md` at the repo root. Start each item with `- [ ] (N) Title`. A new item takes the largest number in the file plus one, counting every section, or 1 when there are no items. A number never changes and is never reused. When `## Done` holds more than 20 items, remove the ones with the lowest numbers until 20 remain. Always keep the item with the largest number in the file. Refer to a TODO.md item as `(N)` and reserve `#N` for GitHub issues and pull requests. When an item needs more detail, add four-space `*` description bullets. Use the same bullets for record fields (`owner`, `criteria`, `work`, `verdict`, `next`) as they become known. Indent nested lists eight spaces. Leave exactly one blank line between items in the same section and none inside an item.

```markdown
- [ ] (2) Title
    * description
    * criteria: ...
    * next: ...
        1. ...

- [ ] (1) Fix the README typo
```

`## Ready` holds, in the order they will be done, the items the operator may assign without asking the user; only a person puts an item there or reorders it, never the team. Members report discoveries to @pm, or the operator when there is no @pm, who records them in `## Backlog`. An assigned item moves to `## In progress` with its owner at the end of the checkbox line, `(@lead-dev)`. Integrated work moves to the top of `## Done` as `- [x]` with its commit.

## Workflow

1. The operator assigns tasks with acceptance criteria. When the team has a @pm, it records owner, criteria, branch or commit, verdict and next step in the tracker, and only @pm updates it; otherwise the operator does.
2. Developers agree shared interfaces before parallel work, implement, then ask @reviewer with the task, criteria and exact diff or commit. They fix findings until approved; after three rounds on one task the operator decides how to go on.
3. The operator reviews the result and integrates it.
4. One feature or issue is in progress at a time. Its tasks may run in parallel when each writer has its own worktree; in a shared checkout only one task writes at a time.

## Notes

Disabled.
