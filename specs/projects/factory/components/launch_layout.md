---
status: complete
---

# Component: Launch and layout

## Purpose and scope

Everything `up` and `down` do to terminals and to the `herdr-app` layout doc: checking and marking the operator, starting a member with its harness arguments, deciding it is ready, saving and resuming native sessions, placing and removing panes, and reporting interrupted members (§6, §7, §9). It does not type messages (briefs are appended to the log; the keeper types them) and does not decide deliverability.

## 1. `harnesses.toml` changes

Removed keys: `operator_wait`, `operator_wait_timeout`. New keys:

| Key | Values | Meaning |
|---|---|---|
| `session` | `"id"`, `"dir"`, or absent | What Factory sets at launch: an ID it chooses, or a per-member session directory (§3) |
| `session_env` | a variable name, or absent | The variable in which the harness gives its current conversation ID to the commands it runs (§3) |
| `resume_model` | `"any"` (default), `"provider"` | `"provider"`: resume only when the model's provider prefix (text before the first `/`) is unchanged |
| `dialogs` | list of `[text, keys]` | As v1. An empty `keys` list means: never answer, report it |

| Harness | `session` | `session_env` | `resume_model` | `dialogs` |
|---|---|---|---|---|
| claude | `id` | `CLAUDE_CODE_SESSION_ID` | `any` | v1's two (`down`, `enter`) |
| codex | | `CODEX_THREAD_ID` | `any` | `["Trust and continue", ["enter"]]`, removed if LC-2 succeeds |
| pi | `dir` | none | `provider` | none |
| omp | `dir` | none | `provider` | none |
| opencode | `id` | none known (LC-11) | `provider` | none |
| agy | | `ANTIGRAVITY_CONVERSATION_ID` | `any` | `["Do you trust the contents of this project?", []]`, removed if LC-7 succeeds |

The variables were confirmed live by the team on 26 November 2026 (Claude Code 2.1.330, Codex 0.172, agy 1.5.14). `CODEX_SESSION_ID` also exists but names the session, not the current thread, and is not used. pi and omp have no `session_env` entry: each resumes only with `--continue` in its own session directory, which already reopens the member's newest conversation. (pi does export `PI_SESSION_ID`; Factory neither reads nor saves it. It is LC-6's fallback if `--continue` proves unreliable.)

`resume_model` values are the architect's judgement, not a verified fact: the qualification run that resumes across a model change (§10.6) settles each one. `deliver` is reserved ([keeper.md](keeper.md) §9).

## 2. The operator

```python
def check_operator(cfg: Config, team: str, me: dict, view: View) -> None     # read-only; exits 2 with the reason
def mark_operator(cfg: Config, hc: Hc, team: str, me: dict, view: View, state: pathlib.Path) -> None
```

`me` is `hc.get(os.environ["HC_TERMINAL_ID"])`; with the variable unset `up` exits 2, "run the operator inside an hc terminal, such as a rodeo pane".

`check_operator` changes nothing. It refuses when:

| Condition | Message |
|---|---|
| `me.labels["factory.team"]` is set and is another team | `this pane is @<member> of team <other>; run up from a pane that is not on a team` |
| `me` is this team's `member` or `keeper` | `run up from the operator's pane, not a member's` |
| `me.status` is null | `this pane reports no agent status to hc (OSC 7501); see the harness notes in the docs` |
| `view.operator` is another terminal and its status is not null | `@<operator> is running in <address>; use that pane` (§7.4) |

`mark_operator`, called only after preflight has passed: label any other live terminal of kind `operator` or `operator-lost` as `operator-lost`; then `hc.label(me, {"factory.team": team, "factory.kind": "operator", "factory.member": name, "factory.dir": str(state), "name": "@" + name})`. Adoption needs nothing more: members and the keeper are found by the team label, and the keeper sends to whichever terminal is `view.operator`.

## 3. Sessions

