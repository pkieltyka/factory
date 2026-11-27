---
status: complete
---

# Component: Message log

## Purpose and scope

Durable storage for every message and every per-recipient outcome (§8.5–8.7): the append-only file, its lock, the commit rule, the in-memory index built from it, and the handling of message files. The index is also where everything the keeper must not forget across a crash is derived from: which terminals have a delivered brief, which notes were already sent, and the last quiet and ready-work notes. It does not type, decide deliverability, or talk to `hc`.

## Storage format

`<state>/log.jsonl`, UTF-8, one JSON object per line, appended only. Three record types.

```json
{"t":"msg","id":42,"at":"2026-11-26T17:00:00Z","from":"lead-dev","to":["operator","reviewer"],"kind":"say","text":"status: done …","file":"/…/messages/lead-dev/operator-done-1.md"}
{"t":"ok","id":42}
{"t":"out","id":42,"to":"operator","o":"sending","at":"2026-11-26T17:00:01Z","term":"t12"}
{"t":"out","id":42,"to":"operator","o":"delivered","at":"2026-11-26T17:00:01Z","term":"t12","started":true,"turn":7}
{"t":"out","id":42,"to":"reviewer","o":"queued","why":"blocked","at":"2026-11-26T17:00:01Z"}
```

| `msg` field | Type | Meaning |
|---|---|---|
| `id` | int | Previous highest `msg` id plus one; 1 in an empty log |
| `at` | str | UTC, `%Y-%m-%dT%H:%M:%SZ` |
| `from` | str | Member name, the operator's name, or `factory` for the keeper |
| `to` | list[str] | Recipient names, no duplicates, never the sender |
| `kind` | str | `say`, `brief` or `note` |
| `text` | str | Exactly as written by the sender; never changed |
| `file` | str or absent | Absolute path given with `--file` |
| `meta` | object or absent | Notes only; see [keeper.md](keeper.md) §5–6. `what` is `blocked:<kind>`, `error`, `exit`, `quiet` or `ready`; `about`, `term` and `since` (the state's `status_ms`) identify a member note; `turns` is a quiet note's snapshot; `digest` a ready note's |

`ok` is the **commit record**: `{"t":"ok","id":N}`, written directly after message N by the same writer in the same lock section. A message exists for delivery, for `inbox` and for every derived fact only once its `ok` record is in the log. A `msg` without one is an *uncommitted entry*: its id is used up, it is never delivered, and `status` counts such entries.

| `out` field | Type | Meaning |
|---|---|---|
| `id`, `to` | int, str | A committed message and one of its recipients |
| `o` | str | `sending`, `delivered`, `queued`, `unconfirmed` or `dropped` |
| `why` | str or absent | `queued`: `waiting`, `blocked`, `not ready` or `gone`. `unconfirmed` and `dropped`: a short reason |
| `term` | str or absent | Terminal ID the send went to |
| `started`, `turn` | bool or null, int | `delivered` only, from `hc`'s result; both required there, with `term`. `sending` requires `term` |

The current outcome of (message, recipient) is its last `out` record; with none it is `queued`, reason `waiting`. `delivered`, `unconfirmed` and `dropped` are final. `refused` and `not stored` are never written. The reason `busy` (§8.3) is written for a between-turns recipient that is `working` ([keeper.md](keeper.md) §9).

```
queued ──▶ sending ──▶ delivered
   ▲          ├──────▶ unconfirmed
   │          ├──────▶ dropped        (down stopped the recipient and nothing was typed)
   └──────────┘                       (hc refused before typing: back to queued with the reason)
queued ──▶ dropped                    (down; a superseded brief; a quiet or ready note that is no longer true)
queued ──▶ queued                     (a new reason, e.g. waiting → blocked; the same reason again is a no-op)
```

Replay is strict: a recorded move the diagram does not allow (`queued → delivered`, a second `sending`, any record after a final outcome) is `LogDamage` at its line, and the log is kept for repair, because ignoring it could expose a delivered pair for redelivery. A `queued` record may change the reason of a queued pair (`blocked` to `gone`, say), as `out` writes it; the one tolerated repeat is a `queued` record with the reason the pair already has. Because readers reject what they do not know, a later Factory version may only widen what it accepts, never narrow or rename it; and while an older version may still be running (during an update a keeper and a command of different versions write the same log), it must keep writing only records and moves the older one accepts, enabling a new shape only once no older reader remains.

## Locking

The state directory itself is the `flock` target for `log.jsonl`, `log.check`, `team.json` and `sessions.jsonl`, locked through the same no-follow descriptor every operation opens. There is no lock file: nothing can remove or replace the lock while the directory holds the log, so two writers can never append to one log under different locks, with or without a checkpoint. Factory's state must be on a local filesystem (on Linux's NFS, for one, an exclusive `flock` needs a descriptor opened for writing, which a directory cannot be). Any failure of a writer's exclusive lock raises an `OSError` that keeps the original errno and cause, names the state path and suggests setting `XDG_CACHE_HOME` to a local path. Readers take `LOCK_SH`, writers `LOCK_EX`. It is held only around file operations, never across an `hc` call or a sleep (v1's rule). The lock gives mutual exclusion only. It is not the commit rule: a writer can die, or fail twice, while holding it, and the log must still say only true things. The commit record is what makes that so.

