# `/devteam status` and `/devteam cost`

## Status

```bash
devteam.py status
```

One row per agent, you included: role, harness, model, thinking, state (`idle`, `working`, `blocked`, `done`, `gone`), pane, and **Changed in brief**: settings in `.devteam/team-brief.md` that differ from what the member is running.

- `blocked` means the member waits on an approval or a question. Read its pane (`herdr agent read <name> --source recent-unwrapped --lines 80`) and ask the user before answering it with `herdr agent send-keys`.
- `gone` → `/devteam continue`.
- To apply a change to a running member: ask the user, then close that member's pane (`herdr pane close <pane>`; only panes devteam started) and run `devteam.py continue <name>`. It resumes the session with the new settings. A change to the prose only needs `devteam.py brief --send`.

Summarize for the user in a few lines rather than pasting the table, unless they ask for it.

## Cost

```bash
devteam.py cost          # table
devteam.py cost --json
```

Tokens and dollars per agent, read from each harness's session files through the session ids herdr reports. Members that exited still count, and so do earlier sessions of a resumed member.

| Harness | Tokens | Dollars |
|---|---|---|
| pi, omp | live | live, recorded per message |
| Claude Code | live, subagents included | when the session exits or resumes; before that only an estimate (`~`), if `[prices]` is set in the team brief or `models.toml` |
| Codex | live | only via `[prices]` |
| OpenCode | not read yet | `opencode stats` |

A total of `$X+?` means some agents have tokens but no price. Members with no numbers have not reported a session to herdr: run `herdr integration install <harness>`.
