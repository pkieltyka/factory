# `/a-team status`, `/a-team wait` and `/a-team cost`

## Status

```bash
ateam.py status           # members with a pane
ateam.py status --all     # plus members of the brief that were never started
```

One row per member, you included: role, harness, model and thinking from the brief, and state:
- `idle`, `working`, `blocked` or `done`: the agent is running;
- `gone`: the member's pane is there but its agent is not;
- `not started`: no pane.

Panes are found by the label a-team gave them: the member's @name.

- `blocked`: the member is waiting on a question. That is rare: members run without approval prompts. Read its screen (`herdr agent read <name> --source visible`; herdr cannot read further back while an agent is blocked) and ask the user before answering it with `herdr agent send-keys`.
- `gone` → `/a-team continue`.
- The brief changed?
  - Prose only: `ateam.py brief --send`.
  - A running member's model, thinking, permissions or harness: see the last section of `cmd_continue.md`.

Summarize for the user in a few lines rather than pasting the table, unless they ask for it.

## Wait

```bash
ateam.py wait                    # until a member is blocked or gone, or nobody is working any more
ateam.py wait lead-dev           # ... or lead-dev stops working
ateam.py wait --timeout 3600     # seconds; exits 4 if nothing happened (default 1800)
```

Run it **in the background** after handing out work, and go on with your own. It returns when you are needed:

- **a member is blocked**: it prints the end of that member's pane, so you can see what it asks. Take the question to the user;
- **a member is gone**: its agent exited. `ateam.py continue <name>`;
- **a named member stopped**: it finished or gave up. Its report should have reached you as a message; if not, read its pane;
- **nobody is working**: the whole team is waiting on you.

Members report by message, so `wait` is the safety net, not the channel: it catches the member that stops at a prompt nobody sees, and the one whose message never arrived. Start it again after each return while work is out.

## Cost

```bash
ateam.py cost            # table
ateam.py cost --json
```

Tokens and dollars for each agent, you included, across **every session it has had**: a member restarted with `--fresh` keeps its earlier spend. Every command that looks at the team notes each agent's current session in `~/.cache/a-team/<repo>/sessions.jsonl`; `cost` adds them up. If that file is deleted, only the current sessions count.

| Harness | Tokens | Dollars |
|---|---|---|
| pi, omp | live | live, recorded per message |
| Claude Code | live, subagents included | when the session exits or resumes; before that, an estimate (`~`) if `[prices]` is set in the brief or `harnesses.toml` |
| Codex | live | only via `[prices]` |
| OpenCode | not read yet | `opencode stats` |

A total of `$X+?` means some agents have tokens but no price. A member with no numbers has not reported a session to herdr: run `herdr integration install <harness>`.
