---
status: complete
---

# Factory

The `/factory` skill turns your agents into an autonomous software engineering team. It's a software factory skill that forms, launches and leads a team of coding agents (an operator plus members across Claude Code, Codex, pi, omp, OpenCode and agy) inside a terminal multiplexer. This project is Factory v2: the same skill, rebuilt on herdr-core.

## Starting point: Factory v1

The reference is our previous factory, built on herdr 0.9.5: the `v1` branch of this repository (tag `v1.0.0`, also checked out at `/home/peter/Dev/pkieltyka/herdr-factory`). It works quite well, and it's the starting point. A lot of the time we can copy its skill files and project structure almost exactly: the roles, references, templates, harnesses, the FACTORY.md brief and factory.toml formation, the inbox, worktrees, and so on. All the harnesses and everything still hold true.

## Why a v2

v1 was built on top of herdr 0.9.5. It works, but herdr has a new direction: **herdr-core** (the CLI `hc`), which is the PTY server, and **herdr-app**, a client to that server. herdr-core supports OSC 7501, through which agents report their own state, so there are no herdr integrations (per-agent plugins/hooks) needed.

The GitHub issues on v1 show we hit a lot of problems around messaging between the members and the operator, which is what prompted looking at OSC 7501. herdr-core is the future, and the new factory targets herdr-core (`hc`) specifically.

## Clients

The factory doesn't care about the client, since it builds on `hc` directly; any client works. For our own use we'll use **rodeo** (`/home/peter/Dev/pkieltyka/rodeo`), a third-party TUI client for herdr-core. We need to compile it and get it running so we can work with herdr-core and rodeo together.

## Versions and repository

v2 lives in the same repository as v1, github.com/pkieltyka/factory. v1 (herdr 0.9.5) is frozen on the `v1` branch (tag `v1.0.0`) and gets fixes only; `master` becomes v2 and targets herdr-core (`hc`) only. One skill does not support both backends: each version of `/factory` is picked by branch (skills-sync follows `v1` until v2 runs a team). We carry v1's backend-independent parts over deliberately (roles, templates, recipes, formation, worktrees, harness launch and resume facts, inbox guarantees) and turn v1's recorded lessons into requirements, treating v1's source, tests, commits and notes as the truth and its docs as claims to check.

## Goals

- A factory skill for the herdr-core environment: simpler and leaner than v1.
- Remove the hacky parts of v1's `factory.py`. We can still have a script if we need one; there's nothing wrong with that.
- Keep what matters: an inbox and messaging between the operator and members, and the ability to wake up agents and type into their terminals.
- Plan the overview, functional spec and architecture with the spec process so we get this right, then get the skill working, and try rodeo and herdr-core (`hc`) along the way.
