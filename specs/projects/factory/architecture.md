---
status: complete
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
2. **Talk to `hc` through `hc connect`.** Factory spawns `hc connect` and speaks newline-delimited JSON-RPC on its stdin and stdout (verified). It never resolves a socket path itself, so it follows `HC_SESSION`, `HC_SOCKET` and `hc`'s config exactly as `hc` does. The request is written only after `hello` has been read and the version and core checked.
3. **Poll once a second, do not subscribe.** The keeper reads the team with one label-filtered `terminal.list` per tick (0–1 ms, verified). What a poll cannot see is in [keeper.md](components/keeper.md) §10 (owner decision 8).
4. **`hc`'s terminal records are the membership database.** Which terminal is which member is a label on the terminal. A member is `not started` when it has no record and `gone` when its newest record is finished. Records and labels survive a core restart (verified).
5. **One append-only log is the only durable memory of what happened.** Messages, their commit records and per-recipient outcomes are appended under one lock, the state directory itself, never held across an `hc` call. A message exists once its commit record is written, whatever fails or dies around it. Every fact the keeper must not forget across a crash (which terminals are briefed, which notes were sent) is derived from the log, never kept in a flag beside it.
6. **Only the keeper types.** `say`, `up` and `brief --send` append to the log and wait for the outcome records the keeper writes. Briefs are messages of kind `brief`.
7. **Nothing changes before preflight passes**, and `up` and `down` hold one team lock for their whole run, so two launches cannot claim the same member.
8. **A small `team.json`** holds the team root, layout ids, the ready check's cleared mark and the terminal IDs explicitly stopped by `down`. Keeper delivery pauses when this file cannot be read.
9. **A member's conversation is learned from the member itself.** Each harness exports its current conversation ID to the commands it runs (Claude Code, Codex and agy, confirmed live), so every `say` a member runs records it, and a switch made inside the harness is followed from the next `say`. Launch flags that fix the conversation are used where they exist (Claude Code and OpenCode an ID, pi and omp a per-member directory). No harness file is scanned and no screen is read for this.

Rejected:
- *Direct Unix socket.* Works (verified), but Factory would have to reimplement `hc`'s path rules (`HC_SOCKET`, config `socket`, `XDG_RUNTIME_DIR`, length limits).
- *The `hc` CLI per call.* No CLI for `doc.get`/`doc.set`, no `--env` on `hc run`, and the CLI adds its own `caller`, which tags messages.
- *A roster file for membership.* It can disagree with `hc` after a crash; labels cannot.
- *Labels or `team.json` flags for "ready" and "already noted".* A crash between the log append and the flag leaves them wrong (review round 1). The log already holds both facts.
- *Rollback under the lock as the only commit rule.* It fails when a sync error and a failed truncate coincide, or the writer dies holding the lock (review round 1). The commit record costs one small append per message.
- *Checking only the log's tail before an append.* It would extend a log whose earlier part is damaged. A stat-based checkpoint proves the prefix unchanged at the same cost.
- *Finding the current conversation by scanning transcripts or session stores.* Six fragile readers of other programs' private files. The harness's own environment variable gives the same fact for free.
- *A separate `keeper.py`.* Decided by the owner: one file.

## 2. Processes and data flow

