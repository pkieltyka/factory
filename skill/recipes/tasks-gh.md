# Tasks: gh

Choose gh for GitHub Issues without a project board. No team has run this label-based ready flow yet (see Tested with).

## Needs

The person installs [gh](https://cli.github.com), signs in with `gh auth login`, and has access to the repository. A person creates two distinct labels for ready and active work before setup. Nothing installs or upgrades itself.

## Check

`gh repo view OWNER/REPO --json hasIssuesEnabled,viewerPermission`

Pass only when `hasIssuesEnabled` is `true` and `viewerPermission` is `ADMIN`, `MAINTAIN`, `WRITE` or `TRIAGE`.
This does not prove a fine-grained token can write issues; setup asks the owner to confirm that permission before choosing gh.

## FACTORY.md section

```markdown
## Tasks

Tasks and bugs are GitHub Issues on OWNER/REPO, managed with `gh`. Run from the checkout; learn it with `gh issue --help`.

Only a person adds READY_LABEL; the team never adds it. It means the operator may assign the issue without asking, not that a member may start it. At assignment, the tracker owner replaces it with ACTIVE_LABEL. Open issues with neither label are the backlog.

@pm owns tracker writes, or the operator when there is no @pm. Members report discoveries to that owner. Record owner, criteria, branch or commit, verdict and next step on the issue.

- List ready: `gh issue list --label 'READY_LABEL' --limit 100 --json number,title,labels`; list active with the same command and 'ACTIVE_LABEL' (if 100 come back, rerun with a higher `--limit`).
- Read: `gh issue view <issue> --json title,body,comments`.
- Record: `gh issue comment <issue> --body-file <file>`; create a backlog item: `gh issue create --title '<title>' --body-file <file>` (no ready label).
- Assign: `gh issue edit <issue> --remove-label 'READY_LABEL' --add-label 'ACTIVE_LABEL'`.
- Finish: link the pull request when there is one; `gh issue close <issue>` only after the merge or integration is confirmed.

Never delete issues. Reconcile an uncertain write before retrying.
```

## Ready check

Set this read-only command as `ready_check` in your local `factory.toml`. Replace the capitalized names with your tracker settings. Increase the limit if the ready label can cover more than 100 open issues.

`ids=$(gh issue list --repo 'OWNER/REPO' --state open --label 'READY_LABEL' --limit 100 --json number --jq 'sort_by(.number) | .[].number') || exit 2; [ -n "$ids" ] || exit 1; printf '%s\n' "$ids"`

## Tested with

2026-11-18: gh 2.104.0; the repository check returned `hasIssuesEnabled: true` and `viewerPermission: ADMIN`; authentication, label-filtered list (using an existing label) and issue view also ran in this repository. Edit, comment, create and close flags were checked in help, not executed. The label-based ready flow has not been trialled by a team.

2026-11-27 UTC, gh 2.105.0. Ready check: an existing open label returned IDs and exit 0, a label with no matches returned exit 1, and an invalid repository returned exit 2.
