---
status: draft
issue: 170
phase: 5b
branch: peter/170-layout-recovery
owners: ["@architect", "@lead-dev"]
---

# Phase 5b: Layout and recovery

Base `b12fb47` (Phase 5a merged; the branch was rebased onto it). Plan reviewer: @architect-3. Writer: @lead-dev. Follower: @lead-dev-3. The names of 5a's functions below were checked against `b12fb47` on 27 November 2026; §10 lists what that check changes for the integration steps.

Read first: [launch_layout.md](../components/launch_layout.md) §5, §6 and §8, [functional_spec.md](../functional_spec.md) §7, §8.5 (brief recovery), §8.7 and §9, [cli_skill.md](../components/cli_skill.md) §2 (`status`, `brief`), [testing.md](../components/testing.md) §6 (the rodeo driver), and LC-8's record in `docs/qualification/hc-checks.md`. This plan adds only what those leave open and names every place where it changes them.

## 1. Decision and scope

After 5a the team runs with no panes. This phase puts it on screen and closes the recovery paths: the team tab and keeper pane in the `herdr-app` doc, the labels that follow placement, what `status` says about panes, the session entry for a member whose first brief arrived late or by hand, leftover message files on a cold `up`, and adoption by a new operator proven end to end. It ends with the first rodeo smoke test and the simplification pass.

