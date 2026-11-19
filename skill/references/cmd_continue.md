# Bring the team back

Use after agents exit, herdr restarts or you lose context, including when no member and no session is left. `FACTORY.md`, the tracker and Git must be enough to recover the project; sessions only make it faster.

```bash
factory.py status
factory.py up                # every configured member, including those with closed panes
factory.py up lead-dev       # selected member, even if its pane closed
factory.py up --restart      # reapply settings to running members
factory.py up lead-dev --fresh --restart  # new session
```

Before any launch or restart, `up` checks the affected members with current `factory.toml` settings. A failure exits 2. It names each member it starts fresh or resumes from its last noted session of the same harness. Use `--fresh` to bypass saved sessions. To keep a stopped member out, name the others or remove it from `factory.toml`. Observations live in `~/.cache/factory-skill/<repo>/sessions.jsonl`; herdr forgets a session when its agent exits.

- Reuse the old shell pane only when its cwd matches the configured checkout and the harness needs no new pane environment. Otherwise create a replacement; clear the old label and leave that pane as a plain shell. Worktrees remain intact.
- Codex receives an explicit working directory on resume; its saved session alone may point at an old checkout.
- After a herdr restart, run `up --restart`: herdr restores names and sessions but omits factory launch settings and standing-instruction flags.
- Failed resume: inspect the error and use `up <name> --fresh` if the saved session cannot reopen.
- Failed brief delivery exits 3: resolve the cause and `factory.py brief --send <name>`.
- The inbox outlives the team. A cold `up` (no member live, no `--restart`) announces unread messages; the last 100 acknowledged ones stay available to `factory.py inbox seen`.

## Reconcile before assigning

When everyone is running, `up` sends nothing. Check the tracker and Git and use `say` to ask the members concerned for a one-line status.

Read in this order: `FACTORY.md`, `factory.toml` and project plans; the tracker; Git (`git status`, `git log --oneline -10`, `git worktree list`, each worktree's `git -C <path> status` and `git diff <main>...<branch>`, and PRs and CI when the project has them); last, members' replies and the unread messages `wait` prints. Where they disagree, Git and CI outrank the tracker, and the tracker outranks anyone's memory, yours included. Have the tracker's owner correct a stale record; do not act on it.

- Match every uncommitted change to an in-progress item before any member writes. A change no item explains is not built on, committed or discarded: ask the user.
- A verdict covers only the commit or paths it names. Ask for a new review when the record has no verdict or no checks, when the branch moved past the reviewed commit, or when approved work was never committed.
- Tell the user where the project stands: each open item with its state, evidence and next action, then anything unexplained or contradictory. Then carry on; stop only for those and for decisions that are the user's.
- Send each fresh member its task again: goal, criteria, branch or files, the evidence so far and the required fixes still open. Resumed members report what they were doing; check it against the above.

Then follow [leading.md](leading.md).

## Change a running member's settings

After an authorized `factory.toml` change, run `up <name> --restart`. It validates before closing the selected member's pane, then resumes in a new pane with current settings and standing instructions. Coordinate any active work first. Add `--fresh` for a new session; alone, `--fresh` leaves running agents alone.