## Public interface

```python
class LogDamage(Exception):
    def __init__(self, path, line: int | None, reason: str): ...
    # str(): "message log needs repair: <path>, line <n>: <reason>. Nothing was written. Repair or move the file, then run factory.py up."
    # line is None for a fault of the files rather than of one line (a checkpoint without its log)

class NotStored(Exception):
    partial: bool     # True: an incomplete line may remain; readers will report damage
    detail: str

class Log:
    def __init__(self, state: pathlib.Path, base: pathlib.Path | None = None): ...
        # base: the owner's cache base above state; everything from base down is opened without following links
    def open_dir(self, create: bool) -> int | None
        # a no-follow descriptor to the state directory; the caller closes it
    def locked(self, exclusive: bool)
        # context manager yielding the directory descriptor; no public Log call inside it may take the lock again
    def read(self, offset: int = 0) -> tuple[list[dict], int]
        # LOCK_SH. Records from byte offset to the end and the new offset. offset == 0: full validation. Raises LogDamage.
    def refresh(self, box: "Box") -> None
        # LOCK_SH. Refresh a full box from its proven log lineage, or rebuild it when the lineage changed.
    def add(self, sender: str, to: list[str], text: str, kind: str = "say",
            file: str | None = None, meta: dict | None = None) -> tuple[int, int, str | None]
        # LOCK_EX. Validates the prefix, allocates the id, appends msg then ok. Returns (id, offset after the ok record, warning or None).
        # Raises ValueError (bad arguments, nothing touched), LogDamage (nothing written), NotStored (the message will never
        # be delivered), or OSError "storage outcome unknown; do not resend; check inbox after repair" (see Append and commit).
    def out(self, box: "Box", id: int, to: str, o: str, **extra) -> bool
        # LOCK_EX. Brings box up to the validated file, then appends the out record only if the move is allowed from the pair's
        # current outcome: sending from queued; delivered and unconfirmed from sending; queued from queued (a new reason) or
        # sending; dropped from queued or sending; nothing after a final outcome. A stale or repeated attempt returns False.
        # Raises ValueError (bad arguments, a partial box), LogDamage, NotStored, OSError (unreadable read-back).
    def drop_queued(self, box: "Box", names: set[str], why: str = "stopped by down") -> int
        # LOCK_EX, one section: dropped for every pair still queued to one of names; returns how many were written.
    def remove_delivered_file(self, box: "Box", id: int) -> str | None
        # LOCK_EX. Unlinks the message's managed file once every recipient is delivered; a warning naming the path if it failed.
    def leftovers(self, now: float | None = None) -> tuple[list[dict], list[str]]
        # LOCK_EX. Files left in messages/*/: rows {sender, path, age_s, id or None, outcomes} and warnings (§ Message files).

def managed_file(state, sender, path) -> bool    # the removal predicate of § Message files; lexical, resolves nothing
def format_leftovers(rows) -> list[str]           # up's lines for leftovers, with the reminder once
def inbox_rows(log: Log, name: str, count: int = 10) -> list[tuple[dict, dict]]
    # the last count committed messages to name, oldest first, with its outcome; read-only (the inbox command uses it)

class Box:
    """In-memory index of the committed log."""
    offset: int
    msgs: dict[int, dict]                         # committed messages; text is dropped once every recipient is final
    uncommitted: int                              # msg records with no ok
    @classmethod
    def for_message(cls, path, id: int, recipients: list[str], offset: int) -> "Box"
        # a partial, outcome-only view of one committed message from the offset add returned (say's wait); it ignores other
        # ids and can never authorize a write or prove a prefix
    def apply(self, records: list[dict]) -> None  # raises LogDamage on a broken reference or an illegal recorded transition;
                                                  # a box that raised stays unusable until Log.out rebuilds it
    def outcome(self, id: int, to: str) -> dict   # the last out record, or {"o": "queued", "why": "waiting"}
    def pending(self, to: str | None = None) -> list[tuple[dict, str]]   # (msg, recipient) pairs not final, by id
    def last_to(self, name: str, sender: str | None = None) -> dict | None   # newest message delivered to name
    def last_from(self, name: str) -> dict | None                            # newest message sent by name
    # derived facts (§ "Derived facts")
    def briefed(self) -> set[str]                 # terminal IDs with a delivered brief
    def last_note(self, term: str) -> dict | None # meta of the newest member note about that terminal
    def quiet_note(self) -> dict | None           # newest quiet note whose outcome is not dropped
    def ready_note(self) -> dict | None           # newest ready-work note whose outcome is not dropped
    deliveries: int                               # delivered outcomes of non-notes (full boxes only)
```

