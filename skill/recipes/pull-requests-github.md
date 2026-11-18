# Pull requests: GitHub

Choose GitHub pull requests when reviewed work must pass CI and reach the integration branch through a PR.

## Needs

The person installs [gh](https://cli.github.com), Git and a `timeout` command (GNU coreutils), signs in with `gh auth login`, and has access to the GitHub repository. Nothing installs or upgrades itself.

## Check

`gh auth status`

## FACTORY.md section

During setup, add the owner's decided permissions on two separate lines: pushing/opening/updating PRs, and merging. Leave an undecided permission out. Match the Workflow section to the chosen checkout mode; follow the skill's pull-request reference.

```markdown
## Pull requests

Work reaches BASE_BRANCH only through a pull request on GitHub, repository OWNER/REPO. Ticket branches are `<user>/<ticket>`; the team merges only by squash merge. CI: CI_DESCRIPTION. The task record links its PR and closes only after a confirmed merge.
Run from the checkout. Learn the commands with `gh pr --help`; check access with `gh auth status`.

- Push/open/update: `git push -u origin <branch>`; `gh pr create --base BASE_BRANCH --head <branch>`; later pushes update the PR.
- Wait for checks: `timeout 900 gh pr checks <branch> --watch --interval 60`. Fit the command limit to the harness; restart the watcher if it times out with checks pending.
- Read head/checks/mergeability: `gh pr view <branch> --json state,headRefOid,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup`; comments/reviews: `gh pr view <branch> --comments`.
- Review threads: `gh api graphql --paginate --slurp -F owner=OWNER -F repo=REPO -F number=<pr> -f query='query($owner:String!,$repo:String!,$number:Int!,$endCursor:String){repository(owner:$owner,name:$repo){pullRequest(number:$number){headRefOid reviewThreads(first:100,after:$endCursor){nodes{id isResolved isOutdated path line comments(first:1){nodes{url}}} pageInfo{hasNextPage endCursor}}}}}'`. An unresolved thread has `isResolved` false; read its linked discussion.
- Merge, only with permission and a ready head: `gh pr merge <branch> --squash --match-head-commit <sha>`. Confirm with the state command; a queued merge is not yet merged. Never force-push.
```

## Tested with

2026-11-18: gh 2.104.0, GitHub PRs and CI in this repository, shared checkout. Worktree mode has not been trialled.
