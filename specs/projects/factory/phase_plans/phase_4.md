---
status: draft
issue: 168
phase: 4
branch: peter/168-keeper-delivery
owners: ["@architect", "@lead-dev"]
---

# Phase 4: Keeper delivery and `say`

Base `300a8bf` (Phase 3 merged as `a8385b4`). Line numbers below are `skill/scripts/factory.py` at that commit. Plan reviewer: @architect-3. Writer: @lead-dev. Follower: @lead-dev-3.

Read first: [keeper.md](../components/keeper.md) §1–4 and §8, [cli_skill.md](../components/cli_skill.md) §1–2, [message_log.md](../components/message_log.md) (interface, "`say`: waiting for outcomes", "`inbox` output"), [functional_spec.md](../functional_spec.md) §8.1–8.7, [testing.md](../components/testing.md) §2 and §4. This plan adds only what those leave open, and names every place where it changes them.

## 1. Decision and scope

Build the keeper's delivery loop, `ensure_keeper`, the v2 `say` and `inbox` commands, the team helpers they share with Phase 5a, and move the messaging lines of `status` and `up` to the v2 log. Remove v1's `wait`, `say`, `Inbox` and what only they used.

**In this phase**

| Part | Source |
|---|---|
| `Keeper` start-up, tick steps 1–5 and 8, send results, exit rules, pane lines | keeper.md §1–4, §7 (lines only), §8 without the version check |
| `head_of`, `decide` (all twelve rows, N1 included) | keeper.md §3 |
| `ensure_keeper`, `cmd_keeper`, `read_beat` | keeper.md §8 |
| `team_key`, `state_dir`, `open_log`, `read_team`, `update_team`, `resolve_team`, `find_team` | architecture.md §3, cli_skill.md §1 |
| `cmd_say`, `wait_outcomes`, `outcome_lines` | cli_skill.md §2 |
| `cmd_inbox`, `pending_lines`, the pending lines of v1's `cmd_status` and `cmd_up` | cli_skill.md §2, the cutover decision on #168 |
| `Log.locked` (public), `Log.refresh` | this plan §3 |

