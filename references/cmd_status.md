# `/devteam status` and `/devteam cost`

## Status

```bash
devteam.py status         # members with a pane
devteam.py status --all   # plus members of the brief that were never started
```

One row per member, you included: role, harness, model and thinking from the brief, and state:
- `idle`, `working`, `blocked` or `done`: the agent is running;
- `gone`: the member's pane is there but its agent is not;
- `not started`: no pane.

Panes are found by the label devteam gave them: the member's @name.

- `blocked`: the member is waiting on an approval or a question. Read its pane (`herdr agent read <name> --source recent-unwrapped --lines 80`) and ask the user before answering it with `herdr agent send-keys`.
- `gone` → `/devteam continue`.
- The brief changed?
  - Prose only: `devteam.py brief --send`.
  - A running member's model, thinking or harness: ask the user, then stop that member's agent (`herdr agent send-keys <name> ctrl+c`, or `/exit` in its pane) and run `devteam.py continue <name>`. It resumes in the same pane with the new settings.

Summarize for the user in a few lines rather than pasting the table, unless they ask for it.

## Cost

```bash
devteam.py cost          # table
devteam.py cost --json
```

Tokens and dollars for each labeled pane, you included. They are read from the harness session file herdr recorded for that pane, so members whose agents exited still count, as long as their pane exists.

| Harness | Tokens | Dollars |
|---|---|---|
| pi, omp | live | live, recorded per message |
| Claude Code | live, subagents included | when the session exits or resumes; before that, an estimate (`~`) if `[prices]` is set in the brief or `models.toml` |
| Codex | live | only via `[prices]` |
| OpenCode | not read yet | `opencode stats` |

A total of `$X+?` means some agents have tokens but no price. A member with no numbers has not reported a session to herdr: run `herdr integration install <harness>`.
