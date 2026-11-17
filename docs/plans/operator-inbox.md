# Operator inbox (#22)

Members reach the operator with `herdr agent prompt`, which types into the operator's composer: the box the owner types in. A member's message arriving mid-sentence cuts the owner's text off or merges with it. This plan moves member-to-operator messages into an inbox file that `ateam.py wait` reads, for operators whose harness wakes on a finished background command.

Agreed by @architect and @lead-dev; decided by the owner and @operator (see "Decided" at the end).

**Amended by #24** (last section before "Decided"): every operator harness now uses the inbox, the per-harness key says how the operator waits, and message ids are printed as `message 12`. Where the sections below say that only Claude Code is on the inbox, name `operator_messages`, or print `#12`, the amendment replaces them.

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
- The route is written to the state file (below) before any member is launched or briefed. Everything that renders text for members (`member_prompt`, `render_team_brief`, the `continue` closing) and `inbox send` read it from there; a missing or damaged state file, or a damaged inbox, means `typed` (see Storage). `prompt` and `brief` previews outside herdr therefore show the typed text.
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
- **Damage degrades the session to typed, and nothing writes.** The inbox is damaged when it has a line that is not a report as in the table, does not end in a newline or has ids that do not increase, or when the state file is not as in the table. While that lasts no command changes either file, and every byte is kept. Each command says so: `inbox needs repair: <path>, line <n>: <what is wrong>. This session runs on typed messages. After repairing the file, run ateam.py continue --restart.`
  - Everything that reads the route (`member_prompt`, the brief sentence, the `continue` closing, the line `up` and `continue` print) reads `typed`. Nothing is stored, so after a repair the stored route counts again; a-team cannot tell that members were started typed in between, which is why the repair line names `continue --restart`.
  - `up` and `continue` print the line, skip the state update and the per-team clear, and start the team.
  - `inbox send` gives "not stored" (exit 5), so a member with inbox instructions sends typed with the `Inbox unavailable:` line.
  - `wait` prints the line once and watches members as before, ignoring the inbox; the typed team still needs its watcher. `wait --seen` exits 2: it cannot acknowledge.
  - `status` prints the line with its table.
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
| Definitely not stored: state says `typed`; no state file for this session; a damaged inbox or state; a mode, symlink or write error **before the append began** | 5 | `not stored: <full reason>. Nothing was queued. Send it typed, once, exactly as printed:` then one line: `herdr agent prompt <operator> <shell-quoted text>`. The text is `[from @<name>] Inbox unavailable: <cause>.`, a newline, the message, a final newline. | Runs the printed command once, exactly as printed, and checks its result. |
| Anything else (an error after the append began, a failed `fsync`) | 1 | `not confirmed: <error>. The message may or may not be stored. Do not send it again by any route.` | Says so in its pane and stops. |

`<cause>` is one of three fixed words, so the first line of the typed message is short enough to be copied whole; the full reason, with its path, stays on the "not stored" line in the member's pane:

| Cause | When |
|---|---|
| `needs repair` | the inbox or state file is damaged |
| `no inbox route` | the state says `typed`, or there is no state file for this session |
| `cannot write` | any OS error before the append began: permissions, a read-only file system, a symlink or non-regular file at a path, a mode that cannot be set |

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
- A partly live team, a restart, a failed team query or a damaged inbox deletes nothing.
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
It queues the message without typing into {operator}'s pane. `queued #N` means stored, not read: finish with `Report queued for {operator}`, not "sent". `not stored` prints a herdr command: run it once, exactly as printed. Any other failure: do not send the message again by any route; say so in your pane and stop. In Reports below, "keep it pending" and the `agent_blocked` retry apply to herdr messages only.
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
- A typed message from a member is off the route. Either it could not store the message, and the text should then open `Inbox unavailable:` with a cause, or it holds old instructions. Handle the message as usual, then look at `ateam.py status` and the member's pane for the cause, and tell the user once per session. `needs repair`: see the next bullet. `no inbox route`: restart that member with `ateam.py continue <name> --restart`. `cannot write`: inspect its sandbox and the cache path or permissions.
- `inbox needs repair` means this session runs on typed messages until the user repairs the file: tell them, keep your watcher running, and run `ateam.py continue --restart` after the repair.
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
12. State `typed`, no state file, a damaged inbox, an unwritable cache directory and a symlink at the inbox path each give exit 5, write nothing, and print a command whose text is exactly `[from @<name>] Inbox unavailable: <cause>.` (`no inbox route`, `needs repair` or `cannot write`), a newline and the original message, round-tripped through the shell; the "not stored" line keeps the full reason.
13. A failure injected after the append began gives exit 1 and no herdr command; a stored message (exit 0) prints no herdr command either. A short write is completed before "queued" is printed.
14. Unknown `--from` and an empty message are errors.

