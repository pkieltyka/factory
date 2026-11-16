# Continue

Use after agents exit or the operator loses context. Re-read `TEAM.md`, `team.toml`, the tracker, project plans, `git status`, `git log --oneline -10` and `git worktree list` before assigning more work.

```bash
ateam.py status
ateam.py continue                # members with panes; re-brief all running members
ateam.py continue lead-dev       # selected member, even if its pane closed
ateam.py continue --restart      # reapply settings to running members
ateam.py continue lead-dev --fresh --restart  # new session
```

`continue` checks every member needing launch before starting any; failures exit 2. Current `team.toml` settings apply. It resumes each member's last noted session of the same harness, or starts fresh if the harness changed or no session was noted. Observations live in `~/.cache/a-team/<repo>/sessions.jsonl`; herdr forgets a session when its agent exits.

- Reuse the old shell pane only when its cwd matches the configured checkout and the harness needs no new pane environment. Otherwise create a replacement; clear the old label and leave that pane as a plain shell. Worktrees remain intact.
- A member with `worktree = true` works on branch `<user>/<name>`. To keep using a checkout made before that, on `a-team/<name>`, set `worktree = "a-team/<name>"` for it in `team.toml`; `check` stops and says so when it finds one.
- Codex receives an explicit working directory on resume; its saved session alone may point at an old checkout.
- After a herdr restart, run `continue --restart`: herdr restores names and sessions but omits a-team launch settings and standing-instruction flags.
- Failed resume: inspect the error and use `continue <name> --fresh` if the saved session cannot reopen.
- Failed brief delivery exits 3: resolve the cause and `ateam.py brief --send <name>`.

Members are asked to report what they were doing and their next step. Compare replies with Git and the tracker; reassign missing context for fresh sessions. Then follow [leading.md](leading.md).

## Change a running member's settings

After an authorized `team.toml` change, run `continue <name> --restart`. It validates before closing the selected member's pane, then resumes in a new pane with current settings and standing instructions. Coordinate any active work first. Add `--fresh` for a new session; alone, `--fresh` leaves running agents alone.
