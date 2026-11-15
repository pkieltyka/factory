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
1. names your agent and your pane after `[lead] name` (`cto`), and labels your tab `a-team orchestrator`;
2. opens one tab, `a-team workers`, for the whole team. Each member gets a pane there, labeled with its @name. A new pane always splits the largest one, so the panes stay a balanced grid;
3. creates the git worktree of a member that has one, and starts that member in it;
4. starts each member with its standing instructions (its role, how to talk, the report form, the tracker, the commit rule, where the brief lives) and its permission level;
5. sends each new member the team brief. Members that were already running get one line saying who joined.

**`--role`** adds a member without renaming anyone: `@backend-dev`, then `@backend-dev-2`, `@backend-dev-3`. It starts from the first member's harness, model and thinking unless you supply creation flags. To change an existing member, edit the brief and restart it with `continue`; launch overrides are only for a new `--role` member.

## 3. If a member does not start

`up` reports failed starts or undelivered briefs and exits non-zero. For an undelivered brief, resolve the reported cause and run `ateam.py brief --send <name>` before assigning work.

- **"did not come up"**: read the pane it names (`herdr pane read <pane> --source recent-unwrapped --lines 60`), tell the user what you see, fix the cause, and run `ateam.py up <name>` again. `up` skips members that are already running.
- **"started but is waiting on a dialog"**: the member stopped at a question `up` does not know. `up` answers the first-run questions it does know by itself (trusting the folder, on Claude Code and Codex; they are listed as `dialogs` in `harnesses.toml`), and waits a few seconds so a notice that clears by itself is not reported. Read what is on its screen (`herdr agent read <name> --source visible`; herdr cannot read further back while an agent is blocked) and take the question to the user. Once it is answered, run `ateam.py brief --send <name>`.

## 4. Permissions

The default, `full`, uses each harness's unattended permission mode, and `up` clears known first-run questions. Existing harness sandbox settings and higher-level policies can still apply, and an unknown startup question can still block. It assumes the machine is itself a sandbox or a VM. Messages between members need no approval on any harness.

One stricter level exists for a machine that is not a sandbox: `permissions = "guarded"` in the brief, for the team or for one member.

| Harness | `full` (default) | `guarded` |
|---|---|---|
| Claude Code | bypass-permissions mode; keeps an inherited sandbox | auto mode: a classifier reviews each action, and can stop and ask |
| Codex | no sandbox, no approvals | its workspace sandbox, with automatic review of what leaves it |
| pi, omp, OpenCode | every command runs | the same: they have no guard |

- At `full`, Claude's inherited sandbox permits Unix sockets so wrapped messaging commands can reach herdr: all Unix sockets on Linux, the `HERDR_SOCKET_PATH` socket on macOS (all sockets if that variable is absent). The macOS path form has not been live-tested. `guarded` adds no socket allowance; standalone messaging commands use the command exemption.
- The flags are in the skill's `harnesses.toml`. Native flags a member needs beyond them go in its `args` in the brief, so `continue` applies them again.
- A `guarded` member can still end up blocked. `ateam.py wait` tells you when one is.
- Trusting a folder is saved by the harness (Claude Code and Codex each keep their own list), so it is asked once per folder: the repo, and each worktree.

## 5. After launch

- Members do not reply to the brief. Check that each one is idle with `ateam.py status`, and read the pane of any that is not.
- Brief the project: what we are building, for whom, scope and constraints, where things live, and the first tasks with owners. Use `ateam.py say all --file <draft>` or a message. Write it for agents who have never seen this conversation. If the user runs `/spec`, point members to its project folder instead of repeating it.
- Then hand out work: see `leading.md`.
