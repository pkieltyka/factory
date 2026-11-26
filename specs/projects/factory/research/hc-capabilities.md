# herdr-core (`hc`) Capabilities Research for Factory v2

**Verified against:**
- `hc 0.1.20` (`/nix/store/bdmj436qfas1lav2wblf85nk1n29s4bj-herdr-core-0.1.20/bin/hc`)
- `herdr-app 0.1.15` (`/nix/store/w195qjg701j4rzki452nnyqhd1pw2agy-herdr-desktop-0.1.15/bin/herdr-app`)
- `rodeo` (`/home/peter/Dev/pkieltyka/rodeo`, `docs/{plan,architecture,parity}.md`, `crates/{hc,doc,rodeo}`)
- Installed harnesses: `claude` 2.1.330, `codex` 0.171.0, `pi` 1.6.0, `omp` 19.1.0, `opencode` 2.2.4+45898d0, `agy` 1.5.14
- Live isolated `hc 0.1.20` sessions (`HC_SESSION=exp*`, temp `HOME`/`XDG_*`, `HC_SETTLE_MS=0`)

Anything not directly executed live against `hc 0.1.20` or verified from source/schema is explicitly marked **[UNVERIFIED]**.

---

## 1. Architecture Split: `hc` vs `herdr-app` Doc vs `herdr-app do`

`hc` (herdr-core) is a **headless PTY runtime and JSON-RPC server**. It owns terminals, processes, screen buffers, OSC 7501 status, multi-machine SSH links, and an opaque versioned JSON document store (`doc.get` / `doc.set`). It has **no native concept of spaces, tabs, or panes**.

Layout (spaces, tabs, split trees, panes) lives in a JSON document named `herdr-app` stored inside `hc` via `doc.get` / `doc.set`, shared live between `herdr-app` (desktop GUI) and `rodeo` (TUI).

### 1.1 Sessions, Sockets, and Paths

Verified from `rodeo/crates/hc/src/paths.rs` and live isolated `hc` runs:

| Mechanism | Resolution / Behavior |
|---|---|
| `HC_SESSION` | Session name (default `"default"`). Trimmed; cannot be `.`, `..`, or contain control chars or `/\:*?"<>\|`. |
| `HC_SOCKET` | Explicit Unix socket path override. Highest precedence over config and `XDG_RUNTIME_DIR`. |
| `XDG_CONFIG_HOME` | Config at `$XDG_CONFIG_HOME/herdr-core/config.toml` (default `~/.config/herdr-core/config.toml`). Can set `socket = "..."`, `notes = false`, `draft_wait = 60`, `wait_timeout = 180`, `shell = [...]`, `headless = "200x120"`. |
| `XDG_STATE_HOME` | State dir `$XDG_STATE_HOME/herdr-core/<session>` (default `~/.local/state/herdr-core/<session>`). Holds `core.lock` (contains listening socket path) and logs. |
| `XDG_RUNTIME_DIR` | Default socket dir `$XDG_RUNTIME_DIR/herdr-core/<session>.sock` (fallback `/run/user/<uid>/herdr-core/<session>.sock`, else inside state dir). Socket path must fit Unix `SUN_LEN` (`<= 107` bytes on Linux, `<= 103` on macOS, accounting for `.holder.sock` + 9 bytes). |
| `HC_TERMINAL_ID` | Set by `hc` inside every spawned terminal to its address (`t1`, `t7`, etc.). Replaces v1's `HERDR_ENV` for in-multiplexer detection (`parity.md:199`) and is used as default `caller` / `parent` by CLI commands. |
| `HC_SETTLE_MS` | Test override (e.g. `0`) to skip quiet-pause settling delays. |

### 1.1b herdr 0.9.5 → `hc 0.1.20` Concept Map (Verified from `rodeo/docs/parity.md`)

