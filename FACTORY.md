# Team

## Project

factory: an agent skill (`/factory`) that forms, launches and leads a software team of coding agents across Claude Code, Codex, pi, omp, OpenCode and agy, and the team iterates on the skill itself. Users are developers running agent teams; the agents that read SKILL.md, roles/, references/ and templates/ are users too, so those files are product, not docs.

This is v2, on `master`: it targets herdr-core (`hc`, 0.1.20 or newer) only. `hc` owns the terminals and reports each agent's own state through OSC 7501, and the layout lives in the `herdr-app` doc, so the team shows up in any client (rodeo, the Herdr desktop app). v1, for herdr 0.9.5, lives on the `v1` branch (tag `v1.0.0`) and gets fixes only. v2 is being specced and built under `specs/projects/factory/`; until it replaces it, the v1 tree on `master` is the reference, not the target.

Stack: Python 3.11+ standard library (`skill/scripts/factory.py`), `hc`'s CLI and JSON-RPC, Git worktrees, `skill/harnesses.toml` for harness and model facts. Tests: `python3 tests/test_factory.py`.

Principles: keep it small and simple. Prefer deleting words and code to adding them; every line an agent reads costs context. A change to behaviour updates the docs that describe it in the same change. Never touch the owner's own `hc` sessions in tests or experiments: use an isolated `hc` session or socket.

## Tasks

Tasks and bugs are GitHub Issues on `pkieltyka/factory`, managed with `todos`; the board is `pkieltyka/2` (columns Backlog, Todo, In progress, Done). Run from the checkout. Learn it once with `todos skill`; agent operations always pass `--json`. Only a person moves an issue into Todo: the operator may then assign it without asking the owner; it is not an assignment to a member. Assigned work moves to In progress. @pm owns tracker writes, or the operator when there is no @pm; members report discoveries to that owner, who records them in Backlog. Record owner, criteria, branch or commit, verdict and next step on the issue. Check with `todos doctor --json`; reconcile an uncertain write before retrying. Never run `todos rm`; close instead, after the merge is confirmed.

## Workflow

The team works through the board on its own. The owner sets direction; the team carries each ready ticket from plan to merge, then takes the next, and stops only for the owner's decisions.

| Step | Who | Output |
|---|---|---|
| 1. Direction | The owner, with the operator | Goals and priorities. Only the owner moves tickets into Todo (ready). |
| 2. Tickets | @pm | Tickets with acceptance criteria from the direction and from discoveries; a current board. |
| 3. Plan | An architect | A plan for the ticket against the specs and the code: interfaces, edge cases, tests, files. Another architect or a lead dev may challenge it; the operator settles disagreements. Small fixes skip this step. |
| 4. Build | Lead devs, plus frontend-dev for UI work when the team has one | Code and tests on the ticket branch, following the plan. |
| 5. Review | @reviewer | A verdict on the exact diff (see Review). Fixes until approved; after three rounds on one ticket the operator decides. |
| 6. Pull request and CI | The developer who wrote it | Pushes, opens the pull request, watches CI and fixes failures. |
| 7. Merge | The operator | Confirms the reviewer approved the head commit and CI is green, squash-merges, and tells @pm, who closes the ticket. |
| 8. Next | The operator | Takes the next ticket in Todo without asking. |

- **Pairing on every ticket.** One lead dev is the main writer; the other lead devs follow along closely: they review each commit as it lands on the ticket branch, run its tests, and send the writer suggestions. The architect who planned the ticket and the other architects review the plan before building starts (the plan is final only after their review) and give the final design review of the finished work against the plan before @reviewer's verdict.
- The operator assigns each step with acceptance criteria. When the team has a @pm, it records owner, criteria, branch or commit, verdict and next step on the issue, and only @pm updates the tracker; otherwise the operator does.
- Consult an architect before changing a shared interface, the brief or formation format, or the shape of SKILL.md and references/. Its plans are advice; the operator decides.
- Developers with a worktree work on its branch (`<user>/<member>`); the others share the main checkout, where only one task writes at a time. Members do not commit in the main checkout.
- One ticket is in progress at a time; its tasks may run in parallel when each writer has its own worktree.
- Every member reports to whoever assigned its work when it finishes, is blocked, or needs a decision. A member that is rate-limited or stuck says so; the operator reassigns.

## Pull requests

Work reaches `master` only through a pull request on GitHub. Ticket branches are `<user>/<ticket>`, for example `peter/16-pr-workflow`. The team merges only by squash merge.

- The developer who wrote a ticket's change commits it on the ticket branch, pushes it, opens or updates the pull request and watches CI. The operator may also push ticket branches and open or update pull requests.
- The operator may squash-merge a pull request once it is ready: the reviewer approved its head commit and every check passed. It does not wait for the owner.
- CI: the `Tests` workflow runs on every push and pull request.
- The issue links its pull request and closes only after the merge is confirmed.
- Commands (`gh`; learn with `gh pr --help`; check with `gh auth status`). Open: `gh pr create --base master --head <branch>`. Wait: `timeout 900 gh pr checks <branch> --watch --interval 60`. State: `gh pr view <branch> --json state,headRefOid,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup`. Comments and reviews: `gh pr view <branch> --comments`. Review threads (unresolved ones have `isResolved` false; read each linked discussion): `gh api graphql --paginate --slurp -F owner=pkieltyka -F repo=factory -F number=<pr> -f query='query($owner:String!,$repo:String!,$number:Int!,$endCursor:String){repository(owner:$owner,name:$repo){pullRequest(number:$number){headRefOid reviewThreads(first:100,after:$endCursor){nodes{id isResolved isOutdated path line comments(first:1){nodes{url}}} pageInfo{hasNextPage endCursor}}}}}'`. Merge: `gh pr merge <branch> --squash --match-head-commit <sha>`.

## Review

Reviews start in the fast lane: @reviewer's own full review. Deep lane: for any change beyond a small fix, @reviewer also runs codegenie (github.com/0xPolygon/codegenie) from the ticket's worktree: `codegenie review --base <base sha> --head <head sha> --no-progress --max-time 10` for committed work on a branch, or `codegenie review --uncommitted --no-progress --max-time 10` for uncommitted work in a worktree. Learn it with `codegenie skill` and `codegenie review --help`; check it with `codegenie --version`. Every run is limited to 10 minutes with `--max-time 10`. If it stops at the limit, @reviewer uses the partial report as it is, says so in the verdict and tells the operator, and does not rerun it. Never pass `--post-github-comments` or anything else that publishes. Its report is input, not the verdict: verify each finding, read its coverage and health sections, cover anything it skipped, and name the lane and what it reviewed. Run it with `hc` pointed at a private, throwaway session and socket, so nothing it starts can reach the owner's sessions.

## Specs

v2 is specced with the `spec` skill: `specs/projects/factory/` holds the project overview, functional spec, architecture and implementation plan, with research under `research/`. Artifacts carry `status: draft` or `complete` in their front matter; only the owner marks one complete. Read the completed ones before working on v2.

## Notes

Enabled in `notes/`: `activity/` (the team's working notes), `research/` (findings the owner asked for), `discussions/` (records of meetings and their decisions). Follow the factory notes convention; search with rg. `notes/private/` holds each person's own notes, ignored by Git. That person's team may read and search it, under the private-folder rule of the notes convention.