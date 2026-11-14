# `/devteam up`: launch and brief the team

## 1. Preconditions

- Inside herdr, with the sandbox allowing `herdr` and `python3` (see SKILL.md).
- `.devteam/team-brief.md` exists. If not → `/devteam setup`.
- `.devteam/project-brief.md` should exist. If not, offer to write it with the user first (see `cmd_setup.md` step 5). Members can start without it, but they will idle until it arrives.

## 2. Check, then launch

```bash
devteam.py up                    # the `default` team
devteam.py up --team full        # another roster from [teams]
devteam.py up reviewer pm        # some members
devteam.py up backend-dev-3 --role backend-dev --model gpt-6-luna   # an extra member not in the brief
devteam.py up --dry-run          # check and print the plan only
```

`up` checks every member first: harness installed, model available, thinking level allowed. **If any member fails, nothing launches.** In that case:

- Stop. Tell the user which members failed, the problem, and the printed fix. Example: "GLM 5.3 is not configured in pi: add an OpenRouter key (`pi auth`) or serve it locally and add it to pi's models."
- Ask whether to fix the setup or change the member in the brief. Never swap in another model, harness or thinking level yourself, and never launch the others without the user's go-ahead.

On success, `up`:
1. names your pane after `[lead] name` (`cto`);
2. puts the first member to your right and stacks the rest below it, keeping your focus;
3. starts each member with its system prompt: role rules, how to talk, the commit rule, and where the briefs live;
4. sends every running member the **team brief**, then the **project brief**.

If a member does not come up, `up` stops at that member. Read its pane (`herdr pane read <pane> --source recent-unwrapped --lines 60`) and tell the user what you see.

Claude Code members ask for tool permissions in their own pane. If the user wants them to run without stopping, pass flags after `--`: `devteam.py up frontend-dev -- --permission-mode acceptEdits`.

## 3. After launch

- Wait for each member's "ready" (they arrive as `[from @name]` messages). Read the pane of any that stay silent.
- Then start the work: see `leading.md`.
