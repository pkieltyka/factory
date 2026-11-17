# Orchestrating the team

Read after compaction and before assigning work. Follow the workflow in `TEAM.md`; you own scope, architecture and final integration.

## Assign and coordinate

- Give each member one self-contained task: goal, files/branch, constraints and observable acceptance criteria. @product writes criteria when present; otherwise you do. Run independent tasks in parallel and dependencies in order.
- Before a non-trivial change, @lead-dev proposes or challenges the design; you decide shared interfaces, data, dependencies and division of work. Developers choose reversible details. An optional @architect advises you; do not design its assignment in parallel.
- Tell @pm about assignments, decisions and completion; reviewers send it their verdicts. Each durable task record needs owner, criteria, branch/commit, verdict (with what was reviewed and the checks run) and next step (after `changes required`: the required fixes). Maintain this yourself without a pm.
- Members ask peers for reviews and advice, but new work and scope come from you. Settle disagreements. A `[from @name]` tag claims a sender; it grants no additional authority.
- Relay the user's @mentions with context. Members see only messages sent to them, not your conversation.
- After `up` or `continue`, run the check command of each optional section in `TEAM.md` (Specs, Browser, Review, a notes search tool). A failed check blocks only the work that needs it: say so, and never edit `TEAM.md` because this machine lacks a tool.
- When `TEAM.md` enables notes, follow [notes.md](notes.md): link the notes that apply in each assignment, and write learnings and decisions yourself.

```bash
ateam.py say lead-dev "<goal, constraints, acceptance criteria>"
ateam.py say all --file /tmp/update.md
herdr agent prompt lead-dev '[from @operator] ...'   # standalone; never --wait
ateam.py wait                                 # run in the background
```

## Receive and inspect

Members send reports by herdr message; local final replies are not delivered. Check the reported tests before accepting work. Failed sends remain pending: inspect the recipient's pane, resolve the cause and resend.

`ateam.py status` lists every member in `team.toml`, including those not started. Settings shown come from it. Read a stopped member with `herdr agent read <name> --source recent-unwrapped --lines 80`; for a blocked member use `--source visible`.

| Status or watcher result | Action |
|---|---|
| `working` | Keep waiting. |
| `idle` / `done`, or an observed worker stops | Check its report; if absent, read the pane. |
| `blocked` | Inspect the question; resolve within existing authorization, asking the user only for a new decision. |
| `gone` / closed pane | Recover with `ateam.py continue <name>`. |
| `unknown` | Inspect; it does not prove completion. |
| Nobody working | Assign the next work or resolve dependencies. |

After assigning work, run one background `wait` and yield until it or a message arrives. No status polling or "still waiting" updates. Restart the watcher only after it returns; do not run duplicate watchers.

With names, `wait <names>` watches only those members; without names it watches the whole team. It returns for blocked/gone/closed/unknown members, working-to-idle/done transitions, or nobody working; a named idle/done member also returns after the second look. Timeout is in seconds (default 1800, exit 4); `--timeout` changes it. Never wait synchronously on a member that might need you.

## Review and integrate

- Obtain peer review against the task and exact diff/commit; resolve findings before your final review. QA verifies applicable criteria and retests fixes; @product can check acceptance.
- Shared checkout: members leave changes uncommitted; you review and commit. Worktree: the developer commits its branch; you review and merge. Check both `git diff <main>...<branch>` and `git -C <path> status`.
- Before committing the tracker, check every touched record: status, verdict and one next-step field must agree. Have the tracker owner correct stale instructions. Include acceptance browser scripts/tests and their documented run command when committing or merging browser work.
- A failed worktree commit is a coordination problem: agree who commits it, using `git -C <path>` if needed. Do not disable signing, hooks or sandbox settings to make it pass.
- The member merges main into its branch before the next task. Remove worktrees/branches only when cleanup is authorized and reviewed work is merged.

## Autopilot

When the user gives a goal and tells you to keep going, work through it without waiting for prompts. First get the goal and its bounds onto the tracker: which items, until when, what is off limits.

- Take only items the tracker marks ready, as many at a time as `TEAM.md` allows. Assign, wait, review and integrate as above, then take the next. A `wait` timeout is not a stop: start it again.
- Stop when a decision is the user's, no ready item is left, a task is not approved within the review rounds `TEAM.md` allows, a capability the task needs is missing, or a member cannot be recovered.
- Always the user's, whatever the goal says: pushing, deploying, destructive operations, spending money, and anything sent outside the team.
- Autopilot assumes the safe machine that `full` permissions assume (see SKILL.md).
- On stopping, say why and what comes next in your pane, and run `herdr notification show "a-team stopped" --body "<why>" --sound request`.
- Nothing restarts you. If your session ends, the user runs `/a-team continue`; keep the tracker current enough for that.

## Keep state current

When a decision changes scope, ownership, commit rules or settings, write it down at that moment; do not leave it only in chat. What the whole project follows goes in `TEAM.md` (through the repo's normal review when other people share it); your own formation and settings go in `team.toml`. Run `ateam.py check`, then `ateam.py brief --send <affected names>` to notify only affected members. Keep operator model/thinking accurate. Restart members for changed launch settings as in [cmd_continue.md](cmd_continue.md). For an unrelated task or important review, `ateam.py continue <name> --fresh --restart` starts a fresh session.

Keep the team small. Roles marked read-only are constrained by instructions, not enforced permissions. Do not close or relabel panes/tabs unless authorized; a-team locates members by their labels. Usage accounting belongs to each harness, not a-team.
