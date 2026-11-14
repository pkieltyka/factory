# `/devteam continue`: pick up where the team left off

Use this when members' agents exited, herdr restarted, or your own session was lost and the user started you again.

## 1. Re-read before acting

Your own context may be new. Read:
1. `team-brief.md`: who is on the team, how you work, and the project.
2. The tracker the brief names, if any: what is done, in progress, or blocked.
3. `git status` and `git log --oneline -10`: uncommitted work and recent commits.
4. Spec or plan documents the project uses (for example `specs/`), if any.

## 2. See what is running

```bash
devteam.py status
```

Each member's pane carries its @name. A member is:
- `idle`, `working` or `blocked`: running;
- `... (unnamed)`: running, but herdr resumed it after a restart (see below);
- `gone`: its pane is there but its agent is not;
- missing from the list: its pane was closed.

**After a herdr restart**, herdr itself resumes every agent whose session its integration recorded (Claude, Codex, pi, omp, OpenCode), in the restored panes. This is on by default; `[session] resume_agents_on_restore` in herdr's config controls it. Two things are lost:
- agent names, which do not survive a restart, so these members show as `(unnamed)`;
- the member's flags: herdr resumes with the bare resume command, so the agent runs on its harness's default model and without its appended system prompt.

**If no member panes show up at all** after a restart, the pane labels did not survive it; herdr's docs do not say whether they do. The agents are still there, resumed in unlabeled panes in this repo:
1. List them with `herdr pane list`.
2. Ask each one who it is: `herdr agent prompt <pane> "[from @cto] Which team member are you? Reply with your @name only."`. Each one's context says who it is.
3. Label each pane: `herdr pane rename <pane> <name>`.
4. Then run `continue`.

## 3. Bring the members back

```bash
devteam.py continue              # every member with a pane whose agent is gone
devteam.py continue lead-dev     # just these (also members whose pane was closed)
devteam.py continue --fresh      # new sessions instead of resuming
```

For a member that herdr already resumed (`unnamed`), `continue` **adopts** it:
- it names the agent again;
- it re-sends the member's standing instructions as a message;
- it reports that the agent may be on a different model than the brief says. To apply the brief's model and thinking, exit that agent and run `devteam.py continue <name>`.

For a member whose agent is gone, `continue` starts the agent again in the member's own pane and reopens the harness session herdr recorded for that pane, so the member keeps its context: Claude `--resume`, Codex `resume`, pi `--session`, omp `--resume`, OpenCode `--session`.

- Settings come from the brief **as it is now**, so edited models and thinking levels take effect.
- A member whose harness changed starts fresh: a session cannot move across harnesses.
- A member whose pane was closed gets a new labeled pane and a fresh session.
- It runs the same check as `up` and launches nothing if any member fails. Handle that as in `cmd_up.md`.

Then it re-sends the team brief to every running member, plus a note asking each to re-read it, check the tracker, and tell you in one line what it was doing and what comes next.

## 4. Resume the work

- Collect the one-line answers. Compare them with the tracker and `git status`, and fix any mismatch with @pm.
- Members that started fresh lost their context. Re-brief them on the project and their task.
- Tell the user where things stand: done, in progress, blocked, next. Then continue as in `leading.md`.
