---
status: draft
---

# Architecture: Factory v2

Technical design for the [functional spec](functional_spec.md) (complete). Section numbers such as §8.5 refer to that spec. Component designs hold the detail:

| Component | File | Covers |
|---|---|---|
| hc client | [components/hc_client.md](components/hc_client.md) | Transport, calls used, error mapping, polling |
| Message log | [components/message_log.md](components/message_log.md) | Storage format, append protocol, outcomes, damage |
| Keeper | [components/keeper.md](components/keeper.md) | Delivery loop, notes, team-quiet rule, ready check, liveness, pane |
| Launch and layout | [components/launch_layout.md](components/launch_layout.md) | Harness adapters, readiness, labels, layout doc, sessions |
| CLI and skill text | [components/cli_skill.md](components/cli_skill.md) | Commands, exit codes, output, skill and template changes |
| Testing | [components/testing.md](components/testing.md) | Fake hc, fake agent, clock, live qualification, CI |

Facts marked **verified** were run by the architect on an isolated `hc 0.1.20` core on 26 November 2026 (results in §9 below). Items marked **LC-n** need a live check before the code that depends on them is merged; each names its fallback.

## 1. Decisions

1. **One file, one process model.** Everything is `skill/scripts/factory.py`, Python 3.11+ standard library. Commands are short-lived processes. The only long-lived process is `factory.py keeper`, one per team, running in its own `hc` terminal.
2. **Talk to `hc` through `hc connect`.** Factory spawns `hc connect` and speaks newline-delimited JSON-RPC on its stdin and stdout (verified). It never resolves a socket path itself, so it follows `HC_SESSION`, `HC_SOCKET` and `hc`'s config exactly as `hc` does, and the test double is one small script.
3. **Poll, do not subscribe.** The keeper reads the team with one label-filtered `terminal.list` per tick (0–1 ms, verified). No event stream, no missed-event handling.
4. **`hc`'s terminal records are the membership database.** Per-terminal facts are labels on the terminal (member name, ready, last state, what was noted). They live and die with the terminal record, survive a core restart (verified) and need no file or lock. A member is `not started` when it has no record, `gone` when its newest record is finished.
5. **One append-only log for messages and outcomes**, written under one file lock that is never held across an `hc` call. A record is committed when the appender releases the lock; a failed append is rolled back by truncating to the previous length. No commit markers are needed.
6. **Only the keeper types.** `say`, `up` and `brief --send` append to the log and wait for the outcome records the keeper writes. Briefs are messages of kind `brief`.
7. **A small `team.json`** holds the few team-level facts with no terminal to carry them (root, layout ids, quiet and ready-check bookkeeping). Losing it costs a repeated note or a second team tab, never a message.
8. **Sessions are assigned, not discovered, wherever the harness allows** (a Factory-chosen ID or a per-member session directory). Discovery by scanning harness files is the fallback for Codex and agy only.

Rejected:
- *Direct Unix socket.* Works (verified), but Factory would have to reimplement `hc`'s path rules (`HC_SOCKET`, config `socket`, `XDG_RUNTIME_DIR`, length limits).
- *The `hc` CLI per call.* No CLI for `doc.get`/`doc.set`, no `--env` on `hc run`, and the CLI adds its own `caller`, which tags messages.
- *`events.subscribe`.* Needs a stream reader, heartbeats and `missed` recovery; the shortest deadline the keeper has is about a second.
- *A second mutable store for membership* (a roster file). It can disagree with `hc` after a crash; labels cannot.
- *Commit-marker records in the log.* Rollback under the lock gives the same guarantee with one record per message.
- *A separate `keeper.py`.* Decided by the owner: one file.

## 2. Processes and data flow

```
owner ── types ──▶ operator pane (hc terminal, kind=operator)
                      │ factory.py up / say / status / down
                      ▼
   ┌──────────────── state dir (per team) ────────────────┐
   │ log.jsonl  team.json  sessions.jsonl  lock           │
   │ keeper.lock  keeper.beat  messages/<member>/         │
   └───────▲──────────────────────────▲───────────────────┘
           │ append msg, read outcomes │ read msgs, append outcomes
   say / up / brief (any team pane)    keeper (hc terminal, kind=keeper)
           │                           │ terminal.list  terminal.send  terminal.label
           └──────── hc connect ───────┴──────▶ hc core ──▶ member panes (kind=member)
```

