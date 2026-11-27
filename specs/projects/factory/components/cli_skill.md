---
status: complete
---

# Component: CLI and skill text

## Purpose and scope

The command surface of `factory.py` (arguments, who may run what, output, exit codes), the small `Config` additions, and every change to the files agents read: `SKILL.md`, `references/`, `templates/`, `recipes/`, `harnesses.toml`, and the user docs. Mechanisms live in the other component docs; this one fixes the contract.

## 1. Finding the team

```python
def resolve_team(args, hc: Hc | None) -> tuple[Config, pathlib.Path | None, dict | None]
    # config, state directory, caller's terminal record
def find_team(args) -> tuple[Config, pathlib.Path | None, dict | None]
    # tries hc when HC_TERMINAL_ID is set; falls back offline on HcDown
```

1. `HC_TERMINAL_ID` is set and `hc` answers: `t = hc.get(me)`. If `t.labels` has `factory.team` and `factory.dir`: the state directory is `factory.dir`, the root is `team.json`'s `root`, `Config(root)`.
2. Otherwise `Config(args.cwd or os.getcwd())`, team key from its root. With `hc`: the `factory.dir` label of any terminal in the team view; none: `<cache base>/factory-skill/<team>/<hc.core[:8]>`.
3. Without `hc` (offline `inbox`): choose a directory under `<cache base>/factory-skill/<team>/` with a `log.jsonl` or `team.json`. Prefer the newest log, or the newest `team.json` when there is no log. Break ties by pathname. State the choice on stderr when several qualify. Ignore symlinked directories and diagnose unsafe log files. No qualifying directory returns None as the state.

`say` uses only step 1 and refuses otherwise. The caller's identity is `(t.labels["factory.kind"], t.labels["factory.member"])`.

## 2. Commands

`--cwd DIR` is accepted by every command except `say` and `keeper`.

| Command | Arguments | Needs `hc` | Caller | Exit codes |
|---|---|---|---|---|
| `init` | | no | anyone | 0; as v1 |
| `harnesses` | | no | anyone | 0 |
| `check` | `[names…]` | no | anyone | 0; 2 a member fails |
| `inbox` | `[N] [--for NAME]` | no | anyone | 0; 2 damaged log or no team state |
| `brief` | `[NAME]` | no | anyone | 0 |
| `brief --send` | `[all | names…]` | yes | operator | 0; 1 a brief not `delivered`; 2 refused |
| `up` | `[names…] [--fresh] [--timeout S]` | yes | operator pane | 0; 2 refused, preflight failed or the team lock is held (nothing changed); 3 a member not started, not ready or not briefed |
| `down` | `[names…]` | yes | operator | 0; 1 a member could not be stopped or a removed member is still running after shutdown; 2 refused or the team lock is held |
| `status` | | yes | anyone | 0; 2 `hc` unreachable |
| `say` | `NAME [NAME…] (--file PATH | TEXT)` | yes | operator or member | 0; 1; 2 (below) |
| `keeper` | `--state DIR` | yes | Factory | 0 |

Removed: `wait`, `prompt`, `continue`, `up --restart`, `up --no-brief`, `wait --seen`. `--timeout` is now seconds (default `LAUNCH_TIMEOUT`).

### `say`

Steps: `resolve_team` → `record_session` when the caller is a member ([launch_layout.md](launch_layout.md) §3) → validate → read text → `log.add` → `ensure_keeper` → wait ([message_log.md](message_log.md)) → print. Every `hc` call uses `Hc(expect_core=<team.json core>)`, so a pane whose environment points at another core is refused before anything is written.

Refusals exit 2 and print `nothing sent: <reason>`. A storage failure has the diagnostics below.

| Condition | Reason text |
|---|---|
| no `HC_TERMINAL_ID`, or the terminal has no team labels, or its kind is `operator-lost` | `run up first` |
| the caller is the keeper | `the keeper cannot send` |
| the caller is a member no longer configured | `@x is no longer in factory.toml` |
| a name is not the operator's or a configured member's | `@x is not on the team (members: …)` |
| a name is the sender | `choose another teammate as the recipient` |
| a named member's state is `not started` | `@x is not running` |
| `all` and no other member is live | `no other member is running` |
| a member passes inline text | `members send with --file` |
| neither `--file` nor text; unreadable file; empty text | `give a message or --file`; `cannot read message file <path>: <error>`; `give a non-empty message` |
| text longer than `MAX_TEXT` bytes | `message is <n> bytes; the limit is 65536. Write it to a file and send the path` |
| a file in the sender's message directory with a bad name | v1's message |
| `LogDamage` | the diagnosis |