Storage
15. Directory `0700` and files `0600` on creation, and after starting from `0755`/`0644`.
16. Damage: with a torn last line, and with a state file that does not parse (also one that said `inbox` before the inbox was torn): `inbox send` gives exit 5; `wait --seen` exits 2; plain `wait` prints the repair line once and still returns on a member stopping; a cold `up` and `continue` print the line, start the team, render the typed text for members and clear nothing; `status` prints the line; both files' bytes are unchanged after each.

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
- A damaged inbox turns the session typed until a person repairs it and runs `continue --restart`: the collision can return for that time, visibly, and unread reports in the damaged file are read by the person, not printed by `wait`. A torn write needs a crash in the middle of an append.
- Messages may quote private notes. In the inbox they stay in a `0700` directory until acknowledged, then they are deleted. Copies elsewhere (transcripts, the operator's quotes) are not covered.

## Upstream ask (herdr)

A non-typing or queued message to an agent: delivered through each harness's own channel where one exists, else held until the composer is empty. Channels seen here, none of them ACP: Codex `codex queue --thread <id> --message <text>` and the `--remote` app server; OpenCode's background server with `opencode api`; Claude Code's background-command events. ACP could be a herdr backend one day; it is not an a-team runtime.

## Amendment (#24): the inbox for every operator harness

A live run with each harness as the operator (#23, #24; `notes/activity/20261117-191817-operator-inbox-per-harness.md`) showed that all five can receive through the inbox, in one of two ways:

| Operator | Wakes on a finished background command | How it waits |
|---|---|---|
| Claude Code 2.1.325 | yes | background |
| OpenCode 2.2.4 | yes, twice from idle | background |
| OMP 18.20.0 | its own async background; two cycles in the landing gate, on a non-default model | background |
| Codex 0.169.0 | no | foreground, rerun after each return |
| pi 1.5.2 | has no background commands | foreground, rerun after each return |

### Data

`operator_messages` would now be `inbox` in every table, so it goes. One required key per harness table replaces it:

```toml
#   operator_wait  how an inbox operator waits: background (native completion wakes it) or foreground
#                  (blocking wait, then rerun while members work). New harnesses start as foreground.

[claude]    operator_wait = "background"
[opencode]  operator_wait = "background"
[omp]       operator_wait = "background"
[codex]     operator_wait = "foreground"
[pi]        operator_wait = "foreground"
```

- **Route**: `inbox` whenever `[operator] harness` is set and equals the live kind, on any harness. `typed` stays only as the fallback it already is: harness not set, config and live kind differ, a damaged inbox or state, inbox unavailable. Nothing new is stored; the state file is unchanged.
- **Default timeout of `wait`**: `--timeout` defaults to 60 s when the session's route is `inbox` and the operator's harness is a foreground one, and to 1800 s otherwise (a background harness, the typed fallback, no harness set), which is today's value. An explicit `--timeout` always wins. So every command a-team prints, the `--seen` hint included, is plain `ateam.py wait`.
- **A foreground operator never waits on an idle team.** `wait` already returns when nobody is working; that return is where the foreground loop stops and the operator ends its turn, so the user is heard at once whenever the team is idle. On a foreground harness the line `wait` prints then says so: `nobody is working: <states>. End your turn; run ateam.py wait again after you assign work.` On a background harness the line is unchanged.
- A harness table without the key counts as `foreground` (the shape test requires the key; this is the safe reading if one is ever missing). With no `[operator] harness` set, and on the typed fallback, nothing changes: no foreground loop, 1800 s.
- The member side and the transport do not change.

### What is printed

`up` and `continue`, in place of the single inbox line:

```text
members reach @<operator> through the inbox (ateam.py inbox send); run ateam.py wait in the background, it wakes you with their messages
members reach @<operator> through the inbox (ateam.py inbox send); run ateam.py wait in the foreground, and again each time it returns while a member works
```

`status`: `operator messages: inbox, background wait` or `operator messages: inbox, foreground wait` (then the unread count as before); on the typed fallback `operator messages: typed` as before.

**Message ids** must not look like issue numbers. They stay plain integers in the file and for `--seen`; only the printed form changes:

```text
[Message 12 from @lead-dev at 17:31:06Z]:
    <text>
[Message 13 from @reviewer at 17:32:40Z]:
    <text>
2 unread messages. After you have acted on or recorded these messages: ateam.py wait --seen 13
```

`inbox send` prints `queued message 12`, and the member block says "`queued message N` means stored, not read". The counts say "message" for one and "messages" otherwise, in all three places. The other two: `status` ends `; <n> unread messages (ateam.py wait prints them)`, and the per-team clear prints `<n> unread messages from the earlier team; ateam.py wait prints them`.

### Docs

`references/leading.md`:

- The Inbox sentence loses "(Claude Code)".
- The code block comment for `ateam.py wait` becomes `# in the background, or the foreground when status says so`.
- "After assigning work, run one background `wait` and yield until it or a message arrives." becomes "After assigning work, run one `wait` the way `status` names. With a background wait, yield until it or a message arrives."
- "Timeout is in seconds (default 1800, exit 4)" becomes "(default 1800, or 60 with a foreground wait; exit 4)".
- "Never wait synchronously on a member that might need you." becomes "With a background wait, never wait synchronously on a member that might need you."
- Two bullets are added under "With the inbox", after "Keep a watcher running":

```markdown
- With a foreground wait your harness does not wake on a background command: run `ateam.py wait` in the foreground and run it again each time it returns while a member is working, after a timeout too and after you answer the user. When it says nobody is working, end your turn and do not wait: the user is then heard at once. While you wait, a message the user steers with Enter reaches you by the next wait return, within a minute; queued follow-ups may wait until your turn ends. They can also interrupt you.
- Give the `wait` command a time limit above its timeout, as far as your harness allows, so one call blocks for the whole wait and you do not poll it. If the harness allows less, pass a smaller `--timeout` that fits. A watcher the harness kills stops watching.
```

`SKILL.md` line 17: "on the inbox route (a Claude Code operator) members reach you" becomes "on the inbox route members reach you". `README.md`: "(with `operator_messages`)" becomes "(with `operator_wait`)", and in the first paragraph the clause from "; with a Claude Code operator, their messages" to "the pane you type in." ends the sentence after "message each other directly" and becomes, with the typed fallback stated:

```markdown
When the configured operator harness matches its live pane, members' messages to the operator go to an inbox that `ateam.py wait` reads, so nothing is typed into the pane you type in. An unset or mismatched harness, or a damaged or unavailable inbox, uses typed messages instead.
```

### Tests

1. Every harness table has `operator_wait` in `("background", "foreground")` and no `operator_messages`.
2. A Codex and a pi operator (config and live kind equal) get the inbox text for members, the foreground line from `up`, `inbox, foreground wait` from `status`, and a 60 s default timeout. With no member working, their `wait` returns on the second look with the "End your turn" line; a background operator's line is unchanged.
3. A Claude, an OpenCode and an OMP operator get the background line, `inbox, background wait` and 1800 s.
4. An explicit `--timeout` wins in both modes; with no `[operator] harness`, and on the typed fallback of a foreground harness, the default is 1800 s.
5. The typed text for members is byte-identical to before on every fallback (harness not set, mismatch, damage, inbox unavailable).
6. `wait` prints `[Message <id> from @<name> at <time>]:` and "<n> unread messages. After you have acted on or recorded these messages:"; `status` and the per-team clear say "unread messages"; `inbox send` prints `queued message <id>`; the member prompt says `queued message N`; `--seen` still takes the bare integer.

### Landing gates, on throwaway panes

1. Codex and pi each run the foreground loop on the 60 s default: a full timeout (exit 4) and a rerun without being told, several times in a row; what one timeout-and-rerun costs in tokens, and whether the operator reruns quietly; a plain Enter message from the user during a wait (steering): when it is delivered, whether the operator answers it and then runs `wait` again; and, with no member working, that the operator ends its turn on the "nobody is working" return instead of waiting.
2. OMP shows a second cycle, and whether its own command limit allows the 1800 s default (its probe ran a 90 s wait under a 120 s limit). If not, OMP ships with a smaller default, or as foreground once that has been tested on it.

### Risks

- On Codex and pi the owner is heard at once only while the team is idle. While a member works, a steering message (plain Enter) should arrive when the current `wait` returns, within 60 s; that is what gate 1 has to confirm. An end-of-turn queued message (Tab on Codex, Alt+Enter on pi; tested, text arrived verbatim) waits until the operator's turn ends, which can be many waits. The harness's own interrupt is immediate.
- The 60 s default costs one operator round-trip a minute while a member works: 60 an hour. Gate 1 measures it; if it is too dear, the default is the one number to change.
- A foreground operator that ends its turn has no watcher: the same accepted gap as before.
- OMP is proven on two cycles, on a non-default model (its default provider has no key on this machine) and with an 1810 s command limit accepted; a watcher that actually runs the full 1800 s was not tested.

## Opt-out (#25): typed messages by choice

A person who prefers the old route sets it in their own `team.toml`:

```toml
[operator]
messages = "typed"   # "inbox" or "typed"; absent means inbox
```

- `Config` rejects any other value, next to the harness check: `[operator] messages <x> must be inbox or typed`.
- `configure_messages` applies it before the live-kind match: with `typed` the route is typed on any harness. The effective route is stored as today, so every later reader follows it; nothing else reads the key.
- `up` and `continue` print `members reach @<operator> by typed herdr messages: [operator] messages = "typed" in team.toml`. `status` prints `operator messages: typed` with the usual unread count. Changing the key while a member runs gives the existing `the route to @<operator> changed ...: run ateam.py continue --restart` line.
- Unchanged by it: unread inbox messages and their acknowledgement (`wait` still prints them), the cold clear, the damage and unavailable fallbacks, the typed text for members, no inbox grant in Claude settings or Codex arguments, and `wait`'s 1800 s default with no foreground hint.
- Docs: one commented line under `[operator]` in `templates/team.toml` (`# messages = "typed"   # members type into your pane with herdr instead of using the inbox`), `messages` among the operator fields named in `README.md`, and the two sentences of its first paragraph (from #24) gain "By default" and the explicit choice, so the opening does not contradict the new field:

```markdown
By default, when the configured operator harness matches its live pane, members' messages to the operator go to an inbox that `ateam.py wait` reads, so nothing is typed into the pane you type in. An explicit typed choice, an unset or mismatched harness, or a damaged or unavailable inbox, uses typed messages instead.
```

Tests: an invalid value fails `check`; no key and `messages = "inbox"` behave alike; a Claude operator with live kind `claude` and `messages = "typed"` gets the typed line with that reason, typed member text, state `typed`, no inbox grants and 1800 s; changing the key with a member running prints the restart line; unread messages survive the switch and are still printed by `wait`; a Codex operator with `messages = "typed"` gets no foreground default or hint.

## Decided

By the owner, through @operator (2026-11-17):

- A route per operator harness in `harnesses.toml`; Claude Code inbox, the rest typed until proved.
- Names: members run `ateam.py inbox send`; the operator keeps `ateam.py wait` and `ateam.py wait --seen <id>`.
- Delete on read: `wait --seen <id>` removes every report up to `id`, so the file holds only unread ones. No age sweep, no one-hour rule. Ids never repeat.
- A damaged inbox or state file is reported with its path, line and fault; nothing is written and every byte is kept. The team still runs: the session falls back to typed, `wait` keeps watching members, and only `wait --seen` is refused. No finer handling.
- Typed recovery only when a message was definitely not stored, sent by the member; "not confirmed" means no resend by any route.
- The operator quotes each message it handles; a live feed pane is later work with #2.
- Storage under the cache directory, `0700`/`0600`, no server.
- The inbox is cleared per team; the gap when no watcher runs is accepted for v1.

After the five-operator QA run (#23, 2026-11-17), where a member rewrote the printed recovery command and dropped its first line:

- The typed recovery message opens with one of three fixed causes, not the full reason, and the member is told to run the command exactly as printed.
- The operator treats any typed message from a member on the inbox route as off the route and looks at `status` and the member's pane for the cause, whether or not the message says so.

After the inbox runs on every operator harness (#24, 2026-11-17), by the owner:

- The inbox is the route on all five harnesses. `operator_wait` replaces `operator_messages`: background for Claude Code, OpenCode and OMP, foreground for Codex and pi.
- `wait` defaults to 60 s on a foreground harness while the inbox route is in effect (the owner found five minutes too long), and 1800 s otherwise.
- A foreground operator does not wait while no member is working: it ends its turn on the "nobody is working" return.
- Message ids are printed as `message 12`, never `#12`; they stay integers in the file and for `--seen`.
- `[operator] messages = "typed"` in `team.toml` opts a person out of the inbox (#25); absent or `"inbox"` keeps it.
