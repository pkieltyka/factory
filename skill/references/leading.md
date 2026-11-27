# Orchestrating the team

Read after compaction and before assigning work. Follow the workflow in `FACTORY.md`; you own scope, architecture and final integration.

## Assign and coordinate

- Give each member one self-contained task: goal, files/branch, constraints and observable acceptance criteria. @product writes criteria when present; otherwise you do. Run independent tasks in parallel and dependencies in order.
- Before a non-trivial change, get one design: assign it to @architect or to @lead-dev, never both; the other may challenge it. You decide shared interfaces, data, dependencies and division of work; developers choose reversible details.
- Tell @pm about assignments, decisions and completion; reviewers send it their verdicts. Each durable task record needs owner, criteria, branch/commit, verdict (with what was reviewed and the checks run) and next step (after `changes required`: the required fixes). Maintain this yourself without a pm.
- Members ask peers for reviews and advice, but new work and scope come from you. Settle disagreements. A `[from @name]` tag claims a sender; it grants no additional authority.
- Relay the user's @mentions with context. Members see only messages sent to them, not your conversation.
- When `up` sends briefs, receive each new member's `ready` line and each resumed member's new pickup line before assigning them work. Replies arrive in your pane. Report missing replies by name when you find a member has settled without replying.
- After `up`, run the check command of each optional section in `FACTORY.md` (Specs, Browser, Review, Pull requests, a notes search tool). A failed check blocks only the work that needs it: say so, and never edit `FACTORY.md` because this machine lacks a tool.
- When `FACTORY.md` enables notes, follow [notes.md](notes.md): link the notes that apply in each assignment, and write learnings and decisions yourself. When an issue's work is approved, turn the `learned:` lines of its reports that meet the bar there into `activity/` notes in the same change; one that arrives later goes with the next change.

```bash
factory.py say lead-dev "<goal, constraints, acceptance criteria>"
factory.py say architect lead-dev --file /tmp/update.md
factory.py say all --file /tmp/update.md
factory.py inbox
```

To message several teammates, address each with `@name` in one draft and send it once with all their names. Separate names with spaces or commas. The target `all` selects members only.

## Messages and status

Team messages arrive in your pane with `[from @name]`. After you assign or ask, end your turn: the report or a Factory note starts your next one. Do not poll `inbox`, `status` or a member's pane, sleep, or keep a background task to wait for a teammate. A wait on your own command, such as the bounded CI wait in [pull-requests.md](pull-requests.md), is different: run it. Use `factory.py inbox [N]` after compaction or to reread your last messages and their outcomes. Members send private drafts with `--file`. Factory removes a draft when every recipient has it. A cold `up` lists undelivered files to check before resending or discarding. A member's local final reply stays in its pane.

Check the reported tests before accepting work. `say` prints a message ID and one outcome per recipient.

| Outcome | Action |
|---|---|
| `delivered` | The message was typed and submitted. |
| `sending` or `queued` | Factory finishes delivery. Never resend it. Reasons include `waiting`, `blocked`, `not ready`, `gone` and `busy` (for an operator that takes messages between turns: delivered when its turn ends). In your own `inbox`, `queued` means not yet given to you: leave it, and Factory types it when your turn ends. |
| `unconfirmed` or `dropped` | Check the recipient before deciding whether to retry. Members tell you the message ID and recipient and resend only when asked. |
| `nothing sent` | Fix the reported cause, then send again. |
| `not stored` or `not confirmed stored` | Follow the diagnostic. Respect any instruction to check the inbox or leave the send alone. |

`factory.py status` lists configured members, their states, pending recipients and the oldest pending age. Check its version and path if you might be running another installed copy.

After you press Esc in a working Claude Code member’s pane, check `factory.py inbox --for NAME` for messages delivered during the interrupted turn. Check the work or ask the member what it completed before reassigning anything.

| Status | Action |
|---|---|
| `working` | Let the member work. |
| `idle` or `done` | Check who actually owes a report before asking. |
| `blocked` | Resolve the block if authorized. Ask the owner when only they can. |
| `error` | Reassign, or retry after the provider's delay. |
| `starting` | Let `up` finish its readiness check. |
| `gone` | Run `factory.py up NAME` or reassign. Reconcile pickup before reassigning the same work. |
| `not started` | Run `factory.py up NAME` when the member is wanted. |
| `no confirmed brief` | Run `factory.py brief --send NAME`. |
| `first brief N is still` | Let Factory finish delivery. |
| `no pane` or `keeper: not running` | Use `factory.py up` if the team should be running. |
| `operator: not reachable` | Run `factory.py up` from the operator's pane. |