```
owner ── types ──▶ operator pane (hc terminal, kind=operator)
                      │ factory.py up / say / status / down
                      ▼
   ┌──────────────── state dir (per team) ────────────────┐
   │ log.jsonl  log.check  team.json  sessions.jsonl      │
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
| `team.json` | See §3.3 | `up`, `down`, keeper; atomic replace under the team lock |
| (the state directory) | The team lock: `flock` on its no-follow descriptor for `log.jsonl`, `log.check`, `team.json` and `sessions.jsonl`; no lock file (message_log.md §Locking). It must be on a local filesystem; a failed lock names the path and the original error and suggests a local `XDG_CACHE_HOME` | — |
| `log.jsonl` | Messages, commit records and outcomes | `say`, `up`, `brief`, `down`, keeper |
| `log.check` | Checkpoint proving the log unchanged since the last validated append | every log writer |
| `up.lock` | `flock` held across `up`, `down` and `brief --send` | — |
| `keeper.start` | `flock` serializing keeper starts | — |
| `sessions.jsonl` | Saved native sessions, append-only, one entry per line, under the team lock | `say` (members), `up`, `down`, `brief --send` |
| `keeper.lock` | `flock` held for the keeper's lifetime | keeper |
| `keeper.beat` | One line `<pid> <version> <terminal>`; its mtime is the heartbeat | keeper |
| `<member>.md`, `<member>.settings.json` | Standing instructions and Claude Code settings, rewritten at every launch | `up` |
| `messages/<member>/` | That member's message files (§8.7) | the member; removed by the keeper |
| `sessions/<member>-<harness>/` | Session directory for pi and omp members | the harness |

All files are created with v1's `private_open` (`O_NOFOLLOW`, mode 0600, regular file only); directories are 0700 and never symbolic links.

### 3.2 Labels

| Label | On | Value |
|---|---|---|
| `factory.team` | every team terminal | team key |
| `factory.kind` | every team terminal | `operator`, `operator-lost`, `member` or `keeper` |
| `factory.member` | operator, members | the @name without `@` |
| `factory.dir` | every team terminal | absolute state directory |
| `factory.last` | members | last state word the keeper observed (`working`, `idle`, …); shown by `status` and exit notes, nothing depends on it |
| `factory.down` | members | `1`, set by `down` just before it ends the terminal |
| `factory.launch` | members | JSON launch fact with `how`, `value`, `fresh`, `harness`, and `model`; cleared once its session entries are saved |
| `factory.version` | keeper | the keeper's `VERSION` |
| `name` | members, keeper, operator | `@<member>` or `keeper` (rodeo's sidebar name) |
| `herdr.space`, `herdr.tab` | members, keeper | the space and tab that hold the pane, written after placement |

Rodeo owns `herdr.seen`.

Factory never sets an `hc` terminal name (§6.1) and always addresses a terminal by its ID after reading its labels. Whether a member is ready, and what the operator has been told about it, are not labels: they are read from the log ([message_log.md](components/message_log.md), "Derived facts").

### 3.3 `team.json`

```json
{"v": 1, "root": "/abs/checkout", "team": "factory-ab12cd34", "core": "2b0e89bc…",
 "up": true,
 "stopped": ["t4"],
 "layout": {"space": "sp…", "tab": "tb…", "made_space": false, "keeper": "t7"},
 "ready_clear": 0}
