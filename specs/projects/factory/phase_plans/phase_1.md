---
status: ready
issue: 165
phase: 1
branch: peter/165-early-live-checks
owners: ["@architect-3", "@lead-dev-2"]
---

# Phase 1 Plan: Early Live Checks (#165)

## 1. Goal and boundaries

Answer the four early live checks (`LC-1`, `LC-9`, `LC-10`, `LC-13`) on isolated `hc` cores, record `tests/hc_fixtures.json` from the real `hc 0.1.20` core, and apply each finding/fallback to the architecture docs before Phases 2–4 build on them.

- **In scope:** `specs/projects/factory/phase_plans/phase_1.md`, `tests/live.py` (Phase-1 subset only: private-core isolation, minimal NDJSON RPC client, minimal framed-attach client, rodeo driver prototype for LC-13, `check` subcommands `lc1`/`lc10`/`lc13`/`all`, and `fixtures` recorder), `tests/hc_fixtures.json`, `docs/qualification/hc-checks.md`, and resulting updates to `specs/projects/factory/architecture.md` §10 and any affected component doc.
- **Out of scope:** No v1 code or tests removed; no `factory.py` edits; no `tests/fake_hc.py` or `tests/fake_agent.py` yet (Phase 2). `python3 tests/test_factory.py` must stay green unchanged.
- **Safety invariant:** Never touch the owner's own `hc` sessions or `~/.config/{herdr-core,rodeo}`. Every private core uses a short temp dir under `tempfile.gettempdir()` (`tempfile.mkdtemp(prefix="hc-")`, asserting `len(HC_SOCKET) < 95` for Unix `SUN_LEN`), unique `HC_SESSION`, explicit `HC_SOCKET`, private `HOME`, `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, strips `HC_TERMINAL_ID`, `HC_TERMINAL_SOCKET`, and `HC_SCOPE`, verifies `hello.core.id` before any RPC, and verifies `server.status` core ID before `server.stop`.

## 2. Interfaces in `tests/live.py` (Phase-1 subset)

```python
class HcError(Exception):   # mirrors hc_client.md so Phase 2 can swap in factory.Hc directly
    kind: str; message: str; hint: str

class HcDown(Exception):
    sent: bool; detail: str

class IsolatedCore:
    """Private hc core in a temp dir. Context manager: starts core, stops on exit."""
    def __init__(self, tag: str, *, config_toml: str = "", extra_env: dict[str, str] | None = None, settle_ms: int | None = None): ...
    # settle_ms=None by default so real hc draft/settle timers run (pass settle_ms=0 for fixtures only).
    env: dict[str, str]        # HC_SESSION, HC_SOCKET, HOME, XDG_RUNTIME_DIR, XDG_STATE_HOME, XDG_CONFIG_HOME (+ extra_env, optional HC_SETTLE_MS)
    core_id: str               # read from first hello; verified on every later call and before teardown
    def rpc(self, method: str, params: dict | None = None, timeout: float | None = 15.0) -> dict   # NDJSON via `hc connect`; raises HcError / HcDown
    def spawn_send(self, term: str, text: str) -> tuple[subprocess.Popen, float]
        # opens `hc connect`, validates hello (core.id & version), flushes terminal.send, returns (Popen, flushed_monotonic_ts)
    def stop(self) -> None     # asserts server.status core_id == self.core_id, runs `server.stop`, removes temp dir

class FramedAttach:
    """Minimal framed terminal.attach client (rodeo/crates/hc/src/wire.rs). Counts as human input."""
    def __init__(self, core: IsolatedCore, term: str, cols: int = 120, rows: int = 40): ...
    def send_bytes(self, data: bytes) -> None   # channel=attach_ch, kind=0 (Data); triggers hc draft_wait
    def close(self) -> None                     # kind=2 (Close) + close stdin + wait/kill

class RodeoDriver:
    """rodeo attached to qa_core, running inside a second private driver IsolatedCore."""
    def __init__(self, qa: IsolatedCore, *, mode: str = "unset_env", cols: int = 200, rows: int = 60): ...
    # RODEO_BIN default: env RODEO_BIN or shutil.which("rodeo") or "/home/peter/Dev/pkieltyka/rodeo/target/release/rodeo"
    # mode="unset_env": argv=["env", "-u", "HC_TERMINAL_ID", "-u", "HC_TERMINAL_SOCKET", "-u", "HC_SCOPE", RODEO_BIN]
    # mode="allow_nested": argv=["env", "-u", "HC_TERMINAL_SOCKET", "-u", "HC_SCOPE", RODEO_BIN] with RODEO_CONFIG_PATH setting [experimental]\nallow_nested = true
    def keys(self, *names: str) -> None         # driver.rpc("terminal.keys", {"terminal": rodeo_term, "keys": list(names)})
    def type(self, text: str) -> None           # keys(*list(text)) — one key per char, no Enter
    def screen(self) -> str                     # driver.rpc("terminal.read", {"terminal": rodeo_term, "lines": 0})["text"]
    def wait_for(self, needle: str, timeout: float = 10.0) -> str
    def close(self) -> None                     # kill rodeo_term, stop driver core
```