## Algorithms

### Validation

`parse(raw: bytes, first_line: int, state) -> list[dict]`, where `state` carries the highest `msg` id and the previous record across calls:

1. Empty is valid. Otherwise `raw` must end with `\n`, else `LogDamage(line = count + 1, "incomplete line")`.
2. Each line must decode as a JSON object with `t` of `msg`, `ok` or `out`.
3. `msg`: `id` is an int greater than the highest `msg` id so far; `at`, `from`, `text` are strings; `to` is a non-empty list of distinct strings; `kind` is one of the three.
4. `ok`: the record directly before it is a `msg` with the same `id`.
5. `out`: `id` is an int; `to` is a string; `o` is one of the five.
6. Any violation raises `LogDamage` with the 1-based line number and what was expected.

References are checked by `Box.apply` for a box that started at offset 0: an `out` must name a committed message and one of its recipients, else `LogDamage` ("outcome for unknown message", "outcome for a non-recipient"). A box started at a later offset (the wait loop of `say`) ignores records for ids it does not hold.

### Proving the prefix before extending it

Every writer validates everything already in the log before it appends (§8.6). Reading the whole file on every `say` would grow with the log, so writers keep a checkpoint, `<state>/log.check`:

```json
{"ino": 1234567, "size": 81234, "mtime_ns": 1795713707392000000, "last": 42, "lineage": "9f2c4e1a7b3d5608", "digest": "…"}
```

`_prefix() -> int` (the highest `msg` id), called inside `LOCK_EX`:

1. `st = os.stat(log)`. If `log.check` parses and its `ino`, `size` and `mtime_ns` all equal `st`'s: the file is the one a previous writer validated and extended; return `last`.
2. Otherwise read and `parse` the whole file and build a throwaway `Box` from offset 0 (which also checks references). Damage raises `LogDamage` and nothing is written. Success: return the highest id.

After every successful append the writer rewrites `log.check` from a fresh `os.stat` (write a uniquely named temp file, `os.replace`; no fsync, since losing it only costs one full validation; a failure to publish it, cleanup included, never changes a committed result). Any change to the log that did not go through a writer, including one byte in an early line, changes `mtime_ns` or `size` and forces step 2. A missing log with no checkpoint is the empty log, id 0; a checkpoint without its log is damage, so ids are never reused. An unsafe `log.check` (a symbolic link, not a regular file) is an error; unparsable JSON, or fields that no longer match their `digest` (a SHA-256 of ino, size, mtime_ns, last and lineage), is only a miss. Timestamps (`at`) must be real UTC times, not only the right shape. Accepted limit: an outside edit that keeps the size and inode and restores the old mtime is not detected.

