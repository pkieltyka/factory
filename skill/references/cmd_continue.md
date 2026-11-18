# Continue

Use after agents exit, herdr restarts or you lose context, including when no member and no session is left. `FACTORY.md`, the tracker and Git must be enough to recover the project; sessions only make it faster.

```bash
factory.py status
factory.py continue                # members with panes, or the whole team when none has one; re-briefs everyone running
factory.py continue lead-dev       # selected member, even if its pane closed
factory.py continue --restart      # reapply settings to running members
factory.py continue lead-dev --fresh --restart  # new session
```

`continue` checks every member needing launch before starting any; failures exit 2. Current `factory.toml` settings apply. It resumes each member's last noted session of the same harness, or starts fresh if the harness changed or no session was noted. A fresh member is told to wait for its assignment, and `continue` lists the fresh ones. Observations live in `~/.cache/factory-skill/<repo>/sessions.jsonl` (`~/.cache/a-team/<repo>/sessions.jsonl` for a project that ran under a-team); herdr forgets a session when its agent exits.

- Reuse the old shell pane only when its cwd matches the configured checkout and the harness needs no new pane environment. Otherwise create a replacement; clear the old label and leave that pane as a plain shell. Worktrees remain intact.
- A member with `worktree = true` works on branch `<user>/<name>`. To keep using a checkout made before that, on `a-team/<name>`, set `worktree = "a-team/<name>"` for it in `factory.toml`; `check` stops and says so when it finds one.
- Codex receives an explicit working directory on resume; its saved session alone may point at an old checkout.
- After a herdr restart, run `continue --restart`: herdr restores names and sessions but omits factory launch settings and standing-instruction flags.
- Failed resume: inspect the error and use `continue <name> --fresh` if the saved session cannot reopen.
- Failed brief delivery exits 3: resolve the cause and `factory.py brief --send <name>`.
- The inbox outlives the team. A cold `continue` or `up` (no member live, no `--restart`) announces unread messages; the last 100 acknowledged ones stay available to `factory.py inbox seen`.

## Reconcile before assigning

Read in this order: `FACTORY.md`, `factory.toml` and project plans; the tracker; Git (`git status`, `git log --oneline -10`, `git worktree list`, each worktree's `git -C <path> status` and `git diff <main>...<branch>`, and PRs and CI when the project has them); last, members' replies and the unread messages `wait` prints. Where they disagree, Git and CI outrank the tracker, and the tracker outranks anyone's memory, yours included. Have the tracker's owner correct a stale record; do not act on it.

- Match every uncommitted change to an in-progress item before any member writes. A change no item explains is not built on, committed or discarded: ask the user.
- A verdict covers only the commit or paths it names. Ask for a new review when the record has no verdict or no checks, when the branch moved past the reviewed commit, or when approved work was never committed.
- Tell the user where the project stands: each open item with its state, evidence and next action, then anything unexplained or contradictory. Then carry on; stop only for those and for decisions that are the user's.
- Send each fresh member its task again: goal, criteria, branch or files, the evidence so far and the required fixes still open. Resumed members report what they were doing; check it against the above.

Then follow [leading.md](leading.md).

## Change a running member's settings

After an authorized `factory.toml` change, run `continue <name> --restart`. It validates before closing the selected member's pane, then resumes in a new pane with current settings and standing instructions. Coordinate any active work first. Add `--fresh` for a new session; alone, `--fresh` leaves running agents alone.