```

`up` is true from the first `up` that passes preflight until `down` stops the last member. `down` records intentionally stopped terminals in `stopped` before removing their records or dropping queued messages. The keeper checks that list under the same lock it uses to settle a definite refusal. A missing or unreadable file withholds settlement until metadata is available again. `ready_clear` is the highest message id at the last ready check that found no work. `up` can rebuild missing or unreadable metadata from the checkout and its terminals.

`layout.keeper` names the terminal in the keeper pane. If the keeper is replaced headless, `up` reuses the recorded block and the last `down` removes it.

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
| `gone` | no live terminal and a finished record exists (it exited or was lost without `down`); or a live terminal with a null status that has reported before (a shell was left in its place) |
| `starting` | live, `status` is null and it has never reported (not up yet, or Codex's silent trust dialog) |
| `idle`, `working`, `done`, `blocked`, `error` | live; the word is `status.state` unchanged |

The operator is **reachable** when `view.operator` exists and its `status` is not null. The latch of §7.4 is the label: when a message is pending for the operator and its terminal has had no status for `LATCH_AFTER` seconds, the keeper sets `factory.kind=operator-lost` and queues the message as `gone`; only `up` sets `operator` again. A shorter gap (a harness resetting) latches nothing.

## 4. Module layout of `factory.py`

There is no line-count target. Code is kept as simple as each change allows; one simplification pass over the whole file comes at the very end of v2, after Phase 10. The table lists the sections in file order.

| Section | Main units | From v1 |
|---|---|---|
| Constants, `Problem`, files | `find_root`, `refuse_dangling_links`, `VERSION` | kept |
| Config | `Config`, `parse_role`, `plan` | kept; three new optional keys |
| Availability checks | `run_probe`, `listed_models`, `resolve_listed`, `Checker`, `check_all`, `checked` | kept |
| hc client | `Hc`, `HcError`, `HcDown`, `team_view`, `View` | new |
| Message log | `Log`, `Box`, `LogDamage`, `NotStored`, `inbox_rows`, message-file helpers | replaces `Inbox` |
| Harness adapters | `CODE`, `launch_argv`, `claude_settings`, session functions | kept; sessions new |
| Briefs | `member_prompt`, `render_team_brief`, `message_for` | kept |
| Layout | `edit_layout`, `resolve_tab`, `add_panes`, `remove_panes`, `swap_terminal` | new |
| Launch | `check_operator`, `mark_operator`, `start_member`, `await_ready` | rewritten |
| Keeper | `Keeper`, `decide`, `head_of`, `ensure_keeper`; quiet rules in Phase 6 | new |
| Team state and messaging commands | `resolve_team`, metadata helpers, `cmd_say`, `cmd_inbox`, `wait_outcomes` | replaces v1 messaging |
| Other commands | `cmd_init`, `cmd_up`, `cmd_status`, `main` and remaining lifecycle commands | lifecycle rewritten in Phase 5a |

Removed from v1: `Inbox`, `cmd_wait`, `operator_wait`, `queue_message`, `herdr()`, integration checks, `Team`, `place`, `launch`'s typed start, `deliver`, `prompt_error`, `report_launch_drift`, `harness_argv`, `print_retry`, `cmd_prompt`, `session_key`, `session_id`, `remember_sessions`' herdr reads.

## 5. Shared constants

```python
HC = os.environ.get("HC_BIN", "hc")
HC_MIN = (0, 1, 20)
VERSION = hashlib.sha1(pathlib.Path(__file__).read_bytes()).hexdigest()[:12]   # the build actually loaded
TICK = 1.0                           # keeper period, seconds
LATCH_AFTER = 5                      # operator with no status this long, and a message pending: unreachable until up
BEAT_STALE = 30                      # heartbeat older than this: the keeper is hung
KEEPER_START = 5                     # wait for the new keeper's first beat
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
| `hc` missing, older than 0.1.20, another core than the team's, or `hc connect` cannot reach a core | any team command | Exit 2 with the reason; decided from `hello`, before any request is written. Offline commands never call `hc`. |
| `HC_TERMINAL_ID` unset | `up` | Exit 2: "run the operator inside an hc terminal, such as a rodeo pane" |
| Caller's terminal has no team labels | `say` | Exit 2: "run up first" |
| The operator check or preflight fails, or another `up`/`down` holds the team lock | `up` | Exit 2; no label, terminal, layout, `team.json` or log change was made |
| A member does not start or gets no brief | `up` | Exit 3 naming it; other members proceed |
| Log damaged | `say`, `brief --send`, keeper, `inbox`, `status` | Diagnosis with path, line and reason; nothing written. `say`/`inbox` exit 2. `status` prints members' states, then the diagnosis. The keeper prints it once per minute and delivers nothing. |
| Append fails | `say` | `not stored`, exit 2. The message has no commit record and is never delivered, whatever else failed |
| A resumed member exits at start | `up` | One fresh restart with a warning |
| A `sending` record cannot be synced | keeper | Nothing is sent; the keeper stops delivering and says so until the log can be written |
| `hc` error on send | keeper | Mapped to an outcome ([hc_client.md](components/hc_client.md) §4) |
| Connection lost during a send | keeper | `unconfirmed` |
| Layout write keeps conflicting | `up`, `down` | After `CAS_TRIES`: warn; the terminals run without panes and without `herdr.*` labels, and the next `up` places them |
| `team.json` unreadable | all but `up` | Exit 2: "run factory.py up" |
| Session cannot be written | `say`, `up` | A warning; the next `say` tries again |
| `sessions.jsonl` cannot be appended to | `down` | Nobody is stopped; exit 1 |

Every error message names the member or file and says what to run next. Nothing is retried blindly: a call that may have typed is never repeated.

## 7. Build order

Each step is mergeable alone and ends with its tests green ([testing.md](components/testing.md) names them).

1. hc client and `team_view` with the fake `hc`; `check` prints `hc` version and `draft_wait`.
2. Message log and `Box`; `inbox`.
3. Keeper delivery only (no notes, no quiet rule); `say`.
4. Launch, readiness, labels, sessions; `up` without layout; `down`; `status`.
5. Layout doc; keeper pane.
6. Keeper notes, team-quiet rule, ready check, liveness, re-exec on a new version, crash-recovery tests.
7. `brief`, recovery of `not ready`, leftover message files, adoption by a new operator.
8. Skill text, templates, docs; remove v1 code and tests that no longer apply.
9. The live checks of §10, then per-harness qualification (§10) and the end-to-end runs.

## 8. Risks and gaps