A message's life: `say` validates and appends a `msg` record, makes sure the keeper runs, then reads the log for up to 10 s and prints the outcomes. The keeper sees the record on its next tick, decides per recipient, writes `sending`, calls `terminal.send`, and writes the result.

## 3. Identity and state

### 3.1 Team key and state directory

```
team   = f"{root.name[:24]}-{sha1(str(root))[:8]}"          # v1's cache key, unchanged
state  = <cache base>/factory-skill/<team>/<core.id[:8]>/   # mode 0700; cache base = $XDG_CACHE_HOME or ~/.cache
```

`root` is the main checkout (`find_root`, as v1). `core.id` comes from `hello` and is stable across core restarts (verified), so a team's state is per repository and per `hc` session (§7.3, v1 #108). `up` creates the directory and writes its absolute path into the label `factory.dir` on every team terminal. Every later command takes the path from that label, never from its own environment, so two processes with different `XDG_CACHE_HOME` cannot split the log (v1 activity note 20261123-222200).

| File | Content | Written by |
|---|---|---|
| `team.json` | See §3.3 | `up`, `down`, keeper; atomic replace under `lock` |
| `lock` | Empty; `flock` target for `log.jsonl` and `team.json` | — |
| `log.jsonl` | Messages and outcomes | `say`, `up`, `brief`, `down`, keeper |
| `sessions.jsonl` | Saved native sessions, append-only, v1 format plus `model` and `how` | `up`, `down` |
| `keeper.lock` | `flock` held for the keeper's lifetime | keeper |
| `keeper.beat` | One line `<pid> <version> <terminal>`; its mtime is the heartbeat | keeper |
| `<member>.md`, `<member>.settings.json` | Standing instructions and Claude Code settings, rewritten at every launch | `up` |
| `messages/<member>/` | That member's message files (§8.7) | the member; removed by the keeper |
| `sessions/<member>/` | Session directory for pi and omp members | the harness |

All files are created with v1's `private_open` (`O_NOFOLLOW`, mode 0600, regular file only); directories are 0700 and never symbolic links.

### 3.2 Labels

