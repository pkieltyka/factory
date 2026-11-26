---
status: complete
---

# Component: Keeper

## Purpose and scope

`factory.py keeper`: the team's one long-lived process (§8.5, §8a). It delivers every message, sends the operator notes about members, notices a quiet team, runs the ready check, and reports its own liveness. It runs no model, reports no OSC 7501 status, and reads no typed input. It does not launch or stop members and does not edit the layout.

## Public interface

```python
class Keeper:
    def __init__(self, state: pathlib.Path, hc: Hc, clock=time, emit=print, spawn=None): ...
        # clock: time() and monotonic(); emit: one pane line; spawn(fn) runs fn off the main thread
        # (default: a daemon threading.Thread). Tests pass their own clock and spawn.
    def run(self) -> int        # take keeper.lock, start up, loop until an exit rule holds
    def tick(self) -> None      # one pass over the team

def head_of(name: str, box: Box, ready: bool) -> tuple[dict | None, list[dict]]
    # the next message for this recipient, and older briefs it supersedes
def decide(name: str, msg: dict, view: View, box: Box, cfg: Config, now: float,
           op_null_since: float | None, still_true: bool = True) -> tuple
    # pure: ("send", term) | ("queue", why) | ("drop", why) | ("hold",) | ("latch",)
def quiet_now(view: View, box: Box, cfg: Config, now: float, inflight: dict, op_null_since) -> bool   # pure

def ensure_keeper(cfg: Config, state: pathlib.Path, hc: Hc, view: View, force: bool = False) -> str | None
    # the live keeper's terminal ID, or None when the team is stopped
def cmd_keeper(args) -> None    # Keeper(pathlib.Path(args.state), Hc(timeout=10, expect_core=<team.json core>)).run()
```

CLI: `factory.py keeper --state <dir>`. Exit 0 always, including "already running".

## 1. State

| Held in | What |
|---|---|
| memory | `box` (log index and offset); `inflight: dict[recipient, msg id]`; `results: queue.Queue`; `quiet_since`; `op_null_since`; `ready_job`, `ready_next`; last title; last error line and when |
| the log | Everything that must survive a crash: outcomes, which terminals are briefed, which notes were sent, the last quiet and ready notes ([message_log.md](message_log.md), "Derived facts") |
| `team.json` | `up` and `ready_clear`: read under `LOCK_SH` each tick; `ready_clear` written under `LOCK_EX` |
| labels | `factory.last` per member terminal (shown by `status` and exit notes only); `factory.kind` on the operator |
| `factory.toml` | Re-read through `Config(root)` when its mtime changes: roster, `quiet_grace`, `ready_check`, `ready_check_every` |

Only the main thread writes the log, `team.json` and labels, and prints. Worker threads make one blocking call each (`hc.send`, or the ready-check command) and put the result on `results`.

