# `/a-team continue`: pick up where the team left off

Use this when members' agents exited, herdr restarted, or your own session was lost and the user started you again.

## 1. Re-read before acting

Your own context may be new. Read:
1. `team-brief.md`: who is on the team, how you work, and the project.
2. The tracker the brief names, if any: what is done, in progress, or blocked.
3. `git status`, `git log --oneline -10` and `git worktree list`: uncommitted work, recent commits, and members' branches.
4. Spec or plan documents the project uses (for example `specs/`), if any.

## 2. See what is running

```bash
ateam.py status
```

Each member's pane carries its @name. A member is:
- `idle`, `working`, `blocked` or `done`: running;
- `... (unnamed)`: running, but herdr brought it back after a restart without its name (see below);
- `gone`: its pane is there but its agent is not;
- missing from the list: its pane was closed.

**After a herdr restart**, herdr restores the panes with their labels, and resumes every agent whose session its integration recorded. This is on by default; `[session] resume_agents_on_restore` in herdr's config controls it. An agent that comes back without its name may also be running without the model, thinking and permissions the brief gives it.

## 3. Bring the members back

```bash
ateam.py continue                # every member with a pane; everyone is re-briefed
ateam.py continue lead-dev       # just these (also members whose pane was closed)
ateam.py continue --fresh        # new sessions instead of resuming
```

For a member whose agent is gone, `continue` starts the agent again in its pane when the shell is still in the configured directory, or a replacement pane otherwise and reopens its last harness session, so the member keeps its context: Claude `--resume`, Codex `resume`, pi `--session`, omp `--resume`, OpenCode `--session`. herdr forgets a pane's session when its agent exits, so a-team notes each agent's session whenever a command looks at the team (`~/.cache/a-team/<repo>/sessions.jsonl`), and resumes from that.

- Settings come from the brief **as it is now**, so edited models, thinking levels and permissions take effect.
- If the configured worktree changes, or the old shell moved to another directory, the member gets a new pane in the configured checkout. The old pane loses its member label and remains a plain shell.
- A member whose harness changed starts fresh: a session cannot move across harnesses.
- A member whose pane was closed gets a new pane in the team tab, and its session back if one was noted. Its worktree, if it has one, is kept.
- A member whose session was never noted (it exited before any command looked at the team) starts fresh.
- It runs the same check as `up` and launches nothing if any member fails. Handle that as in `cmd_up.md`.
- If a member does not come up because its session cannot be resumed, run `ateam.py continue <name> --fresh`.

For a member that herdr already resumed without its name (`unnamed`), `continue` **adopts** it: it names the agent again and sends it its standing instructions with the brief. To put it back on the brief's model, thinking and permissions, exit that agent and run `ateam.py continue <name>`.

Then it sends the team brief, ending with a request to re-read it, check the tracker, and tell you in one line what the member was doing and what comes next. With names, only those members get it. With no names you are catching up, so every running member does.

## 4. Resume the work

- Collect the one-line answers. Compare them with the tracker, `git status` and members' branches, and fix any mismatch with @pm.
- Members that started fresh lost their context. Re-brief them on the project and their task.
- Tell the user where things stand: done, in progress, blocked, next. Then continue as in `leading.md`.

## To change a running member's model, thinking or permissions

Once the settings change is authorized, edit the brief, then exit that member's agent in its pane (its own quit command, for example `/exit` in Claude Code or `/quit` in Codex, typed with `herdr agent prompt <name> '/exit'`). When `ateam.py status` shows it `gone`, run `ateam.py continue <name>`. It resumes its session in the same pane with the new settings.
