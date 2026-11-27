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

The team works through the ready items on its own. The owner sets direction; the team carries each ready item from plan to integration, then takes the next, and stops only for the owner's decisions.

| Step | Who | Output |
|---|---|---|
| 1. Direction | The owner, with the operator | Goals and priorities. Only the owner marks an item ready. |
| 2. Items | The pm, or the operator without one | Items with acceptance criteria; a current tracker. |
| 3. Plan | An architect, when the team has one | A plan for the item: interfaces, edge cases, tests, files. Small fixes skip this step. |
| 4. Build | Developers | Code and tests, following the plan. |
| 5. Review | The reviewer | A verdict on the exact diff. Fixes until approved; after two rounds on one item the operator decides. |
| 6. Integrate | The operator | Confirms the approval and the checks, then integrates and has the tracker updated. |
| 7. Next | The operator | Takes the next ready item without asking. |

- The operator assigns each step with acceptance criteria. Only @pm writes the tracker when present. Without @pm, the operator writes it. Record owner, criteria, branch or commit, verdict and next step.
- One item is in progress at a time. Its tasks may run in parallel when each writer has its own worktree; in a shared checkout only one task writes at a time.
- Every member reports to whoever assigned its work when it finishes, is blocked, or needs a decision. A member that is rate-limited or stuck says so; the operator reassigns.

## Notes

Disabled.
