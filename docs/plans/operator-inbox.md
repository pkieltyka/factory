# Operator inbox (#22)

Members reach the operator with `herdr agent prompt`, which types into the operator's composer: the box the owner types in. A member's message arriving mid-sentence cuts the owner's text off or merges with it. This plan moves member-to-operator messages into an inbox file that `ateam.py wait` reads, for operators whose harness wakes on a finished background command.

Agreed by @architect and @lead-dev; decided by the owner and @operator (see "Decided" at the end).

## Decision

- **Inbox for every member-to-operator message** (reports, questions, heads-ups), when the operator's harness supports it. Member-to-member and operator-to-member messages stay on `herdr agent prompt`.
- **A fixed route per operator harness**, `inbox` or `typed`, chosen at `up` and `continue` and stored. Claude Code is `inbox`; Codex, pi, OMP and OpenCode are `typed` (today's behaviour, unchanged) until a probe proves they wake on a background command.
- **Members run one generated command**, `ateam.py inbox send`. It stores the message and never calls herdr.
- **The operator needs no new command.** `ateam.py wait` returns on unread reports and prints them; `ateam.py wait --seen <id>` acknowledges them after the operator has acted on or recorded them.
- **Delete on read.** Acknowledging removes the reports, so the inbox holds only unread ones. Nothing unread is ever deleted: not by age, not when a team ends.
- **Degraded typed delivery only when a message was definitely not stored**, sent by the member, announced in its first line. This is owner-authorized recovery; it brings the collision back for that one message and is not part of the fix.

## Rejected

| Option | Why not |
|---|---|
| Deliver only when the operator's composer is empty | herdr 0.9.5 has no composer state, no queue and no non-typing message (checked: help, skill, API schema). It would mean reading five TUIs off the screen. |
| ACP as the transport | Native in OMP and OpenCode only; adapters for the others are not installed here and wrap an SDK or server, not the TUI. There is no verified drop-in path to an agent already running in a pane (a shared backend per harness is not ruled out), so the portable path runs the operator headless under an ACP client, where native TUI interaction and feature parity are not preserved automatically and vary by adapter. a-team would become a daemon and a UI. ACP may suit herdr as a backend. |
| A typed fallback chosen by watcher liveness | A lock proves a process is alive, not that its output was read; it races, and a typed message can stall (#18) or merge with the owner's draft. |
| `inbox send` itself sending the typed fallback | It may run inside the sandbox that denied the write, where herdr's socket can be denied too. Keeping "inbox send never calls herdr" makes the rule testable without conditions, and the member's `herdr agent prompt` is the one command every permission setup already exempts. |
| `ateam.py inbox wait`, or a separate acknowledge command | The operator runs exactly one watcher, and it must wake on reports and on members stopping: that is `ateam.py wait`. Acknowledgement rides the command the operator runs next anyway. |
| `ateam.py report` or `inbox prompt` as the member command | Not every message is a report; `ateam.py prompt <name>` already prints standing instructions, and "prompt" suggests typing into a pane. |
| Keeping acknowledged reports for an hour, or removing reports by age | Kept reports need a sweep; removing unread ones by age is the #22 loss again. The owner chose delete on acknowledgement. |
| A notification when no watcher runs | Wakes only the owner, adds a lock and herdr calls to `inbox send`. Possible later opt-in. |
| Fewer messages; a separate owner pane | The first lowers the rate, not the failure. The second needs a relay agent or the owner typing shell commands. |

## Route: `inbox` or `typed`

`harnesses.toml`, one new key in every harness table, documented in the header comment:

```toml
#   operator_messages  how an operator on this harness receives members' messages: `inbox` (members run
#                `ateam.py inbox send`; a finished background `wait` wakes the operator, nothing is typed)
#                or `typed` (`herdr agent prompt` into its pane). It says nothing about a member on this harness.

[claude]
operator_messages = "inbox"
# codex, pi, omp, opencode:
operator_messages = "typed"
```

Selection, at `up` and `continue` only, after `ensure_operator` and `Team(cfg)`:

- `conf = cfg.operator.get("harness")`; `live = (team.pane(operator) or {}).get("agent")` (the agent kind herdr reports for the operator's pane; no extra herdr call).
- Route is `TABLE[conf]["operator_messages"]` only when `conf` is set and equals `live`. Otherwise `typed`. The route depends on the operator's harness alone; there is no per-member override.
- The command prints the route and, when it fell back to typed, why:
  - `members reach @<operator> through the inbox (ateam.py inbox send); wait prints their messages`
  - `members reach @<operator> by typed herdr messages: [operator] harness is not set in team.toml`
  - `members reach @<operator> by typed herdr messages: team.toml says <conf>, herdr sees <live> in this pane`
- A declared `[operator] harness` that is not a table in `harnesses.toml` is rejected by `Config` (so by `check` and every command): `[operator] harness <x> is not one of <names>`.
- The route is written to the state file (below) before any member is launched or briefed. Everything that renders text for members (`member_prompt`, `render_team_brief`, the `continue` closing) and `inbox send` read it from there; a missing state file means `typed`. `prompt` and `brief` previews outside herdr therefore show the typed text.
- When the route differs from the stored one and a running member is not being restarted, print: `the route to @<operator> changed to <route>: run ateam.py continue --restart so running members get it`.

## Storage and security

All under the per-repo cache directory (`cfg.cache`, `~/.cache/a-team/<repo>-<hash>/`, honouring `XDG_CACHE_HOME`); never `/tmp`. `<s>` is this herdr session: `HERDR_SESSION` reduced to `[A-Za-z0-9_.-]`, cut to 24 characters, plus `-` and the first 8 hex digits of the SHA-1 of the whole value, so two session names never share files (`default` when unset).

| File | Content |
|---|---|
| `inbox-<s>.jsonl` | The unread reports, one per line: `{"id": 12, "at": "2026-11-17T17:31:06Z", "from": "lead-dev", "text": "..."}` |
| `inbox-<s>.json` | State: `{"mode": "inbox", "seen": 11}`. `seen` is the highest acknowledged id. |
| `inbox-<s>.lock` | Lock file for every read-modify-write below (`fcntl.flock`, held for milliseconds, never while waiting). It is never unlinked or replaced: processes must lock the same inode. |

- **Ids** never repeat: a new report gets `max(seen, every id in the file) + 1` under the lock. Unread means `id > seen`.
- **Modes**: the cache directory `0700`, these files and their temp files `0600`, set at creation; a looser mode found is tightened. A symlink or non-regular file at one of these paths is refused. If a mode cannot be set, nothing is written.
- **Append**, under the lock: write the whole line, checking the length written and continuing after a short write; then `fsync` the file, and the directory too when the file was just created. Only then is the message "queued".
- **Acknowledge** (`wait --seen <id>`), under the lock: first write the state with `seen = id`, then rewrite the inbox without the lines up to `id`. Both rewrites are a temp file in the same directory, `fsync`, `os.replace`, `fsync` of the directory. A crash between the two leaves acknowledged lines in the file. Readers ignore them (`id <= seen`), and the next command that takes the lock to write (`inbox send`, `wait --seen`, the per-team clear) removes them before it does anything else, so "nothing unread" and "empty file" mean the same again. Plain `wait` only reads and never deletes. No crash can drop an unread report or move `seen` back.
- **Damage is refused.** If the inbox has a line that is not a report as in the table, does not end in a newline or has ids that do not increase, or the state file is not as in the table, every command that needs the inbox stops, writes nothing and keeps every byte: `inbox needs repair: <path>, line <n>: <what is wrong>`. `inbox send` gives "not stored" (exit 5); `wait`, `up` and `continue` exit 2 before doing anything else; `status` prints the line with its table. A person repairs the file.
- No server, no daemon, no timer, no sweep.

## Member side: `ateam.py inbox send`

```bash
python3 <skill>/scripts/ateam.py inbox send --cwd <main root> --from <name> '<message>'
python3 <skill>/scripts/ateam.py inbox send --cwd <main root> --from <name> --file <path>
```

`inbox` is a parent command with one subcommand, `send` (`inbox follow`, a live feed, is later work with #2). `--cwd` is the main checkout (`cfg.root`), written into the generated command because `find_root` does not resolve a worktree to it. `--from` must be a member of `team.toml`. An empty message is an error. `inbox send` makes no herdr call and does not need the operator to be alive.

| Outcome | Exit | Output | Member does |
|---|---|---|---|
| Stored | 0 | `queued #<id>` | Finishes as usual. Queued means stored, not read. |
| Definitely not stored: state says `typed`; no state file for this session; a damaged inbox or state; a mode, symlink or write error **before the append began** | 5 | `not stored: <reason>. Nothing was queued. Send it typed, once:` then one line: `herdr agent prompt <operator> <shell-quoted text>`. The text is `[from @<name>] Inbox unavailable: <reason>.`, a newline, the message, a final newline. | Runs the printed command once, as printed, and checks its result. |
| Anything else (an error after the append began, a failed `fsync`) | 1 | `not confirmed: <error>. The message may or may not be stored. Do not send it again by any route.` | Says so in its pane and stops. |

A sandbox that refuses to run the command at all gives no such output: the member treats that as "not confirmed". The operator's `wait` already returns when a member stops and points at its pane.

## Operator side: `ateam.py wait` and `status`

- `wait --seen <id>`: first, under the lock, `id` larger than the highest id that ever existed is an error (exit 2, nothing changes); `id <= seen` is a no-op; otherwise it acknowledges, and so deletes, every report up to `id`. Then the wait.
- Before anything else in each look, including the `not started` exit and with names given, `wait` reads the inbox. If any report is unread it prints them all and returns 0:

```text
#12 from @lead-dev at 17:31:06Z
    <text, each line indented four spaces>
#13 from @reviewer at 17:32:40Z
    <text>
2 unread. After you have acted on or recorded them: ateam.py wait --seen 13
```

- Plain `wait` never acknowledges. A cancelled watcher, lost output or a compaction before acting costs nothing: the next `wait` prints the same reports.
- Lifecycle returns, timeout (exit 4) and `--interval` are unchanged.
- `status` adds one line under the table: `operator messages: <route>` and, when any, `; <n> unread (ateam.py wait prints them)`.
- `wait` and `status` read the inbox in both routes, so a message stored before a route change is still seen.

## Retention

- The inbox holds only unread reports; acknowledged ones are gone. Nothing is removed by age.
- **Per-team clear**, at `up` and `continue`, before anything is started or closed, when the team query succeeded and **no member of `team.toml`** (all of them, not only the named ones) is live (`team.live`) in this herdr session:
  - no unread report: under the lock, delete this session's `.jsonl` and state file (the lock file stays). The state file is then written again with the new route and `seen = 0`; ids start again at 1.
  - any unread report: keep both, update only `mode`, and print `<n> unread from the earlier team; ateam.py wait prints them`.
- A partly live team, a restart or a failed team query deletes nothing.
- Two herdr sessions on one repo each touch only their own files.
- A cold `continue` with unread reports keeps them: they are the last evidence, after the tracker and Git.
- Known limit: the inbox of a herdr session that never comes back stays on disk, private by its modes.
- Scope: a-team clears only its own inbox files. Copies of a report in a harness transcript, in a background command's output or in the operator's quote are outside it.

## Permissions

- **Claude members, inbox route only**: `claude_settings` adds `python3 <abs ateam.py> inbox send *` to `sandbox.excludedCommands` and `Bash(python3 <abs ateam.py> inbox send *)` to `permissions.allow`, so the generated command runs as a standalone command like the herdr ones. `argv_claude` needs the command prefix; pass it in the member plan.
- **Guarded Codex members**: a scoped writable grant for the cache directory only, added to the guarded launch arguments. The exact flag is unverified and is landing gate 2. A denied grant is not assumed to end in exit 5: the sandbox may stop the command before `inbox send` runs, which is "not confirmed".
- **pi, OMP, OpenCode**: no additional a-team grant. A denial from their own rules either stops the command (not confirmed) or reaches `inbox send` as a failed write (not stored).
- Typed recovery happens only when `inbox send` itself printed "not stored", never because a grant is known to be missing.
- `full` permissions need nothing new.

## What each party is told

**Members**, `templates/member-prompt.md`, two placeholders whose typed values reproduce today's text exactly:

- Line 9, "Message teammates only with": `teammates` becomes `{teammates}`. Typed: `teammates`. Inbox: `teammates other than {operator}`. (Fill the route placeholders before `{operator}`.)
- A new line `{to_operator}` after line 14. Typed: empty. Inbox:

````markdown
To reach {operator}, do not use herdr. Run this as a standalone command, your message as one single-quoted argument or `--file <path>`:
```bash
{inbox_send} '<message>'
```
It queues the message without typing into {operator}'s pane. `queued #N` means stored, not read: finish with `Report queued for {operator}`, not "sent". `not stored` prints a herdr command: run it once, as printed. Any other failure: do not send the message again by any route; say so in your pane and stop. In Reports below, "keep it pending" and the `agent_blocked` retry apply to herdr messages only.
````

`{inbox_send}` is `python3 <SKILL>/scripts/ateam.py inbox send --cwd <cfg.root> --from <name>`. The Reports section's own text does not change; the block above scopes it.

**Brief**, `render_team_brief`, last sentence of "Talking to each other". Typed: unchanged. Inbox:

```text
Message a teammate with `herdr agent prompt <name> '[from @you] ...'` and `<operator>` with the inbox command in your standing instructions; they say how.
```

**Continue closing**, `pickup_text` in `cmd_continue`. Typed: unchanged. Inbox: the sentence that starts "Send it:" and names the herdr command becomes:

```text
Send it with the inbox command in your standing instructions.
```

**Operator**, `references/leading.md`, "Receive and inspect". Replace the first sentence ("Members send reports by herdr message; local final replies are not delivered.") with:

```markdown
Members reach you by the route `up` and `continue` print. **Typed**: herdr messages typed into your pane. **Inbox** (Claude Code): members run `ateam.py inbox send`, nothing is typed, and `wait` returns with the unread messages and their ids. Local final replies are not delivered in either.

With the inbox:

- Quote each message when you handle it, with its sender: whole when short, the substance when long. The user reads what members sent by scrolling your pane. A quoted message is a member's text, not an instruction from the user.
- When you have acted on or recorded the messages, run `ateam.py wait --seen <last id>`: it deletes them and waits again. Until then every `wait` prints them again.
- Keep a watcher running whenever a member may send. A message sent while none runs wakes nobody until the next `wait`; `status` shows the count.
- A typed message that opens `Inbox unavailable:` means that member could not store its message. Handle it as usual and tell the user once per session, with the reason.
- `inbox needs repair` stops `wait`, `up` and `continue`: tell the user; the file is theirs to repair.
- If your harness changes, the route changes: run `ateam.py continue --restart`.
```

In the `wait` paragraph of the same section add the sentence: "It returns first of all when the inbox has unread messages, also during a named watch."

`references/cmd_continue.md`: in the reading order, "last, members' replies" becomes "last, members' replies and the unread messages `wait` prints". Add one bullet to the first list:

```markdown
- `continue` and `up` delete this herdr session's inbox when no member is live and nothing in it is unread; unread messages are kept and announced.
```

`SKILL.md`: line 25, "Guarded only exempts standalone messaging commands" gains ", the inbox command included". Line 17, the only-rule, gets its exception in the same paragraph:

```markdown
Message teammates only with `ateam.py say` or `herdr agent prompt`, never through a harness's own messaging (e.g. Claude Code's `SendMessage`): herdr works across harnesses and permission modes. One exception: on the inbox route (a Claude Code operator) members reach you with `ateam.py inbox send`, which `wait` prints; nothing is typed into your pane. See `references/leading.md`.
```

`README.md`: in the last line, "add its table" becomes "add its table (with `operator_messages`)". In the first paragraph, after "message each other directly", add:

```markdown
; with a Claude Code operator, their messages to the operator go to an inbox that `ateam.py wait` reads, so nothing is typed into the pane you type in
```

## Files

| File | Change |
|---|---|
| `harnesses.toml` | `operator_messages` in all five tables and the header comment |
| `scripts/ateam.py` | `Config`: reject an unknown `[operator] harness`. New: inbox helpers (paths, lock, modes, read with the damage check, append, acknowledge), `cmd_inbox_send` and the `inbox` parser with its `send` subcommand (`--from`, `--file`, message, `--cwd`). `cmd_wait`: `--seen`, inbox check. `cmd_status`: the route line. `cmd_up`, `cmd_continue`: route selection, per-team clear, notices, state written before launch. `member_prompt`: the `teammates`, `to_operator` and `inbox_send` placeholders. `render_team_brief` and `pickup_text`: the route's sentence. `claude_settings`, `argv_claude`: the inbox command. Guarded Codex launch arguments: the grant |
| `templates/member-prompt.md` | `{teammates}` on line 9, the `{to_operator}` line |
| `references/leading.md`, `references/cmd_continue.md`, `SKILL.md`, `README.md` | the text above |
| `scripts/test_ateam.py` | below; the fake herdr must report an agent kind for the operator's pane |

## Tests (fake herdr)

Route and text
1. Every harness table has `operator_messages` in `("inbox", "typed")` (extend the table-shape test in `ConfigTests`).
2. Claude operator, live kind `claude`: `up` prints the inbox line; `prompt`, the brief and the `continue` closing carry the inbox command for members on every harness; the member prompt says "teammates other than" the operator and `Report queued for`; the state file says `inbox`.
3. Codex operator: the same three texts are byte-identical to today's.
4. `[operator] harness` absent, and config `claude` with live kind `codex`: typed, with the matching notice.
5. Unknown `[operator] harness`: `check` fails with the message.
6. Route change with a running member not restarted prints the `continue --restart` line.
7. Claude members' settings contain the inbox command in `excludedCommands` and `allow` in the inbox route only.

Inbox send
8. From a worktree with `--cwd <main>` the line lands in the main checkout's cache; `queued #1`, then `#2`.
9. Multiline and Unicode text, and `--file`, come back exactly from `wait`.
10. Two sends at once (two processes) leave two whole lines with different ids.
11. No herdr call is made by `inbox send` in any outcome (assert on the fake's call log).
12. State `typed`, no state file, an unwritable cache directory and a symlink at the inbox path each give exit 5, write nothing, and print a command whose text opens `[from @<name>] Inbox unavailable:` and round-trips through the shell to the original message.
13. A failure injected after the append began gives exit 1 and no herdr command; a stored message (exit 0) prints no herdr command either. A short write is completed before "queued" is printed.
14. Unknown `--from` and an empty message are errors.

Storage
15. Directory `0700` and files `0600` on creation, and after starting from `0755`/`0644`.
16. Damage: with a torn last line, and with a state file that does not parse, `inbox send` gives exit 5, `wait`, `wait --seen` and a cold `continue` exit 2 with the repair line, `status` prints it, and both files' bytes are unchanged after each.

Wait and status
17. A message pending before `wait` starts, one arriving during a named watch, and one pending when no member is started each make `wait` print it and return 0, ahead of any lifecycle return.
18. Plain `wait` twice prints the same messages. `--seen 2` removes lines 1 and 2 from the file, and later messages still print; `--seen` below `seen` is a no-op; above the highest id is exit 2 with both files unchanged.
19. After everything was acknowledged the file is empty and the next id is above `seen`.
20. `--seen` writes the state before it rewrites the inbox; interrupted between the two, the leftover lines are not printed again, no unread message is lost, plain `wait` leaves the file as it is, and the next `inbox send` removes the leftovers before it appends.
21. A send and an acknowledgement at once (two processes) lose no unread message and leave the lock file's inode unchanged.
22. `status` shows the route and the unread count.
23. Existing `WaitTests` pass unchanged.

Retention
24. Cold `up` and cold `continue` with nothing unread delete inbox and state, then write a fresh state; the lock file stays.
25. The same with one unread message keep both, print the count, and the next `wait` prints it.
26. A partly live team, `continue --restart` and a failing team query delete nothing.
27. A second `HERDR_SESSION` has its own files and is untouched by the first one's clear.

## Build order

One issue, one review; each step leaves the suite green.

1. Storage helpers, `inbox send`, `wait --seen` and the inbox check, `status`: tests 8-23.
2. Route: `harnesses.toml`, `Config` check, selection and state at `up`/`continue`, member prompt, brief, closing, `claude_settings`: tests 1-7.
3. Per-team clear: tests 24-27.
4. Docs: `leading.md`, `cmd_continue.md`, `SKILL.md`, `README.md`, checked by @reviewer against steps 1-3.
5. Landing gates, on throwaway panes.

## Landing gates

1. A Claude Code operator runs the real `ateam.py wait` in the background; a message wakes it; a half-typed draft in its composer is intact; the operator can read the messages from the collapsed result. (Proved so far with `sleep`, Claude Code 2.1.325.)
2. Guarded Codex: the scoped grant lets `inbox send` write the cache directory. If it cannot be made to work, this is a blocker reported to the operator for a decision; nothing routes guarded members to typed on its own.
3. herdr's agent kind for the operator's pane equals our harness names (seen for `claude`; check `codex`, `pi`, `omp`, `opencode`), or route selection can never say `inbox` for a harness moved there later.

## Not verified

- Whether Codex, OMP, OpenCode or pi wake on a finished background command. Until proved, they stay `typed`.
- The lock and modes on macOS.
- A Claude Code operator with several watchers finishing close together.
- How often the "no watcher" gap occurs in practice (mainly when the owner talks to a member directly).

## Risks

- The inbox makes one case less reliable than today: a message sent while the operator is idle with no watcher wakes nobody until the next `wait`. Accepted for v1; `status` shows the count.
- The route depends on `[operator] harness` being right. A wrong or missing value costs the benefit (typed), never a message.
- The typed recovery path can still collide with the owner's typing and can stall on a last word starting with `@` or `$`; `inbox send` ends the printed text with a newline to prevent the stall.
- A damaged inbox stops `wait`, `up` and `continue` until a person repairs it. Chosen over finer handling for simplicity; a torn write needs a crash in the middle of an append.
- Messages may quote private notes. In the inbox they stay in a `0700` directory until acknowledged, then they are deleted. Copies elsewhere (transcripts, the operator's quotes) are not covered.

## Upstream ask (herdr)

A non-typing or queued message to an agent: delivered through each harness's own channel where one exists, else held until the composer is empty. Channels seen here, none of them ACP: Codex `codex queue --thread <id> --message <text>` and the `--remote` app server; OpenCode's background server with `opencode api`; Claude Code's background-command events. ACP could be a herdr backend one day; it is not an a-team runtime.

## Decided

By the owner, through @operator (2026-11-17):

- A route per operator harness in `harnesses.toml`; Claude Code inbox, the rest typed until proved.
- Names: members run `ateam.py inbox send`; the operator keeps `ateam.py wait` and `ateam.py wait --seen <id>`.
- Delete on read: `wait --seen <id>` removes every report up to `id`, so the file holds only unread ones. No age sweep, no one-hour rule. Ids never repeat.
- A damaged inbox or state file is refused with its path, line and fault; nothing is written and every byte is kept. No finer handling.
- Typed recovery only when a message was definitely not stored, sent by the member; "not confirmed" means no resend by any route.
- The operator quotes each message it handles; a live feed pane is later work with #2.
- Storage under the cache directory, `0700`/`0600`, no server.
- The inbox is cleared per team; the gap when no watcher runs is accepted for v1.
