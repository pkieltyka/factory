# Factory

> **This checkout is Factory for herdr-core.** Until v2 ships, install the current release from the [`v1` branch](https://github.com/pkieltyka/factory/tree/v1) (tag `v1.0.0`).

## What's new in v2

Factory runs a team of coding agents in [herdr-core](https://github.com/herdrdev/herdr-core) (`hc`) and shows it in [rodeo](https://github.com/lukeed/rodeo). You set the direction. The operator assigns work, coordinates review and integrates approved changes. Members run in their own native harnesses and panes.

- **State comes from the agents.** Factory uses the OSC 7501 state reported to `hc` to show which members are working, idle, blocked or gone.
- **Messages arrive in the pane.** The keeper delivers stored messages and records their outcomes. Use the inbox to reread them.
- **The team works through ready items.** The keeper tells a reachable operator when the team goes quiet. An optional read-only tracker check can announce new ready work.

**Factory is your minimal autonomous software engineering agent team, built for the tools you already use, and packaged as a simple skill.**

It lets you build autonomous software engineering teams from the agents and models you already use: Claude Code, Codex, pi, omp, OpenCode and Antigravity (agy), across model families, on your own plans and subscriptions. Each member runs in its own native harness, in its own rodeo pane, and the team talks it through: members message each other, share context and put their heads together on the work.

It's a full autonomous software factory, yet it's just a skill: a few plain files and one script. That keeps it lightweight, easy to tweak and clear about how it works. Your project's `FACTORY.md`, which starts from a template, is where you describe your team's rules and how you want the factory to work.

Factory feels like a big upgrade to a coding agent flow yet with the same tools we're already using. In our experience the results come out more refined and sharper.

**Mix model families by role.** A favourite setup:

| Role | Model |
|---|---|
| architect, architect-2 | Claude Fable, Codex Astra |
| lead-dev, lead-dev-2 | Claude Opus, Codex Sol |
| reviewer, pm | Codex Luna |
| frontend-dev | Claude Opus |

Or run GLM 5.3 as a pi member, with a Fable architect, an Opus lead developer and Luna reviewing. Spreading the work across roles lets you balance cost and intelligence, and use each model for what it's best at. It's all configured in `FACTORY.md` and `factory.toml`.

**How you work with it.** You pick an operator: the harness and model you talk to, and a medium-level model is plenty. The operator directs the whole team, each member in its role. You sit in the operator's cockpit and lead, the way a head of engineering or CTO works with their engineering team: members do the work, review each other, and report back to you. Make the team as big or as small as you like. The team's brief, behaviour and roles live in `FACTORY.md`, committed with your project. Your own `factory.toml` says which harness and model fills each role, so every person who uses the factory can run the same team with their own favourite harnesses and models.

[Get started](docs/getting-started.md) · [The factory on one page](docs/README.md)

## Why Factory

- **Your harnesses, natively.** Members run the real Claude Code, Codex, pi, omp, OpenCode or agy, with your plans and subscriptions. Mix model families on one team.
- **Real roles.** Architect, lead developer, frontend and backend developers, designer, product, project manager, QA and reviewer. Add your own responsibilities, and give any role any harness and model.
- **Autonomous, but close to the harness.** The team plans, builds, reviews and integrates on its own, but nothing is hidden: every member is a live pane you can watch, talk to and steer, and the factory itself is a few plain files you can reshape.
- **Unix philosophy.** Small, direct tools composed together: `hc` for terminals and messaging, rodeo for panes, git for history, your tracker for tasks, a single script for the team. Simple to understand, powerful in combination.
- **Pluggable.** The issue tracker is a choice, not a lock-in: a Markdown `TODO.md` punch list, GitHub Issues via `gh`, or the `todos` CLI with a kanban board. Review, pull requests, specs and browser testing plug in the same way, through short sections in `FACTORY.md`.
- **It learns as it works.** Members record what they learn as notes in the repo, which later tasks read, so the team stops repeating mistakes.

## Single-player and multiplayer

- **Single-player:** one person running a team of agent engineers.
- **Multiplayer:** a team of people, each running their own factory on the same repo. `FACTORY.md` and shared notes are committed and shared; your `factory.toml` (your own team) and private notes stay yours. Work follows normal git practice: branches, reviewed pull requests, CI.

## Quickstart

Install `hc` 0.1.20 or newer, rodeo, Python 3.11+, Git and a signed-in harness. [Install the skill from this checkout](docs/getting-started.md#install-the-skill).

From your project directory, run `rodeo --session first-factory` and start your operator harness in its pane. In Claude Code, enter the commands below. [Other harness invocations](docs/getting-started.md#operator-harnesses)

```text
/factory setup
/factory up
```

The default member permissions assume a safe machine such as a sandbox or VM. Choose `guarded` during setup to keep the harness guard for Claude Code and Codex.

[Getting started](docs/getting-started.md) · [The team and commands](docs/README.md) · [Design and implementation plan](specs/projects/factory/)

## Coming next

Pluggable project context (notes, specs and knowledge per project), and cost and usage tracking per session.

## What else is in this repo

- [skill/](skill/) — what you install: instructions, roles, templates, recipes and `scripts/factory.py`.
- [docs/](docs/README.md) — the team, command map and getting-started guide.
- [tests/](tests/) — fake harness and fake `hc` tests, a private real `hc` core, real Git checks and the Gate 1 fixture. Run `python3 tests/test_factory.py`, or set `FACTORY_TEST_HC=real` for the private core suite. Run `python3 tests/live.py smoke` to exercise the rodeo workflow.

To add a harness, add its table in `skill/harnesses.toml`, its code block and a test fake.

## License

MIT. See [LICENSE](LICENSE).
