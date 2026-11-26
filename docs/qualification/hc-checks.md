# `hc` Live Checks (`docs/qualification/hc-checks.md`)

Verified facts about `hc` and `rodeo`, collected on isolated private cores (never touching the owner's sessions). Each check maps to `specs/projects/factory/architecture.md` §10.

## Environment (Phase 1, 2026-11-26)

- Local: `hc 0.1.20` (Nix build), `rodeo 0.2.1`, branch `peter/165-early-live-checks` (commits `53e2530`, `f8f5292`, `151bd43`, `3a17c76`).
- CI: GitHub Actions `ubuntu-latest`, pinned `hc 0.1.20` release tarball (`https://releases.herdr.dev/core/0.1.20/herdr-core-linux-x86_64.tar.gz`, `sha256: ac6b86890c451a37584620aaa438b71bc0d0bd430a2e7c1d7e095f052c414e74`).

---

## LC-1: In-flight `terminal.send` when its `hc connect` client dies

- **Command:** `python3 tests/live.py check lc1`
- **Setup:** Private `hc 0.1.20` core (`draft_wait = 3`, `HC_SETTLE_MS` unset); OSC 7501 `idle` probe; unsent draft typed through framed `terminal.attach` (`Kind::Data`); `terminal.send` held on `draft_wait`, then client terminated 0.8 s after request.
- **Observed:**
  - **Control (client stays alive):** typed 3.0 s after last key (2.7 s after send); reply `{"started": false, "status": "idle", "turn": 0}` at 7.9 s (typing + 5 s `started` window); terminal remained `live`.
  - **`SIGKILL` of `hc connect`:** **never typed** (watched `draft_wait + 3` s); terminal stayed `live`.
  - **`SIGKILL`, then person presses Enter 0.3 s later (1.1 s after send):** **never typed**; clearing the draft did not revive the cancelled send.
  - **Clean EOF (stdin closed, `hc connect` left alive):** process still running 5 s later and **typed at 3.0 s** (same as control). Closing stdin alone does not cancel; dropping the connection does.
  - **Variant C (no draft, 20 runs, `SIGKILL` 0–291 ms after paste appears):** **20/20 submitted cleanly, 0 pasted without Enter.** Once `hc` begins pasting, it completes paste and Enter even if the client dies.
- **Verdict:** **Answered (safe).**
- **Consequence:** `keeper.md` §8's `kill_children()` (`SIGKILL`) cancels sends held on `draft_wait` and leaves no half-pasted lines if pasting already started. `hc_client.md`: closing stdin alone does not cancel a held send. Interrupted pairs still become `unconfirmed` on keeper restart.

---

## LC-9: Installing a pinned `hc 0.1.20` release in GitHub Actions

- **Command (CI job `hc-release` on `peter/165-early-live-checks`):** download `https://releases.herdr.dev/core/0.1.20/herdr-core-linux-x86_64.tar.gz`, verify `sha256sum -c` against `ac6b86890c451a37584620aaa438b71bc0d0bd430a2e7c1d7e095f052c414e74`, extract `herdr-core`, symlink `hc -> herdr-core`, run `hc --version` and `python3 tests/live.py fixtures` on a private core, compare reply keys against `tests/hc_fixtures.json`.
- **Run:** <https://github.com/pkieltyka/factory/actions/runs/45880045216> (`hc-release`, `ubuntu-latest`, **11 s**, green).
- **Observed:**
  - SHA-256 matched; tarball contains `herdr-core`, `LICENSE`, `THIRD-PARTY-NOTICES`.
  - `hc --version` printed `hc 0.1.20`.
  - `python3 tests/live.py fixtures` ran a private core with PTY, OSC 7501, and `hc connect` cleanly; reply-key diff printed `differ: none`.
  - Only value difference vs local Nix build: `server.status.installed_by` is `null` on the release build and `"nix"` on the Nix build.
- **Verdict:** **Pass.**
- **Consequence:** Phase 2 can add a real-`hc` CI job pinned to `hc 0.1.20` + SHA-256 alongside fake mode (`components/testing.md` §8). Contract tests (`HcContract`) compare reply key sets and, where both values are non-null, JSON types (`null` matches any type), not host/build-specific values (`components/testing.md` §4). Temporary CI job was reverted in `151bd43` for Phase 1.

---

## LC-10: `HC_DRAFT_MS`, `config.toml` precedence, and `HC_SETTLE_MS=0`

- **Command:** `python3 tests/live.py check lc10`
- **Setup:** Private `hc 0.1.20` cores; draft typed via framed `terminal.attach`; `terminal.send` issued 0.3 s later; elapsed times measured from last draft key.
- **Observed:**
  - **10a (`HC_DRAFT_MS=3000`, no config):** typed at **3.0 s** (reply at 7.9 s). Honoured.
  - **10b (`draft_wait = 6` in `config.toml` + `HC_DRAFT_MS=2000`):** typed at **6.0 s** (reply at 10.9 s). `config.toml` wins, matching `hc --skill`.
  - **10c (neither set):** still held at the 20 s cap (60 s default); typed at **20.52 s** immediately after Enter was sent.
  - **10d (`HC_DRAFT_MS=3000` + `HC_SETTLE_MS=0`):** typed at **3.0 s** (reply at 7.9 s).
  - **10e (`draft_wait = 4` + `HC_SETTLE_MS=0`):** typed at **4.0 s** (reply at 8.9 s).
- **Verdict:** **Pass (with `config.toml` precedence confirmed).**
- **Consequence:** `HC_DRAFT_MS` works and `HC_SETTLE_MS=0` does not shorten draft holds, so tests may combine them. Because `config.toml` overrides `HC_DRAFT_MS`, live qualification cases 3 and 10 must give the QA core a private `XDG_CONFIG_HOME` (holding its own `herdr-core/config.toml` or none with `HC_DRAFT_MS`, plus symlinks to all other entries of the owner's `~/.config` so harnesses keep their configs without changing `start_member`) (`components/testing.md` §6).

---

## LC-13: Driving `rodeo` from a second private `hc` core without tmux

- **Command:** `python3 tests/live.py check lc13`
- **Setup:** QA core (`hc 0.1.20`, `draft_wait = 4`) with `herdr-app` doc containing tab `"team"` and two `row` blocks (`@op`, `@dev`) on OSC 7501 `idle` probes. Driver core (second private `hc 0.1.20` core) running `rodeo 0.2.1` at `160x48` with the QA core's private `HC_SESSION`, `HC_SOCKET`, `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME` and `HOME`, plus a private `RODEO_CONFIG_PATH` and `HERDR_CONFIG_PATH` (so rodeo never reads or migrates the owner's config).
- **Observed:**
  - Driver terminal env carries `HC_SCOPE`, `HC_SESSION`, `HC_SOCKET`, `HC_TERMINAL_ID`, `HC_TERMINAL_SOCKET`.
  - **Nesting bypass:** both `env -u HC_TERMINAL_ID -u HC_TERMINAL_SOCKET -u HC_SCOPE rodeo` and `experimental.allow_nested = true` (with only `HC_SESSION`/`HC_SOCKET` overridden, nothing unset) rendered `"team"`, `"@op"`, and `"@dev"` within 15 s via `terminal.read` on the driver core. Only `HC_TERMINAL_ID` triggers rodeo's nesting check.
  - **Focus switching:** focus starts on the first block (`@op`); separate `terminal.keys` calls 0.2 s apart (`ctrl+b`, then `l` for right / `h` for left) switch focus deterministically between `@op` and `@dev`.
  - **Human-input / `draft_wait` proof:** keys typed through rodeo reached `@op`'s PTY as human input; a QA `terminal.send` stayed held past 1.5 s, landed at **1.71 s** immediately after rodeo received `enter` (send returned at 5.37 s), the concatenated PTY stream contained `"draft\r"` followed by the token, and the token appeared on rodeo's rendered screen.
  - **Partial LC-8 observation:** `keep` stayed **`false`** on both doc-placed terminals (`@op`, `@dev`) after rodeo rendered the doc.
- **Verdict:** **Pass.**
- **Consequence:** Keep the `Rodeo` driver class in `components/testing.md` §6 using `env -u HC_TERMINAL_ID` (or `experimental.allow_nested = true`), doc written before launch, and `ctrl+b` + `h`/`l` (0.2 s apart) for pane focus.

---

## Fixtures (`tests/hc_fixtures.json`)

- **Command:** `python3 tests/live.py fixtures --out tests/hc_fixtures.json`
- **Recorded on:** isolated `hc 0.1.20` core (`HC_SETTLE_MS=0`); 32 KB.
- **Coverage:**
  - Replies: `hello`, `server.status`, `server.stop`, `terminal.run` (`agent`, `plain`), `terminal.get` (`live`, `finished`, `after_restart` with `reason: "server_stop"` and labels kept), `terminal.list` (`all`, `live`, `after_restart`), `terminal.send` (`agent`, `plain` with `started: null`), `terminal.keys`, `terminal.read`, `terminal.label` (`set`, `remove`, `finished`), `terminal.keep` (`on`, `off`), `terminal.kill`, `terminal.rm`, `doc.get` (`empty`, `present`), `doc.set`.
  - Errors: `blocked`, `conflict`, `finished`, `invalid_params`, `live`, `not_found`, `spawn_failed`, `status_mismatch`.
- **Redaction:** Core IDs, hostnames, usernames, home/temp paths, `/nix/store` hashes, PIDs, and timestamps scrubbed to same-type placeholders; exact key sets preserved; verified `differ: none` against the Linux `hc 0.1.20` release build in CI.