| Label | On | Value |
|---|---|---|
| `factory.team` | every team terminal | team key |
| `factory.kind` | every team terminal | `operator`, `operator-lost`, `member` or `keeper` |
| `factory.member` | operator, members | the @name without `@` |
| `factory.dir` | every team terminal | absolute state directory |
| `factory.ready` | members | `1` once a brief was `delivered` to this terminal |
| `factory.last` | members | last state word the keeper observed (`working`, `idle`, …) |
| `factory.noted` | members | what the operator was last told: `blocked:<kind>`, `error`, `exit`, or absent |
| `factory.down` | members | `1`, set by `down` just before it ends the terminal |
| `factory.version` | keeper | the keeper's `VERSION` |
| `name` | members, keeper, operator | `@<member>` or `keeper` (rodeo's sidebar name) |
| `herdr.space`, `herdr.tab`, `herdr.seen` | members, keeper | layout ids; `herdr.seen=0` once at start, never written again |

Factory never sets an `hc` terminal name (§6.1) and always addresses a terminal by its ID after reading its labels.

### 3.3 `team.json`

```json
{"v": 1, "root": "/abs/checkout", "team": "factory-ab12cd34", "core": "2b0e89bc…",
 "up": true,
 "layout": {"space": "sp…", "tab": "tb…"},
 "quiet": {"noted": false, "note_id": null, "turns": {"lead-dev": 7}},
 "ready": {"last": null}}
```

`up` is true from the first successful `up` until `down` stops the last member. A missing or unreadable `team.json` is rebuilt by `up` from the checkout and the team's terminals; other commands print "run factory.py up" and exit 2.

### 3.4 Member and operator state

One function, used by every command and by the keeper:

```python
def team_view(hc: Hc, team: str) -> View      # one terminal.list(all=True, labels={"factory.team": team})
class View:
    def terminal(self, name) -> dict | None    # newest live terminal of that member or operator kind
    def finished(self, name) -> dict | None    # newest finished record when there is no live one
    def state(self, name) -> str               # the word shown by status
    operator: dict | None                      # live terminal with kind == "operator"
    keeper: dict | None
```

| `state(name)` | Condition |
|---|---|
| `not started` | no terminal record carries this member |
| `gone` | no live terminal; a finished record exists (it exited or was lost without `down`) |
| `starting` | live, `status` is null (not reporting yet, or Codex's silent trust dialog) |
| `idle`, `working`, `done`, `blocked`, `error` | live; the word is `status.state` unchanged |

The operator is **reachable** when `view.operator` exists and its `status` is not null. The latch of §7.4 is the label: whoever queues a message for the operator as `gone` while its terminal is live without a status sets `factory.kind=operator-lost`; only `up` sets `operator` again.

## 4. Module layout of `factory.py`

Sections in file order, with a line budget. v1 is 1,786 lines; the budget is the target, see §8 for the risk.

| Section | Main units | From v1 | Lines |
|---|---|---|---|
| Constants, `Problem`, files | `find_root`, `refuse_dangling_links`, `VERSION` | kept | 60 |
| Config | `Config`, `parse_role`, `plan` | kept; three new optional keys | 130 |
| Availability checks | `run_probe`, `listed_models`, `resolve_listed`, `Checker`, `check_all`, `checked` | kept | 190 |
| hc client | `Hc`, `HcError`, `team_view`, `View` | new | 110 |
| Message log | `Log`, `Box`, `LogDamage`, `NotStored` | replaces `Inbox` | 150 |
| Harness adapters | `CODE`, `launch_argv`, `claude_settings`, session functions | kept; sessions new | 220 |
| Briefs | `member_prompt`, `render_team_brief`, `message_for` | kept | 80 |
| Layout | `edit_layout`, `add_pane`, `remove_pane`, `swap_terminal` | new | 110 |
| Launch | `ensure_worktree`, `start_member`, `await_ready`, `ensure_keeper` | rewritten | 130 |
| Keeper | `Keeper` | new | 240 |
| Commands | `cmd_init` … `cmd_keeper`, `main` | rewritten except `init`, `harnesses`, `check` | 370 |
| | | **Total** | **1,790** |

Removed from v1: `Inbox`, `cmd_wait`, `operator_wait`, `queue_message`, `herdr()`, integration checks, `Team`, `place`, `launch`'s typed start, `deliver`, `prompt_error`, `report_launch_drift`, `harness_argv`, `print_retry`, `cmd_prompt`, `session_key`.

## 5. Shared constants

```python
HC = os.environ.get("HC_BIN", "hc")
HC_MIN = (0, 1, 20)
VERSION = hashlib.sha1(pathlib.Path(__file__).read_bytes()).hexdigest()[:12]   # the build actually loaded
TICK_BUSY, TICK_IDLE = 1.0, 5.0      # keeper sleep, seconds: something pending or in flight / nothing
BEAT_STALE = 30                      # heartbeat older than this: the keeper is hung
SAY_WAIT = 10                        # say waits this long for outcomes
LAUNCH_TIMEOUT = 60                  # per member, from terminal.run to ready
QUIET_GRACE = 120                    # default; factory.toml quiet_grace overrides
READY_EVERY, READY_TIMEOUT = 300, 30 # ready check period and time limit; ready_check_every overrides the first
MAX_TEXT = 64 * 1024                 # bytes of message text; longer is refused
CAS_TRIES = 8                        # layout doc compare-and-set attempts
```

## 6. Error handling

| Failure | Where | Result |
|---|---|---|
| `hc` missing, older than 0.1.20, or `hc connect` cannot reach a core | any team command | Exit 2: "hc 0.1.20 or newer is required (found X)" or "cannot reach hc: <stderr>". Offline commands never call `hc`. |
| `HC_TERMINAL_ID` unset | `up` | Exit 2: "run the operator inside an hc terminal (rodeo or the Herdr app)" |
| Caller's terminal has no team labels | `say` | Exit 2: "run up first" |
| Preflight fails | `up` | Exit 2, nothing changed (v1 `checked`) |
| A member does not start or gets no brief | `up` | Exit 3 naming it; other members proceed |
| Log damaged | `say`, `brief --send`, keeper, `inbox`, `status` | Diagnosis with path, line and reason; nothing written. `say`/`inbox` exit 2. `status` prints members' states, then the diagnosis. The keeper prints it once per minute and delivers nothing. |
| Append fails | `say` | `not stored`, exit 2; rolled back, or the damage path above if rollback fails |
| `hc` error on send | keeper | Mapped to an outcome ([hc_client.md](components/hc_client.md) §4) |
| Connection lost during a send | keeper | `unconfirmed` |
| Layout write keeps conflicting | `up`, `down` | After `CAS_TRIES`: warn "layout not updated"; the terminal still runs and clients adopt it by its `herdr.*` labels |
| `team.json` unreadable | all but `up` | Exit 2: "run factory.py up" |
| Session cannot be written at `down` | `down` | That member is left running; exit 1 |

Every error message names the member or file and says what to run next. Nothing is retried blindly: a call that may have typed is never repeated.

## 7. Build order

Each step is mergeable alone and ends with its tests green ([testing.md](components/testing.md) names them).

1. hc client and `team_view` with the fake `hc`; `check` prints `hc` version and `draft_wait`.
2. Message log and `Box`; `inbox`.
3. Keeper delivery only (no notes, no quiet rule); `say`.
4. Launch, readiness, labels, sessions; `up` without layout; `down`; `status`.
5. Layout doc; keeper pane.
6. Keeper notes, team-quiet rule, ready check, liveness and version handling.
7. `brief`, recovery of `not ready`, leftover message files, adoption by a new operator.
8. Skill text, templates, docs; remove v1 code and tests that no longer apply.
9. Live checks LC-1 to LC-9, then per-harness qualification (§10) and the end-to-end runs.

## 8. Risks and gaps

- **Size.** The budget lands at v1's size with nothing to spare; the keeper and the sessions code are the likeliest to overrun. If the total passes 1,900 lines, cut before merging: `cmd_harnesses`' per-model detail and the worktree `generic` user note are the cheapest.
- **Log growth.** The log is never compacted in v2.0. At about 0.5 MB a busy day, a full read (keeper start, `status`, `inbox`, `up`) stays under 100 ms for months. `status` warns above 64 MiB. `say` reads only the tail, so its cost does not grow.
- **A missed short state.** Polling can miss a block that begins and clears inside one tick (1–5 s). The `turn` counter still shows that work happened, which is all the quiet rule needs.
- **Session capture** is unverified for five of six harnesses (LC-3 to LC-7). Each has a fallback that degrades to "starts fresh, with a warning".
- **Layout doc shape** is taken from rodeo's source through the research, not from a client rendering a Factory-written doc (LC-8).

## 9. Verified on `hc 0.1.20` (isolated core, 26 November 2026)

| # | Fact | Consequence |
|---|---|---|
| V1 | `hc connect` accepts `hello` then any number of requests, one JSON per line; a round trip is about 1 ms | Transport |
| V2 | `terminal.list{all, labels}` filters by every given label; finished records keep their labels | `team_view` |
| V3 | Finished records and their labels survive `hc server stop` and the next start; reason is `server_stop` | `gone` after an `hc` restart; labels as store |
| V4 | `terminal.label` works on a finished terminal; `key: ""` removes a label | `factory.noted=exit` |
| V5 | `terminal.send` to an idle agent that starts at once returns in 0.2 s; to one that never starts, in 5.2 s with `started: false` | `SAY_WAIT = 10` |
| V6 | Three concurrent sends to one terminal are serialized by `hc`, none interleaved | One in-flight send per recipient is Factory's own ordering rule, not a safety need |
| V7 | `terminal.send` types into a terminal with no status (`started: null`) | Factory must check status itself (§8.2) |
| V8 | Errors arrive as `{"code": -32000, "message": …, "data": {"kind": …, "hint": …}}` with kinds `blocked`, `status_mismatch`, `not_found`, `finished`, `live`, `conflict` | Error mapping |
| V9 | `if: "working"` on an idle terminal fails with `status_mismatch`, "nothing was typed" | `busy` delivery |
| V10 | `terminal.kill` returns the finished record (`reason: "killed"`); `terminal.rm` then removes it; `rm` on a live one fails with `live` | `down` |
| V11 | `core.id` is the same before and after a core restart | State directory key |
| V12 | Every terminal's environment has `HC_TERMINAL_ID`, `HC_SESSION` and `HC_TERMINAL_SOCKET`; `env` in `terminal.run` adds to the core's environment | Caller identity; Claude Code's socket allowance |
| V13 | A program's OSC 2 title shows as `Terminal.title` | Keeper title |
| V14 | `doc.set` with a stale version fails with kind `conflict`; a never-written doc is `{"value": null, "version": 0}` | Layout compare-and-set |
| V15 | A terminal started without `size` is 200×120 until a viewer attaches | Dialog text is not wrapped at start |

## 10. Live checks still owed

| # | Question | Needed by | Fallback |
|---|---|---|---|
| LC-1 | Does an in-flight `terminal.send` still type if its `hc connect` client dies during `draft_wait`? | keeper restart test (§10.10) | Either answer is safe: the message is `unconfirmed`. Record the answer in the docs. |
| LC-2 | Codex: does `-c 'projects."<dir>".trust_level="trusted"'` suppress the trust dialog without writing `config.toml`? | launch | Answer the dialog with `enter`, as v1 |
| LC-3 | Claude Code: does `--resume <uuid>` keep the same session ID, and does `--session-id` accept a fresh UUID with `--append-system-prompt-file`? | sessions | Scan `~/.claude/projects/<slug>/` for the newest transcript that contains the member's instructions path |
| LC-4 | OpenCode: which ID format does `--session <id>` accept for a session that does not exist yet? | sessions | `opencode session list` after the brief, newest in the member's directory |
| LC-5 | Codex: is the developer-instructions pointer (which contains the member's instructions path) stored in each rollout file, including a thread started with `/new`? | sessions | Match on the first message only; a switched thread is then not tracked |
| LC-6 | pi and omp: does `--session-dir <dir> --continue` resume the newest session in that directory, and start a new one without `--continue`? | sessions | pi: `--session-id <uuid>`; omp: `--resume=<path of newest file>` |
| LC-7 | agy: where are conversation IDs stored, and can one be found for a running member? Can the trust dialog be avoided with `--settings`? | sessions, launch | No capture: `up` starts agy fresh with a warning; the dialog is left to the owner (§6.3) |
| LC-8 | Do rodeo and the Herdr app render the tab and panes Factory writes, keep a block's `size.weight`, and leave unknown terminals alone? | layout | Drop the keeper's `size`; let clients adopt by label |
| LC-9 | Can GitHub Actions install a pinned `hc` release? | CI | Fake `hc` only in CI (already the plan) |

## 11. Decisions needed from the owner

1. **Keeper after a skill update.** The spec says it "exits, and the next command starts the new version". With an idle team no command comes, so the quiet rule and notes are off until someone runs one. I recommend the keeper re-executes itself (`os.execv`) once its sends finish: same terminal, same pane, no gap. The design below follows the spec; the change is five lines in [keeper.md](components/keeper.md) §8.
2. **"`down` reads each member's session again" (§9.4)** is possible only where the harness exposes its current conversation. With assigned IDs (Claude Code, OpenCode) a conversation the member switched to inside the harness (`/clear`, `/resume`) is not seen; `down` saves the one it launched. I propose to accept this and print it per harness in the qualification record, unless LC-3 to LC-5 show a cheap way.
3. **A seventh status word.** A live member that reports no status yet has no word in §5.2's list. I use `starting`.
4. **Three optional `factory.toml` keys** (`quiet_grace`, `ready_check`, `ready_check_every`). v1 rejects unknown top-level keys, so v1 needs a one-line back-port to accept and ignore them (§4).
5. **Message size limit** of 64 KiB, refused with "write it to a file and send the path". The spec sets none.
6. **`down` with no session to save.** I read §9.4's "left running" as a storage failure only. A member whose harness gives no session ID (agy today) is stopped, and `down` says "up will start it fresh", as v1 did.
