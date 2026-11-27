---
status: draft
issue: 174
phase: 9
branch: peter/174-e2e
owners: ["@architect", "@lead-dev"]
---

# Phase 9: End-to-end autonomy

Base `52ecd34`. Plan reviewer: @architect-2. Writer and runner: @lead-dev. Follower: @lead-dev-2. Runs beside Phase 8 (#173), which owns the real-harness launch code this phase reuses (§2).

Read first: [functional_spec.md](../functional_spec.md) §8a and §10 ("Autonomy tests"), [testing.md](../components/testing.md) §6 and §7, [phase_8.md](phase_8.md) §1 and §2 (isolation, evidence and the owner-state rules), and `skill/SKILL.md`, `skill/references/leading.md` and the two recipes this run pastes from.

## 1. Decision and scope

Three live runs with real harnesses and no human input after one prompt, each judged from durable evidence (the message log, Git, the tracker, the forge), never from what an agent says it did:

| Run | Command | Proves |
|---|---|---|
| Local | `python3 tests/live.py e2e --tracker file` | Three ready items planned, built, reviewed, merged and closed, through an interrupted turn and a killed member |
| GitHub | `python3 tests/live.py e2e --tracker github` | The same with a project board, real pull requests and CI on `pkieltyka/factory-test` |
| Empty board | `python3 tests/live.py e2e --tracker github --empty` (and `file --empty` as its rehearsal) | One quiet note, then silence for 15 minutes; a ticket moved to Todo is picked up through the ready check |

**Decided here**
- **The operator is told to read the candidate skill by path; `/factory` is not typed.** The one prompt names `<run>/skill/SKILL.md` and `<run>/skill/scripts/factory.py` absolutely. A bare `/factory` would load whatever skill the owner has installed (v1 today), and installing the candidate into a harness's skill directory is a change to the owner's settings that the rules of §3 forbid. So this phase tests the skill's text and commands end to end, not a harness's skill discovery; that gap is listed in §9 and belongs to the release.
- **The verdict is computed from records, by one function, item by item.** `judge(evidence) -> rows` reads the log (through `factory.Log`, read-only), Git, the tracker and, for GitHub, the forge. Each item needs its own chain: a plan, an approval that names the item and the exact commit, that commit checked and integrated, then closure. The driver's own acceptance checks are not in the repository the agents see.
- **The driver only watches.** Between the prompt and the evidence cut-off it runs no Factory command (§3).
- **Collecting and judging are separate.** A collector does all the I/O and produces one normalized evidence object (log records with their offsets, commits and ancestry, tracker states, forge records, the driver's own observations); `judge` is a pure function of that object, so every row can be tested without a run.
- **Order is never taken from timestamps.** Factory's `at` is whole UTC seconds. "Before" and "after" mean the order of records in the log (byte offsets), Git ancestry, the forge's own ordering, or the driver's monotonic observation counter, which it stamps on each fault, each tracker move and each integration it sees.
- **Faults are injected once each, from observed facts**: the assignment in the log and the developer's `working` state, never a timer.
- **Two checkout modes are covered**: the local run gives the developer a worktree (its branch, merged locally, stands in for the pull request); the GitHub run uses the shared checkout with ticket branches, the mode the pull-request recipe was tested with.
- **Everything the GitHub run creates carries the run's ID** and lives on its own integration branch and its own project; `master` of the test repository and the owner's other boards are never written.
- **Members run with the formation's default permissions (`full`)**, as the product ships and as the owner runs it, in a throwaway repository under `TMPDIR`. The tasks are small edits inside that repository.

**Not in this phase:** qualification of single cases (Phase 8); operators other than Claude Code and Codex; a harness's own skill installation and discovery; any deletion of GitHub artifacts (§5 leaves a manifest).

**Rejected**
- *A scripted operator for the real runs.* The point is that a real operator, reading the shipped text, keeps the team moving.
- *Letting the agents' reports count as evidence* ("all three done"). Only records count.
- *Fake time for the 15 quiet minutes.* The keeper is a real process; the wait is real.
- *Writing to `master` of the test repository so CI exists.* A workflow file on the run's own branch runs for its pull requests.

## 2. What is shared with Phase 8

This phase adds scenario code; it does not add a second way to start a real harness.

- From `tests/live.py` on `master`: `Core`, `Rodeo`, `owner_env`, `Harness` and its helpers, `smoke()`'s scratch-team set-up, `harness_version`.
- From #173 once its first pull request merges: the row and JSON collector (statuses `pass`, `fail`, `not_run`, `measured`; exit 1 before 2 before 0; checkpointing with `--json`), the real-harness adapter that starts an operator in a QA terminal with its own login, the `rodeo_tabs` tab selection, and the record of trust entries added.
- Order of work: build §4's fixtures, §6's triggers, `judge` and the rehearsal on `master`'s helpers now; rebase onto #173's driver before the first real run. If #173's driver has not merged when the real run is otherwise ready, tell @operator; do not copy its launch code.

## 3. The run

**Isolation and owner state.** As phase_8.md §2 and @operator's decisions for #173: a private QA core and a private driver core with rodeo; every `hc` call through a core-ID-checked client; a private `XDG_CACHE_HOME` for Factory's state and a private `hc` config; harnesses started normally with their own logins; scratch directories under `TMPDIR` named `factory-qa-<run_id>-…`; accepted writes are a harness's own session files, history, logs and caches, plus, for Claude Code and Codex only, the per-directory trust entries those scratch directories need, each listed in the report (harness, file, directory); nothing else in shared settings, no credentials read or copied, agy's trust dialog never accepted; a run that would need more is `not_run` with the reason.

**Team.** Operator: `--operator claude|codex` (default `claude`). Members: `lead-dev` and `reviewer`, harnesses from `--developer` and `--reviewer` (defaults `codex` and `claude`), each with an explicit model the runner has a login for, at a low or medium thinking level to bound time. No pm: the operator keeps the tracker. The report records harness, version, model and thinking for all three.

**Formation** (`factory.toml` in the scratch repository, written by the driver): the three agents; `quiet_grace = 30`; `ready_check_every = 60`; `ready_check` = the template's `TODO.md` command (file) or the `todos` recipe's command with the run's project (GitHub); `worktree = true` for `lead-dev` in the local run only; `user = "qa"`.

**Skill copy.** `skill/` of the branch under test is copied to `<run>/skill`, and the driver records its `VERSION` (sha1 of the copied script, 12 characters) before the prompt. Which Factory the team actually loaded is read passively: the keeper terminal's `argv` from `hc` must name `<run>/skill/scripts/factory.py`, and its `factory.version` label and `keeper.beat` must carry that version. Otherwise the run stops as `fail` ("another Factory was loaded").

**Observation is passive.** From the prompt until the evidence is collected, the driver runs no Factory command: `status` calls `ensure_keeper` and would itself repair a team the test is watching, and `up`, `say` or `brief --send` would be input. It reads `hc` (terminal records, screens), the log through `factory.Log` (readers create and rewrite nothing), Git and the forge. A `status` the operator itself ran is extra evidence when it shows in its pane. The evidence is collected and its cut-off time recorded before the driver runs `down`.

**The one prompt**, typed through rodeo into the operator's pane and submitted once:

> Read `<run>/skill/SKILL.md` and follow it; `factory.py` is `<run>/skill/scripts/factory.py`. You are the operator of the team in this repository. Run `up`, then work through every ready item in the tracker as `FACTORY.md` says until none is left. I am away: do not wait for me.

After that the driver types nothing into any pane of the QA session. It only observes (`hc` reads, the log, Git, the forge), injects the two faults of §6 through `hc`, and, in the empty-board run, moves one ticket as the owner would.

**Budget.** From the prompt's submission: 60 minutes for the local run, 120 for GitHub (CI latency), 45 for an empty-board run (30 s grace, 15 quiet minutes, one ready-check period and the assignment). `--budget MIN` overrides. The budget is a deadline, not a duration: the driver collects and judges as soon as every required row is proved. At the deadline it judges what is there and records the rest as `fail`. Only after the evidence is collected does it stop the team with `down` and close the cores.

## 4. The local run

**Repository** (built by the driver in `<run>/repo`, with a bare `<run>/origin.git` as `origin`; Git identity set in the repository's own config to a neutral name and a no-reply address):
- A small Python package, `calc/`, with `tests/` and `ci.sh` (`python3 -m unittest discover -q`; exit status is the verdict).
- `FACTORY.md` from `skill/templates/FACTORY.md`: Project filled in one sentence; Tasks as the template (`TODO.md`); Workflow as the template with two project lines added: run `./ci.sh` before integrating and integrate only when it passes; after integrating, `git push origin main`, which this brief permits because the remote is the project's own test remote. Notes disabled.
- `TODO.md` from the template with three items under `## Ready`, each with a `criteria:` bullet:
  1. Add `calc.clamp(x, lo, hi)` with tests.
  2. Add `calc.mean(xs)` that raises `ValueError` on an empty list, with tests.
  3. Fix `calc.parse_int`, which the fixture seeds with a bug (it rejects a leading `+`), and add a regression test.
- One initial commit, pushed to `origin`.

**Every item has its own chain of evidence.** The brief's Workflow gains three project lines so the chain is recorded where a test can read it: every assignment, report and verdict names its item as `(N)`; before building an item, a short plan is written on it as a `plan:` bullet in `TODO.md`; a verdict names the item and the full commit it covers, as `status: approve; item (N); commit <sha>`; the operator integrates by merging that exact commit into `main` (no squash, no rebase), so the reviewed code is recognisable afterwards. `R` is the reviewed code revision on the developer's branch; the operator's separate tracker commits on `main` never change which commit that is. For item N, with `R` the commit named by an approving verdict:

| Row (per item) | Evidence |
|---|---|
| `planned` | The item carries a `plan:` bullet, and the driver observed that committed plan before it observed the first implementation change for the item on the developer's branch. The plan commit and the developer's commits may sit on diverged branches, so the plan need not be an ancestor of `R` |
| `reviewed` | A delivered message from `reviewer` with `status: approve` naming `(N)` and `R`, observed before the integration. A verdict of `changes required`, a verdict for another item, one naming a commit that is not what was integrated, or one that arrives after the integration does not count |
| `checked` | `R` is on the developer's branch; in a clone at `R`, the driver's own `python3 -m unittest discover -s tests` exits 0 and ran at least one test (the fixture has `tests/__init__.py`; `ci.sh` runs the same command) |
| `integrated` | The reviewed tip `R` was integrated from the developer's branch: a recorded merge on `main` whose other parent is `R`, or a fast-forward to `R` that the driver observed; not merely a later tree that happens to contain similar code. The driver's acceptance check for item N passes in a fresh clone of `origin/main` |
| `closed` | The item is under `## Done` as `- [x]` with a commit, and under no other section |

| Row (per run) | Evidence |
|---|---|
| `no-direct-code` | Every commit on `main` after the initial one that touches `calc/` or `tests/` is an approved `R`, an ancestor that `R` brings in from the developer's branch (a branch may hold several commits), or the merge that brings `R` in. Tracker commits to `TODO.md` are the operator's and are allowed |
| `ci-green` | `./ci.sh` exits 0 with at least one test in a fresh clone of `origin/main` |
| `no-unconfirmed` | No pair in the log has the outcome `unconfirmed` |
| `quiet-then-chase`, `exit-then-up` | §6 |
| `one-prompt` | The driver's own count of submitted inputs into QA panes is 1, and it ran no Factory command before the cut-off (§3) |
| `loaded-version` | §3 |
| `within-budget` | All rows above held before the budget ran out; the elapsed time is recorded (`measured`) |

## 5. The GitHub run

Target: the private `pkieltyka/factory-test` (verified: private, default branch `master`, issues on, no workflow yet), with `gh` and `todos` as the recipes describe. The owner authorized creating its board, tickets, branches and pull requests.

**Set-up, by the driver as the owner, every name carrying `factory-qa-<run_id>`:**
1. Clone into `<run>/repo`. Create the integration branch `qa/<run_id>/main` from an empty tree with: the same `calc/` package and tests, `.github/workflows/test.yml` with one job named `tests` (runs the tests on `pull_request` and on `push` to `qa/**`), `FACTORY.md` (below). Push it. `master` is never touched.
2. Create a project `factory-qa-<run_id>` owned by the same account. Read back its Status field and bind the fixture to the options that are really there; a new project has Todo, In Progress and Done and no backlog column, so add a `Backlog` option there and record every option's ID. Record the project's ID, number and URL.
3. Create three issues titled `[factory-qa-<run_id>] …` with the three tasks and their criteria; add them to that project with status Todo. (For `--empty`: with status Backlog.)
4. **Every tracker write and the ready check name both the repository and that project explicitly** (`--repo` and `--project`, or the IDs); nothing relies on a default board. `todos doctor --json` proves the tool works, not which board it would use, so the driver also reads the run's project back through `todos board --repo … --project … --json` and requires its own three cards and no others. Else the run is `not_run`.

**`FACTORY.md`**: Project; `## Tasks` from `recipes/tasks-todos.md` with the run's repository and project and the columns `Todo`, `In Progress` and `Backlog`; `## Workflow` from the template with step 6 replaced as the pull-request recipe says, plus the project lines of §4 (the plan is an issue comment that starts `plan:`; the item's name is its issue number `#N`; a verdict names `#N` and the pull request's head commit), and one more: after a confirmed merge, close the issue and set its card to Done yourself, because a merge into a branch that is not the default closes nothing; `## Pull requests` from `recipes/pull-requests-github.md` with `BASE_BRANCH = qa/<run_id>/main`, ticket branches `qa/<run_id>/<ticket>`, and both standing permissions written out. Shared checkout: no member has a worktree.

**Pass rows**: the per-run rows of §4, and per issue `#N` with `R` the pull request's head named by the approving verdict:

| Row (per issue) | Evidence |
|---|---|
| `planned` | An issue comment starting `plan:` that the driver observed before the first implementation change for that issue (the first commit on its ticket branch, or the first change in the shared checkout, whichever it sees first); "before the pull request was opened" alone would let a plan be written after the work |
| `reviewed` | A delivered `status: approve` from `reviewer` naming `#N` and `R` |
| `checked` | On `R`, the `tests` job exists, ran at least one test (its log's count) and concluded `success`, with a completion time before the pull request's `mergedAt` |
| `integrated` | One pull request for `#N` into `qa/<run_id>/main`, state `MERGED` by squash, whose head at merge was `R`; the acceptance check for the task passes in a fresh clone of the integration branch |
| `closed` | After the confirmed merge, the issue was closed and its card set to Done by the team (the forge's own events show both after the merge; closing keywords are not relied on) |

| Row (per run) | Evidence |
|---|---|
| `no-direct-code` | Every commit on `qa/<run_id>/main` after the fixture's is the squash of one of those pull requests |
| `scoped-writes` | Every branch, pull request, issue and card the run created carries the run's ID or belongs to the run's project; `master`'s head is unchanged from the start |

**What is left behind.** The run writes a manifest, `<run>/artifacts.json`, copied into the report: the project (ID, number, URL), each issue, pull request and branch (number or name, URL), the trust entries added (harness, file, directory). The driver deletes nothing on GitHub and this phase adds no cleanup command: a merged pull request cannot be deleted through `gh` in any case. If the owner later wants them removed, the report lists what `gh` supports for each kind (close an issue, delete a branch, delete a project).

## 6. Faults and what must follow

Each fault is bound to an item, a terminal and a turn, acts once through the QA core, and is recorded with the log's end offset at that moment.

| Fault | Trigger (all observed) | Action | Required afterwards |
|---|---|---|---|
| 1. A turn ends without a report | A message from the operator to `lead-dev` that names item 1 is claimed and delivered to terminal `A`; `A` is `working` in the turn that assignment caused, judged from the driver's baseline of `A`'s turn before the claim together with the receipt's `turn` (the keeper can log `delivered` after `hc` already shows the turn started, so the turn need not begin after the record); `lead-dev` has sent no report for item 1 yet | `hc interrupt` on `A`; confirm `A` left `working` and record its turn | `quiet-then-chase`: no message from `lead-dev` about item 1 is committed between the assignment and the delivery of a note with `meta.what == "quiet"` to the operator; after that note, a message from the operator to `lead-dev` naming item 1 is delivered before item 1 is integrated |
| 2. A member is lost | A message naming item 2 is `delivered` to `lead-dev`'s terminal `B` after item 1's integration; `B` is `working`; `lead-dev` has no pending pair at all (none `sending`, none queued that could be claimed between the check and the kill) | `hc kill` on `B` | `exit-then-up`: a note with `meta.what == "exit"` and `meta.term == B` is delivered to the operator; after it, a brief has the outcome `delivered` to a terminal `C` that is not `B`, live, labelled `lead-dev` and created after the kill; item 2 is integrated in the end |

- "Names item N" is why §4's brief makes every assignment carry its item: join news, a general question or a follow-up about another item does not arm a trigger.
- If the interrupted developer did report before the quiet note (some harnesses finish a report after an interrupt), the stall was not exercised: the row is `not_run` with the message ID, and the run is repeated, with @operator told if it keeps happening.
- If a trigger never becomes true inside the budget (the operator did the item itself, say), that row is `not_run` with what was observed. A run with a `not_run` fault row is not a pass.
- The "no pending pair" condition keeps the kill from manufacturing an `unconfirmed` outcome of the test's own making. If a claim crosses the kill all the same and ends `unconfirmed`, the run fails `no-unconfirmed` like any other and is repeated.

## 7. The empty-board run

Same team and prompt; the tracker holds no ready item (file: `## Ready` is empty and one item, `(1)`, waits in `## Backlog`; GitHub: the three issues are in Backlog).

| Row | Evidence |
|---|---|
| `one-quiet-note` | From the prompt to the ticket move, the log holds exactly one committed note with `meta.what == "quiet"`, and its outcome is `delivered` |
| `silence` | In the 15 minutes after that note's delivery, no further `msg` record from `factory` is committed. A member seen `working` in that time is recorded as a diagnostic; it does not by itself fail the row |
| `ready-note` | The driver then marks one named item ready as the owner would (file: moves `(1)` under `## Ready` and commits as the owner; GitHub: sets that issue's card to Todo in the run's project). Within two ready-check periods exactly one note with `meta.what == "ready"` is delivered |
| `item-taken` | After that note, **that item** is taken: a message from the operator to `lead-dev` naming it is delivered, and the tracker shows it in progress or done (file: under `## In progress` or `## Done`; GitHub: its card in In Progress or Done). A message about anything else does not count |

Whether the item is then finished is recorded as `measured`, not required: the first two runs prove completion.

## 8. Tests and the rehearsal

**In `tests/test_factory.py` (CI, no `hc`, no login):**
- `judge` on synthetic evidence, written before any model turn is spent: one test per row of §4, §5 and §7 with a passing record set, and these failing ones: a verdict of `changes required` only; an approval naming another item; an approval naming a commit that was not integrated; `ci.sh` or the `tests` job green with zero tests; a code commit made straight on the integration branch; no `plan:` before the reviewed commit; a quiet note with no chase after it; a chase before the note; a report from the interrupted developer before the quiet note (`not_run`, not `pass`); an exit note for another terminal; a brief delivered to the killed terminal's own address; an `unconfirmed` pair; a second quiet note or any Factory message inside the 15 minutes; a ready note followed by a message about another item; a pull request merged before its check completed; an approval that arrives after the integration; a plan first seen after the first implementation change; a developer report for item 1 before the interrupt; a chase that does not name item 1. And these that must pass: a developer branch of several commits whose tip is the approved `R`; a plan commit on `main` that is not an ancestor of `R`; a `delivered` record written after `hc` already showed the caused turn.
- The two trigger predicates of §6 as pure functions of (log records, terminal record): true only in the stated state (a message that does not name the item, join news, or a `sending` pair keeps them false), and false after they fired once.
- The fixture builders: the local repository passes `./ci.sh`, fails the three acceptance checks before any work, and its `TODO.md` ready check exits 0 with three lines; the empty variant exits 1.
- CLI selection: `--tracker`, `--empty`, `--rehearse`; a real run can never be labelled a rehearsal or the reverse.
- The driver's observation path issues no Factory command (a spy on its subprocess calls between prompt and cut-off).

**Rehearsal (`python3 tests/live.py e2e --tracker file --rehearse`; local, real `hc` and rodeo, no model):** fake agents as operator, developer and reviewer; the driver issues the operator's Factory commands itself and makes the commits a developer would. It exists to prove the set-up, both triggers and injections, the keeper's real quiet and exit notes with a 30-second grace, `up` after the kill, and `judge` on a real log, before any model turn is spent. Its output is labelled `fixture` and is never a result of this phase.

## 9. Build order, risks and questions

1. Fixture builders, acceptance checks, `judge`, the trigger predicates, the CLI, with their tests (CI).
2. The rehearsal, green on a private core.
3. Rebase onto #173's driver. A real local run with the pilot's cheapest working pair of harnesses; fix what it shows; the run of record.
4. GitHub set-up against `factory-test`, then its run of record.
5. The empty-board runs (file as rehearsal, GitHub of record).
6. `docs/qualification/e2e-<date>.md`: versions (each harness, `hc`, rodeo, the loaded Factory `VERSION` and source head), the formation, a timeline of the observed events with message IDs, every row with its evidence, durations, what was not run and why, the trust entries added, and the GitHub artifacts left with their removal commands. Both suites, `python3 tests/live.py smoke` and `git diff --check` at the final head.

A run of record is one uninterrupted run at one source head. A failed run is kept in the report with its cause; a rerun is a new run ID.

Risks and gaps:
- **Models decide the outcome.** A run can fail because an agent misread a task. That is a finding about the skill text or the harness, recorded with the evidence; it is not fixed by loosening a row.
- **Skill discovery is not exercised** (§1): the prompt names the files. The release must check an installed copy invoked as `/factory`.
- **The interrupt may not end the turn silently** on every harness (an agent may still report after being interrupted). Then `quiet-then-chase` cannot be shown that way; record what happened and tell @operator before changing the fault.
- **GitHub latency and rate limits** can exceed the budget without any fault of Factory's; such a run is `not_run` for the rows it could not reach, and repeated.
- **Cost and time**: three real runs of up to two hours, plus reruns.

Decided by @operator on 27 November 2026; no question is open:
- Each GitHub run may create a user-level project board named `factory-qa-<run_id>`; it is left in place and listed in the run's manifest. The owner authorized the board and the mock tickets.
- Members run at the product's default `full` permissions, only in the throwaway repository and in the private `factory-test` repository, under the same owner-state rules as #173.