- **Wire protocol verified against `hc 0.1.20` (`rodeo/crates/hc/src/{wire,client}.rs` & `hc schema`):**
  - NDJSON (`framed: false`): write `hello`, read & verify `hello` reply (`core.id == core_id`, `version >= 0.1.20`) before writing line 2; read with `os.read` + `select` (no buffered `readline`).
  - Framed (`framed: true`): line 1 is newline-delimited `hello` with `{"generation": 1, "framed": true}`; read newline-delimited `hello` reply and verify `core.id` and `framed: true`; thereafter frames are `struct.pack("<IBI", channel, kind, len(payload)) + payload` (`CONTROL=0`, `Data=0`, `Credit=1`, `Close=2`, `Resize=3`). Send `terminal.attach` (`{"terminal": term, "cols": cols, "rows": rows}`) on `channel=0, kind=0`; read reply on `channel=0, kind=0` for `result["channel"]`; send human bytes on `channel=result["channel"], kind=0`. Drain incoming frames, sending `Credit` as **`u32` little-endian** (`struct.pack("<I", n)` on `kind=1`, verified in `rodeo/crates/hc/src/client.rs:498-499`) when `Data` frames arrive so the pipe never stalls.
- **Inline OSC 7501 probe:** small Python snippet passed via `sys.executable -c ...` to `terminal.run`: puts stdin in raw mode, enables bracketed paste (`\x1b[?2004h`), emits `\x1b]7501;state=idle:app=probe\x1b\\` (or configured initial state/kind), logs each received byte chunk with `time.monotonic()` timestamp to `$PROBE_LOG` (JSON lines `{"t": float, "b64": str}`) and echoes printable bytes to stdout so `terminal.read` sees them. Helper `concat_stream(log_path)` joins all chunks into a single byte stream (with per-byte timestamps) so assertions never break on PTY chunk splits or coalescing.
- **CLI subcommands:**
  - `python3 tests/live.py check lc1`
  - `python3 tests/live.py check lc10`
  - `python3 tests/live.py check lc13`
  - `python3 tests/live.py check all`
  - `python3 tests/live.py fixtures --out tests/hc_fixtures.json`

## 3. Check specifications, commands, pass/fail, and consequences

### LC-1: In-flight `terminal.send` when its `hc connect` client dies

