---
status: complete
---

# Component: hc client

## Purpose and scope

The only code that talks to `hc`. It turns JSON-RPC calls into Python dicts and typed errors, and builds the team view every command and the keeper use. It decides nothing about messages or members: how a send result becomes an outcome is the keeper's table ([keeper.md](keeper.md) §4). It never attaches to a terminal and never runs the `hc` CLI's own subcommands, except `hc --version` in `check`.

## Transport

One `hc connect` process per call (verified: V1 in [architecture.md](../architecture.md) §9), speaking one JSON object per line.

```json
→ {"jsonrpc":"2.0","id":0,"method":"hello","params":{"generation":1,"framed":false}}
← {"jsonrpc":"2.0","id":0,"result":{"version":"0.1.20","core":{"id":"2b0e…","name":"pax"},…}}
→ {"jsonrpc":"2.0","id":1,"method":"terminal.send","params":{"terminal":"t9","text":"…"}}
← {"jsonrpc":"2.0","id":1,"result":{"started":true,"status":"idle","turn":7}}
```

- **The request is written only after the `hello` reply has been read and checked.** An old core, or a core other than the team's, never receives a mutating call.
- Replies are read with `os.read` on the pipe's file descriptor into the client's own buffer, never through a buffered file object: a buffered `readline` can swallow a second line and leave `select` waiting on an empty descriptor.
- stdin stays open until the reply is read. What `hc` does with a pending send when its client goes away is unknown (LC-1), so the client never closes early on purpose.
- A call costs one process start plus about 1 ms. No connection is kept open, so there is no reconnect logic.
- `caller` is never passed. Only the keeper sends, its terminal reports no agent status, and `hc` tags a message only when the caller is an agent (hc-capabilities §10.2).

## Public interface

```python
class HcError(Exception):
    """hc answered with an error object."""
    kind: str        # data.kind: blocked, status_mismatch, not_found, finished, live, conflict, spawn_failed, unavailable, invalid_params, …
    message: str     # error.message, e.g. 't2 is blocked (permission)'
    hint: str        # data.hint or ""

class HcDown(Exception):
    """No usable answer: hc missing, connect failed, wrong or old core, EOF, timeout, or not JSON."""
    sent: bool       # True once the request line (not hello) was written
    detail: str

class Hc:
    def __init__(self, bin: str = HC, timeout: float = 30, expect_core: str | None = None): ...
    version: tuple[int, ...] | None   # from hello, after the first call
    core: str | None                  # hello.core.id
    children: set                     # Popen objects of calls in progress, for kill_children()

    def call(self, method: str, params: dict | None = None, timeout: float | None = ...) -> dict
    def kill_children(self) -> None                               # kill every hc connect still running
    # thin wrappers; each is one call()
    def list(self, team: str) -> list[dict]                       # terminal.list {all: true, labels: {factory.team: team}} -> ["terminals"]
    def get(self, term: str) -> dict                              # terminal.get
    def run(self, argv: list[str], cwd: str, env: dict, labels: dict) -> dict   # terminal.run; always no_parent=True, no_notify=True; never name, keep, size
    def send(self, term: str, text: str) -> dict                  # terminal.send {terminal, text}; timeout=None
    def keys(self, term: str, keys: list[str]) -> dict            # terminal.keys
    def read(self, term: str) -> str                              # terminal.read {lines: 0} -> ["text"]
    def label(self, term: str, labels: dict) -> dict              # terminal.label; "" removes a label
    def keep_off(self, term: str) -> None                         # terminal.keep {keep: false}
    def kill(self, term: str) -> dict                             # terminal.kill
    def rm(self, term: str) -> None                               # terminal.rm
    def doc_get(self) -> tuple[dict | None, int]                  # doc.get {name: "herdr-app"} -> (value, version)
    def doc_set(self, value: dict, version: int) -> int           # doc.set; HcError kind "conflict" on a stale version

def team_view(hc: Hc, team: str) -> View                          # architecture.md §3.4
```

The keeper builds its client as `Hc(timeout=10)`: every call it makes except `send` is bounded well below `BEAT_STALE`, so one slow call cannot make a healthy keeper look hung.

### `call`

With `deadline = now + timeout` (none when `timeout` is `None`):

1. `p = Popen([bin, "connect"], stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0)`; add `p` to `children`. `FileNotFoundError`: `HcDown(sent=False, "hc not found")`.
2. Write the `hello` line. `line = read_line(p, deadline)`.
3. Check it: it must be a JSON object with a `result`. Store `version` and `core`. If `version < HC_MIN`: `HcDown(sent=False, "hc 0.1.20 or newer is required (found X)")`. If `expect_core` is set and differs from `core`: `HcDown(sent=False, "this is another hc core (<id>) than the team's (<id>)")`. In both cases kill `p`; nothing else was written.
4. Write the request line. From here `sent` is True. `line = read_line(p, deadline)`.
5. `result` is returned; `error` raises `HcError(error.data.kind, error.message, error.data.hint)`.
6. Close stdin, `p.wait(2)` then kill; remove `p` from `children`.