**Crash rule.** The keeper appends to the log first and acts second, and every decision reads the log, not a flag it set earlier. A keeper killed between any two steps therefore restarts into a state that is either "not done yet" (it does the step again, and the log's own check stops a duplicate) or "done". The cases the reviewers named:

| Killed after | Before | On restart |
|---|---|---|
| `delivered` recorded for a brief | anything else | The terminal is in `box.briefed()`: the member is ready |
| a member note committed | anything else | It is `box.last_note(term)` for that terminal and state: not sent again |
| a quiet or ready note committed | it was sent | It is the newest such note and pending: §2 sends it or drops it |
| `sending` recorded | the `hc` call returned | The pair becomes `unconfirmed` (§2 step 4) |
| operator labelled `operator-lost` | `queued gone` recorded | The view has no `operator`: row O1 records it |

## 2. Start-up and main loop

`run()`:

1. `flock(keeper.lock, LOCK_EX | LOCK_NB)`. If it is held: emit `keeper already running`, return 0.
2. Install handlers for SIGINT, SIGTERM and SIGHUP that set `self.stop`. Never read stdin.
3. Read `team.json`; `log.read(0)` into `box` (full validation). On `LogDamage`: emit the diagnosis, set `self.damaged`; a damaged keeper still beats and ticks but delivers nothing and sends no notes, and retries the full read once a minute.
4. For every pair whose current outcome is `sending`: `log.out(box, id, to, "unconfirmed", why="keeper restarted during the send")` (§10.10).
5. Write `keeper.beat` (`<pid> <VERSION> <HC_TERMINAL_ID>`); `hc.label(HC_TERMINAL_ID, {"factory.version": VERSION})`; emit `keeper <VERSION> started`.
6. Loop: `self.tick()`; then `results.get(timeout=TICK)`, putting a result back so the next tick handles it at once. Leave when `self.stop` is set or an exit rule (§8) holds.
7. On leaving: `hc.kill_children()`; emit `keeper stopping: <why>`; return 0.

`tick()`, in this order:

1. Touch `keeper.beat`. Every 30 s compare `VERSION` with the script on disk (§8).
2. Drain `results` and handle each (§4).
3. `view = team_view(hc, team)`. On `HcDown`: emit `hc unreachable: <detail>` at most once a minute and return.
4. `box.apply(log.read(box.offset))`. The same refresh runs after every record the keeper itself appends in steps 6 and 7, so the quiet rule sees a member note just added and step 8 sees every note of this tick.
5. Track `op_null_since`: set to `now` when `view.operator` exists with a null status and it was None; cleared when the status is not null.
6. Member notes (§5).
7. Quiet rule and ready check (§6): may append a note.
8. Deliver (§3): includes a note appended in step 7, in this same tick.
9. Title (§7).

`TICK` is one second, always. One `hc connect` and one `terminal.list` a second is far below 1% of a core; a single period keeps "how long can a state go unseen" a single number (§10).

## 3. Delivery

For each recipient `name` with pending pairs and not in `inflight`:

```python
ready = name == operator or (t := view.terminal(name)) and t["address"] in box.briefed()
msg, superseded = head_of(name, box, ready)
for old in superseded: log.out(box, old["id"], name, "dropped", why="superseded")
action = decide(name, msg, view, box, cfg, now, op_null_since, still_true)
```

`head_of`: for a ready recipient, the oldest pending message. For a member that is not ready, the newest pending `brief` (older pending briefs are superseded), or, with no pending brief, the oldest pending message (which `decide` then queues as `not ready`).

`decide` is a pure function of its arguments. The first matching row wins.

| # | Recipient | Condition | Action |
|---|---|---|---|
| N1 | operator | `msg` is a quiet or ready note and `still_true` is False | `("drop", "no longer true")` |
| O1 | operator | no live terminal of kind `operator` | `("queue", "gone")` |
| O2 | operator | status null for less than `LATCH_AFTER` (5 s) | `("hold",)` |
| O3 | operator | status null for `LATCH_AFTER` or longer | `("latch",)` |
| O4 | operator | `status.state == "blocked"` | `("queue", "blocked")` |
| O5 | operator | otherwise | `("send", address)` |
| M1 | member | `view.state(name) == "not started"` | `("drop", "stopped by down")` |
| M2 | member | `view.state(name) == "gone"` | `("queue", "gone")` |
| M3 | member | `starting` | `("hold",)` |
| M4 | member | not ready and `msg` is not a brief | `("queue", "not ready")` |
| M5 | member | `status.state == "blocked"` | `("queue", "blocked")` |
| M6 | member | otherwise | `("send", address)` |

`still_true` for a quiet or ready note is: the operator is reachable and not `blocked`, and `quiet_now(...)` holds. Because N1 comes first, such a note never reaches the generic rows: it is never queued and never latches the operator. Because "noted" is derived from the log, dropping it is all that is needed to make the rule fire again later.

Applying the action:

| Action | Effect |
|---|---|
| `send` | `log.out(box, id, name, "sending", term=term)`; if it returns True (the record is synced): `inflight[name] = id`; `spawn(lambda: self._send(name, msg, term))`. If it raises `NotStored`, nothing is sent: emit the error and set `self.damaged` (the disk is failing). A `sending` record left visible by that failure becomes `unconfirmed` at the next start, with nothing typed |
| `queue` | `log.out(…, "queued", why=why)` for the head only, and only when `why` differs from its current outcome. With `gone` or `not ready`: the same for every pending pair of that recipient |
| `drop` | `log.out(…, "dropped", why=why)`; with `stopped by down`, for every pending pair of that recipient |
| `hold` | nothing; the default `queued (waiting)` stands |
| `latch` | `hc.label(operator terminal, {"factory.kind": "operator-lost"})`, then as `("queue", "gone")` (§7.4) |

`_send` builds the wire text and calls `hc.send(term, text)` with no timeout, then puts `(name, id, term, result_or_exception)` on `results`. Wire text: `"[from @" + sender + "]\n" + text`, with `"\n"` appended when `text` does not end with one; the sender line is `[from factory]` for notes (§8.1).

The view is at most a few milliseconds old when `hc.send` is called: the "checked right before each send" of §8.2. The known limit (a harness that quits during `draft_wait`) is `hc`'s.

## 4. Handling a send result

This is the only table that maps a send result to an outcome. Always: remove `name` from `inflight`, then:

| Result | Record | Also |
|---|---|---|
| `hc` result | `delivered` with `term`, `started`, `turn` | If every recipient of the message is now `delivered` and its `file` is managed: unlink it |
| `HcError` `blocked` | `queued`, `blocked` | |
| `HcError` `not_found` or `finished`, recipient is the operator | `queued`, `gone` | `down` never stops the operator, so this is never `dropped` |
| `HcError` `not_found` or `finished`, recipient is a member | Settled now, from a fresh `team_view`: if a record for `term` still exists, is finished and has no `factory.down`: `queued`, `gone`. Otherwise (`down` removed or marked it): `dropped`, why `stopped by down` | Never deferred: a `down` followed by `up` must not hand the message to the new terminal (§8.5) |
| `HcDown(sent=False)` | `queued`, `waiting` | Nothing reached `hc`; retried next tick |
| any other `HcError`; `HcDown(sent=True)` | `unconfirmed`, why = the error text | Final; later messages to that recipient go on |

For a quiet or ready note, every row that would write `queued` writes `dropped`, why `operator could not take it`, instead.

A delivered brief needs no further step: readiness is `box.briefed()`. If `log.out` raises here, the keeper emits the error and sets `self.damaged`; the pair stays `sending` in the log and becomes `unconfirmed` at the next start, which is the truth.

## 5. Notes about members (§8.4)

Skipped while the operator is not reachable. For each member with a live terminal `t` whose address is in `box.briefed()`:

1. If `t["keep"]`: `hc.keep_off(address)`. A client may turn `keep` on for panes it shows; Factory turns it off so a dead harness is never replaced by a shell (§9.2, LC-8).
2. If `t["status"]` is null: skip this terminal. It is a shell left where the harness was; `view.state` calls it `gone` and the exit rule below handles it.
3. `s = status.state`. If `s != t.labels.get("factory.last")`: `hc.label(t, {"factory.last": s})`.
4. `what = "blocked:" + (status.kind or "")` when `s == "blocked"`; `"error"` when `s == "error"`; else nothing to note.
5. `last = box.last_note(address)`, the `meta` of the newest note about that terminal. If `last` is None, or `last["what"] != what`, or `last["since"] != t["status_ms"]`: `log.add("factory", [operator], text, kind="note", meta={"what": what, "about": name, "term": address, "since": t["status_ms"]})`.

A note is identified by the **episode** the keeper observes: the terminal, what it reports, and `status_ms`, the moment `hc` says that state began. The comparison is with the newest note only, so each of these gets its own note:

| Observed sequence | Why each is noted |
|---|---|
| blocked (permission) → blocked (question) → blocked (permission), no work between | `what` differs from the newest note each time |
| blocked → idle → blocked, same kind, same turn | `status_ms` changed when it left and re-entered `blocked` |
| blocked, keeper restarts, still blocked | same `what` and `status_ms` as the newest note: nothing sent |

For each member in state `gone` whose terminal is in `box.briefed()` and has no `factory.down`: if `box.last_note(address)` is not an `exit` note, add one with `meta={"what": "exit", "about": name, "term": address}`.

Because a note's identity is in the log record itself, a restarted keeper repeats nothing, and a block or exit still present when it starts, or when a new operator is marked, is noted then. A state that begins and ends within one tick is not seen (§10).

Note texts (`<msg>` is `status.msg` when the harness gave one):

```
@lead-dev is blocked (permission): <msg>. It needs an answer in its pane.
@lead-dev hit an error: <msg>.
@lead-dev exited (code 1) while working. Its last message from you was #41. Run: factory.py up lead-dev
@lead-dev was lost (hc stopped) while idle. Run: factory.py up lead-dev
```

"while <word>" is the `factory.last` label; "#41" is `box.last_to(name, sender=operator)`.

## 6. Team-quiet rule and ready check (§8a)

`quiet_now` is true when all of these hold: the operator is reachable; no live terminal of kind `operator` or `member` has `status.state == "working"`; no member has been `starting` for less than `LAUNCH_TIMEOUT` seconds; `inflight` is empty; and for no recipient does `decide` on its head (quiet and ready notes left out) return `send`.

The quiet state is derived, with `q = box.quiet_note()`:

```python
def rearmed(q, view, box) -> bool:
    return (any(t is working, or t["address"] not in q["meta"]["turns"], or t["turn"] > q["meta"]["turns"][t["address"]]
                for t in live member terminals)
            or any(m["id"] > q["id"] and m["from"] != "factory" and some recipient of m is delivered for m in box.msgs.values()))
told = q is not None and not rearmed(q, view, box)
```

A quiet note that was dropped is not `box.quiet_note()`, so every way a note can fail to arrive puts the rule back to "not told" with no extra step. The operator's own turns never re-arm.

Each tick:

1. If not `told`: `ready_next = None`, so each quiet period waits its own full period before the first ready check. If not `quiet_now`: `quiet_since = None`; stop.
2. If `quiet_since` is None: `quiet_since = clock.monotonic()`.
3. If not `told`, the grace has passed (`quiet_grace`, default `QUIET_GRACE`), and the operator is not `blocked`: `log.add("factory", [operator], quiet_text, kind="note", meta={"what": "quiet", "turns": {address: turn for each live member terminal}})`. Step 8 of this tick sends it.
4. **Ready check.** Only if `cfg.ready_check` is set, `told`, `q`'s outcome is `delivered` or `unconfirmed` (the attempt is over; a note the operator may not have seen must not stop new work from being noticed), the operator is not `blocked`, and no `ready_job` runs. On the first tick where this holds set `ready_next = now + every`; when `now >= ready_next`: spawn the job and set `ready_next = now + every`. The first run is one period after the quiet note, so a board that already has work produces one note, not two.

After a keeper restart `quiet_since` and `ready_next` start again, so the grace and the period are counted afresh; `told` comes from the log, so nothing is repeated.

Quiet note text:

```
team quiet for 2m: nobody is working and nothing is waiting to be delivered.
@lead-dev idle 4m. Last received #41 from @operator. Last sent #39 to @reviewer.
@reviewer done 2m. Last received #40 from @lead-dev. Last sent #42 to @operator.
@pm gone. Last received #12 from @operator. Last sent #13 to @operator.
A member whose last received message is newer than anything it sent may owe a reply.
```

One line per configured member that has a terminal record; the duration is `now - status_ms`; `none` where there is no message.

Ready-check job: `subprocess.run(["sh", "-c", cmd], cwd=root, stdin=DEVNULL, capture_output=True, text=True, timeout=READY_TIMEOUT)`; `(returncode | "timeout", stdout)` goes on `results`. With `r = box.ready_note()` and `last = r["meta"]["digest"] if r and r["id"] > ready_clear else None`:

| Result | Action |
|---|---|
| exit 0, `stdout.strip()` non-empty | `digest = sha1(stdout.strip())`. If `digest != last`, the team is still quiet and the operator can take a message: add a note (`meta={"what": "ready", "digest": digest}`, text `"ready work:\n"` plus the first 20 lines, at most 2,000 characters) |
| exit 1, or exit 0 with empty output | `ready_clear = box`'s highest message id, written to `team.json` |
| anything else, or the time limit | emit `ready check: exit <n>` or `ready check: timed out`; change nothing |

A ready note that is dropped is not `box.ready_note()`, so its digest does not suppress the next one.

## 7. Pane output and title

One line per event through `emit`, prefixed with local `HH:MM:SS`, never redrawn, no message text:

```
16:42:00 keeper 3f2a9c1d07be started
16:42:07 #42 @operator -> @lead-dev delivered
16:42:09 #43 @reviewer -> @operator queued: blocked
16:42:10 #44 @pm -> @operator sending (held)
16:50:00 note #45 @lead-dev exited (1) -> @operator
16:58:12 quiet 2m, note #46 -> @operator
16:59:00 ready check: exit 3
17:10:00 keeper stopping: team is down
```

Every `out` record the keeper writes prints one line, except `sending`: that prints only when the pair is still in flight at the next tick, as `sending (held)`. Title, written as `ESC ] 2 ; text BEL` only when the text changes: `keeper · <n> pending` when any pair is pending, else `keeper · quiet <m>m` while `told`, else `keeper`.

## 8. Liveness, version and exit rules

- **Heartbeat.** Step 1 of every tick. The keeper's `Hc` has a 10 s timeout on every call except `send`, and sends are off the main thread, so neither a slow call nor a `draft_wait` hold stops the beat for `BEAT_STALE` (30 s).
- **Version.** Every 30 s: if `sha1(script on disk)[:12] != VERSION`, set `self.draining`: §3 starts no new send. When `inflight` is empty: emit `keeper restarting: new Factory version`, then `os.execv(sys.executable, [sys.executable, SCRIPT, "keeper", "--state", str(state)])`. The new program runs in the same terminal and pane; `keeper.lock` is released by the exec (its descriptor is close-on-exec) and taken again in step 1 of `run()`. Messages appended during the drain wait as `queued (waiting)` and go out after the exec; nothing is stranded and no command is needed.
- **Team down.** If `team.json`'s `up` is false, `inflight` is empty, and either no pair is pending for the operator or the operator is not reachable: leave with `team is down`.
- **Signals.** SIGINT (Ctrl+C in the pane), SIGTERM, SIGHUP: `hc.kill_children()` and leave at once. Verified (LC-1, [hc-checks.md](../../../../docs/qualification/hc-checks.md)): killing `hc connect` (`SIGKILL`) cancels a send held on `draft_wait` (even if the user later submits the draft), and 0 of 20 trials left an unsubmitted paste when killed 9–264 ms after an observed paste; those pairs still become `unconfirmed` on restart. A keeper killed without the chance to run `kill_children()` (SIGKILL, out of memory) leaves its held sends alive: their `hc connect` clients see only EOF, which LC-1 shows does not cancel, so they are typed when the hold ends. The pairs are `unconfirmed` at the next start, and per-recipient order is not guaranteed across such a crash.

### `ensure_keeper`

Called by `up` (`force=True`), `say`, `brief --send` and `status`.

1. Unless `force`: if `team.json`'s `up` is false and the log holds no pending pair for the operator, return None. A stopped team stays stopped.
2. Take `<state>/keeper.start` with a blocking `flock` (give up after 15 s with "another command is starting the keeper"), then read the view again. Starts are serialized, so two commands never start two keepers.
3. `k = view.keeper`. Read `keeper.beat`: its mtime and the terminal ID in it.
   - **alive**: `k` exists, the beat names `k`'s address, and the beat is at most `BEAT_STALE` old. Return its address. A keeper of another version is alive: it replaces itself.
   - **young**: `k` exists and was created less than `BEAT_STALE` ago (`created_ms / 1000`). It has not had time to prove itself hung. Return its address.
   - **hung**: `k` exists and is neither. `hc.kill(k["address"])`, `hc.rm(k["address"])`: by the terminal ID from the view, never by name or process pattern (v1 #158).
4. Remove every finished record of kind `keeper` (`hc.rm`).
5. Start: `hc.run([sys.executable, SCRIPT, "keeper", "--state", str(state)], cwd=root, env={"PATH": os.environ.get("PATH", "")}, labels={"factory.team": team, "factory.kind": "keeper", "factory.dir": str(state), "factory.version": VERSION, "name": "keeper"})`.
6. Wait up to 5 s for `keeper.beat` to name the new terminal. If the terminal finishes first: `hc.rm` it and raise `Problem("the keeper did not start: <last line of its screen>", "run factory.py status")`.
7. Release `keeper.start`; return the address.

The keeper is always started without a pane. Only `up` gives it one: it adds the returned terminal to the layout in the same edit as the members, whenever that terminal has no block there ([launch_layout.md](launch_layout.md) §6). So a keeper restarted by `say` or `status` runs headless until the next `up`, and a healthy headless keeper gets its pane back on the next `up`.

## 9. Reserved: delivery only between turns (§8.3)

Not built in v2.0 unless a harness fails §10.4. If one does: `harnesses.toml` gets `deliver = "idle"` for it; `decide` gains one row before M6 ("that harness and `working`": `("queue", "busy")`); `hc.send` gains `cond` and passes `if: "idle"` when the member is `idle`; `status_mismatch` maps to `queued`, `busy`. `hc`'s `if` accepts only `idle` or `working` (V9), so a `done` or `error` member is sent to without the guard.

## 10. What a one-second poll does not see

Decided by the owner: the keeper polls, and notes are sent only for states it observes. A state that begins and ends between two ticks is not seen. For the quiet rule that is harmless: `turn` still shows that work happened. For §8.4 it means a block or error shorter than about a second produces no note, which is right: a block the owner cleared within a second needs none. No event stream is built.

## Dependencies

Depends on: hc client, message log, `Config`. Used by: `up`, `say`, `brief`, `status`, `cmd_keeper`.

## Test plan

All with the fake `hc`, a manual clock, and a `spawn` that queues jobs for the test to run, so a send can stay in flight across ticks.

`decide`, `head_of`, `quiet_now` (pure; no keeper, no files)
- One test per row of §3's table: `test_decide_N1` … `test_decide_M6`, plus `test_decide_row_order` (a quiet note to a gone operator is dropped, not queued or latched).
- `test_head_is_oldest_when_ready`, `test_head_is_newest_brief_when_not_ready`, `test_older_briefs_superseded`, `test_head_without_brief_is_queued_not_ready`.
- `test_quiet_now_each_condition`: one case per clause.

Delivery
- `test_delivers_to_idle_member`: `sending` then `delivered`; the fake recorded exactly the wire text; no `caller`.
- `test_wire_text_adds_final_newline_only_when_missing`; `test_note_uses_from_factory`; `test_delivers_while_working`.
- `test_blocked_recipient_queues_once_then_delivers` (§10.5).
- `test_order_per_recipient`; `test_held_send_does_not_delay_others`.
- `test_never_types_without_status` (§10.8); `test_gone_member_queues`; `test_shell_in_member_pane_is_gone` (live, null status, `reported`).
- `test_operator_null_status_holds_then_latches_after_5s`; `test_transient_null_does_not_latch`; `test_latched_operator_gets_nothing_until_marked_again`; `test_marked_again_gets_queue_oldest_first`.
- `test_brief_goes_first_and_makes_ready`; `test_unconfirmed_brief_holds_queue_as_not_ready`; `test_second_brief_releases_queue`.
- `test_managed_file_removed_after_last_delivery`.
- `test_append_between_ticks_is_delivered` (v1 #148).
- `test_note_added_in_step_6_is_sent_in_the_same_tick`.
- `test_damaged_log_stops_delivery_but_not_the_beat`.

Send results (one per row of §4)
- `test_result_delivered`; `test_result_blocked_requeues`; `test_result_unsent_failure_retries`; `test_result_connection_lost_is_unconfirmed`; `test_unconfirmed_does_not_hold_the_queue`.
- `test_result_finished_with_record_is_gone`; `test_result_finished_after_down_is_dropped`; `test_result_finished_for_operator_is_always_gone`.
- `test_unsynced_sending_record_sends_nothing`: the fake records no send; the keeper is `damaged`; after a restart the pair is `unconfirmed`.
- `test_down_then_up_during_send_does_not_resurrect`: the old terminal is downed and a new one for the same member is ready before the result arrives; the pair ends `dropped` and the new terminal receives nothing.
- `test_note_result_blocked_is_dropped`.

Crash recovery (kill the keeper object between two steps, build a new one on the same state)
- `test_restart_marks_sending_unconfirmed_and_never_retypes` (§10.10).
- `test_restart_after_brief_delivered_member_is_ready`: no label exists anywhere; the queue is released.
- `test_restart_after_member_note_appended_sends_it_once`.
- `test_restart_after_quiet_note_appended_sends_it_once`.
- `test_restart_after_latch_label_records_gone`.

Notes
- `test_blocked_member_noted_once`; `test_block_kind_change_noted_again`; `test_block_again_after_work_noted_again`; `test_error_noted`.
- `test_permission_question_permission_in_one_turn_gives_three_notes`; `test_blocked_idle_blocked_in_one_turn_gives_two_notes`; `test_restart_during_block_gives_no_second_note`.
- `test_live_shell_after_keep_is_skipped_then_noted_as_exit`: a briefed terminal with null status does not crash the loop and gets one exit note.
- `test_exit_noted_once_with_code_and_last_state`; `test_down_sends_no_note`; `test_unbriefed_member_exit_is_not_noted`.
- `test_no_notes_to_unreachable_operator`; `test_note_sent_after_operator_marked_again`.
- `test_keep_turned_off_when_a_client_set_it`.
- `test_block_shorter_than_a_tick_is_not_noted`: documents §10.

Quiet and ready check
- `test_quiet_note_after_grace_once`: none before the grace; one after; none in the next 15 minutes of ticks.
- `test_operator_turn_does_not_rearm`; `test_member_work_rearms`; `test_turn_bump_between_ticks_rearms`; `test_new_member_terminal_rearms`; `test_delivered_member_message_rearms`; `test_keeper_note_does_not_rearm`.
- `test_no_quiet_note_while_operator_blocked_then_sent_when_free`; `test_blocked_member_queue_does_not_prevent_quiet`; `test_stuck_starting_member_does_not_prevent_quiet`.
- `test_quiet_note_dropped_when_work_resumes_before_send_and_fires_again_later`: the wedge the reviewers found.
- `test_quiet_note_to_latched_operator_is_dropped_not_queued`.
- `test_quiet_text_lists_members_and_last_messages`.
- `test_ready_check_waits_one_period_after_note`; `test_second_quiet_period_waits_a_full_period_again`; `test_ready_check_runs_after_unconfirmed_quiet_note`; `test_ready_work_noted_once_per_output`; `test_ready_a_none_a_notes_twice`; `test_ready_empty_output_is_none`; `test_ready_other_exit_and_timeout_change_nothing`; `test_dropped_ready_note_does_not_suppress_the_next`; `test_ready_check_not_run_without_config_or_while_the_quiet_note_is_still_pending` (not sent yet, or `sending`; it runs once the note is `delivered` or `unconfirmed`, §8a.2).
- One test per keeper-handled row of §8a.3, named `test_stall_<row>`.

Liveness
- `test_second_keeper_exits`; `test_beat_touched_each_tick_even_with_held_send`.
- `test_version_change_drains_then_execs`: `os.execv` is patched; no new send starts during the drain; a message appended during the drain is still pending when exec is called.
- `test_signal_kills_hc_children`.
- `test_exits_when_team_down_and_nothing_for_operator`; `test_stays_while_operator_has_pending`.
- `test_ensure_keeper_starts_when_missing`; `…_kills_hung_by_terminal_id`; `…_does_not_kill_young_keeper_without_beat`; `…_ignores_beat_of_another_terminal`; `…_serializes_two_starters` (two processes: one `terminal.run`); `…_reports_keeper_that_exits_at_start`; `…_leaves_stopped_team_stopped`; `…_leaves_other_version_alone`.
- `test_pane_lines_and_title`.
