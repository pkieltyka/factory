# Pull-request workflow (#16)

Plan only; no code. It follows the design agreed in issue 16. Six points differ from the issue's text; the owner accepted all six (see "Decided"). Revised twice after @lead-dev's read-only reviews.

## Decision

A project opts in by adding a `## Pull requests` section to `TEAM.md`. One rule then holds in both checkout modes: ticket work is committed on a ticket branch, pushed, and reaches the integration branch only by merging its pull request. One ticket has one branch, one developer writing and one pull request. Without the section nothing changes.

Prose only: no `ateam.py` change, no parser, no watcher, no PR state. a-team names no forge and no tool; the section carries the project's own commands.

| Step | Shared checkout | Worktrees |
|---|---|---|
| Branch | The operator fetches and fast-forwards the integration branch, then creates `<user>/<ticket>` in the main checkout before assigning: clean tree, nobody writing. | The operator fetches and fast-forwards the integration branch in the main checkout, which stays on it. While the developer is not writing, it sets `worktree = "<user>/<ticket>"` for that developer and runs `ateam.py check` and `ateam.py continue <name> --restart`. |
| Write | The developer edits and commits nothing, as today. | The developer commits on its ticket branch, as today. |
| Review | The developer asks @reviewer with the diff and fixes until approved, as today. | The same, on the branch's commits, as today. |
| Commit | The operator commits the approved change on the ticket branch. | Already committed. |
| Push and PR | The operator pushes the branch and opens the pull request, or updates the one that exists. The tracker owner links it on the ticket. | Same. |
| CI | The operator runs one time-limited wait for the checks of that head. | Same. |
| Readiness | @reviewer, when the operator asks, approves the head commit or requires changes. A review tool that needs commits runs here if it has not yet reviewed the change. | Same. |
| Merge | The owner, unless the section gives the operator standing permission. The team merges only by squash merge. | Same. |
| After | The operator confirms the merge on the forge, switches the main checkout to the integration branch and fast-forwards it; the ticket closes. No local merge of the ticket branch. | The operator confirms the merge and fast-forwards the integration branch. |

Members never push, open pull requests or merge. Their standing instructions already say so and stay as they are. Reasons: one writer to the remote, and sandboxed members often have no network or credentials.

What stays the same without the section: every file a member reads, except one conditional bullet in `roles/reviewer.md` and one pointer line in `references/leading.md`. The operator commits and merges locally as today.

## Changed from the issue