- **Command:** `python3 tests/live.py check lc1` (`settle_ms=None`)
- **Procedure:**
  1. Start `IsolatedCore("lc1", config_toml="draft_wait = 8\n")`. Spawn OSC 7501 `idle` probe. Wait until `terminal.get` shows `status.state == "idle"`.
  2. **Control (client stays alive):** Attach `FramedAttach`, type `b"DRAFT_CTRL:"` (no Enter), sleep 0.3 s. Start `p, t_flush = core.spawn_send(term, "TOKEN_CTRL")` and wait for `p` to finish. Confirm `"TOKEN_CTRL"` lands in the concatenated PTY stream after `~draft_wait` (~8 s) and record the baseline hold duration.
  3. **Variant A (`SIGKILL` during `draft_wait`):** Fresh probe terminal, attach `FramedAttach`, send `b"DRAFT_A:"`, sleep 0.3 s. `p, t_flush = core.spawn_send(term, "TOKEN_LC1_A")`. Wait until `t_flush + 1.0` s; assert `p.poll() is None` and `"TOKEN_LC1_A"` not in stream. Send `SIGKILL` to `p` and reap it. Wait 12 s (past `draft_wait = 8`), then send `b"\r"` via `FramedAttach`, then send a **follow-up token** `core.rpc("terminal.send", {"terminal": term, "text": "TOKEN_LC1_A2"})`. Record from concatenated stream + timestamps whether AND when `"TOKEN_LC1_A"` landed, and its order relative to `"TOKEN_LC1_A2"`.
  4. **Variant B (clean EOF during `draft_wait`):** Fresh probe terminal, identical to A except close `p.stdin` and `p.stdout` cleanly instead of `SIGKILL`, plus follow-up `"TOKEN_LC1_B2"`.
  5. **Variant C (no draft, random `SIGKILL` inside paste-to-Enter gap):** 20 runs with no draft; each run calls `p, t_flush = core.spawn_send(term, f"TOKEN_C_{i}")`, sleeps a random `0..300 ms` after `t_flush`, sends `SIGKILL`, waits 1.5 s, and checks the concatenated stream for partial/unsubmitted text (token pasted without trailing `\r`).
