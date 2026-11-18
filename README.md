# Factory

### An owner-directed software factory on herdr

`/factory` is an agent skill that forms a team, assigns work, gets it reviewed and brings it back after a restart. You direct the operator; members work in their own panes using Claude Code, Codex, pi, omp or OpenCode. Keep the built-in workflow or add tools through short project sections.

[Get started](docs/getting-started.md) · [The factory on one page](docs/README.md)

## Quickstart

By default, members run with approval prompts off. Use a sandbox or VM, or ask setup for guarded permissions for Claude Code and Codex. [Install the skill](docs/getting-started.md#install-the-skill).

From your project inside herdr, enter these commands in Claude Code. [Other operator harnesses](docs/getting-started.md#operator-harnesses)

```text
/factory setup
/factory up
/factory continue
```

## What else is in this repo

- [skill/](skill/) — what you install: instructions, roles, templates, recipes and `scripts/factory.py`.
- [docs/](docs/README.md) — the team, command map and getting-started guide.
- [tests/](tests/) — fake harness/herdr tests, real Git checks and the Gate 1 fixture; run `python3 tests/test_factory.py`. `FACTORY_LIVE=1 python3 tests/test_factory.py LiveFlags` checks installed harness flags.

To add a harness, add its table (with `operator_wait`) in `skill/harnesses.toml`, its code block and a test fake.