| Part | Source |
|---|---|
| `new_id`, `blocks`, `resolve_tab`, `add_panes`, `remove_panes`, `swap_terminal`, `edit_layout` | launch_layout.md §5, with the changes of §2 below |
| Placement in `up`, removal in `down`, the `layout` key of `team.json`, `herdr.*` labels | launch_layout.md §6 steps 11 and down 4 |
| `status`: panes and the not-ready hint | cli_skill.md §2, §4 below |
| `factory.launch` label and `settle_launches` | §5 below (replaces `up`'s step 14) |
| Leftover message files on a cold `up` | message_log.md "Message files", `Log.leftovers` (Phase 3) |
| Adoption by a new operator, end to end | functional_spec.md §7.4 |
| `live.py smoke` and `Rodeo.focus` | testing.md §6 |
| Simplification pass | implementation_plan.md Phase 5b, §8 below |

**Not in this phase:** member notes, the quiet rule, the ready check, the keeper's terminal title and re-exec (Phase 6); the skill text (Phase 7).

**Decided here**
- The existing team tab wins over the operator's current space (§2.1): a new operator in another space adopts the tab where it is.
- `team.json`'s `layout` gains `keeper`, the terminal ID in the keeper's block, so a keeper replaced by `say` or `status` gets its old pane back on the next `up` (§3).
- A member's launch fact (how, value, whether it was fresh, harness and model) travels on its terminal as the label `factory.launch`, and one idempotent function saves it once the brief is delivered, whoever sent that brief and however late (§5).
- A malformed or unexpected `herdr-app` doc is never written to and never stops a launch (§2.2).
- Factory writes `herdr.space` and `herdr.tab` and never `herdr.seen` (§3.1). functional_spec.md §6.1 says Factory never changes that marker; launch_layout.md §5 and architecture.md §3.2, which write `herdr.seen=0` once, are corrected to match the spec. A client treats a missing marker on a new terminal (turn 0) as seen.
- **v2 only** (owner's direction, 27 November 2026): nothing in this phase reads, preserves or tests v1 data or behaviour, and the simplification pass deletes what Phase 4 added for that (§8).

**Rejected**
- *Letting `ensure_keeper` fix the keeper's pane when it replaces a keeper.* It runs inside `say`, from any member's pane; only `up` and `down` edit the layout.
- *Recovering the launch fact by parsing the terminal's argv.* It would be the inverse of six argv builders and wrong for OpenCode, whose fresh and resumed launches use the same flag.
- *A pending-launch table in `team.json`.* A label disappears with its terminal; a table needs pruning.
- *Removing any block whose terminal no longer exists.* Factory cannot tell its own stale block from a pane the owner added, so it touches only blocks whose terminal it placed and still knows (§10).

## 2. Layout functions

launch_layout.md §5 is the specification. Build its seven functions as written, in a new section `# layout` after the keeper section, with these changes.

### 2.1 `resolve_tab`: one consistent pair

```python
def resolve_tab(doc: dict, layout: dict, view: View, me: dict) -> tuple[str, str, bool]
    # (space id, tab id, made_space); layout is team.json's "layout" dict or {}
```

§5's two independent rules can return a tab that is not in the returned space (a new operator in another space). Replace them with:

1. **An existing team tab, wherever it is.** First `layout.get("tab")` when it is a tab in `doc`. Otherwise, for a team whose saved ID was lost: count, for each tab in `doc` other than the operator's (the tab that holds `me`'s own block, and the tab `me`'s `herdr.tab` label names), the live terminals of kind `member` or `keeper` whose block is in it or whose `herdr.tab` label names it (each terminal once per tab); the tab with the most wins, the first in doc order on a tie; no tab with any: rule 2. A single label is not trusted on its own, because a pane the owner moved carries the ID of the owner's tab (§3.1). The winner gives the pair (the space that holds it, that tab). So an `up` from another space reuses the team's tab, creates no second one and moves nothing. `made_space` is `layout.get("made_space", False)` when that space is `layout.get("space")`, else False.
2. **Otherwise a new tab.** Space: `me`'s `herdr.space` label when that space is in `doc`; else `layout.get("space")` when it is in `doc`; else propose an ID from `new_id("sp")` and set `made_space` to True. Tab: `new_id("tb")`. Resolution leaves the document unchanged. `add_panes` creates the proposed space and tab only when it has a pane to place.

The fallback count excludes the operator's own tab even when the owner moved a member's pane there. A saved team tab still wins when the owner moved the operator's pane into it.

### 2.2 `edit_layout`: never write to a doc it does not understand

```python
def edit_layout(hc: Hc, change: Callable[[dict], bool]) -> str | None    # None when done; else why the doc was not updated
```

As §5 (up to `CAS_TRIES`, `conflict` retried with a `0.05 * attempt` s sleep), but returning a reason in place of False:
- `value` is neither None nor a dict with a list `spaces`: `"the herdr-app doc has an unexpected shape"`, nothing written.
- `change(doc)` raises `KeyError`, `TypeError` or `AttributeError` (a node of a shape §5 does not describe): the same reason, nothing written.
- `HcDown`, or an `HcError` other than `conflict`: its text.
- Still conflicting after the last try: `"another client kept changing it"`.

**Every attempt starts from the document it just fetched.** `change` computes the space, the tab and the list of terminals it placed or swapped from that document alone, and resets them at its start; the caller reads them only after `edit_layout` returned None, so `team.json` and the labels always describe the attempt that was written. A client that moved a pane or removed the tab between two attempts is honoured, not overwritten.

Callers print `layout not updated (<reason>): …` and carry on. A layout failure never changes an exit code and never stops or restarts a terminal.

### 2.3 `remove_panes`

```python
def remove_panes(doc: dict, layout: dict, terminals: list[str]) -> None
```

Delete each terminal's block wherever it is. Then, from the bottom up, in every tab a block was deleted from: a non-root container left with no child is removed, and one left with a single child is replaced by that child; a tab's root is never replaced, even when empty. Then, only for the tab `layout.get("tab")`: if it has no block left, remove the tab; and if `layout.get("made_space")` and that space has no tab left, remove the space. The caller decides whether the keeper's terminal is in `terminals` (§3.2); the function has no keeper rule of its own, which replaces §5's `keeper` parameter.

### 2.4 `add_panes` and `swap_terminal`

As §5, with rule 3's last case made complete. The size of a child of the root is the number of blocks anywhere below it. Take the child of `cols` with the fewest (the first on a tie): a `block` becomes `{"id": new_id("s"), "type": "column", "children": [it, block]}`; a `column` gets the block appended; **any other container** (a `row` or `stack` the owner made in that place) is kept whole and wrapped the same way, `{"type": "column", "children": [that container, block]}`, so nothing inside it moves. `add_panes(doc, space, tab, panes, keeper=None, *, folder=None)`: `panes` is `[(terminal, title)]` in formation order with the keeper last; the keeper's block is the one whose terminal equals `keeper`. The caller supplies the checkout as `folder` when a new space is needed. Titles are `"@" + name` and `"keeper"`; the tab's name is `"team"`. LC-8 passed on rodeo, so the keeper's block keeps `"size": {"weight": 0.4}`.

One more helper, used for every label: `where(doc, terminal) -> tuple[str, str] | None`, the IDs of the space and tab that hold that terminal's block.

## 3. `up` and `down`

### 3.1 `up`: step 11 of launch_layout.md §6

After the members are started (step 10) and before `await_ready` (step 12), so the owner can see a member that waits on a dialog only they may answer. One `edit_layout` whose `change` does, in order:

1. `space, tab, made = resolve_tab(doc, layout, view, me, cfg.root)`.
2. `have = blocks(doc)`.
3. For each member restarted in this run whose old terminal still has a block: `swap_terminal(doc, old, new)`.
4. The keeper: if `layout.get("keeper")` is another terminal than the current keeper and still has a block, and the current keeper has none: `swap_terminal(doc, layout["keeper"], keeper)`.
5. `add_panes` for every live team terminal that still has no block: members in formation order, then the keeper.
6. Return whether anything changed. Nothing to place means no `doc.set` (§9.1: `up` on a running team does nothing).

When it returns None (done):
- `update_team(state, layout={"space": space, "tab": tab, "made_space": made, "keeper": keeper})`, but **only for IDs that are in the document**: the resolved tab must exist in the document as it stands after the edit, whether or not this edit wrote it, and the value must differ from the saved `layout` (so a rebuilt `team.json` recovers the IDs). When the resolved tab does not exist there (every team pane already sits elsewhere, for example after the owner closed the team tab, including the case where this edit only swapped a terminal into a pane in another tab), nothing is saved: an ID that `resolve_tab` only proposed must never reach `team.json`. `keeper` is saved as the terminal named by the keeper's block, or absent when the keeper has none. If this write fails after the doc was set: one warning line (`layout placed, but team.json was not updated: <error>`), no exit-code change; the next `up` finds the tab through §2.1's count.
- For each terminal this edit placed or swapped in: `hc.label(term, {"herdr.space": s, "herdr.tab": t})` where `(s, t) = where(doc, term)` in the document of the winning attempt (`change` records it per terminal). For an added pane that is the team tab; for a swap into a pane the owner moved it is the owner's tab. Never before the block is in the doc. `herdr.seen` is never written, so an existing marker is kept. A failed label is one warning line; the pane is still placed.

When it returns a reason: no label, no `team.json` change, and `layout not updated (<reason>): <names> run without a pane; run factory.py up again to place them` on stderr. The next `up` places them through rule 5.

The fresh retry of a failed resume (launch_layout.md §3) gets a second edit of the same kind: `swap_terminal(doc, failed, fresh)` when the failed terminal's block is still there, else `resolve_tab` and `add_panes` for the fresh one; then the same label rule and the same `team.json` rule.

### 3.2 `down`: step 4 of launch_layout.md §6

After the stops, the stop evidence, the record removals and the queue sweep: one `edit_layout` with `remove_panes(doc, layout, terminals)`, where `terminals` is the stopped members' terminal IDs, plus, when this `down` leaves no member with a terminal record, the current keeper's ID and `layout.get("keeper")` (the team is going down; the keeper finishes its work without a pane and exits, and the block may still name an earlier keeper that `say` or `status` replaced with a headless one). Then `update_team(state, layout=…)` without what was removed: `keeper` when its block went, `tab` when the tab went, and `space` with `made_space` back to False when the space went; a failure of that write is a warning line, like `up`'s. A reason from `edit_layout` is printed as `layout not updated (<reason>): close the panes of <names> in your client`.

A block already removed by a client is not an error.

### 3.3 What is left alone

The operator's pane, tab and focus; every block whose terminal is not a team terminal of this team; the position and size of every existing node except the wraps of §5.

## 4. `status`

One `hc.doc_get()` and `blocks(doc)`, read only. A failure to read the doc, or a doc of a shape `blocks` cannot walk, prints nothing extra, raises nothing and changes no exit code. The brief hint is left out when the log cannot be read (the diagnosis is already printed).

- The keeper line ends with `, no pane (run factory.py up)` when the keeper is live and has no block (cli_skill.md §2).
- New line, when any live member has no block: `no pane: @x, @y (run factory.py up)`.
- For each live member past `starting` without a delivered first brief, show the pending brief ID and outcome when one exists. Otherwise print `@x has no confirmed brief: run factory.py brief --send x`. A manual brief request for a pending first brief also reports it and exits 1 for that member.

## 5. The launch fact and `settle_launches`

As #169 merged (`b12fb47`): a fresh launch records `new` with a null value for its terminal as soon as it starts, so an older conversation is never resumed in its place, and `up` replaces that with the assigned ID or the folder once the brief is delivered **inside its own wait**. `launch_entry` never replaces an entry `say` made for the terminal and never repeats one. Two gaps remain, and this section closes both: a brief delivered after `up` stopped waiting, or sent by `brief --send` after the owner cleared a dialog, never upgrades `new` to the ID or folder, so a pi, omp or OpenCode member loses its conversation at the next `up`; and a fresh terminal can still exist without its `new` entry when `up` is killed between starting it and writing the entry, or when that line is later damaged and skipped (since `a0a41e3`, an `up` that cannot write `new` stops the terminal itself and exits 3, so a plain write failure no longer leaves one running); an older ID is then the newest entry and a plain `up` would resume it. Fix, in three parts:

1. **Label.** `start_member` adds `factory.launch` to the labels it passes to `hc.run`: a compact JSON object, so the fact is complete on its own and does not depend on what `factory.toml` says later (the owner may change a member's harness or model between that launch and the recovery):

   ```json
   {"how": "id", "value": "<assigned ID>", "fresh": true, "harness": "claude", "model": "claude-opus-5-5"}
   ```

   `how` and `value` come from the session plan this launch actually used and the harness table as shipped, never from the harness name: `id` with `sess["assigned"]` when the plan assigned an ID; else `dir` with `str(session_folder(state, m))` when the harness table gives it a per-member folder (`session = "dir"`, so not for a harness that fell back to an environment ID); else `new` with a null value. `fresh` is `not sess["resume"]`. A launch that resumed by ID gets no label; a resumed launch of a folder harness gets `dir` with `fresh` false. `harness` and `model` are the ones this terminal was started with (`m.get("model")` of the launch plan, the value 5a's own `launch_entry` call saves).
2. **One function.**

```python
def settle_launches(cfg, state, hc, view, box) -> tuple[dict[str, str], list[str]]
    # ({member: why its entry could not be saved}, warnings)
```

   For each configured member, take its live terminal, or with none its newest finished record (`view.finished(name)`: a member that was started or briefed and then exited still owes its entries, and `hc` accepts a label change on a finished record). When that record carries `factory.launch`, with `m = {"name": name, "harness": fact["harness"], "model": fact["model"]}` (the launch as it was started, not today's plan):

   1. **Invalidate.** If `fact["fresh"]`: `launch_entry(state, m, address, "new", None)`. Normally #169 already wrote it at start and this does nothing; it is the second line for a fresh terminal left without its entry, whether or not a brief was ever delivered. `new` must never land after an entry for the same terminal, and that is decided where the journal is locked: `launch_entry`'s `decide` gains one condition, so it skips when the member's newest entry is for this terminal and that entry is `env`, or `how` is `"new"`, or `(how, value)` is the same. `settle_launches` reads no separate snapshot of the journal. The locked write sees any session entry recorded by the member’s `say`.
   2. **Upgrade.** If `fact["how"]` is `id` or `dir` and the address is in `box.briefed()`: `launch_entry(state, m, address, fact["how"], fact["value"])`.
   3. **Clear.** When nothing more is owed (`how` is `new`, or step 2 has run): `hc.label(address, {"factory.launch": ""})`. An `id` or `dir` fact whose terminal is not briefed yet keeps its label.

   A label that is not a JSON object with a `how` of `id`, `dir` or `new`, a boolean `fresh`, a string `harness`, a `model` that is a string or null (a member with no model in `factory.toml` has none), and a string `value` for `id` and `dir`, is a warning and is left alone. The two failures differ: a `launch_entry` that raises puts the member in the first result and leaves the label, so the entry is still owed; a label that cannot be cleared is only a warning, because the journal already has the entry and the next call clears it.
3. **Callers.**
   - `up` calls it twice, each time with a `team_view` and a full box read at that moment (a `LogDamage` there is one warning and the call is skipped). **First** right after `mark_operator` and before `saved_sessions` is read, any `session_plan` is made or any old record is removed (step 10): a brief that arrived after an earlier `up`'s wait is saved before this launch is planned. Else a late-briefed pi or omp member starts over, and a Codex or agy member started with `--fresh` would reopen the conversation from before it. A member in the first result of that call is **not restarted in this run**: its old record and label stay, it is reported as `@x: its conversation could not be saved (<why>); not restarted. Then run: factory.py up x`, and `up` exits 3 for it; planning from the entry before it would resume the wrong conversation. **Second** after this run's brief wait, in place of step 14, for all members and not only the ones it started; here an unsaved member is one stderr line (`@x: its conversation could not be saved yet: <why>`) and no exit-code change, since a later call still owes it.
   - `brief --send` calls it after its wait, with the same stderr line and no exit-code change. It holds `up.lock` from the first terminal and log snapshot through settlement to serialize with replacement and stop.
   - `down` calls it after its session-storage preflight and before it stops anyone: a selected member in the first result is **left as it is**: a live one keeps running and an already finished one keeps its record (no `hc.rm`, so the label that still owes the entry is not lost). It is reported as `sessions cannot be saved (<why>): @x left running` (or `… @x's record kept`), and `down` exits 1 after stopping the others (functional_spec.md §9.4).

No ID or folder is saved before a brief is delivered, as launch_layout.md §3 requires; only `new`, which names no conversation, is saved earlier.

## 6. Leftover message files

In `up`, at launch_layout.md §6 step 7, when no member had a live terminal before this `up`: `rows, warnings = open_log(state).leftovers()`; print `format_leftovers(rows)` on stdout and each warning on stderr. `LogDamage` or `OSError` there is one warning line and does not stop the launch (the pending lines already carry the diagnosis). No other command lists leftovers.

## 7. Adoption by a new operator

No new mechanism: `check_operator` and `mark_operator` are 5a's and the keeper sends to whichever terminal is `view.operator`. This phase proves the whole path and the layout's part in it, with one test (§9): the old operator's terminal ends with messages queued for it; `up` from another terminal in another space marks it, restarts nobody, leaves the team tab where it is, places nothing new, and the queued messages are typed into the new operator's terminal oldest first.

## 8. Simplification pass

Last, on the same ticket, as its own pull request from a branch off the merged layout work (decided by @operator), so the reviewer sees a diff that changes no behaviour.

Rules:
- No behaviour, message text, exit code or file format changes. Every test that exists before the pass passes unchanged after it, except tests whose only subject is code this pass deletes; each such test is named in the pull request with the reason.
- No cut that makes error handling or a recovery path harder to follow (architecture.md §4: none of that is to be compressed).
- Procedure: @lead-dev measures each section, lists every candidate cut with its estimated saving, and sends the list to @architect before cutting; @architect answers once with the ones to drop. Then one commit per kind of cut.

**Required by the owner's v2-only direction** (not candidates; do them):
- Delete `CutoverTests.test_v1_reader_still_prints_the_old_ids` and every assertion that v1 files exist, are read or stay unchanged; what remains of `CutoverTests` (the member prompt names the outcomes; `status` and `up` print the v2 pending lines) is asserted through the v2 commands and moves to `CliTests`.
- Delete the section "Messages from a v1 team" from `docs/getting-started.md`.
- Delete any code left that exists only for v1: the herdr names listed below, `print_pending` if 5a left it, the `continue` pointer in `main` and its test.
- Spec sentences the decision supersedes, to edit in the same change: functional_spec.md §4 (a v1 back-port for every formation change), cli_skill.md §2 (the `continue` pointer) and §3 (the v1 back-port), architecture.md §11 decision 4 (its back-port clause), testing.md §5 (v1 test classes that no longer exist). Past phase plans are records and stay as written.

Candidates to examine (what I saw in Phases 2 to 4; the list after 5a will differ):
- v1 remains: `HERDR`, `HERDR_TIMEOUT`, `TABS`, `SETTLE`, `session_key`, `records`, `session_id`, `past_sessions`, `remember_sessions`, `tag`, and `FAKE_HERDR`, `HerdrContract` and `Env`'s herdr state in the tests, wherever 5a left any.
- The three spellings of "print a line on stderr and exit N" in `cmd_say`, `cmd_inbox` and 5a's commands: one helper.
- Small files in the state directory read the same way in `_read_team_file`, `read_beat` and the session journal: one "open below the directory descriptor, read, parse" helper.
- Outcome wording in `outcome_word`, `outcome_lines`, `Keeper._emit_outcome` and `pending_lines`.
- Path shape checks spread over `open_log`, `state_for`, `resolve_team` and `managed_file`.
- `Log`'s thread-local state: only the keeper's main thread and short-lived commands use a `Log`; examine whether plain attributes suffice, and keep it if any test or caller shares one across threads.
- Test fixtures that build the same team twice (`Env` re-aimed by 5a, `TeamCase` from Phase 4).

End of the pass: measure `factory.py` and each section and record the numbers in architecture.md §4, replacing the estimates.

## 9. Tests

In `tests/test_factory.py`, fake mode unless marked. The isolation rule of phase_4.md §11 stands: no test may reach an `hc` it did not start.

**`LayoutTests`** (pure functions on dicts; launch_layout.md's "Layout" list in full): `test_new_tab_root_is_a_row`, `test_root_is_never_a_block_after_any_add_or_remove` (random sequences), `test_three_members_three_columns`, `test_fourth_member_stacks_in_first_column`, `test_nine_members_three_by_three`, `test_keeper_pane_last_and_narrow`, `test_owner_restructured_root_is_wrapped_not_changed`, `test_owner_added_pane_survives_add_and_remove`, `test_remove_collapses_inner_container_only`, `test_tab_removed_with_last_member`, `test_tab_kept_when_owner_pane_remains`, `test_factory_made_space_removed_when_empty`, `test_missing_block_is_not_an_error`, `test_swap_terminal_keeps_position`, `test_edit_layout_retries_on_conflict`, `test_edit_layout_gives_up`, `test_resolve_tab_ignores_operator_tab`, `test_resolve_tab_prefers_saved_then_member_label_then_new`. Added by this plan:
- `test_resolve_tab_returns_the_space_that_holds_the_tab`: the saved tab is in space A, the caller's `herdr.space` is B: (A, tab).
- `test_edit_layout_refuses_an_unexpected_doc`: a list, a dict without `spaces`, and a tab whose root lacks `children`: a reason, and the fake recorded no `doc.set`.
- `test_resolve_tab_without_saved_id_counts_team_terminals`: the tab with the most team terminals wins; the operator's own tab never does, with a member's pane moved into it.
- `test_removal_prunes_empty_containers_but_keeps_the_root`.
- `test_fourth_pane_wraps_an_owner_made_row`: one of the three columns is a `row` the owner nested; the fourth pane wraps the smallest child in a `column` and the owner's row is unchanged inside it.
- `test_remove_panes_only_empties_the_team_tab`: a member pane the owner moved into their own tab is removed and that tab stays even when empty of team panes.

**Placement** (commands on the fake): launch_layout.md's `test_layout_labels_written_after_doc_set`, `test_no_layout_labels_when_layout_fails`, `test_up_places_live_terminals_without_a_block`, `test_up_restarts_gone_member_in_same_pane`. Added:
- `test_up_on_a_placed_team_writes_no_doc`.
- `test_noop_up_never_saves_proposed_ids`: every team pane sits in the operator's tab and the team tab is closed; `up` writes no doc and `team.json`'s `layout` is unchanged. With `team.json` rebuilt and the team tab present, the same `up` restores its IDs. An `up` whose only edit swapped a terminal into a pane in another tab, with no team tab in the doc, also saves nothing.
- `test_fresh_retry_takes_the_failed_block_or_gets_a_new_pane`.
- `test_restart_in_an_owner_moved_pane_labels_the_actual_tab`: the old block sits in another tab of another space; after the swap the new terminal's `herdr.space` and `herdr.tab` are that pane's, and `layout` still names the team tab.
- `test_last_down_removes_the_recorded_keeper_block_after_a_headless_replacement`.
- `test_layout_metadata_write_failure_is_a_warning` for `up` and `down`: the doc is set, the exit code unchanged.
- `test_replaced_keeper_gets_its_old_pane`: the keeper's terminal changes between two `up`s; the block keeps its ID and position and names the new terminal.
- `test_layout_failure_never_changes_the_exit_code` for `up` and `down`.
- `test_down_removes_panes_and_the_tab_with_the_last_member`; `test_down_keeps_the_keeper_pane_while_members_remain`.
- `test_seen_marker_is_never_written`: no recorded `terminal.label` call names `herdr.seen`, and a marker a client set survives `up`, a swap and `down`.
- `test_conflict_recomputes_from_the_new_doc`: between the first `doc.get` and its `doc.set` a client moves a member's pane to another tab and removes the team tab; the second attempt places into the document as it now is, and `team.json` and the labels carry the second attempt's IDs.
- `test_two_teams_get_two_tabs_and_do_not_touch_each_other` (v1 #108).

**Status and recovery**: `test_status_reports_headless_keeper` (cli_skill.md) and, added: `test_status_lists_members_without_a_pane`, `test_status_hints_brief_send_for_an_unbriefed_member`, `test_status_survives_a_malformed_doc_and_a_damaged_log`, `test_start_member_sets_the_launch_label` (one case per row of §5's label rule, with `fresh` true and false for a folder harness, run against the installed `harnesses.toml` and against a table with each live-check fallback selected), `test_settle_launches_after_a_late_brief`, `test_late_briefed_member_that_exited_keeps_its_conversation` (pi: the brief is delivered after `up` returned, the terminal then exits; the next `up` saves the `dir` entry before planning and resumes with `--continue`), `test_fresh_codex_does_not_reopen_its_older_conversation_after_a_late_brief`, `test_fresh_terminal_without_its_new_entry_is_repaired_before_the_next_plan` (a fresh labelled terminal exists with no entry for it, as after an `up` killed between start and write; the brief arrives late or never, the terminal exits before any `say`: the next plain `up` writes `new` first and starts a new conversation with the notice, and does not resume the older ID), `test_launch_entry_never_writes_new_after_an_entry_for_the_same_terminal` (after `id`, `dir` and `env`; and two processes racing an upgrade against the invalidation end with the upgrade newest), `test_launch_fact_with_no_model_is_valid`, `test_malformed_launch_label_is_ignored_with_a_warning`, `test_launch_fact_keeps_the_harness_and_model_it_was_started_with` (a late-briefed Claude launch is settled after `factory.toml` changed the member to OpenCode, and again after a provider change: the entry is Claude's, and the next `up` starts fresh with the "saved conversation is for" notice), `test_up_does_not_restart_a_member_whose_old_launch_cannot_be_saved` (exit 3, the old record and its label still there; the next `up` saves and resumes), `test_second_settle_uses_a_fresh_view` (a member started in this run is settled), `test_settle_launches_after_brief_send_recovery` (an agy member left on its dialog, cleared, briefed by hand: the entry exists and the label is gone), `test_settle_launches_is_idempotent_and_yields_to_a_ready_line`, `test_down_saves_a_late_launch_before_stopping`, `test_down_leaves_a_member_running_when_its_launch_entry_cannot_be_saved` (exit 1, the others stopped; and for a member already finished, its record is kept), `test_uncleared_launch_label_is_a_warning_and_is_cleared_next_time`, `test_cold_up_lists_leftover_files_and_removes_delivered_ones`, `test_warm_up_lists_no_leftovers`.

**Adoption**: `test_new_operator_adopts_team_tab_and_queue` (§7), on both backends where the fake is not scripted.

**Rodeo** (local, not CI; `tests/live.py`):
- `Rodeo.focus(pane_title)`: verify the key sequence against the QA layout doc for a row of three and for a stacked column, as testing.md §6 left open; record what works in testing.md.
- `python3 tests/live.py smoke`: two private cores as LC-8, the QA core with a private `XDG_CONFIG_HOME` whose `herdr-core/config.toml` sets `draft_wait = 15` (as LC-10 and LC-13 did); a temp bin with one fake harness (a wrapper that answers the model listing as the tests' fake does and otherwise runs `tests/fake_agent.py reports`); a two-member formation; a fake agent as the operator's terminal. Steps and checks: `up` exits 0; rodeo shows the tab `team`, each `@member` title once and the `keeper` pane last and narrowest; `type` plus `enter` in a member pane reaches that fake agent's log; a `say` to a member shows one `delivered` line in the keeper pane; a draft typed without Enter in a member pane holds the next `say`: the message is still absent from that agent's log 6 s later (past `hc`'s short quiet pause, so it is the `draft_wait` hold that is proven) and the keeper pane shows `sending (held)`; after `enter` the draft arrives first and the message behind it; `down` exits 0 and the tab is gone from the doc and the screen. It prints one line per check and exits non-zero on the first failure. @lead-dev runs it before asking for review and pastes its output in the pull request; every later phase ends with it.

Coverage rule (testing.md §5): each rule of §2 and each numbered step of §3.1 has a test.

## 10. Build order, risks and questions

Each step is one commit, green in fake mode; real mode before review.

1. The layout functions of §2 with `LayoutTests`. Independent of #169: start now.
2. `Rodeo.focus` and the scaffolding of `live.py smoke` that needs no `up`. Independent of #169.
3. After #169 merges, rebase. `up` placement (§3.1) and the `layout` key.
4. `down` removal (§3.2).
5. `status` lines (§4).
6. `factory.launch`, `settle_launches` and its three callers (§5); leftovers (§6).
7. The adoption and two-team tests (§7).
8. The smoke run; docs: launch_layout.md §3 (the label and `settle_launches`), §5 (the three changed signatures, the completed rule 3, no `herdr.seen`) and §6 (steps 11, 14 and down 4); architecture.md §3.2 (`factory.launch`; `herdr.seen` is the clients') and §3.3 (`layout.keeper`); cli_skill.md §2 (`status` lines); testing.md §6 (focus keys, smoke).
9. The simplification pass (§8).

**Integration notes against `b12fb47`** (line numbers are `skill/scripts/factory.py` there):
- `_launch_fact(m, state, sess)` (line 3321) already gives `(how, value)` or None by §5's rule; `start_member` builds the label from it, adding `fresh`, the harness and the model. Its labels dict is at line 2923.
- `_up` returns at line 3424 when it has nothing to start. Placement and the first `settle_launches` call must also run on that path (a headless keeper, a member left unplaced, a late-briefed live member), so do both before that return when there are no plans.
- Line 3421 discards `ensure_keeper`'s result; §3.1 needs the keeper's terminal ID from it.
- The first `settle_launches` call goes after `update_team` (line 3412) and before `saved_sessions` (line 3432); a member it reports goes into `failed` and `fixes` and is not started. The second call replaces lines 3527 to 3533, after the brief loop, with `team_view(hc, team)` and `log.read_box()[1]` read then.
- The placement edit goes between the start loop (ends line 3470) and `ready_group` (line 3476); the fresh retry (lines 3479 to 3484) gets the second edit.
- Leftovers are printed beside `pending_lines` under `if not was_live` (line 3418).
- `_down`: `settle_launches` after `sessions_writable` (line 3610) and before the stop loop; the layout edit after the `after` view is read (line 3650), with the keeper IDs added on the branch that sets `up=False`.
- `cmd_status` prints `no pane` unconditionally in its keeper lines (3701, 3704); make it depend on the doc.
- `_brief_send` gets its `settle_launches` call after its outcome loop (line 3815). Its lifecycle lock prevents stale settlement after a replacement. The journal lock separately protects writes from concurrent `say` (§5).

Risks and gaps:
- **Stale blocks Factory cannot see.** If the owner removes a member's terminal record by hand, or a `down` could not update the layout, a block with a dead terminal stays until a client removes it or the owner closes it. Factory does not guess which blocks are its own.
- **Closing a pane in rodeo.** On private hc 0.1.20 and rodeo 0.2.1, `Ctrl+B` then `x` on a probe pane with `keep=false` removed the pane and left the terminal `finished`. Factory treats that record as `gone` and `up` restarts the member.
- **5a's names.** `start_member`, `launch_entry`, `session_plan`, `saved_sessions` and the shape of `up`'s steps are taken from the plan, not from merged code. Confirmed by @lead-dev-2 on 27 November 2026: `start_member` builds the terminal's labels in one dict at its `hc.run` call, where `factory.launch` joins; `session_plan` returns `{"args", "resume", "assigned", "notice"}`; `launch_entry` skips a terminal whose newest entry is its own.

Decided by @operator on 27 November 2026; no question is open:
- v2 is tested with `hc` and rodeo only (owner's direction). No other client and no other operating system is tested, qualified or listed, in this plan, its pull requests or the qualification docs; they are not gaps.
- The simplification pass is its own pull request on #170, after the layout pull request merges.
- The v2-only edits to the owner-approved spec files go in this ticket's pull requests; each pull request body lists the files and sentences it changed.
