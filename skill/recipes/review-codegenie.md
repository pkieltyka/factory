# Review: codegenie

Choose codegenie for an additional review of committed changes. Fast remains the reviewer's full own review; another review CLI can fill the same optional section.

## Needs

The person installs the public [codegenie CLI](https://github.com/0xPolygon/codegenie), connects a supported model provider and chooses its model using that project's setup instructions. It needs Git and committed work. Nothing installs or upgrades itself.

## Check

`codegenie --help`

## FACTORY.md section

```markdown
## Review

Reviews start in the fast lane. Deep lane: @reviewer runs codegenie at pull-request readiness on the committed head against its base.
Learn it with `codegenie review --help`; check it with `codegenie --help`.
Run `codegenie review --no-progress --base <base sha> --head <head sha>`.
Its report is input, not the verdict: verify findings, cover anything skipped, and name the lane and tool coverage in the verdict. A run can take up to 30 minutes. Never pass `--post-github-comments` or anything else that publishes.
In a shared checkout, review uncommitted work in the fast lane first; the operator commits before deep review. Do not make a developer commit just to satisfy the tool.
```

## Tested with

2026-11-18: codegenie v0.7.0 (`6210282503f36faef3a9fecd28f88b38d6d33d25`); committed PR readiness reviews in this repository. The help check does not verify provider access.
