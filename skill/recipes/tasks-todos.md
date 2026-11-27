# Tasks: todos

Choose todos for GitHub Issues managed through a project board, where a column marks work ready to assign.

## Needs

The person installs [todos](https://github.com/pkieltyka/todos) and [gh](https://cli.github.com), signs in with `gh auth login`, and grants access to the repository and board. The board must already exist. Nothing installs or upgrades itself.

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

## Ready check

Set this read-only command as `ready_check` in your local `factory.toml`. Replace the capitalized names with your tracker settings. This example uses `jq`.

`board=$(todos board --repo 'OWNER/REPO' --project 'BOARD_OWNER/BOARD_NUMBER' --json --fresh) || exit 2; ids=$(printf '%s\n' "$board" | jq -er --arg ready 'READY_COLUMN' 'if .truncated != false then error("incomplete board") else [.columns[] | select(.status == $ready)] | if length != 1 then error("ready column not found") else .[0].cards | map(select(.state == "open") | .number) | sort | join("\n") end end') || exit 2; [ -n "$ids" ] || exit 1; printf '%s\n' "$ids"`

## Tested with

2026-11-18: todos `v0.0.0-20261116234621-e210174be96c`, gh 2.104.0; this repository's GitHub Issues and project board.

2026-11-27 UTC, todos v0.0.0-20261120175034-0216833acf6b. Ready check: the Todo column returned IDs and exit 0, Done had no open items and exit 1, and a missing column or an invalid repository returned exit 2.