`read_line(p, deadline)`: loop `select([fd], [], [], remaining)` then `os.read(fd, 65536)`, appending to a per-call buffer until it holds a `\n`; return the bytes before it and keep the rest. An empty read (EOF) or the deadline, at any fragment, kills `p` and raises `HcDown(sent=<as set>, stderr or "timed out")`.

Fields read from a `Terminal` record: `address`, `state` (`live` or `finished`), `status` (`state`, `kind`, `msg`), `reported`, `turn`, `status_ms`, `labels`, `keep`, `created_ms`, `finished_ms`, `reason`, `exit_code`, `signal`. Nothing else is relied on.

### `View`

Built from one `hc.list(team)`.

- `terminal(name)`: the live terminal with `factory.member == name` and `factory.kind` of `member` (or `operator` for the operator's name). If several are live the newest `created_ms` wins; the others go to `view.strays`, which `status` prints.
- `finished(name)`: the newest finished record with that member label and no `factory.down`, when there is no live one.
- `operator`: the live terminal with `factory.kind == "operator"`. `operator_lost`: the live one with `operator-lost`. `keeper`: the live one with `factory.kind == "keeper"`.
- `state(name)`:

| Result | Condition |
|---|---|
| `not started` | no record carries this member (never started, or stopped by `down`) |
| `gone` | no live terminal and a finished record exists; or a live terminal whose status is null although `reported` is true (its harness stopped reporting: a client turned `keep` on and `hc` put a shell there) |
| `starting` | live, status null, `reported` false |
| `idle`, `working`, `done`, `blocked`, `error` | live; `status.state` unchanged |

## Errors outside sends

| Call | Error | Handling |
|---|---|---|
| `run` | `spawn_failed`, `invalid_params` | That member failed to start: `up` reports the message, exit 3 |
| `kill`, `rm` | `not_found` | Already gone: success |
| `rm` | `live` | `kill` first, then `rm` once |
| `label`, `keep_off` | `not_found` | Ignore; the next view shows the terminal is gone |
| `doc_set` | `conflict` | Re-read and retry ([launch_layout.md](launch_layout.md) §5) |
| any | `HcDown` in a command | Exit 2 with "cannot reach hc: <detail>" |
| any | `HcDown` in the keeper's tick | One pane line per minute, keep ticking |

## `check`: version and `draft_wait`

`check` must work with no core running, so it does not use `call`. It runs `hc --version` (parses `hc X.Y.Z`), and reads `draft_wait` from `$XDG_CONFIG_HOME/herdr-core/config.toml` (default `~/.config/herdr-core/config.toml`) with `tomllib`: a missing file or key means 60; a negative value means 0. It prints `hc 0.1.20, draft_wait 60 s` and, under 600, the warning "raise draft_wait to 600 or more in <path> so a report never lands in text you are typing". When `hc` is missing it says so and continues: `check` is an offline command.

## Dependencies

Depends on: the `hc` binary (`HC_BIN`). Used by: every team command, the keeper, launch and layout.

## Test plan

Against the fake `hc` ([testing.md](testing.md)):

- `test_call_returns_result`: `terminal.list` round trip; `version` and `core` are set.
- `test_request_not_written_before_hello_is_checked`: the fake records what it received; with hello 0.1.19 it received only `hello`, and `HcDown.sent` is False.
- `test_wrong_core_refused_before_request`: `expect_core` differs; only `hello` was received.
- `test_call_maps_error_kind`: fake answers `blocked`; `HcError.kind`, message and hint kept.
- `test_missing_binary`: `HcDown(sent=False)`.
- `test_eof_after_request_is_sent`: fake exits after reading the request; `HcDown(sent=True)`.
- `test_timeout_kills_connect`: fake never answers; `HcDown(sent=True)` at the deadline and no process left.
- `test_fragmented_and_coalesced_replies`: the fake writes the reply in three chunks with pauses, and in another run writes extra bytes after the newline; both parse, and the timeout still applies across fragments.
- `test_kill_children_ends_pending_send`: a held send's `hc connect` is gone after `kill_children()`.
- `test_send_has_no_caller`: the recorded request has no `caller`, `force` or `if`.
- `test_run_always_parentless`: recorded `terminal.run` has `no_parent` and `no_notify` true and no `name`, `keep`, `size` or `parent`.
- `test_view_states`: each row of the `state` table.
- `test_view_ignores_other_team`; `test_view_newest_live_wins`; `test_view_down_record_is_not_gone`.
- `test_check_reads_draft_wait`: missing file gives 60 and a warning; 900 gives no warning; `hc` missing is reported without failing the formation check.

Against a real isolated core (`FACTORY_TEST_HC=real`): the first, the two refusal tests (with a fake version floor), and the `View` tests, using the fake agent.
