# `hc` Live Checks (`docs/qualification/hc-checks.md`)

Verified facts about `hc` and `rodeo`, collected on isolated private cores (never touching the owner's sessions). Each check maps to `specs/projects/factory/architecture.md` §10.

## Environment (Phase 1, 2026-11-26)

- Local: `hc 0.1.20` (Nix build), `rodeo 0.2.1`, branch `peter/165-early-live-checks` (head `c245ca6`).
- CI: GitHub Actions `ubuntu-latest`, pinned `hc 0.1.20` release tarball (`https://releases.herdr.dev/core/0.1.20/herdr-core-linux-x86_64.tar.gz`, `sha256: ac6b86890c451a37584620aaa438b71bc0d0bd430a2e7c1d7e095f052c414e74`).

---

## LC-1: In-flight `terminal.send` when its `hc connect` client dies

- **Command:** `python3 tests/live.py check lc1`
- **Setup:** Private `hc 0.1.20` core (`draft_wait = 12`, `HC_SETTLE_MS` unset); OSC 7501 `idle` probe; unsent draft typed through framed `terminal.attach` (`Kind::Data`); `terminal.send` confirmed held past `6 s` (`max(baseline + 3, 6)`, separating the draft hold from `hc`'s ~2.5–3 s quiet pause) before ending the client.
- **Observed:**
  - **Baseline (no draft):** typed immediately (`0.0 s` after send).
  - **Control (client stays alive):** typed **`12.0 s`** after last key (`11.7 s` after send); reply `{"started": false, "status": "idle", "turn": 0}` at `16.9 s` (typing + 5 s `started` window); terminal remained `live`.
  - **`SIGKILL` of `hc connect` at `6 s`:** confirmed held at kill, **never typed** (watched past `draft_wait`); terminal stayed `live`.
  - **`SIGKILL` at `6 s`, then person presses Enter `0.3 s` later (`6.3 s` after send):** **never typed**; clearing the draft did not revive the cancelled send.
  - **Clean EOF at `6 s` (stdin closed, `hc connect` left alive):** process still running `5 s` later and **typed at `12.0 s`** after last key (`11.7 s` after send, when the hold ended; check reports `inconclusive` rather than "did not type" if an alive EOF client produces nothing by deadline). Closing stdin alone does not cancel; dropping the connection does.
  - **Follow-up send after every variant:** a fresh `terminal.send` afterwards typed immediately (`0.0 s`) in all four variants; where the first token was typed (`control`, `eof`), it arrived before the follow-up token and nothing was typed twice.
  - **Variant C (no draft, 20 trials, `SIGKILL` 18–294 ms after observed paste):** **0 of 20 trials left an unsubmitted paste** (20/20 pasted and submitted, 0 not pasted). This is an observation after an observed paste, not a guarantee if the client dies before the full paste arrives.
- **Verdict:** **Answered (safe).**
- **Consequence:** `keeper.md` §8's `kill_children()` (`SIGKILL`) cancels sends held on `draft_wait`, and 0 of 20 trials left an unsubmitted paste when killed after an observed paste. `hc_client.md`: closing stdin alone does not cancel a held send (so a keeper killed by `SIGKILL`/OOM without running `kill_children()` leaves held sends alive until their hold ends). Interrupted pairs still become `unconfirmed` on keeper restart.

---

## LC-9: Installing a pinned `hc 0.1.20` release in GitHub Actions

- **Command (CI job `hc-release` on `peter/165-early-live-checks`):** download `https://releases.herdr.dev/core/0.1.20/herdr-core-linux-x86_64.tar.gz`, verify `sha256sum -c` against `ac6b86890c451a37584620aaa438b71bc0d0bd430a2e7c1d7e095f052c414e74`, extract `herdr-core`, symlink `hc -> herdr-core`, run `hc --version` and `python3 tests/live.py fixtures` on a private core, compare reply keys against `tests/hc_fixtures.json`.
- **Run:** <https://github.com/pkieltyka/factory/actions/runs/45880045216> (`hc-release`, `ubuntu-latest`, **11 s**, green).
- **Observed:**
  - SHA-256 matched; tarball contains `herdr-core`, `LICENSE`, `THIRD-PARTY-NOTICES`.
  - `hc --version` printed `hc 0.1.20`.
  - `python3 tests/live.py fixtures` ran a private core with PTY, OSC 7501, and `hc connect` cleanly; key-shape diff against `tests/hc_fixtures.json` printed `differ: none` (scalars collapsed, so key shape only).
  - Observed build-dependent field: `server.status.installed_by` is `null` on the release build and `"nix"` on the Nix build.
- **Verdict:** **Pass.**
- **Consequence:** Phase 2 can add a real-`hc` CI job pinned to `hc 0.1.20` + SHA-256 alongside fake mode (`components/testing.md` §8). Phase 2 contract tests (`HcContract`) check every required `hc 0.1.20` schema key, check optional keys (such as `Terminal.foreground`) when present, reject unknown keys, and check schema JSON types allowing `null` only on schema-declared nullable fields (`components/testing.md` §4); `tests/hc_fixtures.json` supplies real recorded examples rather than a rigid exact key set. Temporary CI job was reverted in `151bd43` for Phase 1.

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
- **Consequence:** `HC_DRAFT_MS` works and `HC_SETTLE_MS=0` does not shorten draft holds, so tests may combine them. Because `config.toml` overrides `HC_DRAFT_MS`, live qualification cases 3 and 10 must give the QA core a private `XDG_CONFIG_HOME` (with no `herdr-core/config.toml` or its own private `draft_wait`), using private copies for mutable harness settings (especially `agy`'s settings/trust files) and sharing only the read-only credential paths qualification needs (`components/testing.md` §6).

---

## LC-13: Driving `rodeo` from a second private `hc` core without tmux

- **Command:** `python3 tests/live.py check lc13`
- **Setup:** QA core (`hc 0.1.20`, `draft_wait = 15`) with `herdr-app` doc containing tab `"team"` and two `row` blocks (`@op`, `@dev`) on OSC 7501 `idle` probes. Driver core (second private `hc 0.1.20` core) running `rodeo 0.2.1` at `160x48` with the QA core's private `HC_SESSION`, `HC_SOCKET`, `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME` and `HOME`, plus a private `RODEO_CONFIG_PATH` and `HERDR_CONFIG_PATH` (so rodeo never reads or migrates the owner's config).
- **Observed:**
  - Driver terminal env carries `HC_SCOPE`, `HC_SESSION`, `HC_SOCKET`, `HC_TERMINAL_ID`, `HC_TERMINAL_SOCKET`.
  - **Nesting bypass:** both `env -u HC_TERMINAL_ID -u HC_TERMINAL_SOCKET -u HC_SCOPE rodeo` and `experimental.allow_nested = true` (with only `HC_SESSION`/`HC_SOCKET` overridden, nothing unset) rendered `"team"`, `"@op"`, and `"@dev"` within 15 s via `terminal.read` on the driver core. Only `HC_TERMINAL_ID` triggers rodeo's nesting check.
  - **Focus switching:** focus starts on the first block (`@op`); separate `terminal.keys` calls 0.2 s apart (`ctrl+b`, then `l` for right / `h` for left) switch focus deterministically between `@op` and `@dev`.
  - **Cleared-draft baseline & human-input `draft_wait` proof:** with focus on `@op` and no draft, a QA `terminal.send` typed immediately (**`0.0 s`**). With `"draft"` typed through rodeo, a QA `terminal.send` stayed held past the **`6 s`** check point (`max(baseline + 3, 6)`); after rodeo received `enter`, the token typed **`0.17 s`** after Enter (**`6.21 s`** after send; reply at `5.37 s` after Enter), within the pass rule (`<= 2 s` after Enter and before `draft_wait` measured from the last draft key, `released_by_enter: true`), the concatenated PTY stream contained `"draft\r"` followed by the token, and the token appeared on rodeo's rendered screen.
  - **Negative control (`LC13_DRAFT_WAIT=0`):** returned **`fail`** as intended — not held at `6 s`, typed **`2.47 s`** after send (`hc`'s regular quiet pause after recent keys, `released_by_enter: false`), proving the `6 s` check distinguishes a real `draft_wait` hold from quiet pause.
  - **Partial LC-8 observation:** `keep` stayed **`false`** on both doc-placed terminals (`@op`, `@dev`) after rodeo rendered the doc.
- **Verdict:** **Pass.**
- **Consequence:** Keep the `Rodeo` driver class in `components/testing.md` §6 using `env -u HC_TERMINAL_ID` (or `experimental.allow_nested = true`), doc written before launch, and `ctrl+b` + `h`/`l` (0.2 s apart) for pane focus.

---

## LC-8: rodeo renders the tab Factory writes (Phase 5a)

- **Command:** `python3 tests/live.py check lc8` (`hc 0.1.20`, `rodeo 0.2.1`, Linux, 26 November 2026, branch `peter/169-launch`).
- **Setup:** Two private cores, as in LC-13. The QA core holds three OSC 7501 probes placed in a `herdr-app` doc written before rodeo starts: tab `team`, a `row` root with `@alice`, `@bob` and a `keeper` block with `"size": {"weight": 0.4}`, each terminal labelled `herdr.space`/`herdr.tab`/`herdr.seen`. A fourth probe carries no `herdr.*` label. rodeo runs at 160×48 on the driver core; the check waits 5 s past a client's adoption delay.
- **Observed:** each member title appears once; on the title row the panes measure 56, 56 and 20 columns, so the keeper is last and narrowest (0.36 of a member's width for weight 0.4 against 1); `doc.get` returns the doc unchanged, weight included; the unlabelled terminal is not added to the doc; `keep` is `false` on all four terminals.
- **Verdict:** **Pass** with rodeo.
- **Consequence:** `launch_layout.md` §5 keeps the keeper's `size.weight`; no fallback is applied.

---

## rodeo_tabs: selecting a tab by its place (Phase 8)

- **Command:** `python3 tests/live.py check rodeo_tabs` (`hc 0.1.20`, `rodeo 0.2.1`, Linux, 27 November 2026, branch `peter/173-qualification`).
- **Setup:** two private cores. On the QA core a `herdr-app` doc with two tabs, `operator` (one OSC 7501 probe) and `team` (two probes), with each terminal's `herdr.*` labels written; rodeo on the driver core.
- **Observed:** `ctrl+b` then `2` put the next marker only in the team tab's focused probe; `ctrl+b` then `1`, only in the operator tab's; `ctrl+b` then `2` again, only in the team tab's.
- **Verdict:** **Pass.** The qualification driver selects a tab with `prefix+N` by the tab's place in the layout doc (phase_8.md §2).

---

## LC-3, LC-4/LC-11, LC-6, LC-7, LC-12: the login checks (Phase 8)

@lead-dev-3 ran these at `325223a` with the owner's own logins, on private cores, Linux 7.3.5. Each was run as its own command except where noted.

- **LC-3, Claude Code 2.1.332: pass** on the standalone rerun. A fresh start with the assigned `--session-id` reached idle, and the environment's ID matched the assigned one. The ID was stored; a resume reached idle and recalled the word; the ID still matched after the resume. It passed again at 7a90602 on the real HOME (@lead-dev-3, lc3-7a90602.json), with the same observations.
  - The first attempt, inside `check login`, timed out on a trust-dialog race ("Enter to confirm · Esc to cancel").
- **LC-4 and LC-11, OpenCode v2.4.1: pass.**
  - A UUID4 session ID is rejected (`finished (code 1): at ["sessionID"]`).
  - A `ses_`-prefixed ID starts, is stored and reopens with recall.
  - `OPENCODE_SESSION_ID` equals the ID. After `/new` it no longer does, so `session_follow` stays false.
- **LC-6, pi 1.6.1 and omp 19.2.1: fail.** For both harnesses, neither of the two member directories stored anything (`stored=false` on the first and the second fresh launch), and `continue` held nothing. pi's `PI_SESSION_ID` was not set.
  - Consequence: neither directory/continue route can be enabled. pi's case 6 resume fails at 670dd76, consistent with this (see pi.md).
- **LC-7, agy 1.5.18: not counted.** The baseline showed the trust dialog, and the session-only `--settings` suppressed it. But the run used the owner's real settings roots, and agy wrote under `~/.gemini/antigravity-cli/` during it.
  - phase_8.md §4 requires mutable trust state to be isolated before launch, so this LC-7 run does not count. It is to be rerun under an isolated HOME overlay.
- **LC-12, Claude Code, Codex and agy: pass** at db85120 on the real HOME (@lead-dev, lc12-db85120.json). Each harness started to idle, and its parent and an actual child each ran with the parent's ID present.
  - Claude Code: the child's ID equals the parent's. That is the positive evidence `session_follow` needs. Turning it on changes what a resume opens after a switch, so it is left false here for @operator to decide (with a member-switch rerun on that build).
  - Codex and agy: the child's ID differs, so `session_follow = false` stays.
  - The stat-only check found no changed agy settings or trust file. The earlier partial run (agy's child and parent never ran, at 325223a) is superseded.

## Cross cases (Phase 8, phase_8.md §4)

- **Run:** `python3 tests/live.py qualify cross` at `5d36f88` (Factory `31d3e84d827b`), hc 0.1.20, rodeo 0.2.1, Linux, 2026-11-27, run ID `20261127T104137Z-95`.
- **Setup:** real private hc cores and rodeo, with scripted fake agents. These cases test Factory itself, and §1 allows a named scripted peer. The run probed `rodeo_tabs` first, and it passed.
- **Results:**
  - **two-teams: pass.** Two checkouts with the same member names ran on one core, each with its own key, state, keeper and operator.
    - Each concurrent say's action recorded the terminal that ran it, and each was its own team's member.
    - The two members' blocks sat in distinct tabs, and each team's inbox held only its own token.
    - After team A's `down` (exit 0), team B's own member still acted on a new say.
  - **multi-down: pass.** Two briefed members were stopped together.
    - No member-exit or lost note followed, and the terminals went.
    - A message queued for the blocked one ended `dropped (stopped by down)`.
  - **owner-pane: pass.** After the first `up`, the owner added a block of their own to the team tab that holds the members.
    - Through a repeated `up`, `down` of one member and `down` of the last, the block stayed in that tab with its terminal and seeded contents, and each stopped member's block went.
    - Afterwards a marker typed into the owner's pane through rodeo arrived.
  - **other-version: pass.** A comment-only copy of the script was installed atomically under a live keeper while the owner's draft held a send.
    - A second message was queued during the drain. The held send finished delivered.
    - The same keeper terminal's beat and `factory.version` label then showed the new version (one keeper before and after), and the queued message was delivered and acted on once, with no wakeup.
    - Case 7's `upgrade` runs the same trial with each real profile.
- **Earlier runs:** the cross runs at `dbd7fcd` and `b06c253` used weaker checks, which @architect-2's review replaced:
  - two-teams: actions were not attributed to the terminal that ran them;
  - owner-pane: the owner's block was seeded in the operator's tab;
  - other-version: the restart line counted as proof, and the second say came after it.
  This run supersedes both.

## LC-7 again: agy's session-only trust, isolated (Phase 8)

- **Run:** `python3 tests/live.py check lc7` by @lead-dev-3 at `f4e5ea1`, agy 1.5.18, inside a `bwrap --tmp-overlay "$HOME"` overlay. Nothing reached the real HOME.
- **Observed:**
  - Baseline: `dialog: Do you trust the contents of this project?`.
  - With `--settings '{"toolPermission":"always-proceed","trustedWorkspaces":["<folder>"]}'` in another scratch folder: `idle`, and the stored action ran.
  - `settings_changed` lists 21 paths, all session state: conversation databases and WALs, caches, `history.jsonl`, logs, crash logs, presence locks, annotations and `~/.config/antigravity/credential-user-scope`. None is `settings.json` or a trust list.
- **Verdict:** the check reports "inconclusive" because a stat cannot tell a trust list from other state. Reading the path list shows none of them is one.
- **Consequence:** @operator enabled per-launch `trustedWorkspaces` for agy (spec §6.3; `launch_layout.md` §3, "agy's trust dialog"). The earlier LC-7 at `325223a` does not count: it ran against the owner's real settings.

---

## Fixtures (`tests/hc_fixtures.json`)

- **Command:** `python3 tests/live.py fixtures --out tests/hc_fixtures.json`
- **Recorded on:** isolated `hc 0.1.20` core (`HC_SETTLE_MS=0`); 32 KB.
- **Coverage:**
  - Replies: `hello`, `server.status`, `server.stop`, `terminal.run` (`agent`, `plain`), `terminal.get` (`live`, `finished`, `after_restart` with `reason: "server_stop"` and labels kept), `terminal.list` (`all`, `live`, `after_restart`), `terminal.send` (`agent`, `plain` with `started: null`), `terminal.keys`, `terminal.read`, `terminal.label` (`set`, `remove`, `finished`), `terminal.keep` (`on`, `off`), `terminal.kill`, `terminal.rm`, `doc.get` (`empty`, `present`), `doc.set`.
  - Errors: `blocked`, `conflict`, `finished`, `invalid_params`, `live`, `not_found`, `spawn_failed`, `status_mismatch`.
- **Redaction & validation:** Core IDs, hostnames, usernames, home/temp paths, `/nix/store` hashes, PIDs, and timestamps scrubbed to same-type placeholders (`terminal.run` records the actual scrubbed request); recorder validates post-restart state (`finished`, `server_stop`, labels kept) and refuses to write if any case produces the wrong outcome or an error kind is missing; verified `differ: none` against the Linux `hc 0.1.20` release build in CI.