`lineage` names an unbroken run of writers' appends. A writer that proves the file through a matching checkpoint continues its lineage; one that had to validate in full starts a new one. `out` reads only the tail of a caller's `Box` when the box's last proven view belongs to the current lineage, and rebuilds any other box from the whole file: a matching checkpoint written after someone else's full validation proves nothing about an older view.

`Log(state, base)` opens the owner's cache base (which may be a symbolic link) and then each component below it, `factory-skill/<team>/<core>`, one at a time relative to its parent's descriptor with `O_NOFOLLOW`, creating it for a writer; every operation then opens, stats, replaces and syncs its files relative to the state directory's descriptor, so no Factory component is followed through a symbolic link. A writer checks that the state path still names the directory it holds: after locking, where a mismatch is `LogDamage` with nothing written; and after a committed message (its `ok` append) and after an outcome append, where a mismatch is an `OSError` saying the record is in the moved directory and to check before resending. The state directory must not be moved or replaced while Factory runs; a move between those checks is not prevented. Only writers create the directory and its files; readers create, chmod and rewrite nothing. The writer that creates `log.jsonl` first syncs the state directory's entry and each parent's up to the filesystem's root, whoever made the directories (a creator may have died right after `mkdir`); if that fails, nothing is stored.

### Append and commit

`_append(line: bytes) -> str` appends one record inside `LOCK_EX` and returns `"synced"` or `"visible"`, or raises:

1. `before = size of the file`. `fd = private_open(path, O_WRONLY | O_CREAT | O_APPEND)`; failure raises `NotStored(partial=False)`.
2. Write until done; a write that makes no progress is an `OSError`.
3. `os.fsync(fd)`, then fsync the directory. Success: return `"synced"`.
4. On a failure in step 2 or 3: try `os.ftruncate(fd, before)` and `os.fsync(fd)`, ignoring their errors. Then **look at what is actually in the file**: read from `before` to the end, still under the lock. The result depends only on that, never on which call failed:

| Bytes after `before` | Result |
|---|---|
| none | `NotStored(partial=False)`: the log is as before |
| exactly the line | return `"visible"`: the record is whole and every reader will see it; only its survival of a power loss is unknown |
| anything else | `NotStored(partial=True)`: readers will report damage |

`add`:

1. `last = _prefix()`; `id = last + 1`.
2. `_append(msg line)`. On `NotStored`: re-raise. On `"visible"`: stop and raise `NotStored(partial=False, "the disk reported an error; the entry was left uncommitted")`. The `msg` stays without an `ok`, so it is inert.
3. `_append(ok line)`. On `NotStored`: re-raise; the `msg` stays uncommitted and inert (with `partial`, readers report damage). On `"visible"`: the message **is** committed, because the keeper will read the `ok` and deliver it; return with the warning `stored and will be delivered, but the disk reported an error: it may not survive a power loss. Do not resend.`
4. Rewrite `log.check`; return `(id, offset, warning)`.

What this guarantees, including when a write, a sync and a rollback fail together, and when the writer is killed at any point: **a message is eligible for delivery if and only if its complete `ok` record is in the log (it may still end dropped or unconfirmed), and `say` reports it stored if and only if that record is there when `say` releases the lock**, as far as the tail can be read back. If the read-back of a failed `ok` itself fails, `add` raises an `OSError` naming the message ID and the log, "storage outcome unknown; do not resend; check inbox after repair": no `NotStored`, no retry, no checkpoint update, and no unknown-outcome record is appended (a complete `ok` may already be on disk). A complete `ok` found after repair is authoritative. An unreadable read-back of a `msg` is `NotStored(partial=True)` (it was never committed), and of an outcome an `OSError` that authorizes no handoff. Two gaps cannot be closed: a writer killed after writing `ok` and before printing (delivered, sender saw no result), and a power loss after a `"visible"` `ok` (the sender was warned).

