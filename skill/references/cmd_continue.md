# Bring the team back

Use after agents exit, `hc` restarts or you lose context. Recover the project from `FACTORY.md`, the tracker and Git. Saved conversations help members pick up their work.

```bash
factory.py status
factory.py up
factory.py up lead-dev
factory.py inbox
```

`up` tries each member's compatible saved conversation and says when it starts a fresh one instead. If a resumed harness exits before becoming ready, Factory retries it once with a new conversation. Use `factory.py up NAME --fresh` when you want a new conversation for a stopped member. Keep a stopped member out by naming only those you want to start.

After an `hc` restart, `status` can report interrupted members as `gone`. Run `factory.py up NAME` for the members you want back. Members intentionally stopped with `down` stay stopped until selected by `up`.

- Exit 2 reports a refusal or failed preflight. Exit 3 names the members it could not start, ready or brief. Follow the printed recovery command.
- A pending first brief delivers when it can. For a live member with no confirmed or pending first brief, run `factory.py brief --send NAME`.
- Messages survive `down` and a core restart. `factory.py inbox [N]` rereads yours. `factory.py status` shows pending recipients.
- A cold `up` lists message files that were never delivered to everyone. Check their outcomes before resending or discarding them.

## Reconcile before assigning

1. Read `FACTORY.md`, `factory.toml` and project plans, then the tracker.
2. Check Git, including each worktree's status and diff, and the current PRs and CI where applicable. Match each uncommitted change to an active item. Leave unexplained changes alone and ask the user.
3. Summarize each open item, its evidence and next action to the user before reading member replies. Git and CI outrank the tracker when they disagree. Have the tracker owner correct a stale record.
4. Read your recent messages with `factory.py inbox` and check pickup replies. Ask the members concerned for a status if needed. Assign or repeat a task only after verifying it still needs assignment.
5. A verdict covers the commit or paths it names. Request a new review when the head moved, checks or verdict are missing, or approved changes were never committed.

Give a member that started fresh the relevant criteria, branch or files, evidence and remaining work when it needs the task again. Then follow [leading.md](leading.md).

## Change a running member's settings

Coordinate active work first. After an authorized change to `factory.toml`, run `factory.py down NAME`, then `factory.py up NAME`. Use `--fresh` on `up` for a new conversation.