When `--file` is present, every positional argument is a recipient. Inline text passed beside it is validated as a name.

`NotStored` exits 2 and prints `not stored: <detail>. Nothing will be delivered. Check the disk, then send again.` With `partial` it prints `not stored, and the log may now need repair: <detail>. Do not resend; tell the operator.` An unknown storage outcome prints `not confirmed stored: <detail>. Do not resend; check factory.py inbox.` and exits 1.

When `log.add` returns a warning (the commit record is completely written but the disk reported an error), `say` proceeds normally and prints on stderr: `warning: stored and will be delivered, but the disk reported an error: it may not survive a power loss. Do not resend.`

Once the message is stored, `say` prints its ID before trying the keeper. Startup or outcome-read errors include the ID and leave the stored message available through `inbox`.

Names are split on spaces and commas, `@` stripped, duplicates removed; `all` expands to every other member with a live terminal. One refused name refuses the whole command (§8.5).

Output and exit code:

```
message 42
@operator: delivered
@reviewer: queued (blocked)
@pm: sending
```

Exit 0 when every outcome is `delivered`, `sending` or `queued`; 1 when any is `unconfirmed` or `dropped` (followed by one line: `@x may not have it. Do not resend unless asked; tell the operator.`). A message queued for an unreachable operator prints `@operator: queued (gone): operator not running; message queued` (§8.5).

### `inbox`

`inbox [N] [--for NAME]`: N defaults to 10. NAME defaults to the caller's `factory.member` label, or to the operator's name when the caller is not on the team. It never calls `hc` unless `HC_TERMINAL_ID` is set, and falls back to step 3 of §1 when `hc` does not answer. Output: [message_log.md](message_log.md).

### `status`

```
factory 3f2a9c1d07be (/…/skill/scripts/factory.py), hc 0.1.20, team factory-ab12cd34
Member     Role      Harness  Model            Thinking  State    Turn  Terminal
@operator  operator  claude   claude-opus-5-5  medium    working  12    t3
@lead-dev  lead-dev  codex    gpt-6.2-sol      high      idle     7     t9
@pm        pm        omp      …                high      gone           t11
@pm interrupted: exited (code 1) while working. Last message to it: #41 from @operator at 2026-11-26T17:00:00Z.
pending: @operator 2 (oldest 3m, blocked); @reviewer 1 (oldest 10s, waiting)
keeper: running, version 3f2a9c1d07be, last loop 2 s ago
operator: reachable
```