**Rule (§9.4):** Factory learns a member's current conversation from the member itself. A harness exports the conversation ID to the commands it runs, so every `say` a member runs records it; a conversation switched to inside the harness is followed from that member's next `say`. Where a harness has a launch flag that fixes the conversation, Factory uses it too. No transcript or session file is ever scanned, and nothing is read from the screen.

```python
def saved_sessions(state) -> tuple[dict[str, dict], int]       # newest entry per member, and how many damaged lines were skipped
def record_session(cfg: Config, state, member: str, term: str, environ=os.environ) -> str | None
    # called by say: saves the harness's session_env value when it should; returns a warning or None
def launch_entry(state, m: dict, term: str, how: str, value: str | None) -> None
    # writes a new marker at launch or a session fact after the brief; respects say captures
def session_plan(m: dict, state, entry: dict | None, fresh: bool) -> dict
    # {"args": [...], "resume": bool, "assigned": str | None, "notice": str | None}
```

### The file

`sessions.jsonl`, one JSON object per line: `{"member", "harness", "model", "term", "how", "value", "at"}`. `term` is the member's terminal ID at the time; `how` is `env` (recorded by `say`), `id` or `dir` (set at launch), or `new` with `value: null` (a launch whose conversation is not known yet). **The newest entry for a member is the truth.**

Every member's `say` writes here, so there are many concurrent writers. All reading and writing goes through one function that takes `LOCK_EX` on the team lock (the state directory, message_log.md §Locking), reads the file, decides, and appends in a single `os.write` followed by `fsync`. A torn last line (a writer killed mid-write) is handled inside the same lock: the new line starts after a `\n`. A line that is not JSON is skipped and counted, and `up` prints `skipped <n> damaged entries in <path>` (§9.4).

### Recording, in `say`

Right after the caller is identified as a member, before anything else: `var = TABLE[harness].get("session_env")`; `value = os.environ.get(var)`. Inside the lock, append `{…, "term": <caller's terminal>, "how": "env", "value": value}` when all hold:

