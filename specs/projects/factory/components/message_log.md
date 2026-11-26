---
status: draft
---

# Component: Message log

## Purpose and scope

Durable storage for every message and every per-recipient outcome (§8.5–8.7): the append-only file, its lock, the in-memory index built from it, and the handling of message files. It does not type, decide deliverability, or talk to `hc`.

## Storage format

`<state>/log.jsonl`, UTF-8, one JSON object per line, appended only. Two record types.

```json
{"t":"msg","id":42,"at":"2026-11-26T17:00:00Z","from":"lead-dev","to":["operator","reviewer"],"kind":"say","text":"status: done …","file":"/…/messages/lead-dev/operator-done-1.md"}
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
| `what`, `about` | str or absent | Notes only: `what` is `blocked`, `error`, `exit`, `quiet` or `ready`; `about` is the member a member note concerns |

| `out` field | Type | Meaning |
|---|---|---|
| `id`, `to` | int, str | The message and one of its recipients |
| `o` | str | `sending`, `delivered`, `queued`, `unconfirmed` or `dropped` |
| `why` | str or absent | `queued`: `waiting`, `blocked`, `busy`, `not ready` or `gone`. `unconfirmed` and `dropped`: a short reason |
| `term` | str or absent | Terminal ID the send went to |
| `started`, `turn` | bool or null, int | `delivered` only, from `hc`'s result |

The current outcome of (message, recipient) is its last `out` record. With none it is `queued`, reason `waiting`: a `msg` record alone queues the message for all its recipients. `delivered`, `unconfirmed` and `dropped` are final. `refused` and `not stored` are never written: nothing was stored.

Allowed transitions, enforced by the writers, not by the reader:

```
queued ──▶ sending ──▶ delivered
   ▲          ├──────▶ unconfirmed
   │          ├──────▶ dropped        (down stopped the recipient, nothing typed; or a quiet/ready note hc refused)
   └──────────┘                       (hc refused before typing: back to queued with hc's reason)
queued ──▶ dropped                    (down; or a brief superseded by a newer brief)
```

## Locking and the commit rule

`<state>/lock` is the `flock` target for both `log.jsonl` and `team.json`. Readers take `LOCK_SH`, writers `LOCK_EX`. The lock is held only around file operations, never across an `hc` call or a sleep (v1's rule).

**A record is committed when its writer releases the lock.** Because every reader reads under the lock, no reader ever sees a record that is later rolled back.

## Public interface

```python
class LogDamage(Exception):
    def __init__(self, path, line: int, reason: str): ...
    # str(): "message log needs repair: <path>, line <n>: <reason>. Nothing was written. Repair or move the file, then run factory.py up."

class NotStored(Exception):
    partial: bool     # True: the rollback failed and the log may hold a partial line
    detail: str

class Log:
    def __init__(self, state: pathlib.Path): ...
    def read(self, offset: int = 0) -> tuple[list[dict], int]
        # LOCK_SH. Records from byte offset to the end, and the new offset. Raises LogDamage.
    def add(self, sender: str, to: list[str], text: str, kind: str = "say",
            file: str | None = None, what: str | None = None, about: str | None = None) -> tuple[int, int]
        # LOCK_EX. Checks the tail, allocates the id, appends one msg record. Returns (id, offset just before the record).
        # Raises LogDamage (nothing written) or NotStored.
    def out(self, box: "Box", id: int, to: str, o: str, **extra) -> bool
        # LOCK_EX. Reads and applies records after box.offset, then appends the out record only if the
        # current outcome is not final and (for o == "sending") is "queued". Returns whether it wrote.
        # Raises LogDamage, NotStored.

class Box:
    """In-memory index of the log."""
    offset: int
    msgs: dict[int, dict]                       # id -> msg record; text is dropped once every recipient is final
    def apply(self, records: list[dict]) -> None
    def outcome(self, id: int, to: str) -> dict # the last out record, or {"o": "queued", "why": "waiting"}
    def pending(self, to: str | None = None) -> list[tuple[dict, str]]   # (msg, recipient) pairs not final, by id
    def last_to(self, name: str) -> dict | None     # newest msg delivered to name
    def last_from(self, name: str) -> dict | None   # newest msg sent by name
```

## Algorithms

### Reading and validating

`parse(raw: bytes, first_line: int, strict: bool) -> list[dict]`:

1. Empty is valid. Otherwise `raw` must end with `\n`, else `LogDamage(line = count + 1, "incomplete line")`.
2. Each line must decode as a JSON object with `t` of `msg` or `out`.
3. `msg`: `id` is an int greater than the previous `msg` id in this parse; `at`, `from`, `text` are strings; `to` is a non-empty list of strings; `kind` is one of the three.
4. `out`: `id` is an int of at least 1; `to` is a string; `o` is one of the five. With `strict`, `id` must not exceed the highest `msg` id seen.
5. Any violation raises `LogDamage` with the 1-based line number and what was expected.

`read(0)` parses the whole file with `strict=True`. `read(offset > 0)` parses from that offset with `strict=False` (earlier messages are outside the slice); the offset is always one this process got from a previous `read` or `add`, so it sits on a line boundary. If the file is now shorter than `offset`, that is damage ("log was truncated").

### Checking the tail before an append

`add` and `out` do not read the whole file. They read the last 256 KiB (the whole file when smaller), drop everything up to and including the first `\n` when the window starts mid-file, and `parse` the rest with `strict=False`. That catches the damage a crash can cause, a cut-off last entry, at a cost that does not grow with the log. The next id is the highest `msg` id in the window plus one; if the window holds no `msg` record and does not start at byte 0, the window doubles and the step repeats. Full validation runs in the keeper at start, and in `status`, `inbox` and `up`.

### Append protocol

Inside `LOCK_EX`, with `before` = the file's size:

1. Build the payload: `json.dumps(record, ensure_ascii=False) + "\n"`, encoded as UTF-8.
2. `fd = private_open(path, O_WRONLY | O_CREAT | O_APPEND)`. A failure here raises `NotStored(partial=False)`: nothing was written.
3. Write until the payload is done; a write that makes no progress is an `OSError`.
4. `os.fsync(fd)`, then fsync the directory (v1's `sync_directory`).
5. If step 3 or 4 fails: `os.ftruncate(fd, before)` and `os.fsync(fd)`. If both succeed, raise `NotStored(partial=False)`; the log is exactly as before, and whatever a failed fsync left visible is gone. If the truncate fails, raise `NotStored(partial=True)`; later readers will report the damage.

This distinguishes the three cases §8.6 names: nothing stored (step 2), a partial write (step 3) and an uncertain sync (step 4), and "checks what became visible" by removing it under the lock. A message reported as `not stored` therefore can never be delivered.

### What each writer appends

| Writer | Records | Notes |
|---|---|---|
| `say`, `up` (briefs, join news), `brief --send` | `msg` | One `msg` per `say`. Briefs are one `msg` per member, since each member's text differs |
| keeper | `msg` of kind `note`; every `out` except the next row | See [keeper.md](keeper.md) |
| `down` | `out` `dropped` for every pair that is `queued` to a member it stops | One `LOCK_EX` section: full read, build a `Box`, append. A pair that is `sending` is left to the keeper |

### `say`: waiting for outcomes

After `add` returns `(id, offset)`, `say` calls `read(offset)` every 0.2 s and applies the records to a `Box`, until every recipient's outcome is final or is `queued` with a reason other than `waiting`, or `SAY_WAIT` seconds pass. A recipient still `sending` or `queued (waiting)` at the deadline is printed as such.

### Message files (§8.7)

A file is **managed** when its parent directory is exactly `<state>/messages/<sender>`, its name matches `[A-Za-z0-9-]+\.[A-Za-z0-9]+`, and none of `<state>`, `messages`, the sender's directory is a symbolic link (v1's rule). `say` refuses a file in the sender's directory with any other name ("use letters, digits and dashes, plus a plain extension").

- The keeper unlinks a managed file when the last recipient of its message becomes `delivered`. A failed unlink is one pane line; the message is still delivered.
- `up`, when no member was running before it, lists leftovers: for each regular file in `messages/*/`, find the newest `msg` whose `file` is that path. Delivered to everyone: unlink, print nothing. Otherwise print `leftover message file for @<sender> (<n> min old): <path>: message <id>: @a delivered, @b unconfirmed`, or `: never logged`, followed once by "Check before resending or discarding."

### `inbox` output

`inbox [N] [--for NAME]` reads the whole log and prints the last N `msg` records whose `to` contains the name, oldest first:

```
[Message 42 from @lead-dev at 2026-11-26T17:00:00Z: delivered]
    status: done …
```

The word after the time is that recipient's current outcome (`queued (blocked)` with its reason). With no match: `no messages for @NAME`.

## Dependencies

Depends on: the state directory and `private_open`. Used by: `say`, `up`, `down`, `brief`, `inbox`, `status`, the keeper.

## Test plan

No `hc` needed. Most are v1's `InboxTests` re-aimed at the new format.

- `test_first_id_is_one_and_ids_increase`, `test_ids_survive_keeper_and_team_restart`.
- `test_concurrent_adds_get_distinct_ids`: 8 processes, 25 adds each; 200 distinct increasing ids, no torn lines.
- `test_text_is_stored_byte_for_byte`: multi-line text with quotes, tabs, non-ASCII, a trailing `@name`, no final newline.
- `test_files_are_private`: `log.jsonl` and `lock` are 0600, the directory 0700; a symbolic link in place of `log.jsonl` is refused.
- `test_incomplete_last_line_is_damage`, `test_invalid_json_is_damage`, `test_ids_out_of_order_is_damage`, `test_out_for_unknown_message_is_damage`: each names the line; `add` after each writes nothing.
- `test_truncated_log_is_damage`: a reader holding an offset past the end reports it.
- `test_failed_open_is_not_stored_clean`: nothing written, `partial` false.
- `test_short_write_is_rolled_back`: patch `os.write` to fail mid-payload; file size is unchanged, `NotStored(partial=False)`, the next `add` succeeds and reuses no reported id.
- `test_failed_fsync_is_rolled_back`: the complete line is removed; a concurrent reader never saw it.
- `test_failed_rollback_reports_partial`: patch `os.ftruncate` to fail; `NotStored(partial=True)`, and the next reader raises `LogDamage`.
- `test_tail_check_matches_full_read`: a 2 MB log; `add` reads only the window (patch `os.read` sizes) and allocates the right id; a window holding only `out` records widens.
- `test_default_outcome_is_queued_waiting`, `test_last_out_wins`, `test_final_outcomes`.
- `test_out_refuses_final_and_double_sending`: `out(…, "sending")` on a pair already `sending`, `delivered` or `dropped` returns False and writes nothing, including when the other record was appended by another process after the box's offset.
- `test_down_drops_only_queued`: `queued` pairs become `dropped`; a `sending` pair is untouched.
- `test_say_wait_stops_when_settled` and `test_say_wait_reports_sending_at_deadline` (clock patched).
- `test_managed_file_rules`: own directory and good name is managed; another member's directory, an outside path, a bad name and a symlinked directory are not.
- `test_leftovers_listed_and_delivered_removed`: three files: delivered to all (removed silently), partly delivered (listed with outcomes), never logged (listed).
- `test_inbox_filters_and_shows_outcome`, `test_inbox_for_name`, `test_inbox_works_without_hc`.
