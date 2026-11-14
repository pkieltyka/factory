# `/devteam continue`: pick up where the team left off

Use this when herdr restarted, member panes closed, or your own session was lost and the user started you again.

## 1. Re-read before acting

Your own context may be new. Read, in order:
1. `.devteam/team-brief.md`: who is on the team and how you work.
2. `.devteam/project-brief.md`: what you are building.
3. The board (`TODO.md`, or `board` in the settings): what is done, in progress, blocked.
4. `git status` and `git log --oneline -10`: uncommitted work and recent commits.

## 2. See what is running

```bash
devteam.py status
```

Members are `idle`, `working` or `blocked`, or `gone` when their pane or agent is no longer there. "Changed in brief" lists settings edited since the member started.

## 3. Bring back the missing members

```bash
devteam.py continue              # every gone member
devteam.py continue lead-dev     # just these
devteam.py continue --fresh      # new sessions instead of resuming
```

For each gone member, `continue`:
- reopens its **last session**, so it keeps its own context: Claude `--resume`, Codex `resume`, pi `--session`, omp `--resume`, OpenCode `--session`;
- applies the **current** settings from the brief, so edited models and thinking levels take effect. If the member's harness changed, the old session cannot move across harnesses and it starts fresh;
- runs the same check as `up` first, and launches nothing if any member fails. Handle a failure as in `cmd_up.md`.

Then it re-sends the team brief and project brief to every running member, plus a note asking each to re-read them, check the board, and tell you in one line what it was doing and what comes next.

## 4. Resume the work

- Collect the one-line answers. Compare them with the board and `git status`, and fix any mismatch with @pm.
- Tell the user where things stand: done, in progress, blocked, next. Then continue as in `leading.md`.