Sentence proposed for spec §8.6: "If the disk reports an error after an entry is completely written and the entry cannot be removed, the message counts as stored and is delivered; `say` warns that it may not survive a power loss and must not be resent."

`out` uses `_prefix()`, then `read(box.offset)` and `apply`, then the check on the pair's current outcome, then `_append`:

- For `o == "sending"` only `"synced"` counts. A send is handed to `hc` only after its `sending` record is durable: otherwise a power loss could erase the claim while `msg` and `ok` survive, and the next keeper would type the message a second time. On `"visible"` `out` raises `NotStored(partial=False, "the sending record is not durable")` and the caller must not send ([keeper.md](keeper.md) §3).
- For every other outcome `"visible"` counts as written: those records only ever stop a send, so losing one to a power loss cannot cause a duplicate.

### What each writer appends

| Writer | Records | Notes |
|---|---|---|
| `say`, `up` (briefs, join news), `brief --send` | `msg` + `ok` | One message per `say`. Briefs are one message per member, since each member's text differs |
| keeper | `msg` + `ok` for notes; every `out` except the next row | [keeper.md](keeper.md) |
| `down` | `out` `dropped` for every pair that is `queued` to a member it stops | One `LOCK_EX` section: full read, build a `Box`, append. A pair that is `sending` is left to the keeper |

### Derived facts

Computed by `Box` from committed records only, so a crash between two steps of the keeper cannot leave them wrong:

| Fact | Definition |
|---|---|
| `briefed()` | `term` of every `delivered` outcome of a `brief` message. A member terminal is **ready** when its address is in this set |
| `last_note(term)` | The `meta` of the newest note whose `meta.term` is that terminal. A member note is sent only when the state the keeper observes differs from it in `what` or in `since` ([keeper.md](keeper.md) §5) |
| `quiet_note()` | The newest note with `meta.what == "quiet"` whose outcome for the operator is not `dropped` |
| `ready_note()` | The newest note with `meta.what == "ready"` whose outcome for the operator is not `dropped` |
| `deliveries` | The count of `delivered` outcomes of messages that are not notes, in a box replayed from offset 0 (a partial box keeps 0). The quiet note saves it, and a higher count re-arms the quiet rule ([keeper.md](keeper.md) §6) |

### `say`: waiting for outcomes

After `add` returns `(id, offset, _)`, `say` calls `read(offset)` every 0.2 s and applies the records to a box that holds only its own message, until every recipient's outcome is final or is `queued` with a reason other than `waiting`, or `SAY_WAIT` seconds pass. A recipient still `sending` or `queued (waiting)` at the deadline is printed as such.

### Message files (§8.7)

A file is **managed** when its parent directory is exactly `<state>/messages/<sender>`, its name matches `[A-Za-z0-9-]+\.[A-Za-z0-9]+`, and none of `<state>`, `messages`, the sender's directory is a symbolic link (v1's rule). `say` refuses a file in the sender's directory with any other name.

- The keeper unlinks a managed file when the last recipient of its message becomes `delivered`. A failed unlink is one pane line; the message is still delivered.
- `up`, when no member was running before it, lists leftovers: for each regular file in `messages/*/`, find the newest committed message whose `file` is that path. Delivered to everyone: unlink, print nothing. Otherwise print `leftover message file for @<sender> (<n> min old): <path>: message <id>: @a delivered, @b unconfirmed`, or `: never stored`, followed once by "Check before resending or discarding."

### `inbox` output

`inbox_rows(log, name, count)` selects the rows from one validated snapshot, keeping each message's original text after its outcomes are final.

`inbox [N] [--for NAME]` reads the whole log and prints the last N committed messages whose `to` contains the name, oldest first:

```
[Message 42 from @lead-dev at 2026-11-26T17:00:00Z: delivered]
    status: done …
```

The word after the time is that recipient's current outcome (`queued (blocked)` with its reason). With no match: `no messages for @NAME`.

## Dependencies

