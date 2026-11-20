# Factory

**Factory is a software factory skill.**

It lets you build autonomous software engineering teams from the agents and models you already use: Claude Code, Codex, pi, omp, OpenCode and Antigravity (agy), across model families, on your own plans and subscriptions. Each member runs in its own native harness, in its own herdr pane, and the team works together on your project.

[Get started](docs/getting-started.md) · [The factory on one page](docs/README.md)

## Why Factory

- **Your harnesses, natively.** Members run the real Claude Code, Codex, pi, omp, OpenCode or agy, with your plans and subscriptions. Mix model families on one team.
- **Real roles.** Architect, lead developer, frontend and backend developers, designer, product, project manager, QA and reviewer. Add your own responsibilities, and give any role any harness and model.
- **Autonomous, but close to the harness.** The team plans, builds, reviews and integrates on its own, but nothing is hidden: every member is a live pane you can watch, talk to and steer, and the factory itself is a few plain files you can reshape.
- **Unix philosophy.** Small, direct tools composed together: herdr for panes and messaging, git for history, your tracker for tasks, a single script for the team. Simple to understand, powerful in combination.
- **Pluggable.** The issue tracker is a choice, not a lock-in: a Markdown `TODO.md` punch list, GitHub Issues via `gh`, or the `todos` CLI with a kanban board. Review, pull requests, specs and browser testing plug in the same way, through short sections in `FACTORY.md`.
- **It learns as it works.** Members record what they learn as notes in the repo, which later tasks read, so the team stops repeating mistakes.

## Single-player and multiplayer

- **Single-player:** one person running a team of agent engineers.
- **Multiplayer:** a team of people, each running their own factory on the same repo. `FACTORY.md` and shared notes are committed and shared; your `factory.toml` (your own team) and private notes stay yours. Work follows normal git practice: branches, reviewed pull requests, CI.

## Quickstart

By default, members run with approval prompts off. Use a sandbox or VM, or ask setup for guarded permissions for Claude Code and Codex. [Install the skill](docs/getting-started.md#install-the-skill).

From your project inside herdr, in Claude Code ([other operator harnesses](docs/getting-started.md#operator-harnesses)):

```text
/factory setup
/factory up
```

## Coming next

Pluggable project context (notes, specs and knowledge per project), and cost and usage tracking per session.

## What else is in this repo

- [skill/](skill/) — what you install: instructions, roles, templates, recipes and `scripts/factory.py`.
- [docs/](docs/README.md) — the team, command map and getting-started guide.
- [tests/](tests/) — fake harness/herdr tests, real Git checks and the Gate 1 fixture; run `python3 tests/test_factory.py`. `FACTORY_LIVE=1 python3 tests/test_factory.py LiveFlags` checks installed harness flags.

To add a harness, add its table (with `operator_wait`) in `skill/harnesses.toml`, its code block and a test fake.

## License

MIT. See [LICENSE](LICENSE).