- **Pass/Fail:** Succeeds when Control confirms the draft hold, Variants A & B give unambiguous `typed` (with timestamp & ordering) or `not typed` results, and Variant C reports `k/20` unsubmitted-paste count.
- **Consequences:**
  - Either A/B outcome is safe (killed keeper's in-flight send is always `unconfirmed`, never retried).
  - If A/B **not typed**: `keeper.md` §8's `kill_children()` is confirmed effective against held sends.
  - If A/B **typed**: update `architecture.md` §10 and `keeper.md` §8 to note that `kill_children()` cannot cancel an already-queued core send (which may land after a new keeper starts, reinforcing `unconfirmed`).
  - If Variant C shows any pasted text without Enter: update `keeper.md` §8 to document that stopping a keeper mid-send may leave an unsubmitted line in the recipient's pane.

### LC-10: Does the core honour `HC_DRAFT_MS` (and `config.toml` / `HC_SETTLE_MS` interactions)?

- **Command:** `python3 tests/live.py check lc10` (`settle_ms=None` except Control 3)
- **Procedure** (`hc --skill` states `draft_wait` in `config.toml` takes precedence over `HC_DRAFT_MS`):
  1. **Test (`HC_DRAFT_MS` only):** `IsolatedCore("lc10", config_toml="", extra_env={"HC_DRAFT_MS": "6000"})`. Attach `FramedAttach`, send `b"DRAFT10:"`, sleep 0.3 s. Time `terminal.send` (20 s cap, clearing draft if capped) and record elapsed time until token lands / send returns.
  2. **Control 1 (default, no `HC_DRAFT_MS`):** Empty config, no `HC_DRAFT_MS`; cap at 20 s and clear draft (expect held past 20 s, proving the ~60 s default).
  3. **Control 2 (`config.toml` precedence):** `config_toml="draft_wait = 2\n"` plus `HC_DRAFT_MS=6000`; measure elapsed time (expect ~2–5 s, confirming `config.toml` overrides `HC_DRAFT_MS`).
  4. **Control 3 (`HC_SETTLE_MS=0` interaction):** `IsolatedCore("lc10-s0", config_toml="", extra_env={"HC_DRAFT_MS": "6000"}, settle_ms=0)`. Repeat draft + send and record whether `HC_SETTLE_MS=0` preserves or collapses the 6 s draft hold (informs later test setups).
- **Pass/Fail:**
  - Pass = `HC_DRAFT_MS=6000` returns in ~6–9 s while Control 1 holds past 20 s.
  - Controls 2 and 3 record exact precedence and `HC_SETTLE_MS=0` behavior.
- **Consequences:**
  - If **Pass & Control 2 shows `config.toml` wins**: update `architecture.md` §10 and `components/testing.md` §6 so live qualification gives the QA core a private `XDG_CONFIG_HOME` holding its own `herdr-core/config.toml` plus symlinks to every other entry of the owner's real `~/.config` (zero product change to `start_member`; harnesses still see their real configs while `HC_DRAFT_MS` or private `draft_wait` controls the QA core).
  - If **Fail**: apply fallback in `architecture.md` §10 and `components/testing.md` §6 — drop `HC_DRAFT_MS`, use the symlinked private `XDG_CONFIG_HOME` with `herdr-core/config.toml` directly (or the configured `draft_wait`).

### LC-13: Driving `rodeo` from a second private `hc` core without tmux

- **Command:** `python3 tests/live.py check lc13` (`settle_ms=None`). If `RODEO_BIN` is not found, print `not run: rodeo not found` and exit 0 (do not fail falsely on machines without rodeo).
- **Procedure:**
  1. Start `IsolatedCore("lc13-qa", config_toml="draft_wait = 6\n")`. Spawn two `idle` probe terminals (`op` and `dev`).
  2. Write a `herdr-app` layout doc on QA core first (empty doc causes rodeo to spawn a shell space): one space (`folder: <tmp>`), one tab (`name: "team"`, `root: {type: "row", children: [block(op, "@op"), block(dev, "@dev")]}`). After `doc.set`, label both terminals with `herdr.space`, `herdr.tab`, `herdr.seen="0"`, and `name="@op"` / `name="@dev"`.
  3. Start `RodeoDriver(qa, mode="unset_env")` (driver core with its own `HC_SESSION`/`HC_SOCKET`/`XDG_*`, running `env -u HC_TERMINAL_ID -u HC_TERMINAL_SOCKET -u HC_SCOPE rodeo` with private `HOME`/`XDG_CONFIG_HOME`/`XDG_STATE_HOME`/`RODEO_CONFIG_PATH` under `D/rodeo` and QA core's `HC_SESSION`/`HC_SOCKET`/`XDG_RUNTIME_DIR`/`XDG_STATE_HOME`).
  4. **Render check:** `wait_for("team")` and assert `"@op"` and `"@dev"` appear in `screen()`.
  5. **Focus & navigation check:** Type a marker into `rodeo` to identify the initially focused pane; test rodeo's default pane-cycle keys (`ctrl+b`, `tab` and `ctrl+b`, `l`/`h` per `rodeo/crates/config/src/default.toml`) to verify deterministic focus switching between `@op` and `@dev` via their concatenated `$PROBE_LOG` streams.
  6. **Baseline cleared-draft timing:** With `@op` focused and draft confirmed cleared (send `driver.keys("enter")`, wait 0.3 s), time `qa.rpc("terminal.send", {"terminal": op, "text": "BASELINE_LC13"})` from request to token arrival in `@op`'s concatenated stream (`t_base`, measuring `hc`'s normal quiet pause).
  7. **Human-input / `draft_wait` proof:** With `@op` focused, `driver.type("RODEO_DRAFT:")` (no Enter). Confirm `"RODEO_DRAFT:"` appears in `@op`'s concatenated stream. Start `p, t_flush = qa.spawn_send(op, "TOKEN_LC13")`. Wait past `max(t_base + 1.5, 3.0)` s (still `< draft_wait = 6` s): assert `p.poll() is None` and `"TOKEN_LC13"` not yet in stream (proves hold exceeds normal quiet pause). Press `driver.keys("enter")`: assert `p` finishes and concatenated stream contains `"RODEO_DRAFT:"`, `"\r"`, and `"TOKEN_LC13"` in that exact order with monotonic timestamps proving hold > baseline.
  8. **Side observations:** Record `qa.rpc("terminal.get", {"terminal": op})["keep"]` (informational for LC-8), which env removals (`HC_TERMINAL_ID`/`HC_TERMINAL_SOCKET`/`HC_SCOPE`) were needed, and a quick start/render sanity check with `mode="allow_nested"`.
- **Pass/Fail:**
  - **Pass:** `rodeo` renders `"team"`, `"@op"`, `"@dev"` via `hc read`, focus switching routes input to the target pane, and typing into `rodeo` holds `qa.send` past the cleared-draft baseline and delivers draft + Enter + token in order.
  - **Fail:** Any of render, focus switching, or human-input hold proof fails.
- **Consequences:**
  - If **Pass**: keep `Rodeo` driver in `components/testing.md` §6; document chosen launch mode, required env removals, exact focus keys, and `keep` observation in `docs/qualification/hc-checks.md`, `architecture.md` §10, and `components/testing.md` §6.
  - If **Fail**: apply fallback in `architecture.md` §10 and `components/testing.md` §6 — use `FramedAttach` for owner-typing cases (§10 cases 3, 4, 10) and human inspection for LC-8 / smoke tests.

### LC-9: Installing a pinned `hc 0.1.20` release in GitHub Actions

- **Verified upstream artifact** (`https://releases.herdr.dev/core/0.1.20/manifest.json`):
  - URL: `https://releases.herdr.dev/core/0.1.20/herdr-core-linux-x86_64.tar.gz`
  - SHA-256: `ac6b86890c451a37584620aaa438b71bc0d0bd430a2e7c1d7e095f052c414e74`
- **Procedure (on branch `peter/165-early-live-checks` CI ONLY — never on `master`):**
  1. Add a temporary CI job/step in `.github/workflows/test.yml` gated to `peter/165-early-live-checks` that downloads the pinned tarball, checks its SHA-256 with `sha256sum -c`, extracts `herdr-core`, symlinks `hc`, asserts `hc --version` prints `hc 0.1.20`, and runs **`python3 tests/live.py fixtures --out "$TMPDIR/ci-fixtures.json"`** plus asserts the recorded file has `hc_version == "0.1.20"`, `methods["terminal.send.agent"]["status"]` set, and `methods["terminal.read"]["text"]` contains the probe marker (proves real PTY + OSC 7501 + `hc connect` transport on `ubuntu-latest`, the exact code path Phase 2 will run).
  2. Push branch and inspect the GitHub Actions run (`gh run list --branch peter/165-early-live-checks` / `gh run view --log`). Record run URL, status, and duration.
  3. After capturing LC-9 evidence, **revert the temporary CI step in `.github/workflows/test.yml`** before the final PR commit for Phase 1 so `.github/workflows/test.yml` stays clean until Phase 2 adds real-`hc` tests to run against it.
- **Pass/Fail & Consequences:**
  - **Pass:** Pinned `hc 0.1.20` installs, passes SHA-256 check, and `live.py fixtures` passes end-to-end on `ubuntu-latest`. → Update `architecture.md` §10 LC-9 and `components/testing.md` §8 so Phase 2 enables the pinned-`hc` real-mode CI job.
  - **Fail:** Record exact CI error in `docs/qualification/hc-checks.md`; keep `components/testing.md` §8 fallback (CI stays on fake `hc` only).

### Fixtures: Recording `tests/hc_fixtures.json`

- **Command:** `python3 tests/live.py fixtures --out tests/hc_fixtures.json` (`settle_ms=0`)
- **Schema (`methods` + `errors`):**
  - `methods`:
    - `hello`, `server.status`
    - `terminal.run`, `terminal.list.live`, `terminal.list.all` (contains both a live and a finished record)
    - `terminal.get.live`, `terminal.get.finished`, `terminal.get.server_stop` (after `server.stop` + restart: `reason: "server_stop"`, labels preserved)
    - `terminal.send.agent` (to OSC 7501 `idle` probe), `terminal.send.plain` (non-agent terminal with null status → `started: null`)
    - `terminal.keys`, `terminal.read`
    - `terminal.label.set`, `terminal.label.remove` (`""`), `terminal.label.finished` (on a finished record, V4)
    - `terminal.keep.off`, `terminal.keep.on`
    - `terminal.kill`, `terminal.rm`
    - `doc.get.empty` (`value: null, version: 0`), `doc.set`, `doc.get.present`
  - `errors` (full `{code, message, data: {kind, hint}}`):
    - `blocked`, `status_mismatch`, `not_found`, `finished`, `live`, `conflict`, `spawn_failed`, `invalid_params`
- **Redaction for public repo:** Preserve **exact key sets and value types**, recursively scrubbing all host/user/volatile strings (`hello.core.id`, `hello.core.name`, `server.status` / `login_session.user` / host details, `cwd`, `argv`, `foreground.argv`, `foreground.cwd`, error/hint paths, `/nix/store/...`, `/home/...`, temp paths) to fixed placeholders (e.g. `"00000000000000000000000000000000"`, `"fixture-host"`, `"fixture-user"`, `"/tmp/hc-fixture"`). Assert zero occurrences of `$USER`, `socket.gethostname()`, or `/nix/store` remain in the serialized JSON.

## 4. Ordered execution steps

1. **Plan finalized (`@architect-3`, aligned with `@lead-dev-2`, `@architect`, `@lead-dev`):** Ready to build; `@pm` already notified Phase 1 started.
2. **Branch & build (`@lead-dev-2`):** In `/home/peter/Dev/pkieltyka/factory.worktrees/peter-lead-dev-2`, on `peter/165-early-live-checks` (from `fec46cb`), copy in `specs/projects/factory/phase_plans/phase_1.md` and implement `tests/live.py` (Phase-1 subset).
3. **Run local live checks & record fixtures (`@lead-dev-2`):** Run `check lc1`, `check lc10`, `check lc13`, and `fixtures --out tests/hc_fixtures.json`; verify `python3 tests/test_factory.py` passes; send exact outputs to `@architect-3`.
4. **Run LC-9 on branch CI (`@lead-dev-2`):** Push temporary CI check on `peter/165-early-live-checks`, record Actions run URL & logs, revert `.github/workflows/test.yml`, send evidence to `@architect-3`.
5. **Interpret results & update docs (`@architect-3`):** Write `docs/qualification/hc-checks.md`, update `specs/projects/factory/architecture.md` §10 and any component doc whose fallback/detail applies; hand doc changes to `@lead-dev-2` to commit on `peter/165-early-live-checks`.
6. **Review & PR (`@lead-dev-2` → `@reviewer` → `@operator`):** `@lead-dev-2` requests review from `@reviewer` (issue #165, acceptance criteria, exact diff); after approval, pushes branch, opens PR against `master`, watches CI green, notifies `@pm` and `@operator`.

## 5. Risks and mitigations

- **Socket path length (`SUN_LEN`):** Use `tempfile.mkdtemp(prefix="hc-")` (works inside sandboxes where only `$TMPDIR` is writable) and assert `len(HC_SOCKET) < 95`.
- **`rodeo` migrating or writing to owner config:** Always set `HOME`, `RODEO_CONFIG_PATH`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` to private temp paths under `D/rodeo`.
- **Orphaned test cores on crash/Ctrl-C:** `IsolatedCore` uses `try/finally` + `atexit` cleanup gated by `server.status` core-ID check.
- **`rodeo` key timing:** Give `rodeo` a short settle pause (~0.2 s) between `ctrl+b` and the second chord key if needed; record exact working key sequence in LC-13 output.