**Not in this phase** (do not build, stub or bridge): member notes, `keep` and `factory.last` (keeper.md §5), the quiet rule and ready check (§6), the pane title (§7), re-exec on a version change (§8), all Phase 6; `record_session`, labels, launch, the v2 `up`, `down`, `status` table, keeper and operator status lines, and `brief --send` on the log, all Phase 5a (#169); layout and leftover files, Phase 5b.

**Decided, not to reopen**
- Order across an abrupt keeper crash: a spec sentence (functional_spec.md §8.5, `300a8bf`), no Linux helper. Nothing to build.
- Cutover: no migration, dual reader or flag. v1's files are never opened by v2 code.
- #168 merges before #169. Between the two merges `master` is a development build: v1's herdr `up` starts members, and v2's `say` answers `nothing sent: run up first` because no terminal carries the `hc` labels yet. This phase is proven by tests that label terminals themselves. No stub keeper, fallback delivery or second log.

**Rejected**
- *A `wait` parameter on `ensure_keeper`* (my first proposal to the team): keeper.md §8 step 6 already waits up to 5 s for the first heartbeat for every caller. The signature stays `ensure_keeper(cfg, state, hc, view, force=False)`.
- *A `clock` parameter on `wait_outcomes`*: testing.md §4 fixes the rule: the wait loop reads the module's own `time` binding, which tests replace on the `factory` module.
- *Keeper refresh by `log.read(box.offset)` plus `box.apply`* (keeper.md §2 step 4 as written): it skips the lineage check, so a log repaired by hand could leave old text in the box at the same offset. Use `Log.refresh` (§3).
- *Typing text held from before the claim*: the keeper builds the wire text from `box.msgs[id]` only after `log.out(…, "sending")` returned True, because that call has just brought the box up to the validated file.

## 2. Interfaces shared with Phase 5a

Agreed with @architect-2, @lead-dev and @lead-dev-2. #169 calls these and must not copy them.

```python
SCRIPT = pathlib.Path(__file__).resolve()                      # new constant, next to SKILL
KEEPER_START = 5                                               # seconds ensure_keeper waits for a new keeper's first heartbeat

def cache_base() -> pathlib.Path                               # $XDG_CACHE_HOME or ~/.cache (moved out of Config.cache)
def team_key(root: pathlib.Path) -> str                        # f"{root.name[:24]}-{sha1(str(root))[:8]}" (moved out of Config.cache)
def state_dir(root: pathlib.Path, core: str) -> pathlib.Path   # cache_base() / "factory-skill" / team_key(root) / core[:8]
def open_log(state) -> Log                                     # Log(state, base=state.parents[2]) after the shape check of §3
def read_team(state) -> dict                                   # team.json, or {} when it or the directory is missing
def update_team(state, reset=False, **changes) -> dict         # merge the given top-level keys, atomically; returns the result
def state_for(cfg: Config, hc: Hc | None) -> pathlib.Path | None   # the checkout's state directory: §4 steps 2 and 3
def resolve_team(args, hc: Hc | None) -> tuple[Config, pathlib.Path | None, dict | None]
def find_team(args) -> tuple[Config, pathlib.Path | None, dict | None]
def ensure_keeper(cfg, state, hc, view, force=False) -> str | None
def read_beat(state) -> tuple[int, str, str, float] | None     # pid, version, terminal ID, age in seconds; None if missing or unparsable
def wait_outcomes(log: Log, ident: int, recipients: list[str], offset: int, timeout: float = SAY_WAIT) -> dict[str, dict]
def outcome_lines(outcomes: dict[str, dict], operator: str, sender: str) -> tuple[list[str], int]
def pending_lines(state, operator: str, now: float | None = None) -> list[str]
def mark_stopped(state, terms: list[str], box: Box) -> None    # down's stop evidence in team.json (§7.5)
def settle_refusal(log: Log, box: Box, ident: int, to: str, term: str, why: str) -> str | None   # the keeper's use of it (§7.5)

class Log:
    def open_dir(self, create: bool) -> int | None   # today's _open_state, renamed: the verified no-follow directory fd, no lock; the caller closes it
    def locked(self, exclusive: bool)      # today's _locked, renamed; a context manager yielding the directory fd, or None
    def refresh(self, box: Box) -> None    # LOCK_SH; lineage-aware; raises LogDamage
```

`Box` gains `sending_terms() -> set[str]`: the terminal IDs named by pairs whose current outcome is `sending`.

Changed from the list I sent the team: `ensure_keeper` has no `wait`; `wait_outcomes` has no `clock`; `print_outcomes` became the pure `outcome_lines` (the caller prints, and prints `message N` itself); `update_team` gained `reset`.

What #169 adds on top, and where:
- **Stop evidence, in `down`.** After a stop is confirmed for a member's terminal (`hc.kill` returned, or the record was already finished) and before `hc.rm` and before `log.drop_queued`: `mark_stopped(state, [address, …], box)` with the full box `down` already reads. A kill that failed records nothing. `up` never records a stop, whatever it removes. §7.5 says why the order matters.
- One line in `cmd_say`: `record_session(cfg, state, sender, term)` for a member. #168 leaves a comment at that exact place (§5 step 5) and no call.
- `up.lock`, opened relative to `Log.open_dir` (which this phase now provides, because the keeper's own files need it: §7). The keeper and status lines of `status`, built from `read_beat` and the view. The log-size warning above 64 MiB.
- `plan()`'s `draft_dir` must become `<state>/messages/<name>` (cli_skill.md §3). Until then the folder v1's `up` names is not a managed folder, so nothing is removed from it.
- It deletes `print_pending` (§6) when it rewrites `status` and `up`, and calls `pending_lines` directly.

## 3. State helpers and the lock

**Shape check.** A state path is accepted only when it is absolute, equals its own `lexical_path`, and has the form `<base>/factory-skill/<team>/<core8>` with non-empty last two parts. `open_log` raises `Problem(f"{state} is not a Factory state directory", "run factory.py up")` otherwise. The base is `state.parents[2]`: it comes from the state path itself and never from the caller's `XDG_CACHE_HOME`, so a member whose environment has another cache home still opens the operator's log.

**`Log.locked(exclusive)`.** Rename `_locked` (line 948) and its callers; no behaviour change. The lock is a `flock` on a freshly opened directory descriptor, and such a lock belongs to that open descriptor. **Never call a public `Log` method (`add`, `out`, `read`, `read_box`, `refresh`, `leftovers`) or `read_team`/`update_team` inside `locked()`: it opens a second descriptor and waits on its own process for ever.** Inside `locked()` do file work relative to the yielded descriptor only. A sequence such as "append a brief, then update `team.json`" takes the lock twice, one after the other. Put this rule in the docstring of `locked`.

**`Log.open_dir(create)`.** Rename `_open_state` (line 895) and its callers; no behaviour change. Every file Factory keeps in the state directory besides the log is opened relative to this descriptor with `private_open(name, flags, dir_fd=fd)`, never by path: `team.json`, `keeper.lock`, `keeper.start`, `keeper.beat`. `open_dir` takes no lock, so its descriptor may be held across an `hc` call, a wait or the keeper's lifetime; the team `flock` never is.

**`Log.refresh(box)`.**

```python
def refresh(self, box):
    """Bring a full box up to the validated file: its tail when the box's view belongs to the checkpoint's
    lineage, else a rebuild from the start. Reads only. Raises LogDamage; a partial box raises ValueError."""
    with self.locked(exclusive=False) as fd:
        if fd is None:
            box._reset()
            return
        self._refresh(box)
```

`_refresh` (line 1218) writes nothing: `_prove` only records a lineage in memory. A box rebuilt here under an unproven file gets a lineage no checkpoint has, so the next `out` rebuilds it once more under `LOCK_EX`; that is correct and costs one read.

**`team.json`** (architecture.md §3.3). Keys this phase reads: `root`, `team`, `core`, `up`, and the new `stopped` (a list of terminal IDs that `down` stopped on purpose; when present it must be a list of non-empty strings, else the file is unreadable). #169 writes them; this phase's tests write them with `update_team` and `mark_stopped`.

- `read_team(state)`: `with open_log(state).locked(False) as fd`; `fd is None` or no file: `{}`. Open with `private_open("team.json", os.O_RDONLY, fix_mode=False, dir_fd=fd)`. Not JSON, not an object, `v != 1`, or `root`, `team` or `core` not a non-empty string: `Problem(f"{state}/team.json cannot be read: {why}", "run factory.py up")`. So a dict this function returns is either empty or has those three strings; no caller indexes an unchecked key.
- `update_team(state, reset=False, **changes)`: `with open_log(state).locked(True) as fd`. Start from the current content (`{"v": 1}` when missing, or when `reset` is true, which is how `up` rebuilds an unreadable file; without `reset` an unreadable file raises the `Problem` above and nothing is written). Apply `changes` as top-level replacements; keep every other key. Write `team.json.<token>.tmp` with `O_CREAT | O_EXCL | O_NOFOLLOW`, mode 0600, relative to `fd`; `fsync` it; `os.replace(..., src_dir_fd=fd, dst_dir_fd=fd)`; `os.fsync(fd)`. On any `OSError` unlink the temp file and re-raise.

## 4. Finding the team

`resolve_team(args, hc)` follows cli_skill.md §1 and returns a third value, the caller's terminal record (or None), so commands do not ask `hc` twice.

1. `hc` is not None and `HC_TERMINAL_ID` is set: `me = hc.get(id)` (`HcError` `not_found`: `me = None`; `HcDown` propagates). If `me["labels"]` has non-empty `factory.team` and `factory.dir`: `state` is the label; `team = read_team(state)`. Raise `Problem(what, "run factory.py up")`, having written nothing, when: `team` is empty (`no team.json in <state>`); `team["team"]` differs from the label or from `state.parent.name`, or `team["core"][:8] != state.name` (`this pane's labels do not match <state>/team.json`); `team["core"] != hc.core` (`this pane's hc is another core than the team's`). Then `hc.expect_core = team["core"]` and return `(Config(team["root"]), state, me)`.
2. Otherwise `cfg = Config(getattr(args, "cwd", None) or os.getcwd())` and `state = state_for(cfg, hc)`; return `(cfg, state, me)`. `state_for` with `hc`: `key = team_key(cfg.root)`; the `factory.dir` label of the first of `view.operator`, `view.keeper`, then any record of `hc.list(key)` that has one, shape-checked (§3) and with `state.parent.name == key`; none: `state_dir(cfg.root, hc.core)`.
3. `state_for` without `hc`, as phase_3.md §7 handed over: the candidates are the directories directly under `cache_base()/factory-skill/<key>/` that are not symbolic links and contain a `log.jsonl` or a `team.json` (v1's session folders there have neither). Inspect each with `lstat`, following nothing; a `log.jsonl` that is a symbolic link or not a regular file is an error naming the path (`Problem`, exit 2 in `inbox`), not a skipped candidate. Prefer the candidate whose `log.jsonl` has the newest mtime; when none has a log, the one whose `team.json` is newest; ties go to the smaller pathname. With several candidates name the choice on stderr as `using <path>`. A team with only a `team.json` is a valid, empty inbox. No candidate: None.

`up` (#169) does not call `resolve_team`: step 1 requires a readable `team.json`, which `up` is the command that writes. It builds `Config` from the checkout, lets `check_operator` judge the caller's labels, and takes the state directory from `state_for(cfg, hc)`.

`find_team(args)` is for commands that also work offline (`inbox`, and the pending lines of v1's `status` and `up`): `hc = Hc() if os.environ.get("HC_TERMINAL_ID") else None`; `resolve_team(args, hc)`; on `HcDown` it returns `resolve_team(args, None)`.

Refactor `Config.cache` (line 423) to `cache_base() / "factory-skill" / team_key(self.root)`: same value as today.

## 5. `say`

Parser: `say NAME [NAME…] (--file PATH | TEXT)`, no `--cwd`. Without `--file` the last positional is the text and at least one name must come before it (a single positional: `give a message or --file`). With `--file` every positional is a recipient, so inline text given beside `--file` cannot be told from a name and is refused by step 6 as `@… is not on the team`; there is no separate "both" refusal.

A refusal prints `nothing sent: <reason>` on stderr and exits 2. Use one local helper `refuse(reason)`; do not raise `SystemExit(str)` (that exits 1). Order, first failure wins; nothing is stored before step 8:

1. `HC_TERMINAL_ID` unset: `run up first`.
2. `hc = Hc()`; `cfg, state, me = resolve_team(args, hc)` (`resolve_team` reads `getattr(args, "cwd", None)`, since `say` has no `--cwd`). `HcDown`: refuse with its detail. `Problem`: refuse with `f"{what}; {fix}"`. `me` is None or lacks `factory.team`/`factory.dir`: `run up first`.
3. `kind = factory.kind`: `keeper`: `the keeper cannot send`. Anything but `operator` or `member` (so `operator-lost` too): `run up first`.
4. `sender = factory.member` label; missing, empty or not a string: `run up first`. `operator = cfg.operator["name"]`. A member whose name is not in `cfg.members`: `@x is no longer in factory.toml`.
5. Comment only: `# Phase 5a: record_session(cfg, state, sender, me["address"]) for a member, before validation (launch_layout.md §3)`.
6. Names: `re.findall(r"[^,\s]+", " ".join(args.to))`, `@` stripped, duplicates removed in order. `view = team_view(hc, me["labels"]["factory.team"])`. Exactly `["all"]`: every configured member other than the sender with `view.terminal(name)`; none: `no other member is running`. Otherwise, per name: not the operator and not in `cfg.members`: `@x is not on the team (members: a, b, …)`; equal to the sender: `choose another teammate as the recipient`; a member with `view.state(name) == "not started"`: `@x is not running`.
7. Text: a member with inline text: `members send with --file`. Neither `--file` nor text: `give a message or --file`. The file's recorded spelling is `recorded = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)`: made absolute, **never normalized**. Collapsing `..` here would let `messages/<sender>/link/../x.md` be read through a link and a different, managed `x.md` be removed later; with the spelling kept, `lexical_path` returns None for any path with `..`, so `managed_file` and `_newest_use` never match it. A file in the sender's folder (`lexical_path(recorded)` is not None and its directory is `f"{state}/messages/{sender}"`) whose name does not match `MESSAGE_FILE`: v1's sentence (line 2849, without its own `nothing sent:`). Read with `pathlib.Path(path).read_bytes().decode("utf-8")` so the text is exact; `OSError` or `UnicodeError`: `cannot read message file <path>: <error>`. Blank text: `give a non-empty message`. `len(text.encode()) > MAX_TEXT`: `message is <n> bytes; the limit is 65536. Write it to a file and send the path`. Replace v1's `message_text` (line 1630) with this.
8. `log = open_log(state)`; `ident, offset, warning = log.add(sender, names, text, kind="say", file=recorded if args.file else None)`. The file path is recorded for every `--file`; whether it is ever removed is `managed_file`'s decision (Phase 3). By this point the team key, core, sender and every recipient name have been checked; nothing unverified reaches `log.add`.

| `log.add` raises | Print (stderr) | Exit |
|---|---|---|
| `LogDamage` | `nothing sent: <diagnosis>` | 2 |
| `NotStored`, `partial` false | `not stored: <detail>. Nothing will be delivered. Check the disk, then send again.` | 2 |
| `NotStored`, `partial` true | `not stored, and the log may now need repair: <detail>. Do not resend; tell the operator.` | 2 |
| any other `OSError` | `not confirmed stored: <error>. Do not resend; check factory.py inbox.` | 1 |

The two `not stored` lines are printed without the `nothing sent:` prefix: the member prompt and spec §8.5 treat `not stored` as its own outcome. cli_skill.md's refusal table gets that one-line clarification (§10).

9. **From here the message exists; nothing below may print `nothing sent`, ask for a resend, or lose the ID.** Print `message <id>` on stdout and flush. If `warning`: `warning: <warning>` on stderr.
10. `ensure_keeper(cfg, state, hc, view)`. On `Problem`, `HcDown`, `HcError` or `OSError`: stderr `warning: the keeper is not running (<error>). Message <id> is stored and will be delivered once it runs; run factory.py status.` and use `timeout=0` in the next step. On None (a stopped team): `warning: the team is stopped. Message <id> is stored and waits for factory.py up.` and `timeout=0`.
11. `outcomes = wait_outcomes(log, ident, names, offset, timeout)`. On `LogDamage` or `OSError`: stderr `message <id> is stored; its outcome could not be read: <error>. Do not resend; run factory.py inbox.`, exit 1.
12. `lines, code = outcome_lines(outcomes, operator, sender)`; print; `sys.exit(code)`.

**`wait_outcomes`.** `box = Box.for_message(log.path, ident, recipients, offset)`. Loop: `records, offset = log.read(offset)`; `box.apply(records)`; return `{r: box.outcome(ident, r) for r in recipients}` when every outcome is final or is `queued` with a reason other than `waiting`, or when `time.monotonic()` has reached the deadline; else `time.sleep(0.2)`. The first read always happens, so `timeout=0` returns the current outcomes without sleeping (#169 relies on this). `time` is the module's binding.

**`outcome_lines`** (pure). One line per recipient, in the order given:

| Outcome | Line |
|---|---|
| `delivered`, `sending` | `@x: delivered`, `@x: sending` |
| `queued` | `@x: queued (<why>)`; for the operator with `gone`: `@operator: queued (gone): operator not running; message queued` |
| `unconfirmed`, `dropped` | `@x: unconfirmed (<why>)`, `@x: dropped (<why>)` |

When any is `unconfirmed` or `dropped`: the code is 1 and one more line names them all: `@x, @y may not have it. Do not resend unless asked; tell @<operator>.` (the last clause is left out when the sender is the operator). Otherwise the code is 0.

## 6. `inbox`, pending lines, and v1's `status` and `up`

**`cmd_inbox`.** Parser: `inbox [N] [--for NAME] [--cwd DIR]`, N default 10. N below 1: stderr `give a positive number of messages`, exit 2. `cfg, state, me = find_team(args)` (`Problem`: its text, exit 2). NAME: `--for` with `@` stripped; else the caller's `factory.member` label when `me` carries this team's labels; else the operator's name. `state` is None: stderr `no team state for <root>; run factory.py up`, exit 2. `rows = inbox_rows(open_log(state), name, N)`; `LogDamage` or `OSError`: its text, exit 2. No rows: `no messages for @NAME`. Each row:

```
[Message 42 from @lead-dev at 2026-11-26T17:00:00Z: queued (blocked)]
    status: done …
```

The word is `outcome_word(outcome)`; a note's sender prints as `factory` without `@`; every text line is indented four spaces. Never calls `require_herdr`.

**`pending_lines(state, operator, now=None)`.**
- `state` is None: `["pending: none"]`.
- `records, box = open_log(state).read_box()`. `LogDamage`: `[str(error)]`. `Problem` or `OSError`: `[f"message log unavailable: {error}"]`.
- Group `box.pending()` by recipient, recipients ordered by their oldest pending ID: `pending: @operator 2 (oldest 3m, blocked); @reviewer 1 (oldest 10s, waiting)`. The age is `now` minus the oldest message's `at`: `<n>s` under a minute, `<n>m` under an hour, else `<h>h<m>m`. The reason is that pair's current outcome: `sending`, or the `why` of `queued`. Nothing pending: `pending: none`.
- When the operator has a pending pair: `read them with: factory.py inbox --for <operator>`.
- `box.uncommitted` above 0: `log: <n> uncommitted entries (never delivered)` (`1 uncommitted entry`).

**`print_pending(args, operator)`**, the only window glue, deleted by #169:

```python
def print_pending(args, operator):
    try:
        state = find_team(args)[1]
    except (Problem, HcError) as error:
        print(f"pending: unavailable ({error})")
        return
    print("\n".join(pending_lines(state, operator)))
```

**v1 `cmd_status`** (line 2705): replace the `Inbox` block (lines 2719–2726) with `print_pending(args, operator)`. Nothing else changes.

**v1 `cmd_up`** (line 2513): delete the `operator_wait` gate (lines 2520–2523) and the `members reach @operator through the inbox…` line (2544–2546): both exist only for `wait`. Replace `if cold and not args.restart: report_pending_messages(cfg)` (2542–2543) with an unconditional `print_pending(args, operator)`. `info = caller_agent()` stays for `ensure_operator`. Launch, panes and briefs are untouched.

## 7. The keeper

`Keeper(state, hc, clock=time, emit=print, spawn=None)` as keeper.md's interface. `cmd_keeper` (parser: `keeper --state DIR`, no `--cwd`) requires `HC_TERMINAL_ID` (else print `the keeper runs only in an hc terminal`, return), reads `team = read_team(state)` (empty or `Problem`: print it, return; `ensure_keeper` only starts a keeper beside a valid `team.json`), and runs `Keeper(state, Hc(timeout=10, expect_core=team["core"]), emit=functools.partial(print, flush=True)).run()`. Exit status 0 always.

Fields: `unsettled` (results withheld by §7.5), `log`, `dir` (the state directory descriptor from `log.open_dir(False)`, held for the keeper's lifetime), `box` (a `Box(log.path)` from construction on, so it is never missing), `team`, `cfg` and its file mtime, `inflight: dict[str, int]`, `results: queue.Queue`, `op_null_since`, `damaged: str | None`, `stop: str | None`, `held: set` (pairs already printed as held), and the time each repeated error line was last printed.

### 7.1 `run()`

keeper.md §2 steps 1–7, with these details:

1. `self.dir = log.open_dir(False)` (None: emit `no state directory: <state>`, return 0). `keeper.lock`: `private_open("keeper.lock", os.O_CREAT | os.O_RDWR, dir_fd=self.dir)`, `flock(LOCK_EX | LOCK_NB)`; `BlockingIOError`: emit `keeper already running`, return 0.
2. Signal handlers set `self.stop = "signal"`; install them only when running on the main thread.
3. `self.team = read_team(state)` (cmd_keeper's check, repeated: empty or `Problem`: emit it, return 0). `self._load()` (§7.2); only when it succeeded, `self._recover("keeper restarted during the send")`, under the same fault handling as a tick.
4. Write `keeper.beat` (`<pid> <VERSION> <HC_TERMINAL_ID>\n`, through `private_open("keeper.beat", O_WRONLY | O_CREAT | O_TRUNC, dir_fd=self.dir)`); `hc.label(HC_TERMINAL_ID, {"factory.version": VERSION})` (a failure is one emitted line, not fatal); emit `keeper <VERSION> started`.
5. Loop: `tick()`; leave when `self.stop` is set; else `results.get(timeout=TICK)` and put a received item back.
6. In a `finally` that covers steps 3–5, whatever raised: `hc.kill_children()`, close the descriptors. Then emit `keeper stopping: <why>` (`signal`, `team is down`, or the unexpected error's text); return 0.

### 7.2 Loading, damage and recovery

`_load()`: `self.box = self.log.read_box()[1]`; success clears `self.damaged` and returns True. `LogDamage` or `OSError` sets `self.damaged = str(error)`, leaves the previous (possibly empty) box in place and returns False. `_recover` runs only after a load that succeeded, at start and after the minute retry alike.

While `damaged` is set, a tick still touches the beat, drains results (their `log.out` calls may fail again; that is fine) and checks the exit rules, emits the diagnosis at most once a minute, starts no send, and once a minute calls `_load()`; when that succeeds it emits `message log readable again` and calls `_recover("keeper could not record the send")`.

`_recover(why)`: for every `(msg, to)` in `box.pending()` whose outcome is `sending` and for which `inflight.get(to) != msg["id"]`: `log.out(box, id, to, "unconfirmed", why=why)`. This is §10.10 at start, and it is what keeps a pair from staying `sending` for ever after a failed write without a restart.

Any `LogDamage`, `NotStored` or `OSError` from a log call anywhere in a tick sets `damaged` and ends that step; it never ends the process.

`Config`: load `Config(team["root"])` at start and again when `factory.toml`'s mtime changes. A `SystemExit` from it is caught: emit `factory.toml: <message>` at most once a minute and keep the last good one; with none yet, the tick delivers nothing.

### 7.3 `tick()`

1. `os.utime("keeper.beat", dir_fd=self.dir, follow_symlinks=False)`.
2. Retry each withheld result in `self.unsettled` (§7.5), then drain `results` with `get_nowait`; handle each (§7.5).
3. `team = read_team(state)`. A valid dict replaces `self.team`. Empty or a `Problem` is **unknown, not down**: keep the last valid `self.team`, emit `team.json: <why>` at most once a minute, and end the tick here (no dispatch and no exit rule until it reads again). Reload `Config` if needed.
4. `view = team_view(hc, team["team"])`. `HcDown` or `HcError`: emit `hc unreachable: <detail>` at most once a minute and return.
5. `log.refresh(box)`.
6. `op_null_since`: `clock.monotonic()` when `view.operator` exists with a null status and it was None; None when the status is not null or there is no operator.
7. Print `sending (held)` once for each pair still in `inflight` that was already in flight at the previous tick.
8. Deliver (§7.4).
9. Exit rule: `team.get("up")` is not true, `inflight` is empty, and either `box.pending(operator)` is empty or the operator is not reachable (`view.operator` missing or its status null): `self.stop = "team is down"`.

Only the main thread touches the log, `team.json`, labels and `emit`.

### 7.4 Deliver

Recipients are the names with a pending pair, in order of their oldest pending ID, skipping any in `inflight`. For each, keeper.md §3 exactly: `ready`, `head_of`, drop the superseded briefs (`why="superseded"`), `decide`, apply.

- `decide(name, msg, view, box, cfg, now, op_null_since, still_true=True)`: `now` is `clock.monotonic()`. The operator is the name equal to `cfg.operator["name"]`; every other name takes the M rows. With `op_null_since` None in O2/O3, hold. "A quiet or ready note" is `msg["kind"] == "note"` with `meta.what` of `quiet` or `ready`; this phase always passes `still_true=True` (Phase 6 computes it).
- No send starts once `self.stop` is set: check it before each `send`.
- `send`: `if log.out(box, id, name, "sending", term=term):` then, and only then, `inflight[name] = id`, `msg = box.msgs[id]` (read after the claim), build the wire text (keeper.md §3: `[from @sender]` or `[from factory]`, a newline, the text, and a final newline only when missing), `spawn(functools.partial(self._send, name, id, term, wire))`. The four values are bound when the job is made: a `lambda` here would read the loop's variables when it runs, and every queued job would send the last recipient's text to the last terminal. False: do nothing this tick.
- `queue` with `gone` or `not ready` applies to every pending pair of that recipient whose outcome is `queued`; with `blocked` to the head only. `log.out` already returns False for an identical reason, so no check is needed before calling it.
- `drop` with `stopped by down` applies to every pending pair of that recipient whose outcome is `queued`.
- `latch`: `hc.label(view.operator["address"], {"factory.kind": "operator-lost"})` first; on `HcError` or `HcDown` emit it and treat this tick as `hold`; else as `("queue", "gone")`.

`_send(name, id, term, wire)` runs off the main thread: `result = hc.send(term, wire)`, catching `Exception` as the result, then `results.put((name, id, term, result))`. It always puts exactly one item, so `inflight` cannot leak. The default `spawn` is `threading.Thread(target=fn, daemon=True).start()`.

### 7.5 Send results

keeper.md §4's table, row for row. Remove `name` from `inflight` and from `held` once the result is settled; the one exception is a withheld result (below), which keeps its place in both. Details it leaves open:

- `Hc` checks the reply's envelope, not its fields. A result counts as delivered only when it is a dict whose `turn` is an `int` (not a `bool`) and whose `started = result.get("started")` is `True`, `False` or `None` (the recorded `SendResult` schema makes `started` optional and nullable, so an absent one is recorded as null); anything else is `unconfirmed`, `hc returned an unexpected result`, like every other row that frees the recipient.
- `delivered`: `log.out(box, id, name, "delivered", term=term, started=started, turn=result["turn"])`, then `warning = log.remove_delivered_file(box, id)`; emit a warning as one line.
- **A member's result that proves nothing was typed** is one of: `HcError` `blocked` (reason `blocked`), `HcDown(sent=False)` (reason `waiting`), `HcError` `not_found` or `finished` (reason `gone`). Each is settled by `settle_refusal(log, box, id, name, term, reason)`, which replaces keeper.md §4's "fresh `team_view`" rule: a missing record proves nothing (`up` also removes the old record of a member it replaces), so the keeper no longer asks `hc` what became of the terminal. The evidence of an intentional stop is `team.json`'s `stopped` list, written by `down`.
- `settle_refusal`, in **one** `log.locked(True)` section (a separate `read_team` then `log.out` would let `down`'s sweep finish in between): read `team.json` with `_read_team_file(state, fd)`; a missing or unreadable file is **unknown, not "no stop"**: raise `Problem` and write nothing (a valid file without a `stopped` key is real evidence that no stop was recorded); `log._refresh(box)`; when `term` is in `stopped`: the record is `dropped`, `stopped by down`; else `queued` with the given reason; append it with `log._out_locked` and return the outcome word written, or None when the pair had already moved on. It lives beside the team helpers and is the only code outside `Log` that calls those two private methods.
- When `settle_refusal` raises `Problem`, the keeper **withholds the result**: the pair stays `sending` in the log, the recipient stays in `inflight` (so nothing later overtakes it), the tuple is kept in `self.unsettled`, and `team.json: <why>` is emitted at most once a minute. Step 2 of every tick retries `unsettled` before it drains new results. If the keeper restarts first, the pair becomes `unconfirmed` by the existing start-up rule. Requeueing here would let a replacement terminal receive a message that `down` had already swept past.
- Why this is complete. `down` confirms the stop, then `mark_stopped`, then `hc.rm`, then `log.drop_queued`. A settlement that takes the lock before the evidence is written requeues the pair, and `down`'s sweep, which comes later, drops it. A settlement that takes the lock after the evidence drops it directly. Without an intentional stop the pair is `queued (gone)` even when the old record is already removed, and goes out after the replacement's brief (§8.5). A receipt still means `delivered` and an uncertain result still means `unconfirmed`, whatever the list says.
- `mark_stopped(state, terms, box)`: `update_team(state, stopped=[t for t in old if t in box.sending_terms()] + [t for t in terms if t not in old])`, where `old` is `read_team(state).get("stopped", [])`. An old entry is kept only while a `sending` pair still names it, so the list stays short; `up.lock` serializes its only writer. An entry is trusted by terminal ID alone. Verified on a private `hc 0.1.20` core (26 November 2026): IDs are not reused after `terminal.rm`, nor after a core restart with or without remaining records (`t1`…`t4` in one run, then `t5`, then `t7` after two restarts), so a listed ID can never name a newer terminal.
- The operator is never stopped by `down`: its `blocked`, unsent, `not_found` and `finished` results use `log.out` with keeper.md §4's outcomes, as before.
- "Any other" includes a non-`hc` exception from `_send`: `unconfirmed` with `why = str(error) or type(error).__name__`, cut to 200 characters on one line.
- A quiet or ready note: every row that would write `queued` writes `dropped`, `operator could not take it`.

### 7.6 Pane lines

One `emit` per `out` record the keeper writes, except `sending` (see tick step 7), prefixed with local `HH:MM:SS` from `clock.time()`: `#42 @operator -> @lead-dev delivered`, `… queued: blocked`, `… dropped: stopped by down`, `… unconfirmed: <why, 80 characters at most>`, `… sending (held)`. The sender prints as `factory` for a note. No message text, no title.

## 8. `ensure_keeper`

keeper.md §8, steps 1–7. Details:

- Step 1: `team = read_team(state)`. Unless `force`: `up` not true and `open_log(state).read_box()[1].pending(operator)` empty: return None.
- All of its files go through one `dirfd = open_log(state).open_dir(False)` (None: `Problem("no state directory", "run factory.py up")`), closed in a `finally`. `read_beat(state)` opens the directory the same way, reads `keeper.beat` with `private_open("keeper.beat", os.O_RDONLY, fix_mode=False, dir_fd=…)` and takes the age from `fstat`; a missing directory or file, or content that is not `<int> <word> <word>`, is None.
- Step 2: `keeper.start` through `private_open("keeper.start", os.O_CREAT | os.O_RDWR, dir_fd=dirfd)`; try `LOCK_EX | LOCK_NB` every 0.1 s for 15 s, then `Problem("another command is starting the keeper", "run factory.py status")`. Then `records = hc.list(team["team"])` and `view = View(records)`: the `view` argument is only the caller's earlier look.
- Step 3: `alive`, `young` (`time.time() - k["created_ms"] / 1000 < BEAT_STALE`) or `hung` from `read_beat(state)`. Killing a hung keeper ignores `not_found` and `finished`.
- Step 4: `hc.rm` every record in `records` with kind `keeper` and state `finished`.
- Step 5: `hc.run([sys.executable, str(SCRIPT), "keeper", "--state", str(state)], cwd=team["root"], env=…, labels=…)` with keeper.md's labels. `env` is `PATH`, plus `HC_BIN` when the caller has it set, so the keeper uses the same `hc` binary as the command that started it.
- Step 6: poll `read_beat` every 0.1 s for `KEEPER_START` seconds until it names the new terminal. Each second also `hc.get` the terminal: finished: read its screen, `hc.rm` it, raise `Problem(f"the keeper did not start: {last non-blank screen line}", "run factory.py status")`. Still live with no beat when the time is up: return its address (the `young` rule protects it from the next caller).
- The `keeper.start` lock is released in a `finally`.

## 9. Removals

Delete, with their tests: `Inbox`, `InboxDamage` (lines 1466–1590), `print_messages`, v1 `cmd_inbox`, `operator_wait`, `report_pending_messages`, `message_text`, `remove_delivered_draft`, `queue_message` (1592–1697 except `report_leftover_drafts`), `cmd_wait` (2736–2818), v1 `cmd_say` and `print_retry` (2837–2890), the `wait` parser and its `--seen`/`--interval`, and the `operator_wait` and `operator_wait_timeout` keys and their two comment lines in `skill/harnesses.toml`. Remove an import only when nothing else uses it.

Keep until #169: `report_leftover_drafts`, `session_key`, `herdr()`, `Team`, `deliver`, `prompt_error`, `delivery_refused`, `send_brief`, v1 `cmd_brief`, `cmd_prompt`, `cmd_down`, the rest of `cmd_up` and `cmd_status`. v1's `up` and `brief --send` still type through herdr until then.

No v2 code opens `inbox-*.jsonl`, `inbox-*.json` or `inbox-*.lock`.

## 10. Skill text and docs in this change

- `skill/templates/member-prompt.md`: "herdr pane" becomes "pane"; the paragraph that starts "Run the send command alone" is replaced by cli_skill.md §4's outcome paragraph, word for word; the last "Reports" paragraph ends with `Report sent to @<requester>` for `delivered`, `sending` or `queued` (drop the "Report queued" variant). In `member_prompt` (line 2031) `{say}` becomes `shlex.join(["python3", str(SCRIPT), "say"])`, without `--cwd`.
- `docs/getting-started.md`: a short section "Messages from a v1 team" with the two commands of implementation_plan.md Phase 4, saying they run from a v1 checkout in the original herdr session and that v2 neither reads nor changes those files.
- `SKILL.md`, `references/` and the README still describe v1's `wait`. They are Phase 7's and are not edited here.
- `components/keeper.md`: §2 step 4 uses `log.refresh(box)`; §3 "send" reads the text after the claim; §4's member rows for results that prove nothing was typed are settled by `settle_refusal` against `team.json`'s `stopped` list (§7.5), replacing the fresh `team_view`; §2 gains the recovery rule of §7.2; §8 step 5 names `HC_BIN` and `KEEPER_START`.
- `components/cli_skill.md`: §1 `resolve_team`'s third value, step 3 counts only directories with a `log.jsonl`, and `find_team`; §2 `say`: no separate "both `--file` and text" row (§5), the `not stored` lines have no `nothing sent:` prefix, the "not confirmed stored" row, and the rule that nothing after a stored message may suggest a resend.
- `components/message_log.md`: `Log.locked` with the no-re-entry rule, `Log.refresh`; "`inbox` output" loses its "arrive in Phase 4" sentence.
- `architecture.md` §3.3: the `stopped` key and who writes it. (`launch_layout.md` §6's `down` sequence gains `mark_stopped`; that edit is #169's.)
- `architecture.md` §4: the measured size of the keeper and command sections after this phase, as Phase 3 did.

## 11. Tests

All in `tests/test_factory.py`. **No test may reach an `hc` it did not start.** `find_team` makes v1-style commands call `hc` when `HC_TERMINAL_ID` is set, and the team's own panes will soon have it. So `Env.__init__` (line 1335) removes `HC_TERMINAL_ID`, `HC_SESSION`, `HC_SOCKET` and `HC_TERMINAL_SOCKET` from `self.env` and sets `HC_BIN` to `tests/fake_hc.py` with a private `FAKE_HC_DIR`; every in-process command test patches `os.environ` the same way.

**Fixture `TeamCase(HcBackend)`.** A temp checkout with `FACTORY.md` and `factory.toml`, a private `XDG_CACHE_HOME`, `state = state_dir(root, hc.core)`, `update_team(state, root=…, team=…, core=…, up=True)`, and helpers: `terminal(kind, name, status="idle")` (labels `factory.team`, `factory.kind`, `factory.member`, `factory.dir`); `briefed(name)` (a `brief` message with `sending` and `delivered` records for that terminal); `keeper()` (a `Keeper` with `ManualClock`, `emit` into a list and a `spawn` that queues jobs; `run_jobs()` runs them); `live_keeper()` (a labelled keeper record plus a fresh `keeper.beat` naming it, so `ensure_keeper` starts nothing); `say(sender_term, *argv)` (runs `factory.main(["say", …])` in-process with `HC_TERMINAL_ID` patched, returning stdout, stderr and the exit code). Tests that script the fake (`on_send` hold, die, error; a finished keeper) are `@fake_only`; the rest run on both backends. The fake may gain a `run` rule keyed by `factory.kind == "keeper"` if the start tests need it.

**`KeeperTests`**: from keeper.md's test plan, the groups "`decide`, `head_of`" (not `quiet_now`), "Delivery" (all but `test_note_added_in_step_6_is_sent_in_the_same_tick`), "Send results" (all), "Crash recovery" (`test_restart_marks_sending_unconfirmed_and_never_retypes`, `test_restart_after_brief_delivered_member_is_ready`, `test_restart_after_latch_label_records_gone`), and "Liveness" (all but `test_version_change_drains_then_execs` and the title half of `test_pane_lines_and_title`, which becomes `test_pane_lines`). Notes exist in this phase only as records a test adds with `log.add("factory", [operator], …, kind="note", meta=…)`. Added by this plan:

- `test_queued_jobs_keep_their_own_recipient_and_text`: one tick claims sends to two recipients; the queued jobs are run in reverse order; each terminal receives its own text once, both pairs settle, and `inflight` is empty.
- Stop evidence, all with delayed worker results (this replaces keeper.md's `test_result_finished_with_record_is_gone`, `test_result_finished_after_down_is_dropped` and `test_down_then_up_during_send_does_not_resurrect`, and the tests perform `down`'s steps themselves):
  - `test_refusal_before_stop_evidence_requeues_and_the_sweep_drops`: claim on T1; T1 is killed; the `finished` result is settled first (`queued (gone)`); then `mark_stopped` and `drop_queued`: the pair is `dropped`.
  - `test_refusal_after_stop_evidence_is_dropped_directly`: `mark_stopped` first, record removed, a new terminal T2 for the same member started and briefed; then the result: `dropped`, and T2 receives nothing. One case each for `finished`, `not_found`, `blocked` and `HcDown(sent=False)`.
  - `test_replaced_terminal_without_stop_evidence_is_delivered_to_the_new_launch`: claim on T1; T1 exits; T2 is started and T1's record removed with no `mark_stopped`; the `not_found` result gives `queued (gone)`; after T2's brief is delivered the message is typed into T2 exactly once.
  - `test_receipt_and_uncertain_results_ignore_stop_evidence`: with T1 listed, a normal result is `delivered` and `HcDown(sent=True)` is `unconfirmed`.
  - `test_settle_refusal_holds_one_lock`: `Log.locked` is entered once per settlement (a spy); a valid `team.json` with no `stopped` key requeues.
  - `test_unreadable_metadata_withholds_the_settlement`: `down`'s steps are complete for T1 (listed, record removed, sweep done) and T2 is started and briefed; `team.json` is made unreadable just before T1's `finished` result is handled: nothing is written, the pair stays `sending`, the recipient stays in `inflight` and a later message to it is not sent; the original file is restored: the next tick drops the pair, T2 receives only the later message. Same with the file missing.
  - `test_withheld_result_becomes_unconfirmed_on_restart`.
  - `test_mark_stopped_prunes_entries_without_a_claim`.
- `test_text_typed_is_read_after_the_claim`: the keeper holds a box; the log is rewritten by hand with other text for the same ID (new mtime); the next tick types the file's text, not the box's.
- `test_failed_sending_record_recovers_without_restart`: a `sending` record left visible by a failed sync; after the minute retry the pair is `unconfirmed`, nothing was typed, and the next message to that recipient is delivered.
- `test_send_thread_exception_is_unconfirmed_and_frees_the_recipient`.
- `test_bad_factory_toml_keeps_last_config`.
- `test_unreadable_team_json_is_not_team_down`: with `team.json` made unreadable mid-run the keeper keeps beating, delivers nothing, does not exit, and resumes when it reads again.
- `test_startup_with_damaged_log_has_a_box_and_skips_recovery`: no exception, the beat is written, nothing is typed; after repair the minute retry recovers and delivers.
- `test_malformed_send_result_is_unconfirmed`: a reply without `turn`, with a string `turn`, and with `started: "yes"`; each ends `unconfirmed` and the next message to that recipient is delivered. A reply without `started` is `delivered` with `started` null.
- `test_no_send_starts_after_stop_and_children_are_killed_on_any_exit`: also when `tick` raises.
- `test_keeper_files_are_opened_below_the_directory_fd`: a symbolic link in place of `keeper.beat`, `keeper.lock` or `keeper.start` is refused, and nothing outside the state directory is written.
- `test_real_keeper_types_a_say` (`@real_only`): on the private core, `ensure_keeper` starts a real keeper process, a `say` from a labelled operator terminal reaches a briefed fake agent, `say` prints `delivered`, the agent's log holds exactly the wire text, and `team.json` `up=False` makes the keeper exit.

**`CliTests`**: from cli_skill.md's test plan: the four `test_resolve_team_*` tests, `test_two_cache_homes_share_one_log`, one test per row of the `say` refusal table (each: exit 2, the log's bytes unchanged, and the fake received no `terminal.send` or `terminal.run`), `test_say_all_expands_to_live_members_only`, `test_say_names_split_on_commas_and_spaces`, `test_say_to_gone_member_is_queued`, `test_say_output_and_exit_codes`, `test_say_prints_disk_warning_and_still_delivers`, `test_say_refused_when_pane_points_at_another_core`, `test_say_from_operator_accepts_text`, `test_say_keeps_text_exact`, `test_status_counts_uncommitted_entries` (through `pending_lines`), `test_member_prompt_has_no_cwd_and_names_outcomes`; from message_log.md's: `test_say_wait_stops_when_settled`, `test_say_wait_reports_sending_at_deadline`, the four `test_inbox_*` tests through the command. `test_say_records_session_before_validating` is #169's. Added by this plan:

- `test_say_keeps_the_id_when_the_keeper_cannot_start`: `message N` is on stdout, the warning names N, no `nothing sent`, exit 0 with `queued (waiting)`.
- `test_say_keeps_the_id_when_outcomes_cannot_be_read`: exit 1, the ID on both streams.
- `test_say_unknown_storage_outcome_says_do_not_resend`.
- `test_wait_outcomes_timeout_zero_reads_once`.
- `test_labelled_state_with_wrong_shape_or_team_is_refused_without_a_write`; `test_team_json_with_missing_or_mistyped_fields_is_refused`; `test_caller_without_member_label_is_refused` (each: `nothing sent`, exit 2, no traceback).
- `test_file_path_with_dotdot_is_recorded_as_given_and_never_removed`: `messages/<sender>/link/../x.md` with `link` a symbolic link to another directory: the text read is the linked file's, the log holds the path as given, and after delivery the managed `messages/<sender>/x.md` still exists.
- `test_text_beside_file_is_refused_as_an_unknown_name`.
- `test_offline_selection`: newest log wins over a newer metadata-only directory; all metadata-only: newest `team.json`; equal mtimes: the smaller pathname; a symbolic-link candidate is ignored; a `log.jsonl` that is a symbolic link is an error naming it; a metadata-only team prints `no messages for @NAME`, exit 0.
- `test_update_team_keeps_other_keys_and_is_atomic`; `test_read_team_missing_is_empty_and_unreadable_is_a_problem`; `test_update_team_reset_replaces_unreadable`.
- `test_pending_lines`: none; two recipients with ages and reasons; the pointer only when the operator has one; a damaged log gives the diagnosis.
- `test_inbox_default_name_is_the_callers`; `test_inbox_without_team_state_exits_2`.

**Cutover test** (`CutoverTests`, with `Env`): seed v1's two files by hand in `cfg.cache`: `inbox-<session_key>.jsonl` with one unread row, ID 7, and `inbox-<session_key>.json` with `{"seen": 6, "history": [<row 6>]}`; seed a v2 state under the same team key with one message, ID 1, pending for the operator.

- `test_status_and_up_show_v2_pending_only`: `status` and `up` print `pending: @operator 1` and the `inbox --for` pointer, and neither `unread` nor `factory.py wait`; both v1 files are byte-identical afterwards; `wait` is rejected by the parser.
- `test_v1_reader_still_prints_the_old_ids`: skipped unless `git rev-parse --verify` finds `v1.0.0`, `origin/v1` or `v1` (CI's shallow checkout has none). `git archive <ref> skill` into a temp directory, then run that copy's `factory.py wait --cwd <repo> --timeout 0` (prints ID 7) and `factory.py inbox 100 --cwd <repo>` (prints ID 6) on the same fixture. @lead-dev runs it locally and quotes the result in the pull request.

**v1 tests.** Delete `InboxTests`, `StockTransportTests`, `WaitTests`, `Env.say`, and the `operator_wait` assertion (line 5167). In `UpTests`, `WorktreeTests`, `UpRecoveryTests`, `DownTests` and `ConfigTests`: a test whose subject is v1's `say` or inbox (`test_say_*`, `test_member_draft_delivers_…`, `test_member_queues_for_configured_operator_without_a_pane`, `test_draft_name_validation_…`, `test_mixed_send_reports_…`) is deleted, its behaviour being covered by `CliTests` above; a test that only used `e.say` or `wait` as set-up (for example lines 1716, 3443–3445, 5077) keeps its launch, config and recovery assertions and drops those lines. Tests asserting the old prompt or `up` output lines are updated to the new text. Name every deleted test in the pull request description with the test that replaces it, or "v1 only".

Coverage rule (testing.md §5): every row of keeper.md §3's and §4's tables, and of cli_skill.md's refusal table, has a test.

## 12. Build order

Each step is one commit with its tests green in fake mode; run real mode (`FACTORY_TEST_HC=real`) before asking for review.

1. `SCRIPT`, `cache_base`, `team_key`, `state_dir`, `state_for`'s shape check, `open_log`, `Log.open_dir`, `Log.locked`, `Log.refresh`, `read_team`, `update_team`; the `Env` isolation. Proved by the `team.json` tests and the unchanged Phase 3 suite.
2. `resolve_team`, `find_team`, `pending_lines`, `cmd_inbox` on the v2 log. Proved by the resolve, pending and inbox tests.
3. `head_of`, `decide`. Proved by the pure tests.
4. `Box.sending_terms`, `mark_stopped`, `settle_refusal`; `Keeper`: start, tick, deliver, results, recovery, exit rules, pane lines; `cmd_keeper`. Proved by the delivery, result, crash and liveness tests.
5. `read_beat`, `ensure_keeper`. Proved by the `test_ensure_keeper_*` tests.
6. `wait_outcomes`, `outcome_lines`, v2 `cmd_say`. Proved by the `say` tests and the real-core test. Keep the member prompt and its `--cwd` argument until the public command changes.
7. Cutover: `print_pending` in v1's `status` and `up`; the removals of §9; the member prompt; v1 test clean-up; `CutoverTests`; `harnesses.toml`.
8. Docs of §10; measure the sections for architecture.md §4.

Steps 1 and 2 publish everything #169 needs to write against; tell @lead-dev-2 when they are pushed.

## 13. Risks and gaps

- **The window between the two merges** (§1). Nothing on `master` can run a team end to end until #169 merges. Accepted by the owner through @operator (26 November 2026): no team runs from `master` until release, and skills-sync follows v1.
- **Real-mode coverage is one end-to-end test** plus the unmarked backend tests. Holds, connection loss and crashes are scripted on the fake only; real draft holds are Phase 8's qualification (§10 cases 3 and 10).
- **Stale queue reasons.** A pair queued as `blocked` or `gone` keeps that word while the keeper holds for another cause (for example a member that is `starting` again), until it is sent. `status` can show an out-of-date reason for a few seconds. Not corrected: it would add a record per tick.
- **Size.** Expect about 450 lines for the keeper part and 250 for the commands and helpers, minus about 330 removed. Measure, do not compress.
- **Skill text lags.** `SKILL.md` and the references name `wait` until Phase 7.

No question is open: @architect-2 has no objection to removing the "not supported as operator yet" gate together with `operator_wait`.