- `value` is set and matches `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. The saved value later becomes one argument of the harness's command line (never a shell string), so a value that could be read as an option is refused;
- the newest entry is for another terminal, or for another harness, or holds another value.

The entry's `model` is the one in the newest entry for the same terminal: the model `up` resolved for that launch, e.g. `openrouter/openai/…` for a formation's `openai/…`. With no entry for that terminal, it is the formation's model. So the next launch's `resume_model` check compares like with like (Phase 8, a real pi member).

Consequences: the first `say` a member runs is its ready line, so the ID is recorded there without the model being involved; a failed write prints `warning: could not record your session: <error>` on stderr and `say` goes on, and the next `say` tries again; `say` from the operator records nothing.

**A `say` run by a sub-agent (LC-12).** The variable names the conversation of whatever runs the command. If a harness gives a sub-agent or sub-thread its own ID (Claude Code's Task agents, Codex's agents), that `say` would record a child conversation, and a later resume would open it without failing. LC-12 checks, per harness, that a `say` from a sub-agent carries the parent's ID. For a harness where it does not, `harnesses.toml` gets `session_follow = false`: `say` then records only when the newest entry is not already an `env` entry for this terminal. That is once per launch, from the first `say` (the ready line, which the member itself sends), and it does not matter whether `up`'s launch entry for the terminal (`new`, `id` or `dir`) was written before or after: the first captured ID replaces a launch entry, and later captures from that terminal are ignored. That harness no longer follows a switch made inside it.

### Launch arguments

An entry is **compatible** when `entry["harness"] == m["harness"]`, its `value` is not null, and `resume_model` allows the model. `session_plan` resumes only a compatible newest entry and only without `--fresh`; when the entry is for another harness or model its `notice` is `@x starts fresh: its saved conversation is for <harness> <model>`.

| Harness | Always | Fresh | Resume |
|---|---|---|---|
| claude | | `--session-id <new uuid4>` (`assigned`) | `--resume <value>` |
| codex | `--no-daemon` | | `resume <value>` before the options (v1) |
| agy | | | `--conversation <value>` |
| opencode | | `--session <new id>` in the format LC-4 finds (`assigned`) | `--session <value>` |
| pi | `--session-dir <state>/sessions/<member>-pi` | | `--continue` |
| omp | `--session-dir=<state>/sessions/<member>-omp` | | `--continue` |

A member that has run before (it has a finished record or a saved entry) and has no compatible entry gets the `notice` `@x starts in a new conversation: none was recorded for it`, and its brief carries the "new session" closing, so the operator gives it its task again (§9.5).

Other argv changes from v1: for `guarded` Codex `--add-dir <state>` replaces the inbox directory; v1's `session_id(ref)` helper is removed. The adapters' `argv(m, prompt_file, resume)` signature stays; `resume` is the plan dict.

### Saving and recovering a launch

A fresh launch writes a `new` entry before its first brief. The label `factory.launch` carries the launch's `harness`, `model` (string or null), `fresh`, `how` and `value`. Assigned IDs use `how: "id"`. Folder harnesses record `how: "dir"` and the folder actually used, including on a resume. Other fresh launches record `how: "new"` and a null value. A resume by ID needs no launch label.

`settle_launches` reads the live record for each member, or its newest finished record. It restores a missing `new` entry for a fresh terminal, then saves the ID or folder once the log confirms delivery of its first brief. A settled record's launch label is cleared. A corrupt label or a failed label clear is reported as a warning. `launch_entry` decides under the journal lock: it never replaces an `env` entry from the same terminal, repeats an identical entry, or appends `new` after another entry for that terminal.

`up` settles late briefs before reading sessions for a restart, and checks again with a fresh view and log after waiting for briefs. `brief --send` holds the lifecycle lock through its wait and check, so `up` cannot replace a terminal between the snapshot and the journal write. A failed save leaves the label in place. `up` skips restarting a member whose old fact could not be saved, exits 3 and names the member. `down` first checks that the journal can be appended to and synced. Then it settles launch facts before stopping members, leaving a member running or keeping its finished record when its entry cannot be saved, with exit 1.

A switch inside a harness after its last `say` is outside this recovery path.

**When a resume fails.** If a member started with `resume` exits before it is ready, `up` prints `@x: its saved conversation could not be resumed (<reason>); starting fresh`, removes the record, and starts it once more with a fresh plan. A second failure is reported like any failed start.

### agy's trust dialog

Candidate from the team (LC-7): agy's session-only `--settings` accepts `trustedWorkspaces`. `argv_agy` then builds one `--settings` value itself: the JSON object from the permission level in `harnesses.toml` with `"trustedWorkspaces": [m["dir"]]` added, replacing the static `--settings` argument. Nothing is written to agy's global settings (§3 of the spec). If LC-7 fails, the dialog stays in `dialogs` with no keys and is left to the owner (§6.3).

## 4. Starting a member and readiness

```python
def start_member(cfg, state, hc, m: dict, operator: str, sess: dict) -> dict   # Terminal record; raises Problem
def await_ready(hc, started: dict[str, dict], timeout: float = LAUNCH_TIMEOUT, clock=time) -> dict[str, str | None]
    # member -> None when ready, else the reason it is not
