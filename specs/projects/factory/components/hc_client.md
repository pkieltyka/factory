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
← {"jsonrpc":"2.0","id":0,"result":{"generation":1,"framed":false,"version":"0.1.20","core":{"id":"2b0e…","name":"pax"},…}}
→ {"jsonrpc":"2.0","id":1,"method":"terminal.send","params":{"terminal":"t9","text":"…"}}
← {"jsonrpc":"2.0","id":1,"result":{"started":true,"status":"idle","turn":7}}
```

- **The request is written only after the `hello` reply has been read and checked.** An old core, or a core other than the team's, never receives a mutating call.
- Replies are read with `os.read` on the pipe's file descriptor into the client's own buffer, never through a buffered file object: a buffered `readline` can swallow a second line and leave `select` waiting on an empty descriptor.
- stdin stays open until the reply is read. Verified (LC-1, [hc-checks.md](../../../../docs/qualification/hc-checks.md)): closing stdin alone (EOF) does not cancel a held `terminal.send`; only dropping the `hc connect` connection (for example `kill_children()` / `SIGKILL`) cancels it.
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
    sent: bool       # True on the first positive write of the request line
    detail: str
    timeout: bool    # True only when a deadline expired

class Hc:
    def __init__(self, bin: str | None = None, timeout: float | None = 30, expect_core: str | None = None): ...
    version: tuple[int, ...] | None   # from hello, after the first call
    core: str | None                  # hello.core.id
    children: set                     # Popen objects of calls in progress, for kill_children()

    def call(self, method: str, params: dict | None = None, timeout: float | None = ...) -> dict
    def kill_children(self) -> None                               # kill every hc connect still running
    # thin wrappers; each is one call()
    def list(self, team: str) -> list[dict]                       # terminal.list {all: true, labels: {factory.team: team}} -> ["terminals"]
    def get(self, term: str) -> dict                              # terminal.get
    def run(self, argv: list[str], cwd: str, env: dict, labels: dict) -> dict   # terminal.run; always no_parent=True, no_notify=True; never name, keep, size
    def send(self, term: str, text: str, cond: str | None = None) -> dict   # terminal.send {terminal, text[, if]}; timeout=None
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

The default binary is resolved from `HC` at call time. The keeper uses `Hc(timeout=10)`, well below `BEAT_STALE`.

### `call`

One monotonic deadline covers both writes and both reads. With `timeout=None` (used by `send`), the client's default timeout still covers hello and writing the request. The reply can wait through a draft hold.

| Point | Failure | Result |
|---|---|---|
| Start `hc connect` | Missing binary or other launch error | `HcDown(sent=False)` |
| Hello | Write failure, EOF, deadline, malformed reply, old version or wrong core | `HcDown(sent=False)` |
| Request write | Deadline or write failure before any byte was accepted | `HcDown(sent=False)` |
| Request write | Deadline or write failure after a positive write | `HcDown(sent=True)` |
| Request reply | EOF, deadline or malformed reply | `HcDown(sent=True)` |
| Request reply | Valid JSON-RPC error | `HcError(kind, message, hint)` |

The hello reply must have a matching JSON-RPC envelope, an object result, generation 1, `framed: false`, and a non-empty core ID matching `expect_core` when set. Compare the version's leading dot-separated integers against `HC_MIN`, allowing a suffix starting with `-` or `+`. The request reply must have a matching envelope and an object result or error. An error requires an integer code and a string message. An absent or null error hint becomes an empty string.

Reads poll stdout and use `os.read` in a per-call buffer. Writes poll non-blocking stdin and use `os.write`, counting the first positive request write even if the rest fails. stderr goes to a temporary spool to avoid blocking on a full pipe. The last 2,000 bytes are available for transport errors.

`kill_children()` closes the client and kills every registered child. Launch and registration share its lock. Each call reaps its child, closes its descriptors and removes the child from the set, including on errors.

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
| `starting` | live, status null, `reported` false or absent |
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

Built in Phase 5a.

`check` must work with no core running, so it does not use `call`. It runs `hc --version` (parses `hc X.Y.Z`), and reads `draft_wait` from `$XDG_CONFIG_HOME/herdr-core/config.toml` (default `~/.config/herdr-core/config.toml`) with `tomllib`: a missing file or key means 60; a negative value means 0. It prints `hc 0.1.20, draft_wait 60 s` and, under 600, the warning "raise draft_wait to 600 or more in <path> so a report never lands in text you are typing". When `hc` is missing it says so and continues: `check` is an offline command.

## Dependencies

Depends on: the `hc` binary (`HC_BIN`). Used by: every team command, the keeper, launch and layout.

## Test plan

Against the fake `hc` ([testing.md](testing.md)):

- `test_call_returns_result`: `terminal.list` round trip; `version` and `core` are set.
- `test_old_hc_refused_before_request`: with hello 0.1.19 the fake received only `hello` and `HcDown.sent` is False.
- `test_wrong_core_refused_before_request`: `expect_core` differs; only `hello` was received.
- `test_call_maps_error_kind`: `HcError.kind`, message and hint are kept.
- `test_missing_binary`: `HcDown(sent=False)`.
- `test_reply_eof_is_sent`: fake exits after reading the request with `HcDown(sent=True)`.
- `test_reply_timeout_is_sent_and_kills_connect`: fake never answers, giving `HcDown(sent=True, timeout=True)` and no child left.
- `test_send_handshake_is_bounded` and `test_send_reply_outlives_setup_timeout` cover the two parts of an unbounded send.
- `test_fragmented_and_coalesced_replies`: the fake writes the reply in three chunks with pauses, and in another run writes extra bytes after the newline; both parse, and the timeout still applies across fragments.
- `test_kill_children_ends_held_send`: a held send's `hc connect` is gone after `kill_children()`.
- `test_wrapper_requests`: send omits `caller`, `force` and `if`. Run has `no_parent` and `no_notify` true and omits `name`, `keep`, `size` and `parent`.
- `test_view_states`: each row of the `state` table.
- `test_view_ignores_other_team`; `test_view_newest_live_wins`; `test_view_down_record_is_not_gone`.
- Phase 5a's `test_check_reads_draft_wait`: missing file gives 60 and a warning, 900 gives no warning, and a missing `hc` does not fail the formation check.

Against a real isolated core (`FACTORY_TEST_HC=real`): the first, the two refusal tests (with a fake version floor), and the `View` tests, using the fake agent.
