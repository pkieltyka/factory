---
status: draft
---

# Component: hc client

## Purpose and scope

The only code that talks to `hc`. It turns JSON-RPC calls into Python dicts and typed errors, and builds the team view every command and the keeper use. It does not decide anything about messages or members, and it never attaches to a terminal, subscribes to events, or runs the `hc` CLI's own subcommands (except `hc --version` in `check`).

## Transport

One `hc connect` process per call (verified: V1 in [architecture.md](../architecture.md) §9). Factory writes two lines to its stdin and reads two lines from its stdout:

```json
{"jsonrpc":"2.0","id":0,"method":"hello","params":{"generation":1,"framed":false}}
{"jsonrpc":"2.0","id":1,"method":"terminal.list","params":{"all":true,"labels":{"factory.team":"factory-ab12cd34"}}}
```

- stdin stays open until the second reply is read. Closing it early during a long `terminal.send` is untested (LC-1), so the client never does it.
- A call costs one process start plus about 1 ms. The keeper makes one `terminal.list` per tick and one call per send, so no persistent connection is kept: there is no reconnect logic to write.
- `caller` is never passed. Only the keeper sends, its terminal reports no agent status, and `hc` tags a message only when the caller is an agent (hc-capabilities §10.2), so messages carry Factory's `[from …]` line and nothing else.

## Public interface

```python
class HcError(Exception):
    """hc answered with an error object."""
    kind: str        # data.kind: blocked, status_mismatch, not_found, finished, live, conflict, name_taken, spawn_failed, unavailable, invalid_params, …
    message: str     # error.message, e.g. 't2 is blocked (permission)'
    hint: str        # data.hint or ""

class HcDown(Exception):
    """No usable answer: hc missing, connect failed, EOF, timeout, or not JSON."""
    sent: bool       # True once the request line was flushed to hc connect
    detail: str      # stderr of hc connect, or the reason

class Hc:
    def __init__(self, bin: str = HC): ...
    version: tuple[int, ...] | None   # from hello, after the first call
    core: str | None                  # hello.core.id

    def call(self, method: str, params: dict | None = None, timeout: float | None = 30) -> dict
    # thin wrappers; each is one call()
    def list(self, team: str) -> list[dict]                       # terminal.list {all: true, labels: {factory.team: team}} -> result["terminals"]
    def get(self, term: str) -> dict                              # terminal.get
    def run(self, argv: list[str], cwd: str, env: dict, labels: dict) -> dict   # terminal.run; always no_parent=True, no_notify=True; no name, keep, size
    def send(self, term: str, text: str, cond: str | None = None) -> dict       # terminal.send {terminal, text, if: cond}; timeout=None
    def keys(self, term: str, keys: list[str]) -> dict            # terminal.keys
    def read(self, term: str) -> str                              # terminal.read {lines: 0} -> result["text"]
    def label(self, term: str, labels: dict) -> dict              # terminal.label; "" removes a label
    def kill(self, term: str) -> dict                             # terminal.kill
    def rm(self, term: str) -> None                               # terminal.rm
    def doc_get(self) -> tuple[dict | None, int]                  # doc.get {name: "herdr-app"} -> (value, version)
    def doc_set(self, value: dict, version: int) -> int           # doc.set -> new version; HcError kind "conflict" on a stale version

def team_view(hc: Hc, team: str) -> View                          # architecture.md §3.4
```

`call` algorithm:

1. `Popen([bin, "connect"], stdin=PIPE, stdout=PIPE, stderr=PIPE)`. `FileNotFoundError` raises `HcDown(sent=False, "hc not found")`.
2. Write both lines, flush. From here `sent` is True.
3. Wait for each reply line with `select` on stdout up to `timeout` (no limit when `None`). On timeout or EOF: kill the process, raise `HcDown(sent=True, stderr)`.
4. First line is the `hello` result: store `version` and `core`; if `version < HC_MIN` raise `HcDown(sent=False, "hc 0.1.20 or newer is required (found X)")` without reading further.
5. Second line: `result` is returned; `error` raises `HcError(error.data.kind, error.message, error.data.hint)`.
6. Close stdin, `wait()` the process (2 s, then kill).

Fields read from a `Terminal` record: `address`, `state` (`live` or `finished`), `status` (`state`, `kind`, `msg`), `turn`, `status_ms`, `labels`, `created_ms`, `finished_ms`, `reason`, `exit_code`, `signal`. Nothing else is relied on.