```

`start_member` (its worktree already exists, §6 step 8):

1. Make `<state>`, `messages/<member>` and, for `dir` harnesses, the session directory: mode 0700, refuse a symbolic link (v1's check and message).
2. Write `<state>/<member>.md` from `member_prompt` and, for Claude Code, `<member>.settings.json` (§7). Both are rewritten at every launch (§9.4).
3. `argv, env = launch_argv(m, prompt_file, sess)`; `exe = shutil.which(m["harness"])`.
4. Run the harness with its own argv and working directory. Set the Factory identity labels and, when owed, the `factory.launch` fact from §3.

No `herdr.*` label is set here. A terminal that carries `herdr.space` and is not yet in the layout doc is adopted by a client after about two seconds, and adoption turns `keep` on, which §9.2 forbids. The layout labels are written only after the pane is in the doc (§5).

The program is started directly, not typed into a shell, so v1's 1,024-byte line limit is gone and a harness that ends leaves a finished terminal, not a shell. Only `PATH` is taken from `up`'s environment; everything else is the core's login environment.

`await_ready` polls every 0.5 s until each member is settled or its deadline passes:

1. `t = hc.get(address)`. `state == "finished"`: settle with `exited (code <n>)` plus the last non-empty line of `hc.read(address)`.
2. `word = t.status.state` or None. If `word == "working"`: not ready, next member.
3. Otherwise read the screen: `screen = "".join(hc.read(address).split())`. For each configured dialog whose text, with whitespace removed, is in `screen`:
   - empty `keys`: settle with `waits on its trust dialog: accept it in its pane, then run: factory.py brief --send <member>`;
   - not yet answered: `hc.keys(address, keys)` once, remember it, not ready this round;
   - already answered: not ready this round.
4. No dialog on screen and `word == "idle"`: ready (§6.3). No other word counts.
5. Deadline: settle with `still asks "<text>" after the answer was sent` when a dialog was answered and is still shown, else `not ready after <timeout> s (<word or "no status">)`.

The screen is read whenever the member is not `working`, so a dialog is caught when the harness reports `blocked` (Claude Code, agy), nothing (Codex) or `idle` (v1 #125).

## 5. Layout doc

```python
def new_id(prefix: str) -> str
def edit_layout(hc: Hc, change: Callable[[dict], bool]) -> str | None
def blocks(doc: dict) -> dict[str, dict]                # terminal ID -> its block
def where(doc: dict, terminal: str) -> tuple[str, str] | None
def resolve_tab(doc, layout, view, me) -> tuple[str, str, bool]    # space, tab, made_space
def add_panes(doc, space, tab, panes, keeper=None, *, folder=None) -> None
def remove_panes(doc, layout, terminals) -> None
def swap_terminal(doc, old, new) -> bool
```

`edit_layout` reads `herdr-app` and validates its shape. Unwritten docs start as `{"spaces": []}`. Each of up to `CAS_TRIES` attempts recomputes from the current document, then writes with its version. A no-op and a successful write return None. Conflicts retry after `0.05 * attempt` seconds. Invalid shape, other errors or exhausted retries return a reason and leave the layout alone.

A doc's spaces contain tabs with split-tree roots. Each root is a container, even for one pane. Rodeo writes a row root for its own initial single-pane tab. A block contains its title and terminal, with an optional size.

`resolve_tab` selects the saved team tab anywhere in the doc. Without it, the tab with the most team terminals by block or label wins, excluding the operator's own tab. For a new tab it uses the operator's space if present, then the saved space if present, then proposes a new space. It leaves the document untouched. `add_panes` creates a missing space and tab when it places the first block.

- Members fill up to three columns. Each additional member goes in the column with the fewest blocks, first on a tie. A column gets another block. Any other node is wrapped whole in a column with the new block.
- The keeper's new block is last at weight 0.4. New member columns are inserted before it. An owner-arranged root is wrapped in a row if needed. Existing nodes retain their position and size within the tree.
- A replacement takes the old block's position when it still exists. `where` finds its actual space and tab, including panes moved by the owner.
- `remove_panes` deletes the requested blocks and prunes changed non-root containers from the bottom up: empty containers go away, single-child containers collapse into that child. Only an empty tracked team tab is removed, along with its empty space if Factory made it. An already missing block is fine.

After a successful edit, `up` labels added or swapped terminals with their actual `herdr.space` and `herdr.tab`. Rodeo owns `herdr.seen`. The saved layout changes only when the resolved team tab exists in the resulting document. A layout failure is a warning; a later `up` places the live terminals without blocks.

## 6. `up` and `down` sequences

`up [names] [--fresh]`. Steps 1–5 change nothing in `hc`, in the layout or in the team's files, so a refusal or a failed preflight leaves everything as it was (§5.2). The only thing written before step 6 is the private state directory, which is also the team lock.

1. `Config`; the selected plans.
2. `hc` answers and is new enough; `HC_TERMINAL_ID` is set.
3. The state directory: an existing team terminal's `factory.dir`, else the computed path; create it private if missing. Take `<state>/up.lock` with `LOCK_EX | LOCK_NB`; if held: exit 2, `another up, down or brief --send is running for this team`. Names are reserved by this lock, not by labels.
4. **Under the lock:** `me = hc.get(HC_TERMINAL_ID)`; `view = team_view`; `check_operator`. Both are read now, not before the lock, so a caller that waited behind another `up` sees the operator that `up` just marked and is refused instead of demoting it.
5. To start: selected members whose state is `not started` or `gone`. Preflight `checked()` on those: exit 2.
6. `mark_operator`; write `team.json` (`root`, `team`, `core`, `up: true`). Settle launch facts before reading saved sessions, including on a running team.
7. Print the interrupted line for each `gone` member (§8). If no member was live: list leftover message files and the count of pending pairs for the operator.
8. `ensure_worktree` for every member to start (v1). A failure fails that member only.
9. `keeper = ensure_keeper(…, force=True)`.
10. For each member: `session_plan`; `start_member`; then, for a `gone` member, remove its old record (`hc.kill` first when it is a live terminal that stopped reporting, then `hc.rm`). The new terminal exists before the old record is removed, so the keeper never sees the member with no record.
11. Run the placement edit in §5, also when no member needed starting. Swap reusable member and keeper blocks first. Place each remaining headless team terminal in the selected tab. Save only IDs in the winning document and label the panes it placed.
12. `await_ready` on all started members together. For a resumed member that exited: the fresh retry of §3 (steps 10–12 once more for it).
13. For each ready member `log.add(operator, [name], message_for(...), kind="brief")` with v1's closing text (ready line, pickup line, or "new session" line); wait as `say` does until each brief is final or `SAY_WAIT` passes.
14. Settle launch facts with a fresh view and box, including briefs delivered during the wait (§3).
15. News to members that were already running: one `log.add(operator, others, "Joined the team: … No reply needed.")` (v1).
16. Print one line per member; exit 3 if any member did not start, is not ready, or its brief is not `delivered`, naming each with the command that fixes it (`factory.py up NAME`, or `factory.py brief --send NAME` when it runs but has no confirmed brief).

`down [names]`:

1. The caller's terminal must be `view.operator`: else exit 2, "run down from the operator's pane". Take `up.lock` as above. Check that `sessions.jsonl` can be appended to (§3); if not, stop nobody, exit 1.
2. For each named member (default all): no record: `@x was not running`. `hc.label(t, {"factory.down": "1"})`, then `hc.kill` for a live record. Once the kill succeeded, or for a finished record: `mark_stopped(state, [t], box)` with a full box. A terminal that cannot be ended records no stop and makes the exit code 1; a stop that cannot be recorded keeps the record, so `down` can be run again, and makes the exit code 1. A record with `factory.down` is the gate: while one remains, `up` refuses that member and the next `down` finishes its stop.
3. One log section: `dropped` for pairs `queued` to the stopped members; print `stopped @x (was <word>); <n> queued messages dropped`. Only then `hc.rm` each stopped record; if the sweep fails every record stays and the exit code is 1.
4. One layout edit removes stopped members’ panes. On the last stop, remove both the current keeper block and any block for the recorded keeper. Clear removed layout IDs from `team.json`. A write failure is a warning.
5. When no record remains for a configured member, set `team.json` `up: false` and print `run factory.py up to bring members back`. The keeper stops itself ([keeper.md](keeper.md) §8). If a removed member's terminal is still live, name it, show the `hc kill` command for its address, and exit 1.

`factory.down` is set before the kill, so a finished record with it is not a `gone` member (no exit note) and `up` does not replace that member until `down` has recorded and swept the stop. A send in flight to a stopped terminal is dropped through `team.json`'s `stopped` list, which `mark_stopped` writes and the keeper reads when it settles a definite refusal (§8.4, §8.5).

## 7. Claude Code settings and sandboxes

`claude_settings(level, message_dir)` keeps v1's shape with these values: `sandbox.excludedCommands` and the matching `permissions.allow` entries are `python3 <SCRIPT> say *` and `python3 <SCRIPT> inbox *`; at `full`, `sandbox.network` is `{"allowUnixSockets": [os.environ["HC_TERMINAL_SOCKET"]]}` on macOS when that variable is set (V12), else `{"allowAllUnixSockets": true}`; the message directory stays in `additionalDirectories`, `Write(...)` and `sandbox.filesystem.allowWrite`. `say` runs outside the sandbox, so it can write the log, reach `hc` and start the keeper; §10.9 proves it for `guarded`.

## 8. Interrupted members (§9.3)

For a member in state `gone`, `up` and `status` print:

```
@lead-dev interrupted: hc stopped while working. Last message to it: #41 from @operator at 2026-11-26T17:00:00Z.
```

The cause comes from the finished record's `reason`: `exited` → `exited (code <n>)`; `killed` → `was killed`; `server_stop` → `hc stopped`; a live terminal that stopped reporting → `its harness ended`; anything else → `was lost (<reason>)`. "while <word>" is the `factory.last` label, or `state unknown`. The message is `box.last_to(name)`. For a live terminal in that state, `up NAME` starts the new terminal, then kills and removes the old one, in the same order as for a finished record.

## Dependencies

Depends on: hc client, `Config`, `plan`, the harness adapters, message log (briefs), keeper (`ensure_keeper`). Used by: `cmd_up`, `cmd_down`, `cmd_status`, `cmd_brief`.

## Test plan

With the fake `hc` and fake harness executables on `PATH` (v1's `Env` fixture, re-aimed):

Operator and preflight
- `test_up_outside_hc_refuses`; one test per row of `check_operator`'s table, each asserting that the fake recorded no `terminal.label`, `terminal.run`, `terminal.kill` or `doc.set`, and that `team.json` and the log are untouched.
- `test_preflight_failure_changes_nothing`: exit 2; the same assertions.
- `test_failed_adoption_leaves_old_operator_label`: a new pane's `up` fails preflight; the old terminal still has kind `operator`.
- `test_up_marks_operator_after_preflight`; `test_new_operator_adopts_when_old_unreachable`.
- `test_two_ups_at_once`: the second exits 2 on `up.lock`; each member has one terminal.
- `test_operator_checked_under_the_lock`: the fake marks another reachable operator between the caller's start and its lock; the caller is refused and the other terminal keeps kind `operator`.

Launch
- `test_argv_per_harness`: for each of the six, fresh and resume, the recorded `terminal.run` argv equals the expected list.
- `test_run_env_and_labels`: `PATH` plus the adapter's environment only; the `factory.*` and `name` labels and no `herdr.*` label.
- `test_prompt_and_settings_rewritten_each_launch`; `test_claude_settings_values`.
- `test_running_members_left_alone`; `test_worktrees_created_before_any_terminal`; v1's `WorktreeTests` unchanged.
- `test_gone_member_new_terminal_before_old_record_removed`: order of the recorded calls.

Readiness
- `test_ready_on_idle_without_dialog`; `test_done_is_not_ready`; `test_working_is_not_ready`.
- `test_dialog_answered_once_when_blocked`, `…_when_silent` (Codex), `…_when_idle` (v1 #125); wrapped dialog text still matches.
- `test_agy_dialog_is_reported_not_answered`; `test_dialog_persists_after_answer`; `test_exit_during_start_reports_code_and_last_line`; `test_timeout_names_state`.
- `test_brief_appended_only_for_ready_members`; `test_up_exit_3_names_members_and_fix`.

Sessions
- `test_say_records_session_env`: one case per harness with a variable; the entry has `how: "env"`, the terminal, the harness and the model.
- `test_say_records_only_when_value_terminal_or_harness_differs`; `test_say_follows_a_switched_conversation`.
- `test_say_ignores_missing_or_malformed_value`: empty, whitespace, a leading `-`, 200 characters: nothing saved, `say` succeeds.
- `test_say_from_operator_records_nothing`; `test_say_session_write_failure_warns_and_sends`.
- `test_concurrent_says_append_whole_lines`: 8 members' `say` processes at once, 20 rounds; every line parses, one entry per changed value, none duplicated.
- `test_torn_last_line_is_repaired_under_the_lock`.
- `test_session_follow_false_records_once_per_launch`: a second, different value from the same terminal is ignored.
- `test_session_follow_false_first_capture_replaces_launch_entry`: both orders. (a) `up` writes the `new` entry, then the ready line: the ID is recorded and the next `up` resumes it. (b) The ready line first, then `up`'s step 14: no launch entry is added. In both, a later different value is ignored.
- `test_pi_and_omp_never_record_env`: with `PI_SESSION_ID` set in the environment, `say` from a pi member saves nothing; the only entry is `up`'s `dir` entry and the resume argument is `--continue`.
- `test_resume_args_per_harness`: each cell of the launch-argument table.
- `test_nothing_saved_before_brief_delivered`: a member that exits during start, or whose brief is `unconfirmed`, leaves no entry.
- `test_ready_line_before_launch_entry_wins`: the member's `say` records ID X for the terminal, then `up` reaches step 14: no entry is added and the next `up` resumes X.
- `test_fresh_launch_invalidates_previous_conversation`: Codex with entry A; `up --fresh`; brief delivered; `down` before any `say`; the next `up` passes no `resume` argument and prints the notice.
- `test_fresh_flag_ignores_saved`; `test_other_harness_starts_fresh_with_warning`; `test_provider_change_starts_fresh`.
- `test_failed_resume_retries_fresh_once`.
- `test_down_stops_nobody_when_sessions_unwritable`; `test_down_reads_no_harness_files`.
- `test_agy_settings_merge_trusted_workspace` (built only if LC-7 holds).
- `test_damaged_session_line_skipped_and_counted`; `test_sessions_are_per_team` (v1 #108).

Layout (pure functions on dicts)
- `test_new_tab_root_is_a_row`; `test_root_is_never_a_block_after_any_add_or_remove` (property check over random add and remove sequences).
- `test_three_members_three_columns`; `test_fourth_member_stacks_in_first_column`; `test_nine_members_three_by_three`.
- `test_keeper_pane_last_and_narrow`: with the keeper added first, last, and between members.
- `test_owner_restructured_root_is_wrapped_not_changed`; `test_owner_added_pane_survives_add_and_remove`.
- `test_remove_collapses_inner_container_only`; `test_tab_removed_with_last_member`; `test_tab_kept_when_owner_pane_remains`; `test_factory_made_space_removed_when_empty`; `test_missing_block_is_not_an_error`.
- `test_swap_terminal_keeps_position`.
- `test_edit_layout_retries_on_conflict`; `test_edit_layout_gives_up`.
- `test_resolve_tab_ignores_operator_tab`; `test_resolve_tab_prefers_saved_then_member_label_then_new`.

Placement and labels
- `test_layout_labels_written_after_doc_set`: order of recorded calls; `test_no_layout_labels_when_layout_fails`.
- `test_up_places_live_terminals_without_a_block`: a headless keeper and a member left unplaced by an earlier failure both get panes on the next `up`, with no terminal restarted.

Down and interrupted
- `test_down_sets_label_kills_removes_and_drops_queue`; `test_down_from_member_pane_refuses`; `test_down_last_member_marks_team_down`.
- `test_gone_member_reported_interrupted_with_reason_and_last_message`; `test_up_restarts_gone_member_in_same_pane`; `test_up_replaces_live_terminal_that_stopped_reporting`.
