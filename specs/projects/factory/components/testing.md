---
status: complete
---

# Component: Testing

## Purpose and scope

How every behaviour in the functional spec is proven: automated tests that run in CI against a fake `hc`, the same tests against a real isolated core, the live qualification of each harness (§10), and the end-to-end autonomy runs. It names the test doubles and their interfaces; the test cases themselves are listed in each component's test plan.

## 1. Files

| File | Role |
|---|---|
| `tests/test_factory.py` | All automated tests, standard `unittest`, run with `python3 tests/test_factory.py` (v1's entry point and CI step) |
| `tests/fake_hc.py` | The fake `hc`, about 300 lines |
| `tests/fake_agent.py` | A program that reports OSC 7501 and plays a scripted agent |
| `tests/live.py` | Private core helper, fixture recorder, and driver for live qualification and end-to-end runs |
| `tests/gate1-setup.sh` | v1's cold-recovery fixture, kept (§9.5) |
| `tests/hc_fixtures.json` | Replies recorded from `hc 0.1.20`, for the contract tests |

## 2. Fake `hc`

An executable Python script; tests set `HC_BIN` to it. It implements `--version` (prints `hc <FAKE_HC_VERSION or 0.1.20>`) and `connect`: read JSON lines on stdin, answer each on stdout, exit at EOF. State is `$FAKE_HC_DIR/state.json`, read and written under `flock`, so the many `connect` processes of one test share it.

```json
{"core": "fakecore00000000", "next": 1,
 "terminals": {"t1": {"address": "t1", "state": "live", "status": null, "turn": 0, "labels": {}, "argv": [], "cwd": "", "env": {},
                      "created_ms": 0, "status_ms": null, "finished_ms": null, "reason": null, "exit_code": null, "screen": ""}},
 "docs": {"herdr-app": {"value": null, "version": 0}},
 "calls": [{"method": "terminal.send", "params": {}}],
 "rules": {"send": {"t1": {"hold": true}}, "run": {}, "doc_conflicts": 0}}
```

Methods: `hello`, `terminal.run`, `.list` (`all`, `labels`), `.get`, `.send`, `.keys`, `.read`, `.label`, `.keep`, `.kill`, `.rm`, `doc.get`, `doc.set`. It answers `hello` before it reads the next line and records every line it receives, so a test can prove that a refused call never reached it. Anything else answers `invalid_params`, which fails the test that caused it. Results match the `hc` schema and the examples recorded in `hc_fixtures.json` (§4); errors use `hc`'s shape and kinds.

Behaviour that mirrors `hc` (each has a contract test, §4):
- `terminal.send`: `not_found`, `finished`, `blocked`; otherwise it records the text and returns `{"started": …, "status": …, "turn": …}`. It types into a terminal with null status and returns `started: null` (V7).
- `turn` goes up when a status leaves `working`; `status_ms` changes with the status.
- `terminal.kill` gives `reason: "killed"`; `rm` refuses a live terminal with `live`; labels can be set on finished records; `""` removes a label.
- `doc.set` with a stale version answers `conflict`.

Test-side helper in `test_factory.py`:

```python
class FakeHc:
    def __init__(self, directory): ...
    def add(self, labels: dict, status: str | None = "idle", **fields) -> str     # a terminal that exists before the test acts
    def set_status(self, term, state, kind=None, msg=None) -> None                # keeps turn and status_ms consistent
    def finish(self, term, reason="exited", exit_code=0) -> None
    def remove(self, term) -> None
    def screen(self, term, text) -> None
    def on_run(self, member, status=None, screen="", then=None) -> None           # status a newly run member reports, and after how many polls
    def on_send(self, term, error=None, hold=False, die=False, started=True) -> None
    def reply_in_chunks(self, sizes: list[int], delay: float) -> None             # split the next reply across writes
    def hello(self, version="0.1.20", core=None) -> None                          # what the next hello answers
    def received(self) -> list[dict]                                              # every request line, hello included
    def release(self, term) -> None                                               # let a held send return
    def doc_conflicts(self, n) -> None                                            # the next n doc.set calls answer conflict
    def calls(self, method) -> list[dict]
    def sent(self, term) -> list[str]                                             # texts typed into a terminal
    def stop(self) -> None                                                        # connect fails: HcDown
```

`hold` makes the `connect` process poll the state file until `release`: this models a `draft_wait` hold. `die` makes it exit after reading the request, without a reply: `HcDown(sent=True)`.

Replies are built from the recorded fixture of the same shape. The state also keeps `received` (every JSON-RPC line, including hello) and `polls` (the pending status transitions registered by `on_run`).

### Fault and crash injection

- **Disk faults** in the log tests are made by patching `os.write`, `os.fsync` and `os.ftruncate` on the `factory` module's `os` binding for one call each, singly and together ([message_log.md](message_log.md)).
- **A writer killed mid-append** is a child process that the test stops with SIGKILL at a named point (an environment variable makes `Log._append` pause there).
- **A keeper killed between two steps** is modelled by raising from a patched method after the step under test (for example right after `log.out(…, "delivered")`), discarding that `Keeper`, and building a new one on the same state directory and fake `hc`. The crash-recovery tests in [keeper.md](keeper.md) all use this.

## 3. Fake agent

`tests/fake_agent.py [script]`, used wherever a real terminal is needed: the real-`hc` test mode, and dry runs of `live.py`. It puts the terminal in raw mode, enables bracketed paste, and writes `ESC ] 7501 ; state=<word>:app=fake ESC \` (verified against `hc 0.1.20`). Every submitted input is appended to `$FAKE_LOG` with a timestamp.

| Script | Behaviour |
|---|---|
| `reports` (default) | `idle`, then `working` for `FAKE_WORK` seconds on each message. If set, runs the JSON argv in `FAKE_REPLY_CMD` with the message's first line on stdin, then reports `idle` |
| `forgets` | As `reports` without the `say` |
| `works_forever` | `working` after the first message, never leaves it |
| `blocks` | `blocked` (kind `permission`) after the first message, until it receives `enter` |
| `crashes` | Exits with code 1 during its first turn |
| `silent` | Never reports a status |
| `dialog` | Prints `FAKE_DIALOG` text and reports `FAKE_DIALOG_STATE` (`blocked`, `idle` or none) until it receives the configured keys, then `idle` |

A line in a message of the form `FAKE: <script>` switches the script, so one test can change an agent's behaviour by sending it a message.

## 4. Test modes and isolation

- **Fake mode (default).** Each hc test patches `factory.HC` to `tests/fake_hc.py` and sets its own temporary `FAKE_HC_DIR`.
- **Real mode (`FACTORY_TEST_HC=real`).** `setUpModule` starts a private `live.Core` with its own socket, session, HOME and XDG directories. Each hc test patches its environment to select that core and checks the core ID before issuing requests. A test labels its terminals with a unique team and removes them on cleanup. A test that changes the app doc restores its previous value. Restart coverage uses a separate private core. Teardown confirms the stop on the raw socket before deleting the directory. The v1 tests run in both modes.
- **Selection.** `@fake_only` scripts the fake, `@real_only` needs a real core, and unmarked backend tests exercise both. Real mode prints how many tests used the real core.
- **Contracts.** `tests/live.py fixtures` records replies and the reachable schema in `hc_fixtures.json`. The tests' `conforms()` checks required keys, allows optional keys when present, rejects unknown keys, and checks schema types recursively. It allows null only for fields that support it. For example `Terminal.foreground` may be absent, and `StatusResult.installed_by` can be null (release builds) or a string (Nix, LC-9).
- **Wire exceptions.** The extract also records version-scoped discrepancies. In 0.1.20 `terminal.list` omits `machines` on a standalone core. The extract makes that field optional and checks its array type if present. Real mode verifies the omission. Re-record and check the contracts when upgrading the pinned release.

### Time

No test sleeps for a deadline. `Keeper` and `await_ready` take a `clock` object; tests pass

```python
class ManualClock:
    now = 1_000_000.0
    def time(self): return self.now
    def monotonic(self): return self.now
    def sleep(self, s): self.now += s
```

and `say`'s wait loop reads the module's own `time` binding, which tests replace on the `factory` module only (v1's lesson: patching `time.sleep` globally also changes subprocess polling). Keeper tests call `tick()` directly and advance the clock between calls.

## 5. What happens to v1's test classes

| v1 class | v2 |
|---|---|
| `UpTests`, `UpRecoveryTests`, `DownTests` | Rewritten as `LaunchTests`, `SessionTests`, `DownTests` ([launch_layout.md](launch_layout.md)) |
| `WorktreeTests`, `ConfigTests`, `ListingTests` | Kept; `ConfigTests` gains the three keys |
| `InboxTests` | Becomes `LogTests` ([message_log.md](message_log.md)); acknowledgement tests are deleted |
| `StockTransportTests`, `WaitTests` | Deleted with the code |
| `CacheNameTests` | Becomes `StateDirTests`: team key, core suffix, privacy of the directory |
| `DocumentationTests`, `RecipeTests`, `InstalledCopyTests`, `GateFixtureTests` | Kept ([cli_skill.md](cli_skill.md)) |
| `HerdrContract` | `HcContract` |
| `LiveFlags` | Kept, opt-in (`FACTORY_LIVE=1`): each installed harness still accepts the flags Factory passes, now including the session flags |
| new | `HcClientTests`, `KeeperTests`, `LayoutTests`, `CliTests`, `AutonomyTests` |

`AutonomyTests` are the `test_stall_<row>` tests of [keeper.md](keeper.md), the never-loops test, `test_status_leaves_stopped_team_stopped` and the ready check's A → none → A test: the automated half of §10's autonomy tests.

Size: v1's test file is 3,130 lines. Expect about 3,500 for v2, plus the fake `hc` and the fake agent.

Coverage rule: every row of every decision table in the component docs (delivery, send results, refusals, readiness, layout placement) has a test named in that doc's test plan; a reviewer checks a change against the table it touches.

## 6. Live qualification (`tests/live.py`)

`python3 tests/live.py qualify <harness> [--role operator|member] [--case N]`

- Starts its own core, isolated on every axis that selects one: a unique `HC_SESSION=factory-qa-<pid>`, an explicit `HC_SOCKET=<private temp dir>/hc.sock` (it outranks an inherited `HC_SOCKET` and a `socket` set in `hc`'s config) and a private `XDG_RUNTIME_DIR`. `HOME` stays real, because the harness needs its credentials; `XDG_CONFIG_HOME` is the private directory described below. The owner can watch with the same three variables and `rodeo`; the tests themselves drive a rodeo of their own (below).
- Reads the new core's ID from `hello` once and builds every later client as `Hc(expect_core=<that id>)`: a send, interrupt, kill or teardown that would reach any other core fails before the request is written. Teardown runs `hc server stop` only after `server.status` shows that ID.
- Sets the draft hold for cases 3 and 10 with `HC_DRAFT_MS` on the QA core (verified: LC-10; `HC_SETTLE_MS=0` does not shorten draft holds, so they may be combined). Because `draft_wait` in `herdr-core/config.toml` overrides `HC_DRAFT_MS` (LC-10), the QA core runs with a private `XDG_CONFIG_HOME` that has no `herdr-core/config.toml` (or sets its own private `draft_wait`), uses private copies for mutable harness settings (especially `agy`'s settings/trust files, never writing through symlinks to the owner's config), and shares only the credential paths qualification actually needs.
- Builds a scratch repository in a temp directory with a two-member formation in which the harness under test is the operator, the member, or both.
- Types the owner's keystrokes through the rodeo driver (below), which is the path a real owner uses. A small framed `terminal.attach` client (`Kind::Data`, which `hc` counts as human input: hc-capabilities §10.1) also lives in `live.py`: it serves LC-1 and LC-10, which run before the driver exists, and is the fallback for the owner-typing cases if LC-13 fails.
- Runs the ten cases of §10 in order, each with a unique token in its message and a check that the recipient acted on it (a file it was asked to write, or a reply quoting the token), and writes one row per case to `docs/qualification/<harness>.md`: result, observed behaviour (for case 4: queued or steered), harness version, `hc` version, Factory `VERSION`.
- Runs the live checks (LC-1 to LC-4, LC-6 to LC-8, LC-10 to LC-13) as named subcommands (`live.py check lc3`), each printing the fact and writing it to `docs/qualification/hc-checks.md`.

A harness is supported when all ten rows pass in both roles (§10). A case 4 result of "lost or garbled" is the trigger to build delivery between turns ([keeper.md](keeper.md) §9) for that harness; the case is then run again. Case 6 checks, per harness, that the ready line recorded the conversation ID, that `down` then `up` reopens that conversation (the member is asked for a token from before), and that a conversation switched inside the harness is the one resumed after the member's next `say`.

### The rodeo driver

rodeo is the client the owner uses, so the tests that need "the owner at a client" use rodeo itself, without tmux: rodeo runs as a terminal of a second `hc` core, and the tests press keys in that terminal and read its screen through `hc`.

```python
class Rodeo:
    """rodeo attached to the QA core, running as a terminal of a separate driver core."""
    def __init__(self, qa_env: dict, cols: int = 200, rows: int = 60): ...
    def keys(self, *names: str) -> None        # terminal.keys on the driver core: named keys, or single characters
    def type(self, text: str) -> None           # keys, one per character: text typed without Enter (an unsent draft)
    def screen(self) -> str                     # terminal.read on the driver core: rodeo's rendered screen as text
    def wait_for(self, text: str, timeout: float = 10) -> None   # poll screen() until it contains text
    def focus(self, pane_title: str) -> None    # rodeo navigation keys (~0.2 s apart): ctrl+b then h/l (verified: LC-13) plus j/k for stacked columns, tracking focus from the first block against the QA layout doc (to verify at the start of Phase 5b)
    def close(self) -> None                     # end rodeo, stop the driver core
```

- **Two cores, both private (verified: LC-13 on `rodeo 0.2.1`).** The QA core holds the team under test (with its `herdr-app` doc written before rodeo starts, so rodeo does not spawn a default shell space). The driver core (`HC_SESSION=factory-drv-<pid>`, its own `HC_SOCKET` and `XDG_RUNTIME_DIR`) holds one terminal: `rodeo`, started with `env -u HC_TERMINAL_ID rodeo` (or `experimental.allow_nested = true`; only `HC_TERMINAL_ID` triggers nesting refusal), private `HOME`/`XDG_CONFIG_HOME`/`XDG_STATE_HOME`/`RODEO_CONFIG_PATH`/`HERDR_CONFIG_PATH`, and the QA core's `HC_SESSION` and `HC_SOCKET`, at a fixed size (`160x48` or `200x60`) so the layout is the same on every run. `RODEO_BIN` names the binary. Each core has its own `Hc(expect_core=…)`; neither is ever the owner's. LC-13 also observed `keep=false` on doc-placed panes after rodeo rendered them.
- **Typing is the owner's path.** A key pressed in the rodeo terminal is forwarded by rodeo to the focused QA terminal as viewer input, which the QA core counts as human input: `draft_wait` applies exactly as it does for a person. `type()` never presses Enter, so it leaves an unsent draft.
- **Reading is what the owner sees.** `screen()` returns rodeo's rendered text: tab names, pane titles, and the panes' contents.

| Used for | How |
|---|---|
| §10 case 3 (a report while the owner has unsent text) and case 10 (`say` returns while `hc` holds the message) | `focus("@operator")`; `type(draft)`; a member's `say`; assert the report is not on `screen()` during the hold; `keys("enter")`; assert the draft arrived byte-for-byte and the report after it |
| §10 case 4 (the owner interrupts the operator while a message is pending) | `focus("@operator")`; `keys("esc")` mid-turn; then the checks of case 4 |
| LC-8 (the layout as a client renders it) | After `up`: `wait_for("team")`; each `@member` title and `keeper` appear once; the keeper pane is the last and narrowest; an owner-added pane survives `up` and `down`; `terminal.get` on the QA core shows whether rodeo turned `keep` on |
| End-of-phase smoke test, from Phase 5b on (`live.py smoke`) | A team of fake agents (no model cost): `up`; the tab, titles and keeper pane render; `type` and `enter` into a member pane reach the fake agent; a `say` shows in the keeper pane as one line; `down`; the tab is gone |
| LC-13 | The driver itself: rodeo starts this way, `keys` reach the focused QA pane as human input (a held `terminal.send` proves it), and `screen()` returns the rendered text |

CI leaves harness qualification and the rodeo cases to local runs.

## 7. End-to-end autonomy runs

`python3 tests/live.py e2e --tracker file|github --operator <harness>`

1. `file`: a throwaway repository with a local bare remote, `FACTORY.md` from the template with the `TODO.md` tracker holding three small ready items, a `ready_check` that greps `TODO.md`, a local test script standing in for CI and a locally merged branch for the pull request. `github`: the private `pkieltyka/factory-test` repository with its board, pull requests and CI.
2. Starts the operator in a terminal of the QA core with one prompt (`/factory up`, then work through the ready items), a developer and a reviewer. No input after that.
3. Faults: once the first item is assigned, `hc interrupt` on the developer (its turn ends without a report); during the second item, `hc kill` on the developer.
4. Pass: all three items integrated with the checks green and the tracker updated inside the time budget (60 minutes for `file`); the log has no `unconfirmed` pair; it contains a quiet note followed by the operator's message to the developer, and an exit note followed by `up` of that member.
5. Second run, empty tracker: exactly one quiet note, then 15 minutes with no further `msg` record from `factory`; then a ready item is added and the next ready check produces one `ready work` note and the item is taken.

Results go to `docs/qualification/e2e-<date>.md` with the versions tested.

## 8. CI

`.github/workflows/test.yml` runs `python3 tests/test_factory.py` in fake mode on every push and pull request. The `test-real-hc` job verifies the SHA-256 of the pinned hc 0.1.20 Linux release and runs the same suite with `FACTORY_TEST_HC=real`. The installation was verified in LC-9. [Qualification record](../../../../docs/qualification/hc-checks.md)

## Dependencies

Depends on: `hc` 0.1.20 for real mode; the six harnesses for qualification. Used by: every component's test plan.