- **Log growth.** The log is never compacted in v2.0. At about 0.5 MB a busy day, a full read (keeper start, `status`, `inbox`, `up`) stays under 100 ms for months. `status` warns above 64 MiB. A writer does not read the log at all when the checkpoint matches, so the cost of `say` does not grow.
- **A state shorter than a second.** Decided by the owner: the keeper polls and sends notes only for states it observes. A block or error that begins and clears inside one tick produces no note; the `turn` counter still shows that work happened, which is all the quiet rule needs. The spec's §8.4 should say so (§11).
- **Clients and `keep`.** A client may turn `keep` on for a pane it shows; `hc` then leaves a shell when the harness dies. The keeper turns `keep` off when it sees it, a live terminal that stopped reporting counts as `gone`, and nothing is ever typed into a terminal with no status. LC-8 measures how often clients do this.
- **Environment.** A member gets `PATH` from the process that ran `up` and everything else from the `hc` core's login environment. An API key exported only in the operator's shell does not reach members; the docs say so and `up`'s failure line shows the harness's own error.
- **Resume.** The environment variables are confirmed for three harnesses; the launch flags, the by-ID resume arguments and the sub-agent case are not all exercised yet (LC-3, LC-4, LC-6, LC-11, LC-12). Each fails safe: a resume that does not work is retried fresh once, with a warning. A Codex or agy member that never ran `say` has no recorded conversation and starts fresh.
- **Layout doc shape** is taken from rodeo's source through the research, not from a client rendering a Factory-written doc (LC-8, checked through the rodeo driver of LC-13).

## 9. Verified on `hc 0.1.20` (isolated core, 26 November 2026)

| # | Fact | Consequence |
|---|---|---|
| V1 | `hc connect` accepts `hello` then any number of requests, one JSON per line; a round trip is about 1 ms | Transport |
| V2 | `terminal.list{all, labels}` filters by every given label; finished records keep their labels | `team_view` |
| V3 | Finished records and their labels survive `hc server stop` and the next start; reason is `server_stop` | `gone` after an `hc` restart; labels as store |
| V4 | `terminal.label` works on a finished terminal; `key: ""` removes a label | `factory.down`, `operator-lost` |
| V5 | `terminal.send` to an idle agent that starts at once returns in 0.2 s; to one that never starts, in 5.2 s with `started: false` | `SAY_WAIT = 10` |
| V6 | Three concurrent sends to one terminal are serialized by `hc`, none interleaved | One in-flight send per recipient is Factory's own ordering rule, not a safety need |
| V7 | `terminal.send` types into a terminal with no status (`started: null`) | Factory must check status itself (§8.2) |
| V8 | Errors arrive as `{"code": -32000, "message": …, "data": {"kind": …, "hint": …}}` with kinds `blocked`, `status_mismatch`, `not_found`, `finished`, `live`, `conflict` | Error mapping |
| V9 | `if: "working"` on an idle terminal fails with `status_mismatch`, "nothing was typed" | Reserved delivery between turns ([keeper.md](components/keeper.md) §9) |
| V10 | `terminal.kill` returns the finished record (`reason: "killed"`); `terminal.rm` then removes it; `rm` on a live one fails with `live` | `down` |
| V11 | `core.id` is the same before and after a core restart | State directory key |
| V12 | Every terminal's environment has `HC_TERMINAL_ID`, `HC_SESSION` and `HC_TERMINAL_SOCKET`; `env` in `terminal.run` adds to the core's environment | Caller identity; Claude Code's socket allowance |
| V13 | A program's OSC 2 title shows as `Terminal.title` | Keeper title |
| V14 | `doc.set` with a stale version fails with kind `conflict`; a never-written doc is `{"value": null, "version": 0}` | Layout compare-and-set |
| V15 | A terminal started without `size` is 200×120 until a viewer attaches | Dialog text is not wrapped at start |

## 10. Live checks

### Still owed