Depends on: the state directory and `private_open`. Used by: `say`, `up`, `down`, `brief`, `inbox`, `status`, the keeper.

## Test plan

No `hc` needed. Most are v1's `InboxTests` re-aimed at the new format.

Ids and content
- `test_first_id_is_one_and_ids_increase`, `test_ids_survive_keeper_and_team_restart`, `test_uncommitted_entry_uses_up_its_id`.
- `test_concurrent_adds_get_distinct_ids`: 8 processes, 25 adds each; 200 distinct increasing ids, each followed by its `ok`.
- `test_text_is_stored_byte_for_byte`: multi-line text with quotes, tabs, non-ASCII, a trailing `@name`, no final newline.
- `test_files_are_private`: `log.jsonl` and `log.check` are 0600 and there is no lock file, the directory 0700; a symbolic link in place of `log.jsonl` is refused.

Validation
- `test_incomplete_last_line_is_damage`, `test_invalid_json_is_damage`, `test_ids_out_of_order_is_damage`, `test_ok_without_its_msg_is_damage`: each names the line; `add` after each writes nothing.
- `test_out_for_unknown_message_is_damage`, `test_out_for_non_recipient_is_damage`, `test_out_for_uncommitted_message_is_damage`.
- `test_early_corruption_blocks_append`: a 2 MB log; flip one byte in line 3 (so mtime changes); `add` raises `LogDamage` naming line 3 and the file is unchanged.
- `test_checkpoint_skips_full_read`: with a matching `log.check`, `add` does no full read (spy on `Log._snapshot`); with a missing, stale or unparsable one it validates everything and rewrites it.
- `test_truncated_log_is_damage`: a reader holding an offset past the end reports it.

Commit, with faults injected by patching `os.write`, `os.fsync` and `os.ftruncate`
- `test_failed_open_is_not_stored_clean`.
- `test_short_write_is_rolled_back`: file size unchanged; the next `add` succeeds.
- `test_failed_fsync_is_rolled_back`: the complete line is removed.
- `test_msg_sync_and_rollback_both_fail`: `NotStored`; the `msg` line is in the file with no `ok`; a fresh `Box` holds no such message; `pending()` is empty; `uncommitted == 1`.
- `test_ok_sync_and_rollback_both_fail`: `add` returns the id with the warning; the message is in `pending()`.
- `test_truncate_succeeds_but_its_sync_fails`: the line is gone from the file, so the result is `NotStored(partial=False)` for `msg` and for `ok` alike, never a warning.
- `test_result_follows_file_content_not_the_failing_call`: for each combination of failing calls, the outcome matches the table above.
- `test_sending_requires_synced_record`: with the sync of a `sending` record failing and its rollback failing, `out` raises and writes no further record; with the same faults on a `delivered` record, `out` returns True.
- `test_partial_write_and_failed_rollback_is_damage`: `NotStored(partial=True)`; the next reader raises `LogDamage`.
- `test_writer_killed_between_msg_and_ok`: a child process is killed (SIGKILL) after the `msg` append; the parent's box holds no such message and the next `add` gets the next id.
- `test_writer_killed_holding_lock_leaves_valid_log`.

Outcomes and derived facts
- `test_default_outcome_is_queued_waiting`, `test_last_out_wins`, `test_final_outcomes`.
- `test_out_refuses_final_and_double_sending`, including when the other record was appended by another process after the box's offset.
- `test_down_drops_only_queued`.
- `test_briefed_from_delivered_briefs_only`: an `unconfirmed` or `dropped` brief does not count; the terminal ID is the one in the `delivered` record.
- `test_last_note_is_newest_per_terminal`.
- `test_quiet_and_ready_note_ignore_dropped`.
- `test_text_dropped_when_final_but_references_still_checked`.

Commands
- `test_say_wait_stops_when_settled`, `test_say_wait_reports_sending_at_deadline` (clock patched).
- `test_managed_file_rules`; `test_leftovers_listed_and_delivered_removed`.
- `test_inbox_filters_and_shows_outcome`, `test_inbox_for_name`, `test_inbox_works_without_hc`, `test_inbox_hides_uncommitted`.
