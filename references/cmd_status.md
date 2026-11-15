# `/a-team status` and `/a-team wait`

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

- `blocked`: the member is waiting on a question. Unattended modes can still stop at startup questions or policy restrictions. Read its screen (`herdr agent read <name> --source visible`; herdr cannot read further back while an agent is blocked) and resolve it within the user's existing authorization. Ask the user only for a new decision or permission.
- `gone` → `/a-team continue`.
- The brief changed?
  - Prose only: `ateam.py brief --send`.
  - A running member's model, thinking, permissions or harness: see the last section of `cmd_continue.md`.

Summarize for the user in a few lines rather than pasting the table, unless they ask for it.

## Wait

```bash
ateam.py wait                    # until a member needs attention or an observed working member finishes
ateam.py wait lead-dev           # ... or lead-dev stops working
ateam.py wait --timeout 3600     # seconds; exits 4 if nothing happened (default 1800)
```

Run it **in the background** after handing out work, and go on with your own. It returns when you are needed:

- **a member is blocked**: it prints the end of that member's pane, so you can see what it asks. Take the question to the user;
- **a member is gone**: its agent exited. `ateam.py continue <name>`;
- **a member that was working stopped**: it finished or gave up. Its report should have reached you as a message; if not, read its pane;
- **a pane disappeared or a state is unknown**: inspect it; unknown does not mean finished.
- **nobody is working**: the whole team is waiting on you.

Members report by message, so `wait` is the safety net, not the channel: it catches the member that stops at a prompt nobody sees, and the one whose message never arrived. Start it again after each return while work is out.


Usage is not measured by a-team. Check each harness's own usage interface: Claude Code `/cost`, Codex `/status`, pi/omp session statistics, or OpenCode `stats`. The session log exists only to support `continue`.