1. **A new `references/pull-requests.md` instead of more lines in `leading.md`.** `leading.md` is re-read after every compaction by every operator; projects without pull requests then pay one pointer line, not nine. `notes.md` is the precedent.
2. **No commented example in `templates/TEAM.md`.** #19 removed its comments. The section's shape goes in `references/cmd_setup.md`, next to Specs, Browser and Review.
3. **Shared checkout: review comes before the commit, as today.** The issue's Relations paragraph says work is committed before review. That would put the operator's commit in every fix round, and a push and a CI run too if each head is to be checked. Consequences: a review tool that needs commits runs at the readiness step in the shared checkout, not at the first review (in a worktree it runs at the first review as today); and CI runs after the review, so a red run can cost one more round. Accepted for a first version.
4. **The checks wait runs "the way you run `ateam.py wait`"**, not "in the background": Codex and pi operators do not wake on a background command (#24).
5. **"Required checks" is defined.** The checks the forge requires must pass; when it requires none, every check must. A failed optional check is a finding the reviewer triages. This repo's `master` has no branch protection, so nothing is marked required here.
6. **The issue says this repo has no CI.** It has one now: `.github/workflows/test.yml` runs the suite on every push and pull request.

## Rejected

- Members push or open pull requests: see above.
- A draft pull request opened before review, so CI runs beside the review: a trade-off, not a fault. It saves time when CI is slow, and costs an extra state and unreviewed work on the remote. A first version opens the pull request after approval.
- Two developers writing on one ticket: both would get `<user>/<ticket>` and the same worktree path. A ticket that needs two writers is split into two tickets first. Task branches and several pull requests under one ticket are out of this version (see "Decided").
- An `ateam.py` command that creates the ticket branch or watches checks: forge-specific code and state for what three commands in `TEAM.md` do.
- Reusing `<user>/<member>` across tickets in worktree mode: after a squash merge the old commits reappear in the next pull request.
- Starting an independent ticket in the shared checkout while a pull request waits for the owner: fixes to the waiting pull request would then queue behind the new writer. The rule stays one ticket at a time, "or the user says to go on".

## Files and exact text

**New `references/pull-requests.md`:**

```markdown
# Pull requests

Read when `TEAM.md` has a `## Pull requests` section. Ticket work is then committed on a ticket branch, pushed, and reaches the integration branch only by merging its pull request. The section names the branches, the standing permissions and the forge's commands; run those. Assigning, peer review and the task record stay as in [leading.md](leading.md).

- **Branch:** one ticket has one branch, one developer writing and one pull request. Split a ticket that needs two writers into two tickets first. Before you create a branch, fetch and fast-forward the integration branch in the main checkout.
- **Shared checkout:** with a clean tree and nobody writing, create the ticket branch in the main checkout before you assign. Members leave changes uncommitted; after the reviewer approves, commit on the ticket branch. One ticket at a time: the next starts when this pull request is merged, or the user says to go on.
- **Worktrees:** the main checkout stays on the integration branch. While the developer is not writing, give it a new branch for the ticket: set `worktree = "<user>/<ticket>"` in `team.toml`, run `ateam.py check`, then `ateam.py continue <name> --restart`; if the session cannot resume there, retry with `--fresh`. Never reuse a branch whose pull request was merged. You and @pm stay in the main checkout. Do not mix the modes: no member has a worktree while the main checkout is on a ticket branch.
- **Permission:** the section gives standing permission in two lines: one to push ticket branches and open or update pull requests, one to merge. Without the line, ask the user at that action; their answer covers that action only.
- **Remote:** only you write to it. After the reviewer approves, push the ticket branch and open its pull request, or let the push update the one that exists; have the tracker owner link it on the ticket. After an unclear result from a push, an open or a merge, read the remote branch and the pull request before you retry, so nothing is made twice. Never force-push, and delete no branch unasked.
- **Checks:** keep one watcher per pushed head: a wait with a time limit that fits your harness, run the way you run `ateam.py wait`. If it ends while checks are pending, start it again; if none is listed yet, pause first, longer each time. If it fails for another reason, such as sign-in or a rate limit, stop and report. Beyond the watcher, read the forge only to reconcile a retry, just before a merge and to confirm one; no loop polls it. When the checks have finished, passed or failed, ask @reviewer for a readiness verdict on that head.
- **Ready** means, all on the pull request's current head commit: the reviewer approved that commit; every check the forge requires has passed, and when it requires none, every check has; no verified finding is open, whether from a failed optional check, an automated report or a thread; it can be merged. No checks listed is not ready, unless the section says the project has no CI and the forge requires none. A new commit voids all of it. The team's approval is not the forge's review approval: when the forge wants one from another person, report blocked and never bypass.
- **Merge:** ready is a report, not permission. With permission, read the state once more and squash-merge that exact head: never a merge commit or a rebase merge, and if the forge does not allow a squash merge, report blocked. A queued merge is not merged: keep the ticket open and check again with a wait like the one for checks, or report that you wait on the forge. Confirm the merge on the forge, also when the user merged; then switch the main checkout to the integration branch, fast-forward it from the remote and have the ticket closed. Never merge a ticket branch locally.
- **Conflicts:** merge the integration branch into the ticket branch (the developer in a worktree, you in the shared checkout); never rebase. The new head needs new checks and a new approval.
```

**`references/leading.md`**, three edits:

- Line 12, the list of optional sections: `(Specs, Browser, Review, a notes search tool)` becomes `(Specs, Browser, Review, Pull requests, a notes search tool)`.
- New last bullet under "Review and integrate":

```markdown
- When `TEAM.md` has a `## Pull requests` section, follow [pull-requests.md](pull-requests.md): it replaces your commit to the main branch and your local merge.
```

- Autopilot, the "Always the user's" bullet gains one sentence at its end:

```markdown
The exception is standing permission in a `## Pull requests` section of `TEAM.md`: to push ticket branches and open pull requests, and separately to merge.
```

**`roles/reviewer.md`**, new bullet after the review-tool bullet:

```markdown
- A pull request, when {operator} asks: a readiness verdict on its head commit. Check that the head holds the change you approved, and run the review tool now if it needed commits. Read the failed checks, automated review reports and open threads, and verify each finding as you would a review tool's; passing checks do not replace this. `status: approve` covers that head commit only: name it.
```

**`references/cmd_setup.md`**, two edits:

- Step 2, the end of the list of questions: `and any spec, browser or review tool the project uses.` becomes `any spec, browser or review tool the project uses, and whether work reaches the main branch through pull requests.`
- New bullet after the Specs, Browser and Review bullet:

```markdown
- `## Pull requests`: only when the project integrates work through pull requests. It says: the integration branch; the ticket-branch pattern (`<user>/<ticket>`); that the team merges only by squash merge; the standing permissions, each on its own line: whether the operator may push ticket branches and open or update pull requests, and whether it may merge (otherwise the user merges); whether the project has CI; that the task record links the pull request and a ticket closes only after its merge is confirmed; and the forge's commands, in its own words, to open a pull request, wait for its checks with a time limit, read its head commit, checks, review threads and mergeability, and merge at an exact head commit, with one cheap check command. The two permission lines are the user's: write what they said and leave out what they have not decided. Update the Workflow rule on committing and integration to match.
```

**`README.md`**, line 23: `recovery and the notes convention` becomes `recovery, the notes convention and the optional pull-request workflow`.

Not changed: `scripts/ateam.py`, `scripts/test_ateam.py`, `templates/`, `SKILL.md`, `roles/pm.md` (the operator tells @pm, and the section in `TEAM.md`, which @pm reads, says the record links the pull request), `references/cmd_continue.md` (it already reads "PRs and CI when the project has them").

## This repo's section (trial; the general recipe belongs in #12)

The owner opted this repo in for the shared-checkout trial. @operator adds this to `TEAM.md` after the build, with a time limit on the wait that fits the operator's harness. @lead-dev verified the review-threads query read-only on public pull requests (six threads with two unresolved, the same result paged one at a time, an empty pull request, a missing one exiting 1); this repo has no pull request yet, so here only its syntax was checked. The other flags were checked against `gh` 2.104.0 `--help` only; none was run. Not yet observed: what `gh pr checks --watch` does in the seconds after a push, before a check is listed.

```markdown
## Pull requests

Work reaches `master` only through a pull request on GitHub. Ticket branches are `<user>/<ticket>`, for example `peter/16-pr-workflow`. The team merges only by squash merge.

- The operator may push ticket branches and open or update pull requests.
- The owner merges.
- CI: the `Tests` workflow runs on every push and pull request.
- The issue links its pull request and closes only after the merge is confirmed.
- Commands (`gh`; learn with `gh pr --help`; check with `gh auth status`). Open: `gh pr create --base master --head <branch>`. Wait: `timeout <seconds> gh pr checks <branch> --watch --interval 60`. State: `gh pr view <branch> --json state,headRefOid,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup`. Comments and reviews: `gh pr view <branch> --comments`. Review threads (unresolved ones have `isResolved` false; read each linked discussion): `gh api graphql --paginate --slurp -F owner=pkieltyka -F repo=herdr-a-team -F number=<pr> -f query='query($owner:String!,$repo:String!,$number:Int!,$endCursor:String){repository(owner:$owner,name:$repo){pullRequest(number:$number){headRefOid reviewThreads(first:100,after:$endCursor){nodes{id isResolved isOutdated path line comments(first:1){nodes{url}}} pageInfo{hasNextPage endCursor}}}}}'`. Merge: `gh pr merge <branch> --squash --match-head-commit <sha>`.
```

Workflow rule 4 of this repo's `TEAM.md` then reads: "Everyone works in the shared checkout. Members do not commit; the operator commits on the ticket branch and opens the pull request; the owner merges."

## Build order

1. `references/pull-requests.md`, the three `leading.md` edits, the `README.md` words.
2. The `roles/reviewer.md` bullet.
3. The two `cmd_setup.md` edits.
4. @reviewer reviews the exact diff. Proof: `python3 scripts/test_ateam.py` unchanged (82 OK, 1 skipped at ec148a7; no test reads these texts) and `git diff --check` clean.
5. @operator adds this repo's section and the changed rule 4 to `TEAM.md`. It is not part of @lead-dev's build, which is steps 1 to 3 on five paths.
6. Trial in this repo, shared-checkout mode: one ticket from branch to merged pull request, with the pull request linked on the ticket. Worktree mode stays marked untested until a later trial on a small ticket (#28 is a candidate). Record each of these as observed or untested: pending, failed and missing checks are not reported ready; a bot finding on a green run is triaged; a new head commit voids readiness; a failed optional check is triaged; a watcher that times out is started again and one that fails on sign-in stops; a missing permission line stops at "ask"; a forge that demands another person's approval stops at "blocked"; the merge is confirmed before the ticket closes; a retried push or open finds the existing branch and pull request; a merge by the team is a squash merge.

## Verified against the code

- `ensure_worktree` runs `git -C <root> worktree add -b <branch> <path>` for an unknown branch, so a new ticket branch starts at the main checkout's HEAD; an existing branch or path is reused as it is. So `check` and the restart prove neither a fresh branch nor the latest base: the operator fetches first and never reuses a merged branch. It is also why the two modes do not mix.
- `plan()` derives the worktree path from the branch name, so two members given the same branch get the same path: one writer per ticket branch.
- `Config.main_branch` is whatever branch the main checkout is on. It is printed only for worktree members and in the brief's Worktrees section, so a shared-checkout team on a ticket branch is not told a wrong main branch.
- Worktree members are already told to commit on their branch, never push, and merge the main branch into their own. Shared-checkout members are told never to commit, merge or push.
- `worktree = "<branch>"` accepts any branch name; its path is the name with other characters replaced by `-`.

## Decided

By the owner, through @operator (2026-11-17):

- All six points under "Changed from the issue" are accepted.
- This repo opts in for the trial, in shared-checkout mode.
- Standing permission: the operator may push ticket branches and open pull requests without asking each time. Merges stay the owner's. No force-pushes.
- Worktree mode stays marked untested for now; it is trialled later on a small ticket.
- Whenever the operator or any agent merges a pull request, which only `TEAM.md` can allow, it is a squash merge: never a merge commit or a rebase merge. The rule is in `references/pull-requests.md` and in the section's shape in `references/cmd_setup.md`.
- One writer per ticket in this version; a ticket that needs several writers is split.
- @lead-dev supplies and verifies the unresolved-threads command; it is in this repo's section above.

## Open

- **#12 recipe, and the trial here:** what `gh pr checks --watch` does right after a push when no check is listed yet. The reference already tells the operator to pause before starting the watcher again.

## Risks

- With the owner merging, as decided here, a shared-checkout team waits at every ready pull request, and autopilot stops there. Granting the merge line removes the wait.
- Each ticket in worktree mode costs a member restart and leaves a worktree behind until cleanup is authorized. The issue accepts this as evidence for or against automating it (#14).
- The workflow is prose. Nothing stops an operator that skips a step; the trial is the only check.