| # | Question | Needed by |
|---|---|---|---|
| LC-2 | Codex: does `-c 'projects."<dir>".trust_level="trusted"'` suppress the trust dialog without writing `config.toml`? | launch |
| LC-3 | Claude Code: is `--session-id <uuid>` accepted together with `--append-system-prompt-file` and `--settings`, and does `--resume <id>` reopen that conversation? | sessions |
| LC-4 | OpenCode: which ID format does `--session <id>` accept for a session that does not exist yet? | sessions |
| LC-6 | pi and omp: does `--session-dir <dir> --continue` resume the newest conversation in that directory, and start a new one without `--continue`? | sessions |
| LC-7 | agy: does `--settings '{"trustedWorkspaces": ["<dir>"]}'` suppress the trust dialog for that session without writing the global list? | launch |
| LC-11 | OpenCode: is there a variable like the others in its tool environment? | sessions |
| LC-12 | Claude Code, Codex, agy: does a `say` run by a sub-agent or sub-thread carry the parent conversation's ID in the variable? | sessions |

### Resolved (Phase 1 and Phase 5a, 26 November 2026, [docs/qualification/hc-checks.md](../../../docs/qualification/hc-checks.md))

| # | Result | Consequence applied |
|---|---|---|
| LC-1 | `SIGKILL` of `hc connect` at `6 s` (`draft_wait = 12`, past quiet pause) cancels a held `terminal.send` (even if Enter follows); stdin EOF alone does not (typed at `12.0 s`); 0 of 20 trials left an unsubmitted paste when killed 18–294 ms after an observed paste. | `keeper.md` §8 (`kill_children()` stops held sends; SIGKILL/OOM leaves them alive via EOF; pairs stay `unconfirmed`), `hc_client.md` (EOF does not cancel). |
| LC-9 | **Pass** on GitHub Actions `ubuntu-latest` (11 s): pinned `hc 0.1.20` release tarball + SHA-256 ran `live.py fixtures` with matching reply key shape (`installed_by` is `null` on release vs `"nix"` on Nix). | `testing.md` §8 (Phase 2 can enable pinned-`hc` real-mode CI), `testing.md` §4 (contract tests check required schema keys, optional keys when present, and schema types with `null` only on nullable fields). |
| LC-10 | **Pass:** `HC_DRAFT_MS` is honoured; `config.toml` `draft_wait` overrides it; `HC_SETTLE_MS=0` does not shorten draft holds. | `testing.md` §6 (live cases 3 & 10 use a private `XDG_CONFIG_HOME` for the QA core with private copies of mutable harness settings and only needed credential paths shared). |
| LC-8 (rodeo) | **Pass** with `rodeo 0.2.1` (Phase 5a, `live.py check lc8`): a `row` root with `@alice`, `@bob` and the keeper block at `size.weight` 0.4 renders each title once, the keeper last and narrowest (title-row widths 56, 56, 20 of 160 columns); the doc is unchanged, the weight kept; an unlabelled terminal stays out of the doc; `keep` stayed `false` on all four. | `launch_layout.md` §5 keeps the keeper's `size`; the keeper's keep-off rule stays. |
| LC-13 | **Pass** with `rodeo 0.2.1` (baseline `0.0 s`, draft held past `6 s` and typed `0.17 s` after Enter; negative control `draft_wait=0` failed as intended at `2.47 s`): works in a second private core (`env -u HC_TERMINAL_ID` or `allow_nested = true`), `ctrl+b` + `h`/`l` (0.2 s apart) switches focus, `hc read` returns rendered screen, `keep` stayed `false`. | `testing.md` §6 (keep `Rodeo` driver with these launch/focus details). |

## 11. Owner decisions

Decided on 26 November 2026, and built into this design:

| # | Decision |
|---|---|
| 1 | After a skill update the keeper finishes its sends and re-executes itself in place (`os.execv`) |
| 2 | A member's conversation is learned from its harness's environment variable, recorded by each `say` it runs; launch flags are kept where they help; a member with no recorded conversation starts fresh with a notice (spec §9.4, already updated) |
| 3 | A seventh status word, `starting`, for a live member that has not reported yet |
| 4 | Three optional `factory.toml` keys (`quiet_grace`, `ready_check`, `ready_check_every`), with a v1 back-port that accepts and ignores them |
| 5 | Message text is limited to 64 KiB |
| 6 | `down` stops a member that has no recorded conversation and `up` says it starts fresh; `down` stops nobody when sessions cannot be written |
| 7 | About 2,200 lines is a target, not a cap; delivery only between turns (`busy`) is not built until a harness fails §10.4 |
| 8 | The keeper polls and sends notes only for states it observes; no event stream |

Nothing is open. One sentence is proposed for spec §8.6 ([message_log.md](components/message_log.md), "Append and commit"): a message whose commit record is completely written counts as stored even if the disk then reports an error, and `say` warns.
