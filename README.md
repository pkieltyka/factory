# Factory

> **This checkout is Factory for herdr-core.** Until v2 ships, install the current release from the [`v1` branch](https://github.com/pkieltyka/factory/tree/v1) (tag `v1.0.0`).

Factory runs a team of coding agents in [herdr-core](https://github.com/herdrdev/herdr-core) (`hc)) and shows it in [rodeo](https://github.com/lukeed/rodeo). You set the direction. The operator assigns work, coordinates review and integrates approved changes. Members run in their own native harnesses and panes.

- **State comes from the agents.** Factory uses the OSC 7501 state reported to `hc` to show which members are working, idle, blocked or gone.
- **Messages arrive in the pane.** The keeper delivers stored messages and records their outcomes. Use the inbox to reread them.
- **The team works through ready items.** The keeper tells a reachable operator when the team goes quiet. An optional read-only tracker check can announce new ready work.

## Why Factory

- Mix Claude Code, Codex, pi, omp, OpenCode and Antigravity CLI (agy) as members. Choose each role's harness, model and thinking in your own `factory.toml`.
- Give the team a shared brief and workflow in `FACTORY.md`. Use a Markdown `TODO.md`, GitHub Issues or a project board. Add pull requests, notes, specs or browser testing when your project uses them.
- Watch or steer any member in rodeo. Git and the tracker hold the durable record of what the team changed and why.

Each project can have its own team. People sharing a repository can share the brief while keeping their formations and worktrees separate.

## Quickstart

Install `hc` 0.1.20 or newer, rodeo, Python 3.11+, Git and a signed-in harness. [Install the skill from this checkout](docs/getting-started.md#install-the-skill).

From your project directory, run `rodeo --session first-factory` and start your operator harness in its pane. In Claude Code, enter:

```text
/factory setup
/factory up
```

The default member permissions assume a safe machine such as a sandbox or VM. Choose `guarded` during setup to keep the harness guard for Claude Code and Codex.

[Getting started](docs/getting-started.md) · [The team and commands](docs/README.md) · [Design and implementation plan](specs/projects/factory/)

## Development

- [skill/](skill/) contains the installable skill, roles, templates, recipes and `scripts/factory.py`.
- [tests/](tests/) covers the fake harnesses and fake `hc`, a private real `hc` core, Git and recovery. Run `python3 tests/test_factory.py`. Set `FACTORY_TEST_HC=real` for the private core suite.
- Run `python3 tests/live.py smoke` for the private-core rodeo smoke test.

## License

MIT. See [LICENSE](LICENSE).
