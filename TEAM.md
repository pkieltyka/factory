# Team

## Project

a-team: an agent skill that forms, launches and leads a software team of coding agents in herdr panes, across Claude Code, Codex, pi, omp and OpenCode. It is the base of a composable software-factory meta-harness on top of herdr, and the team iterates on the skill itself. Users are developers running agent teams; the agents that read SKILL.md, roles/, references/ and templates/ are users too, so those files are product, not docs.

Stack: Python 3.11+ standard library (`scripts/ateam.py`, one file), herdr's CLI and socket, Git worktrees, `harnesses.toml` for harness and model facts. Tests: `python3 scripts/test_ateam.py` (fake harnesses and herdr, real Git); live flag checks with `ATEAM_LIVE=1 python3 scripts/test_ateam.py LiveFlags`.

Principles: keep it small and simple. Prefer deleting words and code to adding them; every line an agent reads costs context. A change to behaviour updates the docs that describe it in the same change.

## Tasks

Tasks and bugs are GitHub Issues on `pkieltyka/herdr-a-team`, managed with the `todos` CLI. Run `todos skill` once to read how it works, and always pass `--json`. Never run `todos rm`; close instead. An issue in the board's Todo column is ready: the operator may assign it without asking the owner; only the owner moves issues to Todo, never the team. Backlog is not ready.

## Workflow

1. The operator assigns tasks with acceptance criteria. When the team has a @pm, it records owner, criteria, commit, verdict and next step on the issue, and only @pm updates the tracker; otherwise the operator does.
2. Consult @architect before changing a shared interface, the brief or formation format, or the shape of SKILL.md and references/. Its plans are advice; the operator decides.
3. Developers implement, run the tests, then ask @reviewer with the issue, criteria and exact diff. They fix findings until approved; after three rounds on one issue the operator decides how to go on.
4. Everyone works in the shared checkout. Members do not commit; the operator commits on the ticket branch and opens the pull request; the owner merges.
5. One issue is in progress at a time, and in the shared checkout only one task writes at a time. Parallel writers need their own worktrees.

## Pull requests

Work reaches `master` only through a pull request on GitHub. Ticket branches are `<user>/<ticket>`, for example `peter/16-pr-workflow`. The team merges only by squash merge.

- The operator may push ticket branches and open or update pull requests.
- The owner merges.
- CI: the `Tests` workflow runs on every push and pull request.
- The issue links its pull request and closes only after the merge is confirmed.
- Commands (`gh`; learn with `gh pr --help`; check with `gh auth status`). Open: `gh pr create --base master --head <branch>`. Wait: `timeout 900 gh pr checks <branch> --watch --interval 60`. State: `gh pr view <branch> --json state,headRefOid,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup`. Comments and reviews: `gh pr view <branch> --comments`. Review threads (unresolved ones have `isResolved` false; read each linked discussion): `gh api graphql --paginate --slurp -F owner=pkieltyka -F repo=herdr-a-team -F number=<pr> -f query='query($owner:String!,$repo:String!,$number:Int!,$endCursor:String){repository(owner:$owner,name:$repo){pullRequest(number:$number){headRefOid reviewThreads(first:100,after:$endCursor){nodes{id isResolved isOutdated path line comments(first:1){nodes{url}}} pageInfo{hasNextPage endCursor}}}}}'`. Merge: `gh pr merge <branch> --squash --match-head-commit <sha>`.

## Notes

Enabled in `notes/`: `activity/` (the team's working notes), `research/` (findings the owner asked for), `discussions/` (records of meetings and their decisions). Follow the a-team notes convention; search with rg. `notes/private/` holds each person's own notes, ignored by Git. That person's team may read and search it, under the private-folder rule of the notes convention.