### `View`

Built from one `hc.list(team)`. For a name, the live terminal is the one with `state == "live"`, `factory.member == name` and `factory.kind` of `member` (or `operator` for the operator's name); if several are live the newest `created_ms` wins and the others are listed in `view.strays`, which `status` prints. `finished(name)` is the newest finished record with that member label and no `factory.down`. `view.operator` is the live terminal with `factory.kind == "operator"`; `view.operator_lost` the live one with `operator-lost`. `view.keeper` is the live terminal with `factory.kind == "keeper"`.

## Mapping a send result to an outcome

Used by the keeper only.

| `hc.send` ends with | Outcome written | Why |
|---|---|---|
| a result | `delivered`, with `started` and `turn` from it | typed and submitted |
| `HcError` kind `blocked` | `queued`, reason `blocked` | `hc` typed nothing |
| `HcError` kind `status_mismatch` | `queued`, reason `busy` | only with `cond="idle"`; nothing typed (V9) |
| `HcError` kind `not_found` or `finished` | none now; the next tick decides `gone` or `dropped` from the view | the terminal ended before typing |
| any other `HcError` | `unconfirmed`, `why` = kind and message | not known to be before typing |
| `HcDown` with `sent=False` | none; the message stays `queued`, reason `waiting`, and is retried next tick | nothing reached `hc` |
| `HcDown` with `sent=True` | `unconfirmed` | `hc` may have typed |

## Other calls and their errors

| Call | Error | Handling |
|---|---|---|
| `run` | `spawn_failed`, `invalid_params` | That member failed to start: `up` reports the message, exit 3 |
| `kill`, `rm` | `not_found` | Already gone: success |
| `rm` | `live` | `kill` first, then `rm` once |
| `label` | `not_found` | Ignore; the next tick sees the terminal is gone |
| `doc_set` | `conflict` | Re-read and retry ([launch_layout.md](launch_layout.md) §5) |
| any | `HcDown` in a command | Exit 2 with "cannot reach hc: <detail>" |
| any | `HcDown` in the keeper's tick | Print one pane line per minute, keep ticking; sends in flight end `unconfirmed` |

## `check`: version and `draft_wait`

`check` must work with no core running, so it does not use `call`. It runs `hc --version` (parses `hc X.Y.Z`), and reads `draft_wait` from `$XDG_CONFIG_HOME/herdr-core/config.toml` (default `~/.config/herdr-core/config.toml`) with `tomllib`: a missing file or key means 60; a negative value means 0. It prints `hc 0.1.20, draft_wait 60 s` and, under 600, the warning "raise draft_wait to 600 or more in <path> so a report never lands in text you are typing". When `hc` is missing it says so and continues: `check` is an offline command.

## Dependencies

Depends on: the `hc` binary (`HC_BIN`). Used by: every team command, the keeper, launch and layout.

## Test plan

Against the fake `hc` ([testing.md](testing.md)):

- `test_call_returns_result`: `terminal.list` round trip; `version` and `core` are set.
- `test_call_maps_error_kind`: fake answers `blocked`; `HcError.kind == "blocked"`, message and hint kept.
- `test_old_hc_refused`: hello says 0.1.19; `HcDown(sent=False)` naming the found version.
- `test_missing_binary`: `HC_BIN` points nowhere; `HcDown(sent=False)`.
- `test_eof_after_request_is_sent`: fake exits after reading the request; `HcDown(sent=True)`.
- `test_timeout_kills_connect`: fake never answers; `HcDown(sent=True)` after the timeout and no process left.
- `test_send_has_no_caller`: the recorded request has no `caller` key and no `force`.
- `test_run_always_parentless`: recorded `terminal.run` has `no_parent` and `no_notify` true and no `name`, `keep` or `parent`.
- `test_view_states`: none, finished, live without status, live with each status word map to `not started`, `gone`, `starting` and the word.
- `test_view_ignores_other_team`: a terminal with another `factory.team` and the same member name never appears.
- `test_view_newest_live_wins`: two live terminals for one member; the newer is used and the older is in `strays`.
- `test_view_down_record_is_not_gone`: a finished record with `factory.down=1` gives `not started`.
- `test_check_reads_draft_wait`: missing file gives 60 and a warning; 900 gives no warning; `hc` missing is reported without failing the formation check.

Against a real isolated core (same tests, `FACTORY_TEST_HC=real`): the first, second, and the three `View` tests, using the fake agent.
