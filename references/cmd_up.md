# `/a-team up`: launch and brief the team

## 1. Preconditions

- You are inside herdr, and your sandbox (if any) lets `ateam.py` reach herdr and the harnesses (see SKILL.md).
- `team-brief.md` exists at the repo root. If not → `/a-team setup`.
- One team runs per herdr session. For a second project, use a second session (`herdr --session <name>`).

## 2. Check, then launch

```bash
ateam.py up                      # the `default` team
ateam.py up --team full          # another roster from [teams]
ateam.py up reviewer pm          # some members
ateam.py up --role backend-dev --model gpt-6-luna     # one more backend-dev; added to the brief
ateam.py up --role backend-dev --worktree             # ... in a git worktree of its own
ateam.py up --dry-run            # check and print the plan only
```

`up` checks every member first: harness installed, model available, thinking level allowed. **If any member fails, nothing launches.** Then:

- Stop. Tell the user which members failed, the problem, and the printed fix. For example: "GLM 5.3 is not configured in pi: add an OpenRouter key (`pi auth`) or serve it locally and add it to pi's models."
- Ask whether to fix the setup or change the member in the brief. Never swap in another model, harness or thinking level yourself, and never launch the others without the user's go-ahead.

On success, `up`:
1. names your agent and your pane after `[lead] name` (`cto`), and labels your tab `a-team driver`;
2. opens one tab, `a-team workers`, for the whole team. Each member gets a pane there, labeled with its @name. A new pane always splits the largest one, so the panes stay a balanced grid;
3. creates the git worktree of a member that has one, and starts that member in it;
4. starts each member with its standing instructions (role rules, how to talk, the tracker, the commit rule, where the brief lives) and its permission level;
5. sends each new member the team brief. Members that were already running get one line saying who joined.

**`--role`** adds one more member of a role and names it by the rule: the role's only member is `@backend-dev`; from the second on they are all numbered. So adding a second backend-dev renames the first to `@backend-dev-1` (in the brief, its pane and its agent, and it is told) and starts `@backend-dev-2`. The new member starts from the first one's harness, model and thinking unless you pass others.

## 3. If a member does not start

`up` keeps going with the other members, prints what herdr reported for each one that failed, and exits non-zero.

- **"did not come up"**: read the pane it names (`herdr pane read <pane> --source recent-unwrapped --lines 60`), tell the user what you see, fix the cause, and run `ateam.py up <name>` again. `up` skips members that are already running.
- **"started but is waiting on a dialog"**: usually a first-run question, such as trusting the folder. `up` waits a few seconds first, so a notice that clears by itself is not reported. Read what is on its screen (`herdr agent read <name> --source visible`; herdr cannot read further back while an agent is blocked) and take the question to the user. Once it is answered, run `ateam.py brief --send <name>`.

## 4. Permissions

The brief's `permissions` says how much members may do without asking; a member can have its own. The flags for each harness are in the skill's `harnesses.toml`.

| Level | What it means |
|---|---|
| `auto` (default) | Members run unattended, guarded where the harness can: Claude Code's auto mode, Codex's sandbox with automatic review of what leaves it, OpenCode `--auto`. |
| `ask` | No flags: each harness behaves as the user has configured it, which normally means it stops for approvals. Use it when the user wants to approve actions. |
| `full` | No sandbox and no approvals. Only when the user asks for it. |

- pi and omp have no guard at any level: they run every command without asking.
- `auto` falls back to asking where the harness cannot offer it (for example Claude Code's auto mode on an account without it). `ateam.py wait` tells you when a member is blocked.
- Native flags a member needs beyond this go in its `args` in the brief, so `continue` applies them again.

## 5. After launch

- Members do not reply to the brief. Check that each one is idle with `ateam.py status`, and read the pane of any that is not.
- Brief the project: what we are building, for whom, scope and constraints, where things live, and the first tasks with owners. Use `ateam.py say all --file <draft>` or a message. Write it for agents who have never seen this conversation. If the user runs `/spec`, point members to its project folder instead of repeating it.
- Then hand out work: see `leading.md`.