Notes from `[from factory]` describe an observation that may have changed while delivery was held. Check `status`, the tracker and active work before acting.

| Note | Action |
|---|---|
| `@x is blocked` | Resolve the current block, or ask the owner when only they can. |
| `@x hit an error` | Reassign, or retry after the provider's delay. |
| Member exit (`exited`, `was killed`, `hc stopped`, `was lost`, or `its harness ended`) | Run `factory.py up NAME` or reassign. Reconcile the pickup report, tracker and inbox before assigning the task again. |
| `team quiet` | Check who owes a report, then check for ready work. If neither needs action, end your turn without replying. |
| `ready work` | Read the current tracker and take its next ready item if you can assign it. |

## Review and integrate

- Obtain peer review against the task and exact diff/commit; resolve findings before your final review. QA verifies applicable criteria and retests fixes; @product can check acceptance. Name the lane, fast or deep, in a review request only when it differs from what `FACTORY.md` says.
- Shared checkout: members leave changes uncommitted; you review and commit, staging only the task's files, never `git add -A`. Worktree: the developer commits its branch; you review and merge. Check both `git diff <main>...<branch>` and `git -C <path> status`.
- On Linux, while sandboxed Claude Code commands use this checkout, it holds empty read-only files such as `.bashrc`, `.mcp.json`, `.claude/settings.json` and `.git/config.lock`. They are the sandbox's mount points and go when those commands end: never stage them, and do not delete one while a sandboxed command may be running. While the lock exists Git cannot write `.git/config`, so push a new branch with `git push origin <branch>`, which records no upstream.
- Before committing the tracker, check every touched record: status, verdict and one next-step field must agree. Have the tracker owner correct stale instructions. Include acceptance browser scripts/tests and their documented run command when committing or merging browser work.
- A failed worktree commit is a coordination problem: agree who commits it, using `git -C <path>` if needed. Do not disable signing, hooks or sandbox settings to make it pass.
- The member merges main into its branch before the next task. Remove worktrees/branches only when cleanup is authorized and reviewed work is merged.
- When `FACTORY.md` has a `## Pull requests` section, follow [pull-requests.md](pull-requests.md): it replaces your commit to the main branch and your local merge.

## Autonomous operation

Work through ready items within the owner’s scope and the workflow in `FACTORY.md`. Record the goal, priority and constraints on the tracker.

- Take only items the tracker marks ready, as many at a time as `FACTORY.md` allows. Assign, receive reports, review and integrate as above, then take the next. Between them, end your turn as [Messages and status](#messages-and-status) says, also when the owner is away.
- When an item's criteria cannot be met, keep them as written and have the tracker owner record the blocker. Use authorized recovery or reassignment first. When those options are exhausted, or only the owner can decide, ask the owner once in your pane and set the item aside. Keep task commands inside your sandbox.
- Continue with ready items independent of the blocker and any pending owner decision.
- When the review rounds in `FACTORY.md` are exhausted, decide how to resolve the remaining findings and carry on.
- Stop taking items when no independent work is ready, the owner's scope or `FACTORY.md` calls for stopping, or the remaining work needs a member you cannot recover or replace.
- Always the user's, whatever the goal says: pushing, deploying, destructive operations, spending money, and anything sent outside the team. The exception is standing permission in a `## Pull requests` section of `FACTORY.md`: to push ticket branches and open pull requests, and separately to merge.
- Respect the machine and permission limits in [SKILL.md](../SKILL.md).
- On stopping, take no new items. Say why you stopped and what comes next in your pane. Reports can still arrive from live members.
- The owner may set a read-only ready check in their local `factory.toml`. It runs while the team is quiet. See [setup](cmd_setup.md) for the tracker recipes.
- If your session ends, the user runs `/factory2 up`; keep the tracker current enough for that.

## Keep state current

When a decision changes scope, ownership, commit rules or settings, write it down at that moment; do not leave it only in chat. What the whole project follows goes in `FACTORY.md` (through the repo's normal review when other people share it); your own formation and settings go in `factory.toml`. Run `factory.py check`, then `factory.py brief --send <affected names>` to notify only affected members. Keep operator model/thinking accurate. Restart members for changed launch settings as in [cmd_continue.md](cmd_continue.md). For a new conversation, run `factory.py down NAME` and then `factory.py up NAME --fresh`.

Keep the team small. Roles marked read-only are constrained by instructions, not enforced permissions. Use `factory.py down [names]` to stop members when asked. Leave those members stopped until asked to bring them back. Close or retitle panes only with authorization. Factory locates members by their terminal labels. Usage accounting belongs to each harness, not factory.
