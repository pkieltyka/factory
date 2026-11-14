# Leading the team

How you, the lead, work with running members. Re-read this after compaction.

## Hand out work

```bash
devteam.py say lead-dev "Build the gh wrapper for issue list and view. Done = tests pass. Report back."
devteam.py say all --file /tmp/update.md
herdr agent prompt lead-dev "[from @cto] ..."     # the same thing by hand
```

- Every message starts with `[from @name]`; `say` adds yours. Members reply the same way.
- Members do not see your conversation with the user. Each task is self-contained: goal, files, constraints, and what "done" means (tests to run, what to report).
- One task per member at a time. Run independent work in parallel and dependent work in order.
- Tell @pm about every task you hand out and every task that finishes, so the board stays true.
- When the user writes `@reviewer look at X`, relay it to that member with the context it needs.
- Scope and architecture questions come to you. Settle them, or take them to the user. Members do not settle them between themselves.

## The loop

1. You set direction and hand out tasks; @pm records them on the board.
2. Developers implement and leave their work uncommitted.
3. @reviewer reviews the diff. Send it the developer's summary, not your own guess. Developers fix what it finds.
4. QA members test changed flows and endpoints and report bugs to @pm and you.
5. You do the final review and commit. Members never commit while `lead_commits = true`.

Follow the team brief's *Workflow* when it differs from this.

## Get results back

- Members report by messaging you: `[from @lead-dev] done: ...` arrives as your next turn. Do not wait for it in the foreground.
- To be woken up anyway, run in the background: `herdr agent wait lead-dev --timeout 1800000`. Some sandboxes stop a member from reaching herdr, so it cannot message you. Then read its pane.
- Read a pane: `herdr agent read <name> --source recent-unwrapped --lines 120`.
- Never `--wait` on a member that might be waiting on you.

## Keep the files true

- Changed the team? Edit `.devteam/team-brief.md`, run `devteam.py check`, then `devteam.py brief --send`. New members: `devteam.py up <name>`.
- The project changed? Update `.devteam/project-brief.md` with the user, then `devteam.py brief --send`.
- These files are what `/devteam continue` rebuilds from. A decision that matters later belongs in a brief or on the board, not only in chat.

## Rules

- Do not close panes, tabs or workspaces unless the user asks, and only ones devteam started (`devteam.py status` lists them).
- Keep the team as small as the work needs. Add a member when there is parallel work for it.
- Read-only roles are enforced by Codex `-s read-only`, Claude `--disallowedTools Edit Write NotebookEdit`, pi `--exclude-tools edit,write` and OpenCode `edit: deny`. omp relies on the prompt, and shell access can still write.