- The first line is the loaded build (§5): `VERSION` and the script's path.
- Rows: the operator, then every configured member in `factory.toml` order. `Turn` and `Terminal` are empty when there is no live terminal (a `gone` member shows its finished record's ID).
- `pending`: per recipient, the count of pending pairs, the age of the oldest, and its current reason; `pending: none` otherwise.
- `keeper`: `running`, `not running`, `hung (last loop <n> s ago), restarted`, or `running version <v>, replacing itself`; `status` calls `ensure_keeper` first (which leaves a stopped team stopped). A keeper with no pane adds `, no pane (run factory.py up)`.
- `no pane: @x, @y (run factory.py up)` lists live members missing from the layout. For a live member past startup whose harness is still reporting and has no delivered first brief, show the pending first brief’s ID and outcome when one exists. Otherwise print `@x has no confirmed brief: run factory.py brief --send x`. A layout read failure suppresses pane hints. A damaged log suppresses brief hints.
- When the log holds uncommitted entries: `log: <n> uncommitted entries (never delivered)`.
- `operator`: `reachable`, or `not reachable: run factory.py up in the operator's pane`.
- Then v1's hint lines for `blocked` and `gone` members, any `strays`, the log-size warning above 64 MiB, and a `LogDamage` diagnosis if the log cannot be read (the table is still printed).

### `brief`

`brief` prints `render_team_brief` for the configured roster. `brief NAME` prints the header line and `member_prompt` (v1's `prompt`). `brief --send [all | names]`: each named member must be live (`not running: …`, exit 2); one `log.add(operator, [name], message_for(...), kind="brief")` per member, closing with `No reply needed.` for a member whose terminal is in `box.briefed()` and with the ready or pickup request for one that is not (§9.6); then wait and print like `say`. A pending first brief is reported by ID and outcome with exit 1 for that member. The command holds the same lifecycle lock as `up` and `down` through session settlement.

### `check`

v1's table, then the `hc` line and warning from [hc_client.md](hc_client.md).

## 3. `Config` additions

Additional optional settings in `factory.toml`:

| Key | Type | Check | Default |
|---|---|---|---|
| `quiet_grace` | integer seconds | at least 30 | `QUIET_GRACE` |
| `ready_check` | string, a shell line | non-empty | none: no ready check |
| `ready_check_every` | integer seconds | at least 60 | `READY_EVERY` |

A wrong type or value is a `SystemExit` naming the key, like the other checks. `plan(cfg, name, state=None)` returns `state_dir`, `message_dir` and `instructions` (None without a state).

## 4. Skill text and templates

Every file below is product (the agents read it). The rule for all of them: delete what described herdr 0.9.5, waits, watchers and acknowledgements; add only what an agent must do differently.

| File | Change |
|---|---|
| `skill/SKILL.md` | Description: "in herdr-core (hc)"; commands `setup, up, down, status, brief, say, inbox`. Delete the paragraphs on background and foreground operators, `wait`, acknowledgement, watcher hand-off. "Before launch": require `HC_TERMINAL_ID` (a rodeo pane) and `hc` 0.1.20; no integration check; the operator needs `hc`, the harness config directories, `~/.cache/factory-skill` and any worktree parent. Commands table without `wait`; `down NAME` then `up NAME` replaces `--restart`. Bare `/factory` routing as §5 of the spec. Launch: `up` marks this pane as the operator, adds the `team` tab and the keeper; exit codes 2 and 3. One sentence: "Never start a wait, watcher or background task for Factory: messages are typed into your pane." |
| `references/leading.md` | Replace "Receive and inspect" with: messages arrive as `[from @name]` and may start a turn or steer the current one; `factory.py inbox [N]` rereads them after compaction; what each `say` outcome means for the sender; `[from factory]` notes are information from Factory, not a teammate. Add the note table below. Rename "Autopilot" to "Autonomous operation": it is the default; keep its stop rules; delete the `herdr notification` line. Update the status table (`starting`, `gone`, keeper and operator lines). |
| `references/cmd_continue.md` | Commands without `--restart`; after an `hc` restart every member is `gone` and reported interrupted, run `up`; `inbox` replaces `wait --timeout 0`; a member comes back in the conversation it last sent a message from; one that starts in a new conversation says so in `up`'s output and needs its task again; "Change a running member's settings" becomes `down NAME` then `up NAME`, with `--fresh` for a new conversation. |
| `references/cmd_setup.md` | One question added to step 2: whether the keeper may run a read-only ready check, and with which command; it goes in the owner's `factory.toml`, never in `FACTORY.md` or the sample. Permissions: `HC_TERMINAL_SOCKET` replaces `HERDR_SOCKET_PATH`. After `check`: act on the `draft_wait` warning by telling the owner the one line to add to `hc`'s config; never edit it. |
| `references/pull-requests.md`, `notes.md` | No change. |
| `templates/member-prompt.md` | "herdr pane" → "pane". The send command is `{say} NAME --file {draft}` with `{say}` = `python3 <SCRIPT> say` (no `--cwd`). Replace the outcome sentences with the paragraph below. The last "Reports" paragraph ends with `Report sent to @<requester>` for `delivered`, `sending` or `queued`. |
| `templates/FACTORY.md` | `## Workflow` becomes the default below (§8a). |
| `templates/factory.toml` | Operator comment: "pane label" without herdr. Add, commented out, `quiet_grace = 120`, `ready_check_every = 300` and a `ready_check` example line. They stay comments, so `init` never enables a ready check (§8a.2). |
| `recipes/tasks-todos.md`, `tasks-gh.md` | Each gains a "Ready check" paragraph with one command that prints the ready items and exits 1 when there are none, tested against that tool by the developer who writes it. |
| `harnesses.toml` | [launch_layout.md](launch_layout.md) §1; header comment updated; no `deliver` key until a harness fails §10.4. |
| `README.md`, `docs/getting-started.md`, `docs/README.md` | Requirements (`hc` and rodeo); no operator modes; `draft_wait`; the keeper pane; what is not handled (§8a.3); the known limit of §8.2. |

Outcome paragraph for `member-prompt.md`:

> Run the send command alone (no `cd &&`, pipe, redirect or trailing `echo`). Factory adds your sender line and prints a message ID and one outcome per recipient. `delivered`: it was typed into their pane. `sending` or `queued`: Factory will finish it; do nothing more and never resend. `unconfirmed` or `dropped`: it may not have arrived; tell {operator} which message and recipient, and resend only when asked. `nothing sent`: nothing was stored. Fix what the line says, then send again. `not stored` or `not confirmed stored`: follow the diagnostic. When it says not to resend, tell {operator}. Factory removes your file once every recipient has the message.

Note table for `leading.md`.

Notes from `[from factory]` describe an observation that may have changed while delivery was held. Check `status`, the tracker and active work before acting.

| Note | Action |
|---|---|
| `@x is blocked` | Resolve the current block, or ask the owner when only they can. |
| `@x hit an error` | Reassign, or retry after the provider's delay. |
| `@x exited` or `was lost` | Run `factory.py up NAME` or reassign. Reconcile the pickup report, tracker and inbox before assigning the task again. |
| `team quiet` | Check who owes a report, then check for ready work. If neither needs action, end your turn without replying. |
| `ready work` | Read the current tracker and take its next ready item if you can assign it. |

Default `## Workflow` for `templates/FACTORY.md`:

```markdown
The team works through the ready items on its own. The owner sets direction; the team carries each ready item from plan to integration, then takes the next, and stops only for the owner's decisions.

| Step | Who | Output |
|---|---|---|
| 1. Direction | The owner, with the operator | Goals and priorities. Only the owner marks an item ready. |
| 2. Items | The pm, or the operator without one | Items with acceptance criteria; a current tracker. |
| 3. Plan | An architect, when the team has one | A plan for the item: interfaces, edge cases, tests, files. Small fixes skip this step. |
| 4. Build | Developers | Code and tests, following the plan. |
| 5. Review | The reviewer | A verdict on the exact diff. Fixes until approved; after two rounds on one item the operator decides. |
| 6. Integrate | The operator | Confirms the approval and the checks, then integrates and has the tracker updated. |
| 7. Next | The operator | Takes the next ready item without asking. |

- The operator assigns each step with acceptance criteria and records owner, criteria, branch or commit, verdict and next step in the tracker.
- One item is in progress at a time. Its tasks may run in parallel when each writer has its own worktree; in a shared checkout only one task writes at a time.
- Every member reports to whoever assigned its work when it finishes, is blocked, or needs a decision. A member that is rate-limited or stuck says so; the operator reassigns.
```

The pull-request recipe replaces step 6 with its own two steps, as this repository's `FACTORY.md` shows.

## Dependencies

Depends on: every other component. Used by: `main`.

## Test plan

- `test_parser_commands`: the v2 commands parse; retired commands and flags are rejected.
- `test_resolve_team_from_caller_labels`, `…_from_checkout_when_caller_is_outside`, `…_offline_single_dir`, `…_offline_newest_of_several`; `test_two_cache_homes_share_one_log` (the member's environment has another `XDG_CACHE_HOME`; its `say` lands in the operator's log).
- One test per row of the `say` refusal table, each asserting exit 2 and that the log is unchanged.
- `test_say_all_expands_to_live_members_only`; `test_say_names_split_on_commas_and_spaces`; `test_say_to_gone_member_is_queued`.
- `test_say_output_and_exit_codes`: 0 for delivered, sending and queued; 1 for unconfirmed.
- `test_say_records_session_before_validating`: a refused `say` (unknown recipient) still records the member's conversation.
- `test_say_prints_disk_warning_and_still_delivers`; `test_say_refused_when_pane_points_at_another_core`.
- `test_up_and_down_refuse_while_team_lock_held`.
- `test_say_from_operator_accepts_text`; `test_say_keeps_text_exact`.
- `test_status_table_and_lines`: every line of the sample for a scripted team; `test_status_shows_loaded_version_and_path`; `test_status_prints_table_when_log_damaged`; `test_status_counts_uncommitted_entries`; `test_status_reports_headless_keeper`; `test_status_leaves_stopped_team_stopped`.
- `test_brief_prints_and_brief_name_prints_instructions`; `test_brief_send_closing_depends_on_ready`; `test_brief_send_refuses_not_running`.
- `test_config_accepts_new_keys`, `test_config_rejects_bad_quiet_grace`, `test_current_sample_uses_default_ready_check_settings`.
- `test_init_sample_has_no_active_ready_check`: the template's `ready_check` is a comment.
- v1's `DocumentationTests`, `RecipeTests` and `InstalledCopyTests` kept, with the command list and file names updated: every command named in `SKILL.md` and the references exists in the parser, and none of `wait`, `--seen`, `--restart`, `herdr` appears in `skill/`.
- `test_member_prompt_has_no_cwd_and_names_outcomes`.