| herdr 0.9.5 concept | `hc 0.1.20` / `herdr-app` equivalent | Verified? |
|---|---|---|
| Session | `HC_SESSION` (socket via `HC_SOCKET` or `$XDG_RUNTIME_DIR/herdr-core/<session>.sock`) | Yes (live + `paths.rs`) |
| `HERDR_ENV=1` | `HC_TERMINAL_ID` (set to `tN` inside every `hc` terminal) | Yes (live `RUN1`) |
| Workspace / Tab / Split tree | `herdr-app` JSON doc (`doc.get` / `doc.set` CAS, per machine via `on`) | Yes (live + `crates/doc`) |
| Pane | Doc `block` (`content: {terminal: "tN"}`, optional `title`) + terminal labels `herdr.space`, `herdr.tab`, `herdr.seen=0`, `where` + `keep: true` | Yes (live + `reconcile.rs`) |
| Pane metadata `$name` | Terminal label `name=<val>` (read by `rodeo`'s `$name` sidebar token); space metadata `$name` is dropped (`hc` has no space objects) | Yes (live + `parity.md:116-117`) |
| Agent & state | OSC 7501 `status` (`status: null` = not an agent; `status.app` = agent kind; `status.state` with `error` rendered as `blocked`; `turn` vs `herdr.seen` label for `done` vs `idle`) | Yes (live + `agents.rs`) |
| Socket API & pane/workspace CLI | `hc` CLI + JSON-RPC (`terminal.*`, `doc.*`, `machine.exec`, `events.subscribe`) plus `herdr-app do` (GUI-only; see §1.5) | Yes (live + `herdr-app`) |

To isolate tests/experiments without touching user sessions: set `HOME`, `HC_SESSION`, `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `HC_SETTLE_MS=0`, and clean up with `hc server stop` using the same env (a custom `XDG_RUNTIME_DIR` also prevents systemd integration on Linux).

### 1.2 Wire Protocol & Transport

Verified from `hc schema` (`rpc.discover`) and `rodeo/crates/hc/src/{client,wire}.rs`:

- **Transport:** Unix domain socket (local) or `hc connect [--session S]` over stdin/stdout (SSH/remote).
- **Protocol:** JSON-RPC 2.0, protocol generation `1`.
- **Handshake (`hello`):** Sent as a newline-delimited JSON line:
  ```json
  {"jsonrpc":"2.0","id":0,"method":"hello","params":{"generation":1,"framed":true}}
  ```
  Verified reply from `hc 0.1.20` (with `"framed": false` for plain NDJSON RPC):
  ```json
  {
    "core": {"id": "dd55be053ecd6b828df2332552f007ca", "name": "pax"},
    "features": ["framed", "attach", "events", "status", "shell", "resize_notifications", "automatic_input", "restart_environment"],
    "framed": false,
    "generation": 1,
    "methods": [
      "hello", "server.status", "server.stop", "server.restart",
      "terminal.run", "templates.list", "terminal.list", "terminal.get",
      "terminal.kill", "terminal.rm", "terminal.keep", "terminal.label",
      "terminal.read", "terminal.attach", "terminal.send", "terminal.wait",
      "terminal.keys", "terminal.interrupt", "terminal.put",
      "machine.exec", "file.read", "file.write", "file.edit",
      "doc.get", "doc.set", "events.subscribe",
      "machine.add", "machine.list", "machine.connect", "machine.rename",
      "machine.rm", "rpc.discover"
    ],
    "version": "0.1.20"
  }
  ```
- **Plain mode (`framed: false`):** One JSON-RPC message per `\n`-terminated line. Works for all request/response RPCs and for `events.subscribe` (connection switches to streaming notifications only). Cannot open `terminal.attach` (`needs_framed` error).
- **Framed mode (`framed: true`):** After the single newline-delimited `hello` reply, all traffic uses 9-byte little-endian frames: `channel: u32` (`0` = JSON-RPC control) + `kind: u8` (`0` Data, `1` Credit, `2` Close, `3` Resize `[cols:u16, rows:u16]`, `4` Focus `[u8]`, `5` Facts JSON, `6` AutomaticInput) + `len: u32` (max 16 MiB) + `payload`. Required for `terminal.attach` and multiplexed channels.
- **Errors:** JSON-RPC error object with stable `data.kind` (e.g. `not_found`, `name_taken`, `blocked`, `status_mismatch`, `finished`, `conflict`, `invalid_params`, `machine_unknown`, `unavailable`, `spawn_failed`, `live`). CLI writes `{"error":{"kind":...,"message":...,"hint":...}}` to stderr with exit codes including `3` (`not_found`), `6` (`invalid_params`), `7` (`needs_target`), `9` (`blocked`), `10` (`status_mismatch`), `11` (`finished`).

### 1.3 Addressing Terminals

A terminal `<t>` is addressed by:
- **Permanent ID:** `t1`, `t7`, `t1b` (monotonic base-36 ID, unique forever on that core).
- **Live name:** e.g. `lead-dev` (optional, unique among live terminals; 1–32 chars of `a-z 0-9 - _ .`, starting with letter/digit, cannot look like a terminal ID).
- **Remote machine address:** `machine/t7` (e.g. `vps/t3`).

### 1.4 Spaces, Tabs, and Panes (`herdr-app` Doc)

Verified from `rodeo/crates/doc/src/{schema,id,edit,sync}.rs`, `rodeo/crates/doc/tests/fixtures/herdr-app-doc.json`, and live `doc.get` / `doc.set` calls:

- **RPC calls (no CLI subcommand in `hc` 0.1.20):**
  - `doc.get`: `{"name": "herdr-app", "on": null}` → `{"value": <JSON|null>, "version": <u64>}` (`version: 0` and `value: null` when never written).
  - `doc.set`: `{"name": "herdr-app", "value": <JSON|null>, "version": <u64>, "on": null}` → `{"version": <u64>}`. Optimistic CAS: version mismatch returns `data.kind: "conflict"`. Max 1 MiB serialized. Atomic persistence; emits a `doc` event to unfiltered `events.subscribe` listeners.
- **Document shape (`herdr-app`):**
  ```json
  {
    "spaces": [
      {
        "id": "sp<32-hex>",
        "name": "factory",
        "folder": "/home/peter/Dev/pkieltyka/factory",
        "tabs": [
          {
            "id": "tb<32-hex>",
            "name": "factory team",
            "root": {
              "id": "tb<32-hex>",
              "type": "row",
              "children": [
                {
                  "id": "p<32-hex>",
                  "type": "block",
                  "title": "@lead-dev",
                  "content": {"terminal": "t1"}
                }
              ]
            }
          }
        ]
      }
    ]
  }
  ```
  - ID prefixes: `sp` (space), `tb` (tab), `p` (pane/block), `s` (split container) + 32 lowercase hex chars.
  - Container `type`: `"row"` (side-by-side), `"column"` (stacked), `"stack"`, with optional `size: {"weight": f32}` or `{"px": f32}`. Leaf `type`: `"block"` with `content: {"terminal": "tN"}` (or `{"mod": "..."}`, `{"agent": "..."}`, `{"tab": "..."}`) and optional `title` (manual pane name, preserved by both `herdr-app` and `rodeo`).
  - Empty `space.name` displays folder/git-toplevel name; empty `tab.name` displays 1-based tab index.
- **Terminal labels paired with panes** (verified in `rodeo/docs/parity.md` and `rodeo/crates/rodeo/src/app/reconcile.rs`):
  - `herdr.space=<space-id>` — opts an externally-created terminal into the UI!
  - `herdr.tab=<tab-id>` — places it in that tab (splits right if tab exists; creates tab in space if missing).
  - `herdr.seen=<turn>` — tracks which turn a human has looked at (`"0"` initially).
  - `where="<space> / <tab>"` — display location shown in `hc ls`.
  - `keep: true` — replaces lost programs with a shell rather than closing the pane.
- **Automatic orphan adoption:** Both `herdr-app` (`herdr-app --skill`) and `rodeo` (`reconcile.rs:32-138`) watch for live terminals carrying `herdr.space=<id>` that are not yet in the `herdr-app` doc. After a 2 s grace period (`ADOPT_GRACE_MS = 2000` in rodeo), rodeo turns `keep: true` on and writes a pane for the terminal into the `herdr-app` doc (creating the space if unknown, splitting the tab if `herdr.tab` exists, or adding a new tab in the space). Unlabeled terminals stay hidden from the layout (accessible via `hc ls` / `hc attach`, or as sub-agents under their parent).

### 1.5 What `herdr-app do` Is and Whether It Exists Here

- **Exists locally:** Yes — `/etc/profiles/per-user/peter/bin/herdr-app` → `/nix/store/w195qjg701j4rzki452nnyqhd1pw2agy-herdr-desktop-0.1.15/bin/herdr-app` (`herdr-app 0.1.15`).
- **What it is:** CLI remote control for a **running Herdr desktop GUI window** (`~/.config/herdr-app/herdr-app.sock`, overridable via `HERDR_APP_HOME`). Exposes ~50 UI/layout commands (verified via `herdr-app commands`): `space.new`, `space.close`, `space.rename`, `space.move`, `tab.new`, `tab.close`, `tab.rename`, `pane.split`, `pane.close`, `pane.focus`, `pane.swap`, `terminal.open`, `terminal.keep`, `terminal.park`, `terminal.pin`, `agent.focus`, etc.
- **Critical limitation:** Verified live — running `herdr-app do ...` without an open Herdr desktop GUI fails immediately:
  ```text
  herdr-app: no Herdr window at /home/peter/.config/herdr-app/herdr-app.sock: No such file or directory (os error 2)
  ```
  `rodeo` does **not** listen on `herdr-app.sock` and does **not** implement `herdr-app do` (`rodeo/docs/parity.md:217`). Therefore **Factory v2 must NOT depend on `herdr-app do`** if it is to be client-agnostic and work with `rodeo` or headless.

### 1.6 Design Analysis: Should Factory v2 Write `herdr-app` Doc or Create Bare `hc` Terminals?

There are three options:

| Option | Visible in `rodeo` & `herdr-app`? | Control over tab names, pane titles, grid layout? | Works headless (no UI running)? | Complexity |
|---|---|---|---|---|
| **A. Bare `hc` terminals only** (no `herdr.space` label, no doc writes) | **No** (hidden from spaces/tabs; only visible via `hc ls` or as sub-agents under parent) | None | Yes | Lowest |
| **B. Labelled `hc` terminals (`herdr.space` + `herdr.tab`) relying on UI adoption** | Yes, **only while `rodeo` or `herdr-app` is open** (after ~2 s grace) | Limited: adopted spaces/tabs get empty names (`""`), splits always chain right in one row, no pane `title` set | Terminals run, but doc isn't updated until a UI opens | Low |
| **C. Direct `hc` JSON-RPC `doc.get` / `doc.set` (CAS) + matching `herdr.space` / `herdr.tab` / `herdr.seen=0` labels + `keep: true`** | **Yes, immediately** in both `rodeo` and `herdr-app`, even if opened later | **Full**: named space/tabs (`factory operator`, `factory team`), exact split grid, pane `title` (`@lead-dev`), clean teardown removing panes from doc | **Yes** (pure `hc` socket RPC, no `herdr-app` binary needed) | Moderate (~80–120 lines of Python stdlib JSON-RPC + CAS retry) |

**Recommendation for Factory v2:** **Option C** (with graceful fallback to bare terminals if user opts into headless/no-layout mode). Writing `herdr-app` doc directly via `doc.get`/`doc.set` CAS on the `hc` socket is completely client-agnostic (zero dependency on `herdr-app do`), makes the team appear with proper tab names (`factory operator`, `factory team`) and pane titles (`@lead-dev`, etc.) in both `rodeo` and `herdr-app`, and prevents adoption races because the terminals are placed in the doc synchronously with matching `herdr.space`/`herdr.tab`/`herdr.seen=0` labels. Note: `rodeo` and `herdr-app` also reconcile `where` labels and remove panes automatically when terminals exit/are killed (`reconcile.rs:140-177`).

---

## 2. Spawning Programs with `cwd` and `env`

### 2.1 CLI: `hc run`

Verified via `hc run --help` and live runs:
```sh
hc run --bg --name lead-dev --cwd /path/to/repo \
  --label factory.team=myproj --label name=@lead-dev \
  --label herdr.space=sp... --label herdr.tab=tb... --label herdr.seen=0 \
  --no-parent --keep \
  -- <program> [args...]
```
Key flags:
- `--bg`: return immediately (without it, streams output and waits up to `--timeout`, default 180 s).
- `--name <NAME>`: live-unique name (`1–32` of `a-z 0-9 - _ .`, starting with letter/digit, not ID-shaped).
- `--cwd <CWD>`: working directory (defaults to caller's cwd for CLI; core's cwd for raw RPC; remote home for `--on`).
- `--on <MACHINE>`: run on a linked remote machine (`hc machine ls`).
- `--label <K=V>`: repeatable.
- `--parent <PARENT>` / `--no-parent`: parent defaults to `$HC_TERMINAL_ID`. Children die when parent ends unless `--outlive-parent`.
- `--no-notify`: disable automatic `<hc>` status notes typed into parent.
- `--keep`: on unexpected loss/signal/core-stop, replace with a shell in place.
- `--rm`: auto-remove record after exit.
- **Important CLI gap (verified live):** `hc run` does **NOT** accept `--env <K=V>`! Passing `--env A=1` treats `--env` as the program name and fails with `spawn_failed`. To pass per-terminal `env` overrides cleanly, either use the **JSON-RPC `terminal.run` API** (which supports `"env": {"K": "V"}`) or wrap with `env K=V ...` on the CLI.

CLI `--json` output (compact summary):
```json
{"address":"t2","agent":"sh","cwd":"/home/peter/Dev/pkieltyka/factory","name":"osc-test","state":"live","turn":0}
```

### 2.2 JSON-RPC: `terminal.run`

Verified via `hc schema` and live Unix-socket RPC:
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "terminal.run",
  "params": {
    "argv": ["claude", "--model", "..."],
    "cwd": "/home/peter/Dev/pkieltyka/factory",
    "env": {"FOO": "bar42"},
    "name": "lead-dev",
    "labels": {
      "name": "@lead-dev",
      "factory.role": "lead-dev",
      "herdr.space": "sp...",
      "herdr.tab": "tb...",
      "herdr.seen": "0"
    },
    "size": {"cols": 100, "rows": 30},
    "display": {"fg": "#ffffff", "bg": "#000000", "cursor": null, "cell": null},
    "keep": true,
    "no_parent": true,
    "no_notify": false,
    "outlive_parent": false,
    "rm": false,
    "wait": false
  }
}
```
- Use `"argv": [...]` (exec directly, no shell) **or** `"line": "..."` (run via `sh -c`). Neither starts the machine's default shell.
- **Environment:** Inherited from the core's login session + core's terminal identity (`TERM=xterm-256color`, `COLORTERM=truecolor`, `TERM_PROGRAM=ghostty`, no `TERM_PROGRAM_VERSION`) + `HC_TERMINAL_ID=<address>` + explicit `env` overrides.
- **Templates:** If `argv[0]` matches `~/.config/herdr-core/templates/<name>.md`, `hc` starts the template's command, waits up to 60 s for agent readiness, and sends body + remaining args as the first message, recording `first_message: {sent, started, turn, reason}` on the `Terminal` record.
- **Result (`RunResult`):** Full `Terminal` object (verified live):
  ```json
  {
    "address": "t1", "argv": [...], "cols": 100, "rows": 30,
    "created_ms": 1795536101747, "cwd": "/tmp/...", "die_with_parent": false,
    "exit_code": null, "finished_ms": null, "identity": "ghostty",
    "keep": true, "labels": {...}, "name": "lead-dev", "notify": true,
    "parent": null, "pid": 3401810, "reason": null, "reported": false,
    "rm": false, "screen": "exact", "signal": null, "state": "live",
    "status": null, "status_ms": null, "template": null, "title": "", "turn": 0
  }
  ```

### 2.3 Non-Terminal Execution: `hc exec` / `machine.exec`

Verified from `hc schema` and `hc exec --help`: runs a one-shot program locally or on another machine (`--on`), **does** support `--env <K=V>`, `--cwd`, `-i` (base64 `input` up to 8 MiB), `--timeout` (default 60 s, max 600 s), returns `{code, signal, stdout(b64), stderr(b64), truncated, timed_out, ms}`.

---

## 3. Sending Input into a Pane

### 3.1 `hc send` / `terminal.send` (High-Level Prompt Submission)

Verified via `hc send --help`, `hc schema`, and live runs:
```sh
hc send [--json] [--if idle|working] [--force] [--wait] [--timeout S] <terminal> <text>
```
JSON-RPC params: `{terminal, text, caller?, force?, if?, wait?, timeout?}`.

**Built-in safety & pacing behaviors (huge upgrade over v1's manual sleeps):**
1. **Bracketed paste + Enter:** Pastes `<text>` and submits with Enter (respects per-template `submit` / `paste_gap_ms` overrides).
2. **Draft & quiet protection:** Waits for a human to finish or clear an unfinished draft (up to `draft_wait` seconds since their last key, default `60`, configurable in `config.toml`), otherwise ~3 s of quiet. Keys typed during delivery are held, then delivered.
3. **Blocked refusal:** Refuses to type into a terminal whose OSC 7501 `status.state == "blocked"` unless `--force` is passed (exit code `9`, `data.kind: "blocked"`). Verified live:
   ```json
   {"error":{"hint":"typing would answer its dialog; read its screen first (hc read t4), or --force","kind":"blocked","message":"t4 is blocked (permission)"}}
   ```
4. **Status guard (`--if idle|working`):** Atomically checks status before typing; mismatches fail without typing (exit code `10`, `data.kind: "status_mismatch"`). Verified live:
   ```json
   {"error":{"hint":"nothing was typed","kind":"status_mismatch","message":"t3 is working, not idle"}}
   ```
5. **Sender tagging:** When the receiver is an agent (has OSC 7501 status) and `caller` is set (automatically from `$HC_TERMINAL_ID` in CLI) and `notes` is not `false` in `config.toml`, `hc` wraps the pasted text as:
   ```text
   <hc from="t3 w1">task finished</hc>
   ```
   Verified live: setting `notes = false` in `config.toml` delivers raw `"plain text"` without `<hc>` tags and suppresses automatic parent notes.
6. **Return JSON (`SendResult`):**
   ```json
   {"started": false, "status": "idle", "turn": 0}
   ```
   (`started`: `true`/`false` for whether the agent transitioned to `working` within a few seconds, or `null` if it does not report OSC 7501; `wait`: `WaitResult` when `--wait` is set.)

### 3.2 `hc keys` / `terminal.keys` (Raw Named Keys)

Verified live:
```sh
hc keys --json <terminal> down enter
```
- Encodes key names (`enter`, `esc`, `tab`, `up`, `down`, `ctrl+c`, `alt+x`, `f5`, `a`, ...) for the program's active keyboard modes (e.g. Kitty keyboard protocol).
- Waits ~3 s of quiet; does **not** wait for an unsent draft; does **not** refuse `blocked` (used to resolve trust/permission dialogs).
- Returns `{"status": <str|null>, "turn": <u64>}`.

### 3.3 `hc interrupt` / `terminal.interrupt`

- Sends `esc` if the terminal reports OSC 7501 status, else `ctrl+c`; watches up to `timeout` (default 10 s) and returns `{key, outcome: "stopped"|"exited"|"still_working"|"unknown"}`. Never escalates to kill.

### 3.4 Framed Attach Stream (`terminal.attach`)

- Framed-mode clients can send raw PTY input via `Kind::Data` (0) or terminal query replies via `Kind::AutomaticInput` (6), plus `Kind::Resize` (3), `Kind::Focus` (4), `Kind::Facts` (5). Factory v2 shouldn't need raw attach streams for messaging — `terminal.send` and `terminal.keys` handle everything cleanly.

---

## 4. Reading Pane Output

### 4.1 `hc read` / `terminal.read`

Verified live:
```sh
hc read --json [--lines 100] <terminal>
```
JSON-RPC: `{"terminal": "t1", "lines": 100, "caller": null}` → `ReadResult`:
```json
{"complete": true, "how": "screen", "text": "FOO=bar42 ID=t1 PWD=/tmp/hc-exp-vkvqwthq"}
```
- Works on both `live` and `finished` terminals (plain text, escapes stripped).
- `lines: 0` (default): visible screen only (`how: "screen"`).
- `lines > 0`: screen + scrollback history (`how: "history"` for main screen, or `"stitched"` for full-screen apps by scrolling and restoring view).
- **Important safety rule:** If the terminal's OSC 7501 status is `working`, `blocked`, or `error`, history reads (`lines > 0`) do **not** send scroll input; they immediately return the visible screen only with `complete: false, why: "<status>"`.

### 4.2 `terminal.attach` (Live Stream)

- Opens a framed channel delivering initial paint (+ optional `history` main-screen lines) and live VT bytes (`Kind::Data`).
- **Known `hc` caveat** (`rodeo/docs/plan.md`): `herdr-core #111` — `attach --observe` on a busy terminal uses ~99% CPU per stream; `herdr #5814` — detaching a smaller non-observe viewer can leave stale size; detaching the last viewer resizes the terminal back to headless `200x120`. Avoid attaching from `factory.py`; use `terminal.read` and `events.subscribe`.

---

## 5. Event Stream / Subscription API

Verified via `hc events --help`, `hc schema`, `rodeo/crates/hc/src/{model,stream,terminals}.rs`, and live `hc events --json` capture.

### 5.1 Subscribing

- **CLI:** `hc events --json [--terminal <t>] [--children-of <t>] [--label K=V ...]` (prints snapshot line first, then one JSON-RPC notification line per event).
- **JSON-RPC (`events.subscribe`):**
  - Params: `{"terminal": null, "children_of": null, "labels": {}}` (all optional; empty object = unfiltered).
  - **Initial response (`SubscribeResult`):**
    ```json
    {"channel": null, "seq": 2, "terminals": [ /* matching Terminal records */ ]}
    ```
    In plain mode `channel` is `null` and the socket becomes notification-only. In framed mode `channel` is a `u32` delivering newline-delimited JSON notifications (client must return `Kind::Credit` bytes consumed).
- **Critical filter rule (verified in `rodeo/crates/hc/tests/events.rs:103`):** `doc` events are delivered **only to unfiltered subscribers** (`terminal`, `children_of`, and `labels` all empty).

### 5.2 Notification Envelopes & All 13 Event Types

Three JSON-RPC notification methods arrive on the stream:
1. `{"jsonrpc":"2.0","method":"event","params":{"seq":5,"terminal":"t2","type":"<kind>","data":{...}}}` (`machine` field present for remote events; `terminal` is omitted/null for `doc` events).
2. `{"jsonrpc":"2.0","method":"heartbeat","params":{"seq":42}}` (every 15 s).
3. `{"jsonrpc":"2.0","method":"missed","params":{"seq":57,"terminals":[...]}}` (if subscriber fell behind; replace local state with snapshot and ignore `seq <= snapshot.seq`).

All 13 `type` values and exact `data` shapes (verified against `hc 0.1.20` live & `rodeo/crates/hc/src/model.rs`):

| `type` | `data` shape |
|---|---|
| `created` | Full `Terminal` record |
| `restarted` | Full `Terminal` record (kept terminal recovered) |
| `lost` | `{"reason": "signaled"\|"holder_lost"\|..., "exit_code": null, "signal": 9, "finished_ms": null}` |
| `finished` | `{"reason": "exited"\|"killed"\|..., "exit_code": 0, "signal": null, "finished_ms": 17955...}` |
| `removed` | `{}` |
| `keep` | `{"keep": true\|false}` |
| `status` | `{"status": <Status\|null>, "records": {<id>: <Status>}, "reported": true}` |
| `turn` | `{"turn": 1, "state": "blocked"\|"done"\|"idle"\|"error"\|null}` |
| `title` | `{"title": "window title"}` |
| `labels` | `{"labels": {"k": "v", ...}, "record": <Terminal\|omitted>}` (`record` included when a terminal enters a label filter) |
| `size` | `{"cols": 100, "rows": 30}` |
| `foreground` | `{"pid": 3401843, "name": "sleep", "argv": ["sleep","5"], "cwd": "/..."}` or `null` |
| `doc` | `{"name": "herdr-app", "version": 4}` (`terminal: null`, optional `machine`) |

---

## 6. OSC 7501 (Program Status Protocol)

### 6.1 Escape Format & Capability Probe

Verified from `pi`'s `program-status.js`, `claude`/`opencode`/`omp` binaries, and live experiments on `hc 0.1.20`:

- **Status report format:**
  ```text
  \x1b]7501;<key>=<value>:<key>=<value>...\x1b\\
  ```
  Terminator is ST (`\x1b\\`) or BEL (`\x07`). Key/value pairs are colon-separated (`:`).
- **Keys:**
  | Key | Values / Encoding | Notes |
  |---|---|---|
  | `state` *(required)* | `idle`, `working`, `done`, `blocked`, `error`, `clear` | `state=clear` clears root + subrecords (`status: null`, `records: {}`, `reported` stays `true`). `state=clear:id=<id>` clears one subrecord. |
  | `app` | `[A-Za-z0-9_.+-]{1,32}` | Agent/program identifier (e.g. `claude-code`, `codex`, `pi`, `opencode`). |
  | `kind` | `permission`, `question`, `auth` | Used with `state=blocked` only. |
  | `progress` | integer `0..100` | Used with `working` or `blocked`. |
  | `title` | Base64-encoded UTF-8 | Verified live: `title=VGVzdA==` decoded by `hc` to `"title": "Test"`. |
  | `msg` | Base64-encoded UTF-8 (pi caps raw at 2048 B) | Verified live: `msg=YWxsb3c/` decoded by `hc` to `"msg": "allow?"`. |
  | `id` | non-empty string | Subrecord key stored in `terminal.records[id]`; omit for root record (`terminal.status`). |
- **Feature probe & `hc 0.1.20` reply (verified live, even headless with no viewer attached):**
  - Program writes: `\x1b]7501;?\x1b\\`
  - `hc 0.1.20` replies immediately on the PTY stdin:
    ```text
    \x1b]7501;?:states=idle,working,done,blocked,error:kinds=permission,question,auth\x1b\\
    ```
- **Shell-return auto-clear:** Per `hc --skill`, on Unix `hc` automatically clears status when the root interactive shell returns to the foreground (direct commands and shell-script wrappers keep status while their child tools run).

### 6.2 `status`, `turn`, and `herdr.seen` Semantics

Verified live against `hc 0.1.20` and `rodeo/crates/rodeo/src/{agents.rs,app/reconcile.rs}`:

1. **Agent detection:** A terminal is considered an agent when `terminal.status != null` (`status: null` = plain program or cleared). `reported: true` persists after `state=clear`.
2. **`turn` counter (maintained by `hc`):** Starts at `0`, increments by `1` every time the root `status.state` **leaves `working`**.
   - Verified live: `idle` (turn 0) → `working` (turn 0) → `blocked` emits `status` event then `turn` event `{"turn": 1, "state": "blocked"}`. Subsequent `blocked` → `done` → `error` do **not** increment `turn` again until the agent enters `working` and leaves it again.
3. **`herdr.seen` label (maintained by UI clients `rodeo` / `herdr-app`, NOT by `hc`):**
   - UI clients compute displayed state (`rodeo/crates/rodeo/src/agents.rs:64-75`):
     - `working` → **Working**
     - `blocked` or `error` → **Blocked** (error counts as blocked in UI!)
     - anything else (`done`, `idle`, etc.) → **Done** if `turn > seen` (where `seen = int(labels["herdr.seen"])`, or `turn > 0` if unset), else **Idle**.
   - UI clients raise `herdr.seen=<turn>` once per turn only while the pane is visible, focused, and the host terminal/window has focus.
4. **Using `turn` / `seen` in Factory v2:**
   - **Important:** Do **not** overwrite `herdr.seen` from Factory orchestration code, because `rodeo`/`herdr-app` own `herdr.seen` to show unread human notifications ("Done" badge).
   - Instead, Factory v2 can observe turn completion cleanly via:
     - `terminal.turn` monotonically advancing past the `turn` recorded when `terminal.send` delivered a message (plus checking `status.state` for `done`/`idle`/`blocked`/`error` and `state == "finished"`), **or**
     - `hc wait <t>` / `hc send --wait` (which already tracks the exact `turn` at delivery for that `caller`!), **or**
     - A Factory-owned label (e.g. `factory.seen_turn`) or in-memory/state-file cursor if a daemon/command tracks unread completions independently of human focus.

### 6.3 Native OSC 7501 Support Across All 6 Factory Harnesses

**Finding: All 6 harnesses installed on this machine have native OSC 7501 code built in — none require an external screen-scraping wrapper.**

| Harness | Installed Version | Native OSC 7501? | Verified Details (Binary / Source Inspection) | Overrides / Caveats |
|---|---|---|---|---|
| **Claude Code** (`claude`) | `2.1.330` | **Yes (native)** | Probes `OSC 7501 ; ?`, emits `app=claude-code`, supports `state`, `id`, `kind`, `progress`, `title`, `msg`, `state=clear`, `state=clear:id=...`. | **[UNVERIFIED live runtime]** Exact states emitted across turns not live-captured in an authenticated session. |
| **Codex** (`codex`) | `0.171.0` | **Yes (native)** | `codex_tui::terminal_program_status` emits `]7501;state=...:app=codex...:kind=...` (`blocked`/`permission`, `working`, etc.) and `]7501;state=clear` (also emits `]21337;`). | **Required flag (from `hc --skill`):** run Codex with `--no-daemon`, otherwise its commands execute outside `hc`. **[UNVERIFIED live runtime]** Probe behavior & exact `done`/`idle` transitions. |
| **pi** (`pi`) | `1.6.0` | **Yes (native)** | Inspected JS source (`@earendil-works/pi-tui/dist/program-status.js` & `program-status-reporter.js`): probes `\x1b]7501;?\x1b\\`; emits `app=pi`, `state` (`working`, `blocked`, `done`, `error`, `idle`), `kind`, base64 `msg`. | `PI_PROGRAM_STATUS=1\|0` overrides probe. |
| **omp** (`omp`) | `19.1.0` | **Yes (native)** | Probes `\x1b]7501;?\x1b\\\x1b[c`; emits `state`, `kind`, `app=...`, base64 `msg`, `state=clear`. Controlled by setting `terminal.programStatus` (default `true`). | `PI_TUI_PROGRAM_STATUS_PROBE=1` forces probe in multiplexers. **[UNVERIFIED live runtime]** Exact `app=` string value. |
| **OpenCode** (`opencode`) | `2.2.4+45898d0` | **Yes (native)** | OpenTUI (`ot_session_program_status` / `setProgramStatus`) probes `\x1b]7501;?\x1b\\`; OpenCode TUI (`vl="opencode"`) reports root aggregate status AND per-session subrecords (`id=...`) plus `state=clear`. | **[UNVERIFIED live runtime]** Exact state mapping & whether any config disables it. |
| **agy** (`agy`) | `1.5.14` | **Yes (native)** | Added in `1.4.0` (changelog embedded in binary); probes `\x1b]7501;?\x1b\\`; reports working, waiting/blocked, done, error; fixes waiting-for-subagents staying `working`. | `AGY_CLI_OSC7501=on\|off`, `AGY_CLI_OSC7501_MESSAGES=brief`. **[UNVERIFIED live runtime]** Exact `app=` string value. |

Because `hc 0.1.20` automatically answers `\x1b]7501;?\x1b\\` immediately on PTY creation (even headless), all 6 harnesses' startup probes should succeed inside `hc` without needing env-var forcing (though env overrides exist for `pi`, `omp`, `agy` if ever needed). Live-testing one real launch per harness inside an isolated `hc` session during implementation is recommended to confirm each harness's exact initial `idle` vs `working` and `app=` values.

### 6.4 How a Client Observes State Changes

1. **Event-driven (recommended for daemons/UIs):** `events.subscribe` (or `hc events --json`) receives `status` and `turn` events immediately on every OSC 7501 report.
2. **Blocking wait (recommended for CLI / agent turns):** `hc wait <t>` / `hc wait --children any|all` / `hc send --wait <t> <text>` — blocks until status leaves `working` (or child news / incoming message / timeout).
3. **Point-in-time query:** `hc get <t> --json` or `hc ls --json` returns current `status`, `status_ms`, `records`, `reported`, `turn`, `state`, and `foreground`.

---

## 7. Metadata for Naming Members (`@lead-dev`, etc.)

| Mechanism | Allowed Values | Indexed / Addressable by `hc`? | Visible in `rodeo` / `herdr-app`? | Recommendation for Factory v2 |
|---|---|---|---|---|
| **Terminal `name`** (`--name`) | 1–32 chars of `a-z 0-9 - _ .`, must start with letter/digit, not ID-shaped. **Rejects `@`** (verified live: `bad name "@lead-dev"`). Unique among live terminals. | **Yes** — directly usable as `<t>` in `hc send lead-dev`, `hc get lead-dev`, `hc read lead-dev`, `hc kill lead-dev`. | Shown in `hc ls` and `<hc from="t3 lead-dev">` tags. | Set to the member's bare name (`lead-dev`, `architect-3`, `operator`) — or `<team>-<member>` if multiple teams can share one `hc` session. |
| **Terminal `labels`** (`hc label <t> k=v`) | Free-form UTF-8 `key=value`; `k=` deletes. **Accepts `@lead-dev`** (verified live). | **Filterable** in `terminal.list` and `events.subscribe` (`--label K=V`). | `name` label is read by `rodeo`'s `$name` sidebar token (`parity.md:116`). `herdr.space`, `herdr.tab`, `herdr.seen`, `where` drive layout/UI. | Set `name=@lead-dev` (for rodeo sidebar `$name`), plus structured labels like `factory.team=<id>`, `factory.member=lead-dev`, `factory.role=developer` for discovery and filtering. |
| **`herdr-app` doc block `title`** | Free-form string on the `block` `Node` in the `herdr-app` doc. | No (layout doc only). | **Yes** — rendered as the pane border title in `rodeo` and preserved across `herdr-app` writes (`parity.md:25,122`). | Set `title: "@lead-dev"` when creating the pane block in `herdr-app` doc. |

---

## 8. Notification & Messaging Primitives vs Factory v1's Inbox Wake

### 8.1 What `hc` Provides Natively

1. **`hc send` (`terminal.send`):**
   - Replaces v1's brittle paste/enter timing + screen-idle heuristics with core-managed draft detection (`draft_wait`), quiet pause, `--if idle|working`, and `blocked` refusal (exit `9`).
   - Tags agent-to-agent messages automatically as `<hc from="t3 w1">...</hc>` (verified live; toggleable off via `notes = false` in `config.toml`).
2. **Automatic parent notes (verified live):**
   - When a child terminal created with `--parent <op>` (or from inside `<op>`) becomes `blocked`, hits `error`, or exits, `hc` automatically types a note into the parent's PTY (unless `--no-notify` or `notes = false`):
     - `<hc>t3 w1 exited (exit 0). Read: hc read t3</hc>`
     - `<hc>t4 w2 is blocked (permission). See: hc read t4</hc>`
3. **`hc wait` (`terminal.wait`):**
   - `hc wait <t>`: waits until `<t>` leaves `working` (or exits / prints `--output X` / times out).
   - `hc wait --children any|all`: waits for child news (`done`, `blocked`, `error`, `exit`) OR an incoming message to the caller (`reason: "message"`, `from: <sender|null>`).
   - *Verified nuance:* In our live test where `op` was `idle` (not `working`) and called `hc wait --children any` while a child was `working`, sending a message to `op` did **not** wake `hc wait` with `reason: "message"` (it timed out) — matching `hc wait --help`: *"for the parent itself, also a message typed into it while it worked (reason `message`)"*. **[UNVERIFIED live]** Whether `reason: "message"` triggers when `op` is in `state=working` (as a real agent is while executing a bash tool call).

### 8.2 What `hc` Does NOT Replace (Implications for Factory v2's Inbox)

| v1 Inbox / Wake Concern | Covered by `hc`? | Gap / Design Implication |
|---|---|---|
| Typing into an agent without clobbering a human's draft or answering a permission dialog | **Yes (`hc send`)** | `hc send` handles draft wait, quiet pause, and `blocked` refusal natively. Huge simplification over v1. |
| Knowing whether a member is `working`, `idle`, `done`, `blocked`, or `error` | **Yes (OSC 7501 + `hc get`/`hc events`)** | Replaces v1's per-harness hooks/plugins and screen scraping completely. |
| Notifying `@operator` when a member crashes or blocks on a permission prompt | **Mostly yes (parent notes + `hc events`)** | Works natively if members have `--parent <operator>` (and `notes` enabled). Note: `--outlive-parent` is needed if members shouldn't die when the operator restarts! |
| **Durable operator inbox when `@operator` is busy (`working`) or offline** | **No** | `hc send` pastes immediately into PTY stdin (even while `working`, unless `--if idle` is passed — in which case it fails with `status_mismatch` and drops nothing into a queue). Different harnesses handle mid-turn stdin inconsistently (queuing vs interrupting vs ignoring). |
| **Message history, ack/seen IDs, and guaranteed delivery across restarts** | **No** | `hc` has no message queue or persistence for messages. A lightweight file-backed inbox (like v1's JSONL inbox) OR `hc send --if idle` + inbox fallback when operator is `working` is still valuable for reliable `@operator` delivery. |
| **Waking an idle `@operator` when an inbox message arrives** | **Yes (`hc send --if idle`)** | If Factory v2 keeps an inbox for `@operator`, waking an idle operator becomes a single `hc send --if idle <operator> "..."` (zero custom wake daemon needed when `status == idle`; if `working`, queue in inbox and let operator drain on next turn or via `hc wait` / `factory wait`). |

---

## 9. Summary of Verified Gaps & Unverified Items

1. **Verified gaps / gotchas in `hc 0.1.20`:**
   - `hc run` CLI has **no `--env` flag** (must use JSON-RPC `terminal.run` or `env K=V ...`).
   - `hc` has **no CLI subcommand for `doc.get` / `doc.set`** (must speak JSON-RPC over the Unix socket; plain NDJSON mode is sufficient — no framing needed).
   - `events.subscribe` delivers `doc` events **only to unfiltered subscribers**.
   - `hc run --name` rejects `@` (`a-z 0-9 - _ .` only); use `name=@lead-dev` label + doc block `title: "@lead-dev"` for display names.
   - `herdr-app do` only talks to a running GUI `herdr-app` window; it fails under `rodeo` or headless.
   - Codex must be launched with `--no-daemon` inside `hc`.
   - `hc attach --observe` has known high-CPU bug (`herdr-core #111`); use `terminal.read` + `events.subscribe` instead.
   - `notes = false` in `~/.config/herdr-core/config.toml` is a user-level setting that disables both `<hc from=...>` tags and parent auto-notes; Factory v2 should not rely solely on `<hc from=...>` tags for sender attribution if users may set `notes = false`.
2. **Marked [UNVERIFIED]:**
   - Live authenticated runtime capture of OSC 7501 sequences emitted by each of the 6 harnesses (`claude`, `codex`, `pi`, `omp`, `opencode`, `agy`) inside `hc` across full turns (startup trust dialogs for Claude Code, Codex, and agy are now verified in §10.3; full turn transitions and `omp`/`opencode`/`pi` live runs remain unverified).
   - Live reproduction of `hc wait` returning `reason: "message"` when the waiting parent is in `state=working`.
   - Live visual verification of `rodeo` adopting vs reading a `factory.py`-written `herdr-app` doc (verified from `rodeo`'s Rust source `crates/doc` and `crates/rodeo/src/app/reconcile.rs` + tests).

---

## 10. Open-question checks

All checks below were executed live on 2026-11-26 against isolated `hc 0.1.20` cores (`HC_SESSION=oq12|oq2b|oq3`, isolated `HOME` / `XDG_RUNTIME_DIR` / `XDG_STATE_HOME` / `XDG_CONFIG_HOME`, stopped via `hc server stop` after each test; owner sessions untouched).

### 10.1 Draft timeout and "human" keys — **[VERIFIED]**

**Setup:** Isolated core with `$XDG_CONFIG_HOME/herdr-core/config.toml` setting `draft_wait = 2`, `draft_wait = 5`, and `draft_wait = 8` (without `HC_SETTLE_MS=0`, so real settle/draft timers ran). Target terminal ran a raw-TTY Python logger emitting `\x1b[?2004h\x1b]7501;state=idle:app=fake-agent\x1b\\` and logging timestamped stdin bytes.

**Commands tested:**
```sh
# 1) Simulate human typing via framed terminal.attach Kind::Data (code 0), no Enter:
#    frame(channel, kind=0, b"still typing")
#    then immediately:
hc send --json <t> "pushed after 5s"

# 2) Clear draft mid-wait (draft_wait = 8):
#    frame(channel, kind=0, b"unfinished"), start `hc send`, wait 1.5s, send frame(channel, kind=0, b"\r")

# 3) Non-human input (draft_wait = 8):
hc keys --json <t> x y z && hc send --json <t> "after hc keys"
#    and framed terminal.attach Kind::AutomaticInput (code 6):
#    frame(channel, kind=6, b"auto"), then `hc send --json <t> "after auto input"`
```

**Verified behavior:**
1. **After `draft_wait` expires, `hc send` DOES type into the draft and presses Enter (no error):**
   - With `draft_wait = 2`: `b'human draft'` at `t+0.00s` → `b'\x1b[200~agent msg\x1b[201~'` at `t+3.00s` (`max(draft_wait=2s, quiet=3s)`) → `b'\r'` at `t+3.20s`; exit code `0`, `{"started":false,"status":"idle","turn":0}`.
   - With `draft_wait = 5`: `b'still typing'` at `t+0.00s` → `b'\x1b[200~pushed after 5s\x1b[201~'` at `t+5.00s` → `b'\r'` at `t+5.20s`; exit code `0`.
   - The human's unsent text is concatenated with the pasted message and submitted by Enter.
2. **Sending Enter clears the draft early:** With `draft_wait = 8`, `hc send` remained blocked at `t+1.5s`; sending `\r` via `attach` `Kind::Data` at `t+1.70s` released `hc send` `0.20s` later (`t+1.90s`).
3. **What counts as a "human" key:**
   - **Counts:** Framed `terminal.attach` `Kind::Data` (code `0`) — i.e., interactive viewer input (`rodeo`, `herdr-app`, `hc attach`).
   - **Does NOT count:** `hc keys` (`terminal.keys`) and framed `terminal.attach` `Kind::AutomaticInput` (code `6`). Both let `hc send` proceed without waiting `draft_wait = 8s` (total elapsed ~5.5–5.7 s, which is settle + 5 s `started` observation window).
4. **No public draft-inspection API:** `hc` tracks `last_human_ms`, `draft`, `pasting`, `draft_ended` internally (confirmed in binary strings), but none of these fields are exposed on `Terminal`, `terminal.get`, or `events.subscribe`, and `terminal.send` has no fail-on-draft option in `0.1.20`.

### 10.2 Sender tag (`<hc from=...>`) and `caller` override — **[VERIFIED]**

**Setup:** Isolated core with OSC 7501 `idle` terminals `op` (`t5`), child `w1` (`t6`, `--parent op`), sibling `w2`, unrelated OSC 7501 agent `unrel`, and plain non-OSC-7501 terminal `peer1` (`sleep 30`). Tested CLI `hc send` and JSON-RPC `terminal.send` under default config (`notes` on) and `notes = false`.

**Commands tested:**
```sh
HC_TERMINAL_ID=<sender> hc send --json <target> "<text>"
hc send --json <target> "<text>"   # HC_TERMINAL_ID unset
# JSON-RPC terminal.send with caller: "<addr>" | null | omitted | "<name>" | "t999"
# Inside a terminal: unset HC_TERMINAL_ID; hc send --json <op> "inside-unset-env"
```

**Verified matrix (exact bytes delivered inside `\x1b[200~...\x1b[201~` + `\r`):**

| Invocation | `notes` default (on) | `notes = false` |
|---|---|---|
| CLI `HC_TERMINAL_ID=<child agent>` → parent agent | `<hc from="t6 w1">msg-c2p</hc>` | `nf-c2p` |
| CLI `HC_TERMINAL_ID=<parent agent>` → child agent | `<hc from="t5 op">msg-p2c</hc>` | `nf-p2c` |
| CLI `HC_TERMINAL_ID=<sibling agent>` → sibling agent | `<hc from="t2 w1">sib-w1-to-w2</hc>` | *(untested; same path)* **[UNVERIFIED]** |
| CLI `HC_TERMINAL_ID=<unrelated agent>` → agent | `<hc from="t4 unrel">unrel-agent-to-op</hc>` | *(untested; same path)* **[UNVERIFIED]** |
| CLI `HC_TERMINAL_ID=<plain non-agent>` → agent | `msg-peer2op` *(no tag!)* | — |
| CLI outside (`HC_TERMINAL_ID` unset) → agent | `msg-outside` *(no tag)* | `nf-outside` |
| CLI inside plain `sh` terminal with `unset HC_TERMINAL_ID` → parent agent | `inside-unset-env` *(no tag)* | — |
| JSON-RPC `caller: "<agent addr>"` (e.g. `"t6"`) | `<hc from="t6 w1">rpc-caller-child</hc>` | `nf-rpc-caller` |
| JSON-RPC `caller: null` | `rpc-caller-null` *(no tag)* | `nf-rpc-null` |
| JSON-RPC `caller` omitted | `rpc-caller-omitted` *(no tag)* | — |
| JSON-RPC `caller: "<plain terminal name>"` (`"peer1"`) | `rpc-caller-name` *(no tag)* | — |
| JSON-RPC `caller: "t999"` (nonexistent) | `rpc-caller-bogus` *(no tag, no error)* | — |

**Key findings:**
- `hc` wraps text in `<hc from="<addr> <name>">...</hc>` when `notes` is enabled, the receiver is an agent (OSC 7501 status present), **and** `caller` resolves to a terminal that also reports OSC 7501 status (regardless of parent/child relationship — sibling and unrelated agents both get tagged, while a non-agent `sleep` terminal does not).
- **Per-call override works cleanly:** via JSON-RPC `terminal.send`, omitting `caller` or passing `"caller": null` suppresses `<hc from=...>` per call even when `notes` stays enabled globally for parent alerts (§8.4). Passing `"caller": "<agent-addr>"` explicitly enables tagging from any external process.
- **[UNVERIFIED]:** Whether CLI `unset HC_TERMINAL_ID; hc send ...` run from inside a process whose parent terminal **is** an OSC 7501 agent falls back to parent-PID caller detection (in our test the runner was a plain `sh` terminal). Using JSON-RPC `terminal.send` directly avoids any env/PID ambiguity.

### 10.3 Readiness on fresh untrusted directory (startup trust dialogs) — **[VERIFIED]**

**Setup:** Isolated `hc 0.1.20` core (`HC_SESSION=oq3`, temp `HOME`/`XDG_*`). Created fresh `git init` dir `/tmp/hc-oq3-.../untrusted-repo`. Spawned each harness via JSON-RPC `terminal.run` (`size: {cols: 120, rows: 35}`, `no_parent: true`, `env: {HOME: "/home/peter", PATH: ...}` so existing login credentials applied and the workspace trust dialog appeared). Read `hc get --json` and `hc read --json` at +1.5 s, then immediately ran `hc kill` without accepting any dialog (confirmed `~/.gemini/antigravity-cli/settings.json` sha256 unchanged).

**Verified results:**

| Harness & command | Observed `hc get` state at +1.5 s | Screen dialog text |
|---|---|---|
| **Claude Code 2.1.330** (`["claude"]`) | `reported: true`, `turn: 0`, `status: {"app": "claude-code", "kind": "permission", "msg": "trust this folder", "state": "blocked"}` | `"Quick safety check: Is this a project you created or one you trust?"` / `❯ No, exit` / `Yes, I trust this folder` |
| **Claude Code 2.1.330** (`["claude", "--permission-mode", "bypassPermissions"]`) | `reported: true`, `turn: 0`, `status: {"app": "claude-code", "kind": "permission", "msg": "trust this folder", "state": "blocked"}` | Same workspace trust dialog shown first (before any bypass confirmation) |
| **Codex 0.171.0** (`["codex", "--no-daemon"]`) | **`reported: false`, `turn: 0`, `status: null`** | `"Folder access ... Trust this folder?"` / `› 1. Trust and continue` / `2. Back to Agent Command Center` |
| **agy 1.5.14** (`["agy", "--mode", "accept-edits", "--settings", '{"toolPermission":"always-proceed"}']`) | `reported: true`, `turn: 0`, `status: {"app": "antigravity", "kind": "permission", "msg": "Trust this folder?", "state": "blocked"}` | `"Do you trust the contents of this project?"` / `> Yes, I trust this folder` / `No, exit` |

**Key findings:**
- **Claude Code** and **agy** both report OSC 7501 `blocked` (`kind: "permission"`) on their workspace trust dialog (and `agy` reports `app: "antigravity"`).
- **Codex 0.171.0 stays silent (`status: null`, `reported: false`)** on its trust dialog — so Factory v2 **cannot** drop the narrow screen-read check for silent terminals in §6.3 if Codex is supported.
- **[UNVERIFIED]:** Claude Code's second dialog (`--permission-mode bypassPermissions` confirmation) after trust is accepted, and `pi`/`omp`/`opencode` startup dialogs (none are configured in `harnesses.toml`).
