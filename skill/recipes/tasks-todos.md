# Tasks: todos

Choose todos for GitHub Issues managed through a project board, where a column marks work ready to assign.

## Needs

The person installs todos (ask the owner where to get it) and [gh](https://cli.github.com), signs in with `gh auth login`, and grants access to the repository and board. The board must already exist. Nothing installs or upgrades itself.

## Check

From the project checkout: `todos doctor --json`.

## FACTORY.md section

```markdown
## Tasks

Tasks and bugs are GitHub Issues on OWNER/REPO, managed with `todos`; the board is BOARD_OWNER/BOARD_NUMBER. Run from the checkout. Learn it once with `todos skill`; agent operations always pass `--json`.

Only a person moves an issue into READY_COLUMN. That column means the operator may assign it without asking; it is not an assignment to a member. Assigned work moves to ACTIVE_COLUMN.

@pm owns tracker writes, or the operator when there is no @pm. Members report discoveries to that owner, who records them in BACKLOG_COLUMN. Record owner, criteria, branch or commit, verdict and next step on the issue.

Check the board with `todos board --json`; pass `--project BOARD_OWNER/BOARD_NUMBER` if it shows another. Check failures with `todos doctor --json`; reconcile an uncertain write before retrying. Never run `todos rm`; close instead, after confirmed integration. With pull requests enabled, link the PR and close only after its merge is confirmed.
```

## Tested with

2026-11-18: todos `v0.0.0-20261116234621-e210174be96c`, gh 2.104.0; this repository's GitHub Issues and project board.
