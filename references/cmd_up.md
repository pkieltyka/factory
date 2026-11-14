# `/devteam up`: launch and brief the team

## 1. Preconditions

- You are inside herdr, and the sandbox allows `herdr` and `python3` (see SKILL.md).
- `team-brief.md` exists at the repo root. If not → `/devteam setup`.

## 2. Check, then launch

```bash
devteam.py up                    # the `default` team
devteam.py up --team full        # another roster from [teams]
devteam.py up reviewer pm        # some members
devteam.py up backend-dev-3 --role backend-dev --model gpt-6-luna   # a new member; added to the brief
devteam.py up --dry-run          # check and print the plan only
```

`up` checks every member first: harness installed, model available, thinking level allowed. **If any member fails, nothing launches.** Then:

- Stop. Tell the user which members failed, the problem, and the printed fix. For example: "GLM 5.3 is not configured in pi: add an OpenRouter key (`pi auth`) or serve it locally and add it to pi's models."
- Ask whether to fix the setup or change the member in the brief. Never swap in another model, harness or thinking level yourself, and never launch the others without the user's go-ahead.

On success, `up`:
1. names your agent and labels your pane after `[lead] name` (`cto`);
2. puts the first member to your right and stacks the rest below it, keeping your focus;
3. labels each member's pane with its @name, so herdr knows the team;
4. starts each member with its system prompt: role rules, how to talk, the tracker, the commit rule, and where the brief lives;
5. sends every running member the team brief.

If a member does not come up, `up` stops at that member. Read its pane (`herdr pane read <pane> --source recent-unwrapped --lines 60`) and tell the user what you see.

Claude Code members ask for tool permissions in their own pane. If the user wants them to run without stopping, pass flags after `--`: `devteam.py up frontend-dev -- --permission-mode acceptEdits`.

## 3. After launch

- Wait for each member's "ready" (it arrives as a `[from @name]` message). Read the pane of any member that stays silent.
- Brief the project: what we are building, for whom, scope and constraints, where things live, and the first tasks with owners. Use `devteam.py say all --file <draft>` or a message. Write it for agents who have never seen this conversation. If the user runs `/spec`, point members to its project folder instead of repeating it.
- Then hand out work: see `leading.md`.
