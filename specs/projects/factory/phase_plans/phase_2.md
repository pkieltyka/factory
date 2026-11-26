---
status: ready
issue: 166
phase: 2
branch: peter/166-foundation
owners: ["@architect", "@lead-dev"]
---

# Phase 2 Plan: Foundation (#166, with #177)

## 1. Goal and boundaries

Give later phases three things they can build on without touching `hc` details again: the `hc` client in `factory.py`, a fake `hc` and a fake agent that behave like the real ones, and a contract that proves it in CI against both. Plus the shared constants, the three new `factory.toml` keys, the fixture-scrubber fix (#177) and the v1 back-port.

Sources: [hc_client.md](../components/hc_client.md), [testing.md](../components/testing.md) §2–4 and §8, [architecture.md](../architecture.md) §3.4, §4, §5, [cli_skill.md](../components/cli_skill.md) §3, `docs/qualification/hc-checks.md`, and Phase 1's `tests/live.py`.

**In scope**

| File | Change |
|---|---|
| `skill/scripts/factory.py` | Add: the constants of architecture §5; `HcError`, `HcDown`, `Hc`; `View`, `team_view`; three optional `Config` keys. Nothing removed, no v1 behaviour changed |
| `tests/fake_hc.py` | New: the fake `hc` |
| `tests/fake_agent.py` | New: the scripted OSC 7501 agent |
| `tests/hc_fixtures.json` | Re-recorded with a new `schema` section (§4) |
| `tests/live.py` | `fixtures` records the schema extract; #177 scrubber fix |
| `tests/test_factory.py` | New classes: `ConstantsTests`, `HcClientTests`, `ViewTests`, `FakeHcTests`, `HcContract`, `FakeAgentTests`, `LiveToolTests`; additions to `ConfigTests`. v1's classes untouched and green |
| `.github/workflows/test.yml` | Second job: the same suite against the pinned real `hc` |
| `specs/…/components/hc_client.md`, `testing.md` | The doc changes listed in §8 |
| `v1` branch (separate PR) | Accept and ignore the three keys (§7) |

**Not in this phase** (each is named so nobody builds it by accident)

- No command uses `hc` yet. `up`, `say`, `status`, `down`, `wait` stay on herdr; v1's fake-herdr tests stay as they are. `Hc` and `team_view` have no production caller until Phase 4.
- The `check` additions of hc_client.md (`hc --version`, `draft_wait`) move to Phase 5a, where `check` is rewritten. Adding a line to v1's `check` output now would change v1 behaviour for no user.
- `plan()` keeps `inbox_dir` and `draft_dir`; the renames of cli_skill.md §3 come with their first user in Phase 5a.
- `tests/live.py` keeps its own `Conn` (it needs framed attach, a killable send and raw messages). It does not move onto `factory.Hc` in this phase.

**Safety invariant**, unchanged from Phase 1: no test or tool touches the owner's `hc` sessions. Real mode uses `live.Core` (private session, socket, runtime, state, config and home; core ID checked on every call; shutdown confirmed with a raw socket connect, never through `hc`).

## 2. `factory.py`: constants and `Config`

The constant block of architecture §5, verbatim, after v1's constants. `VERSION` is computed at import. All are added now so later phases only reference them.

`Config.__init__`: the allowed top-level keys gain `quiet_grace`, `ready_check`, `ready_check_every`. Validation, each a `SystemExit` naming the file and key like v1's other checks:

| Key | Accepts | Rejects | Attribute |
|---|---|---|---|
| `quiet_grace` | an `int` (not `bool`) of at least 30 | anything else | `cfg.quiet_grace`, default `QUIET_GRACE` |
| `ready_check` | a non-blank `str` | `""`, whitespace, non-strings | `cfg.ready_check`, default `None` |
| `ready_check_every` | an `int` (not `bool`) of at least 60 | anything else | `cfg.ready_every`, default `READY_EVERY` |

Nothing reads these attributes yet.

## 3. `factory.py`: `Hc`

Interface as hc_client.md, with two additions from Phase 1: `HcDown.timeout`, and `bin` resolved at call time.

```python
class HcError(Exception):   # kind, message, hint; str() is "kind: message"
class HcDown(Exception):    # sent: bool, detail: str, timeout: bool = False

class Hc:
    def __init__(self, bin: str | None = None, timeout: float | None = 30, expect_core: str | None = None)
        # bin None means the module's HC at call time, so tests can patch factory.HC
    version: tuple[int, ...] | None; core: str | None
    def call(self, method, params=None, timeout=...) -> dict     # ... means self.timeout; None means no limit
    def kill_children(self) -> None
    # wrappers exactly as hc_client.md: list, get, run, send, keys, read, label, keep_off, kill, rm, doc_get, doc_set
```

`Hc` reads `os.environ` (it has no `env` parameter). It is written fresh in `factory.py` from hc_client.md and the lessons of `live.Conn`; it does not import `tests/live.py`.

### What `call` raises, and when `sent` turns true

The two lines are fixed: `hello` is `{"jsonrpc":"2.0","id":0,"method":"hello","params":{"generation":1,"framed":false}}`; the request has `id` 1. Nothing is dispatched until the `hello` reply has been checked for all of: `id == 0`; `result` is an object; `result.generation == 1`; `result.framed is False`; `result.version` is a string of dot-separated integers; `result.core.id` is a non-empty string.

`sent` becomes True when the first raw `os.write` of the request line returns a positive count, and stays True whatever happens next. Until then nothing of the request has crossed the pipe: an `EPIPE` on that first write, or a deadline before any byte went, is `sent=False`, which the keeper may safely retry (keeper.md §4). After a partial write the request may be on its way, so every later failure is `sent=True`. One deadline covers the whole call (both writes and both reads); `None` means no deadline.

| Point | Failure | Raised |
|---|---|---|
| start `hc connect` | `FileNotFoundError`, `PermissionError`, other `OSError` | `HcDown(sent=False, "hc not found" or the error)` |
| write `hello` | `OSError` (broken pipe) | `HcDown(sent=False)` |
| write `hello` | deadline (the child does not read) | `HcDown(sent=False, timeout=True)` |
| read `hello` | deadline | `HcDown(sent=False, timeout=True)` |
| read `hello` | EOF | `HcDown(sent=False, <stderr>)` |
| parse `hello` | any of the checks above fails (not JSON, wrong `id`, an `error`, `framed` not false, version or core missing or of the wrong type) | `HcDown(sent=False, "unexpected hello reply: …")` |
| check `hello` | version below `HC_MIN` | `HcDown(sent=False, "hc 0.1.20 or newer is required (found X)")` |
| check `hello` | `expect_core` set and different | `HcDown(sent=False, "this is another hc core (<got>) than the team's (<expected>)")` |
| write request, no byte written yet | `OSError` (`EPIPE`) | `HcDown(sent=False)` |
| write request, no byte written yet | deadline | `HcDown(sent=False, timeout=True)` |
| write request, after a positive write | `OSError` | `HcDown(sent=True)`: part of the line may have reached `hc` |
| write request, after a positive write | deadline | `HcDown(sent=True, timeout=True)` |
| read reply | deadline | `HcDown(sent=True, timeout=True)` |
| read reply | EOF | `HcDown(sent=True, <stderr>)` |
| parse reply | not JSON; not an object; `id` not 1; neither `result` nor `error`; an `error` that is not an object, or whose `data` is present and not an object | `HcDown(sent=True, "unexpected reply: …")` |
| reply | `error` object | `HcError(data.kind or "", message, data.hint or "")` |

`timeout=True` only for the five deadline rows; EOF is never a timeout.

### The child process

- `Popen([bin, "connect"], stdin=PIPE, stdout=PIPE, stderr=<spool>, bufsize=0)`, where the spool is a `tempfile.TemporaryFile()`. stderr is never a pipe: an unread pipe that fills would block the child where no deadline can see it, above all on `send` with no timeout. After the process has ended the last 2,000 bytes of the spool go into `detail`.
- **Reads:** only `select` and `os.read` on stdout's descriptor into a per-call buffer; a line ends at `\n`, and bytes after it stay in the buffer.
- **Writes are deadline-aware too.** stdin's descriptor is set non-blocking (`os.set_blocking`); a line is written in a loop of `select` for writability with the time remaining, then `os.write` of what is left. A message can be 64 KiB, more than a pipe takes at once, so a child that stops reading must not hang a call that has a deadline. With `timeout=None` the same loop waits without limit.
- **Registration.** `children` is a set guarded by a `threading.Lock`, and the lock is held across `Popen` and the `add`, so `kill_children()` sees every live child. `kill_children()` takes the lock, sets `self.closed`, and kills every process in the set; a `call` that starts after that raises `HcDown(sent=False, "client closed")` without starting anything.
- On **every** exit from `call`, in a `finally`: on an exception `p.kill()` at once; otherwise close stdin and `p.wait(2)`, then `p.kill()` if it is still there. Always `p.wait()`, close the pipes and the spool, remove `p` from `children` under the lock.
- A `call` whose child is killed by `kill_children()` sees EOF or a failed write and raises `HcDown` with `sent` as it stood at that moment: False when the kill came during the `hello` exchange or before any request byte went, True afterwards (the case for a held send). Its own `finally` reaps the process. LC-1 showed this is what cancels a held send.

## 4. The contract: a schema extract in `hc_fixtures.json`

testing.md §4 already fixes the rule: required fields present, optional ones checked when present, unknown fields fail, types as the schema says, `null` only where the schema allows it. The rule needs the schema offline, since fake-mode CI has no `hc`.

`tests/live.py fixtures` records, next to `replies` and `errors`, a compact extract taken from `hc schema`:

```json
"schema": {
  "results": {"hello": "HelloResult", "terminal.get": "Terminal", "terminal.run": "RunResult", "terminal.list": "ListResult", "...": "..."},
  "types": {
    "Terminal": {
      "address": {"type": "string", "required": true},
      "name":    {"type": "string", "nullable": true},
      "status":  {"ref": "Status", "nullable": true},
      "labels":  {"type": "object", "values": {"type": "string"}, "required": true},
      "records": {"type": "object", "values": {"ref": "Status"}},
      "argv":    {"type": "array", "items": {"type": "string"}, "required": true}
    }
  }
}
```

- `results`: keyed by method name (`"terminal.get"`, not the fixture case `"terminal.get.live"`): for every method the fixtures hold a reply for, the name of its result type. Each fixture entry already carries its `method`, which is how a case finds its type.
- `types`: every type reachable from those, each field reduced to `type` (one of `string`, `integer`, `number`, `boolean`, `array`, `object`, or `any` for a field the schema leaves open, such as `doc.get`'s `value`) or `ref`, plus `items` for arrays, `values` for maps (from `additionalProperties`: `labels` is a map of strings, `records` a map of `Status`), `required`, `nullable`. In the 0.1.20 schema every `anyOf` in reach is one `$ref` plus `null`, so this set is enough (walked by @lead-dev). From the JSON Schema: `required` is membership in the type's `required` list; `nullable` is `"null"` in a `type` list or a `{"type": "null"}` branch of `anyOf`; `ref` is the last segment of `$ref`. The literal schema `true` (as for `DocGetResult.value`) becomes `{"type": "any"}`. The walk keeps a set of visited type names, because `WaitResult.children` is an array of `WaitResult`. A field the extractor cannot reduce this way fails the recording, so nothing is silently skipped.

The validator lives in `tests/test_factory.py`, about 30 lines, and is not a JSON Schema implementation:

```python
def conforms(value, spec: dict, types: dict, path: str = "") -> list[str]   # problems; empty means it conforms
```

Object of a named type: every required field present; every present field known and conforming; `None` accepted only for a nullable field. `integer` rejects `bool`; `number` accepts `int` and `float`; `object` with `values` checks every value against it and without it accepts any dict; `any` accepts anything, `None` included (the open schema `true` does); `array` checks each item against `items`. Errors are checked against a fixed shape: `code` int, `message` str, `data.kind` str, `data.hint` str when present.

## 5. Test doubles

### `tests/fake_hc.py`

As testing.md §2, with one design rule that keeps it honest: **every reply is built from the recorded reply of the same name** in `hc_fixtures.json` (loaded from beside the script), with the varying fields overwritten. A terminal record starts as a copy of `replies["terminal.get.live"].result`; an error is a copy of `errors[kind].error` with the message rewritten. Re-recording the fixtures for a new `hc` therefore moves the fake with it.

- CLI: `--version` prints `hc <FAKE_HC_VERSION or the fixtures' hc_version>`; `connect` serves JSON lines until EOF; anything else exits 2.
- State: `$FAKE_HC_DIR/state.json` under `flock`, shape as testing.md §2, plus `received` (every line read, `hello` included) and `polls` (per terminal, the count of `get`/`list` reads, for `on_run`'s "after n polls").
- It answers `hello` before reading the next line.
- Methods and mirrored behaviour: testing.md §2. `terminal.send` with a `hold` rule polls the state file every 20 ms and records the text as typed only when released; if the process is killed first, nothing is recorded (LC-1). `die` exits after reading the request. `set_status` in the helper keeps `turn` (plus one on leaving `working`), `status_ms` and `reported` consistent.
- Test-side helper `FakeHc` in `test_factory.py`: the methods of testing.md §2.

### `tests/fake_agent.py`

As testing.md §3: raw mode, bracketed paste on, `ESC ] 7501 ; state=<word>:app=fake ESC \`, every submitted input appended to `$FAKE_LOG` with a timestamp, the seven scripts, and `FAKE: <script>` in a message to switch. The `reports` script runs the argv in `$FAKE_REPLY_CMD` (a JSON list) after its work when that variable is set, and does nothing more when it is not; Phase 4 sets it to `factory.py say`. It starts from the probe in `tests/live.py`, which stays where it is.

### Modes

- **Fake mode (default).** Each test that needs `hc` gets a temp `FAKE_HC_DIR` and patches `os.environ` and `factory.HC` for its own duration (`unittest.mock.patch`). Nothing global.
- **Real mode (`FACTORY_TEST_HC=real`).** `setUpModule` imports `tests/live.py` and starts one `live.Core("unit", extra_env={"HC_SETTLE_MS": "0"})`; `tearDownModule` calls its `stop()`. Tests reach it the same way as in fake mode: a per-test patch of `os.environ` with that core's `HC_SESSION`, `HC_SOCKET`, `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, with `HC_TERMINAL_ID` removed, and `Hc(expect_core=core.id)`. The process's own environment is never changed outside a test, so v1's classes run exactly as in fake mode.
- **Tests share the real core, so each isolates its own records.** Every test that starts terminals uses a team label of its own (`"t-" + uuid4().hex[:8]`) for `factory.team` and for every `hc.list`, and its `tearDown` kills and removes what it started. A test that writes the `herdr-app` doc restores it to its previous value and checks the version it wrote against. The environment patch selects the core; the labels and the cleanup are what keep tests apart.
- **The restart test has its own core.** `test_view_gone_after_core_restart` creates a second `live.Core("restart")`, stops it with `server_stop()`, waits until `listening()` is False on the raw socket (never by asking `hc`, which would start it again), and only then calls `start()`. It never restarts the shared core.
- Markers: `@fake_only` (skips in real mode: it scripts the fake), `@real_only` (skips in fake mode: it needs a PTY). Unmarked `hc` tests run in both. Every skip carries its reason, and the end of the run in real mode prints how many `hc` tests ran against the real core.

## 6. Tests

Named so a reviewer can tick them off. "both" means fake and real mode.

**`ConstantsTests`**: `test_version_is_sha1_of_script`.

**`ConfigTests` additions**: `test_new_keys_default`; `test_new_keys_accepted`; one rejection test per row of §2's table (`bool`, below the minimum, wrong type, blank `ready_check`); `test_unknown_top_level_key_still_rejected`; `test_v1_sample_loads_unchanged`.

**`HcClientTests`**
- both: `test_call_returns_result`; `test_call_maps_error_kind` (`terminal.get` of `t999`: `not_found`, message and hint kept); `test_wrong_core_refused_before_request`; `test_old_hc_refused_before_request` (fake: hello says 0.1.19 and `received()` holds only `hello`; real: `factory.HC_MIN` patched to `(99,)` and the terminal list is unchanged afterwards).
- fake only, one per row of §3's table: `test_missing_binary`; `test_hello_timeout_is_unsent`; `test_hello_eof_is_unsent`; `test_malformed_hello_is_unsent` (one sub-case per check: not JSON, `id` 1, an error object, `generation` 2, `framed: true`, no version, a non-string version, no core id), each asserting `received()` holds only `hello`; `test_request_epipe_before_any_byte_is_unsent` and `test_request_failure_after_partial_write_is_sent` (both by patching `factory.os.write` for the request line only: the first raises `BrokenPipeError` on the first call; the second returns a short count, then raises. A fake that exits after `hello` would race the writer, so it is not used here); `test_request_deadline_before_any_byte_is_unsent_timeout`; `test_blocked_write_hits_the_deadline` (the fake stops reading; a 200 KiB request with a 0.5 s timeout raises `HcDown(sent=True, timeout=True)`); `test_stderr_flood_does_not_block` (the fake writes 1 MiB to stderr before answering; the call succeeds, and on a failure `detail` holds the tail); `test_reply_timeout_is_sent_and_kills_connect`; `test_reply_eof_is_sent`; `test_malformed_reply_is_sent` (not JSON, wrong `id`, neither key, `error` a string, `error.data` a list).
- fake only: `test_timeout_flag_only_on_deadlines`; `test_fragmented_and_coalesced_replies`; `test_one_deadline_covers_hello_and_reply`.
- fake only: `test_no_child_left_after_any_outcome`: after each case above and after a success, `hc.children` is empty and the `hc connect` process has been reaped.
- fake only: `test_kill_children_ends_held_send`: a held send in a thread; `kill_children()`; the thread gets `HcDown(sent=True)` (the request was written), the fake recorded no typed text, no process is left. `test_kill_during_hello_is_unsent`: the fake delays its `hello` reply; `kill_children()`; the call raises `HcDown(sent=False)`. `test_no_call_after_kill_children`: a later `call` raises `HcDown(sent=False)` and starts no process. `test_kill_children_sees_a_child_being_started`: with `Popen` patched to pause, a concurrent `kill_children()` waits for the lock and kills that child.
- fake only: `test_wrapper_requests`: each wrapper sends the method and parameters of hc_client.md; `send` has no `caller`, `force` or `if`; `run` has `no_parent` and `no_notify` true and no `name`, `keep`, `size`, `parent`.

**`ViewTests`** (fake; terminals built with `FakeHc.add`): the tests of hc_client.md (`test_view_states` with one case per row, `test_view_ignores_other_team`, `test_view_newest_live_wins`, `test_view_down_record_is_not_gone`), plus `test_view_operator_keeper_and_lost`. Real only: `test_view_states_with_fake_agents` (silent, idle, blocked, exited) and `test_view_gone_after_core_restart` (`reason: server_stop`, labels kept).

**`FakeHcTests`** (fake only): `test_hold_and_release`; `test_killed_held_send_types_nothing`; `test_die_gives_no_reply`; `test_on_run_status_after_polls`; `test_doc_conflicts_rule`; `test_calls_sent_and_received`; `test_unknown_method_is_invalid_params`.

**`HcContract`** (replaces `HerdrContract` in purpose; v1's class stays until herdr goes)
- fake mode: `test_recorded_replies_conform` (every reply and error in the fixtures against the extract); `test_fake_replies_conform` (drives every fake method, success and each error kind); `test_validator_rejects` (a missing required field, an unknown field, a wrong type, `null` in a non-nullable field, `bool` for `integer`, a non-string label value, a `records` value that is not a `Status`); `test_any_accepts_null`.
- real only: `test_real_replies_conform` (the same drive against the real core).
- both, the mirrored behaviours: `test_send_types_into_statusless_terminal` (`started` is null); `test_send_to_blocked_is_refused`; `test_turn_goes_up_on_leaving_working`; `test_kill_then_rm` and `test_rm_live_is_refused`; `test_label_on_finished_record_and_empty_removes`; `test_doc_set_stale_version_conflicts`.

**`FakeAgentTests`** (real only): one test per script of testing.md §3, plus `test_fake_line_switches_script` and `test_log_has_every_input`.

**`LiveToolTests`** (#177, no `hc`; `live.scrubber` with a stub core and patched host and user names)
- `test_identity_fields_redacted_by_position`: `core.name` of `ci`, of one character, and of a value that is **not** this machine's hostname all become `<host>`. `login_session.user` and `login_session.id`, which the 0.1.20 schema declares as integers (a UID and a session number), become `1`; a string there (another `hc` version) becomes `<user>`.
- `test_leaks_flags_any_unredacted_identity_field`: a `core.name` other than `<host>`, and a `login_session.user` or `id` other than `1` or `<user>`, are reported, whatever their length or value.
- `test_placeholders_are_not_leaks`.
- `test_short_names_left_alone_inside_paths`: with user `ci` and with host `ci`, `/srv/ci/build` is unchanged and is not a leak (today only the user has the length guard; the host gets it too).
- `test_protocol_words_that_look_like_names_survive`: with host `idle` and user `live`, `status.state`, `state`, `reason` and label values are unchanged.
- `test_scrubbed_fixture_still_conforms`: a reply with a non-null `login_session` is scrubbed and still passes `conforms` (types are kept).

The fix itself: in `scrub` and `leaks`, the values at `("core", "name")`, `("login_session", "user")` and `("login_session", "id")` are replaced by position alone, with a placeholder of the same JSON type; the two-character minimum stays, for host and user alike, only for whole-word replacement inside values that contain `/`.

## 7. CI and the v1 back-port

**`.github/workflows/test.yml`**: keep job `test` as it is. Add job `test-real-hc` on `ubuntu-latest` with LC-9's steps exactly: download `https://releases.herdr.dev/core/0.1.20/herdr-core-linux-x86_64.tar.gz`, check SHA-256 `ac6b86890c451a37584620aaa438b71bc0d0bd430a2e7c1d7e095f052c414e74`, extract `herdr-core`, link it as `hc`, add its directory to `GITHUB_PATH`, then `FACTORY_TEST_HC=real python3 tests/test_factory.py`. Both jobs run the whole file, so v1's tests are green in both.

**v1 back-port** (its own worktree, branch `peter/166-v1-config` from `origin/v1`, already created by @lead-dev; PR against `v1`): in `Config.__init__` add the three names to the allowed top-level keys; no validation, no attributes. One test, `test_v2_keys_are_accepted_and_ignored`. v1's templates and docs do not change: v1 users never set these keys.

## 8. Doc changes in this pull request

- `hc_client.md`: `HcDown.timeout`; the table of §3 in place of the six-step prose where they differ ("`sent` turns true when the first write of the request returns a positive count", the hello checks, one deadline for the whole call, deadline-aware writes, stderr spooled to a file, `closed` after `kill_children()`); `bin` resolved at call time; a note under "`check`" that it is built in Phase 5a.
- `testing.md` §2: the fake builds replies from the recorded ones; `received`, `polls`. §4: real mode uses `live.Core` and per-test environment patches (it no longer sets the process environment in `setUpModule`); the schema extract and `conforms`; the two markers. §3: `FAKE_REPLY_CMD`. §8: the second CI job exists.

## 9. Order of commits

Each commit leaves `python3 tests/test_factory.py` green. @lead-dev-2 and @lead-dev-3 review each as it lands.

1. This plan; constants; `Config` keys; their tests.
2. `live.py`: #177 fix with `LiveToolTests`.
3. `live.py fixtures`: the schema extract; re-recorded `hc_fixtures.json`; `conforms`; `test_recorded_replies_conform`, `test_validator_rejects`.
4. `HcError`, `HcDown`, `Hc`; a first `fake_hc.py` (`hello`, `terminal.list`, `terminal.get`, the failure rules); `HcClientTests`.
5. The rest of `fake_hc.py` and `FakeHc`; `FakeHcTests`; `test_fake_replies_conform`; the mirrored-behaviour tests in fake mode.
6. `View`, `team_view`; `ViewTests`.
7. `fake_agent.py`; real mode (`live.Core`, markers); `FakeAgentTests`; the real-mode halves of the tests above.
8. The CI job; the doc changes of §8.
9. Separately: the v1 back-port branch and its pull request.

## 10. Acceptance

- `python3 tests/test_factory.py` passes locally and in CI job `test`; `FACTORY_TEST_HC=real python3 tests/test_factory.py` passes locally and in CI job `test-real-hc`; the real run reports how many `hc` tests ran against the real core and no v1 test is skipped that ran before.
- Every row of §3's table and of §2's table has its named test.
- `git grep -n "herdr(" skill/scripts/factory.py` is unchanged: no v1 code path was edited; `factory.py` grows by about 200 lines.
- `tests/hc_fixtures.json` has no leak by `live.py`'s own check, and holds the `schema` section.
- #177's three acceptance criteria are met by `LiveToolTests`.
- The v1 pull request is open against `v1` with its test green.

## 11. Risks

- **The fake drifts from `hc`.** Limited three ways: replies are built from recorded ones, both are held to the same extract, and the mirrored-behaviour tests run in both modes in CI.
- **Real mode is slow or flaky on the runner.** `HC_SETTLE_MS=0` and one core for the whole module; LC-9's run took 11 s. A real-only test that needs a timer uses the fake agent's `FAKE_WORK`, never a sleep over a second.
- **Sandboxed writers.** @lead-dev's Codex sandbox must reach a Unix socket under the temp directory for real mode; if it cannot, real mode is run by a follower and in CI, and the report says who ran it.
- **`hc connect` restarts a stopped core** (Phase 1's learning). Nothing in this phase stops a core except `live.Core.stop`, which already confirms on the socket.
