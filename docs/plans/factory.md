# Factory: rename, migration, review lanes, TODO.md and docs (#12)

## Decision

The skill becomes `factory`: `/factory`, `FACTORY.md`, `factory.toml`. The repository is already `pkieltyka/factory`. A prefixed market name is undecided and appears nowhere.

Vocabulary, used in every file this plan touches:

- **factory**: the whole (the skill, a project's set-up, its tools and rules).
- **team**: the agents inside it: operator, members, roles, the `[operator]` and `[members]` tables. "The team brief" stays the name of what members are sent.
- **owner**, **people**, **the user**: humans.

| Was | Becomes | Step |
|---|---|---|
| skill name `a-team`, `/a-team` | `factory`, `/factory` | 4 |
| `TEAM.md`, `team.toml`, `team.toml.sample` | `FACTORY.md`, `factory.toml`, `factory.toml.sample` | 4 |
| `templates/TEAM.md`, `templates/team.toml` | `templates/FACTORY.md`, `templates/factory.toml` | 4 |
| `scripts/ateam.py`, `scripts/test_ateam.py` | `scripts/factory.py`, `scripts/test_factory.py` | 5 |
| `ATEAM_SETTLE`, `ATEAM_LIVE` | `FACTORY_SETTLE`, `FACTORY_LIVE` | 5 |
| `~/.cache/a-team` | `~/.cache/factory-skill` for new projects; a project that ran under a-team keeps its directory | 5 |
| tab labels `a-team operator`, `a-team workers` | `factory operator`, `factory team` | 5 |

Unchanged on purpose: the file name `LEGACY = "team-brief.md"` (its message follows the new names); the earlier default worktree branch `a-team/<name>` (code, `cmd_continue.md`, tests), which names branches that exist; `docs/plans/` and `notes/`, which are history.

## Steps

Each step is one pull request and needs its own ticket (one ticket, one branch, one pull request). Smallest first; a step depends only on the ones named.

| # | Step | Ticket | Needs |
|---|---|---|---|
| 0 | This plan | #12 | |
| 1 | Review lanes | new | |
| 2 | `TODO.md` template | new | |
| 3 | Move the skill into `skill/` | #36 | |
| 4 | Public names and the migration stop | new | 3 |
| 5 | Internal names | new | 4 |
| 6 | Cutover: install, this repo's own files, restart | new | 4, 5 |
| 7 | Docs and the first recipes | new | 6 |
| 8 | `tasks-gh` recipe and setup's proposal | #21 | 7 |

Steps 1 and 2 reach the running team through the normal skill sync. **From the merge of step 3, the owner holds the sync until step 6**: a sync in between would remove the script path every member's standing instructions call (steps 3 and 5), or fail because the skill's name no longer matches its `[a-team]` entry (step 4). Steps 3 to 5 therefore run back to back.

Every step runs the full suite. Steps 3, 4 and 5 also run the installed-copy test that step 3 adds, so a fault in a moved or renamed path shows in that step and not first at the cutover.

Paths below are today's. After step 3 the skill's files are under `skill/`.

## #36: before the rename

Do #36 after steps 1 and 2 and before step 4.

- The name changes then land on final paths, and the README is rewritten once.
- It decides what an installed copy contains, which step 7 depends on: recipes ship in `skill/recipes/` (see Decided).
- With the sync held, its install change joins the single cutover: one edit of the owner's manifest instead of two.

#36 gets its own design when it starts. This plan needs from it:

- `SKILL` in the script still resolves to the directory holding `SKILL.md`, `roles/`, `templates/`, `references/` and `harnesses.toml`. This repo's `TEAM.md` and `team.toml.sample` stay at the root.
- It is not a pure move: in the same pull request `.github/workflows/test.yml` calls the test at its new path, and `README.md` installs `skill/` and shows the new command paths and links. Step 5 then changes only basenames.
- A new test, the installed copy: copy the skill directory alone to a temporary directory named after the `name:` in its `SKILL.md`, and from that copy run `harnesses`, `init` in an empty project, `check` and `prompt` with the suite's fakes. It fails when the skill needs a file outside its own directory.

## Step 1: review lanes

Fast is the reviewer's own full review and the default. Deep adds a named CLI and runs only when the brief's Review section or the requester calls for it. No template change: a brief without `## Review` has one lane.

`roles/reviewer.md`, the review-tool bullet becomes:

> - Two lanes. Fast, the default, is your own full review as described here. Deep adds the review tool the team brief names: run it only when the brief's Review section or the requester calls for the deep lane, its check passes here and it can review the whole change (it may need commits), within the brief's limits. Its report is input, not a verdict: verify each finding, cover what it skipped or left partial, and name in the verdict the lane and what the tool reviewed. Unusable or failed: review without it and say so. It publishes nothing unless the user authorized that.

Same file, pull-request bullet: replace "and run the review tool now if it needed commits" with "and run the review tool now when the deep lane applies and it has not reviewed these commits".

`references/cmd_setup.md`, in the `## Specs`, `## Browser`, `## Review` bullet, the Review parenthesis becomes:

> `## Review` (only when the project uses a review CLI for the deep lane: which lane a review starts in, fast or deep, and when the other runs, for example deep at pull-request readiness; what the tool can review, a commit range or also uncommitted work; its limits: depth, time, publishes nothing)

`references/leading.md`, Review and integrate, first bullet gains:

> Name the lane, fast or deep, in a review request only when it differs from what `TEAM.md` says.

A tool that reviews only commits cannot run on a shared checkout's uncommitted work, so there the deep lane runs at pull-request readiness; no member commits to satisfy the tool. The section is prose for that reason, not a two-value setting.

This repo's `TEAM.md` Review section (the operator's file) gains "Reviews start in the fast lane." before the codegenie sentence; it already places the tool at readiness.

Test: the rendered reviewer instructions contain "run it only when the brief's Review section or the requester calls for the deep lane".

## Step 2: TODO.md template

New `templates/TODO.md`:

```markdown
TODO
====

## Ready

## In progress

## Backlog

## Done
```

`templates/TEAM.md`, the Tasks section becomes:

> Tasks and bugs are tracked in `TODO.md` at the repo root, one line each: `- [ ] <title> -- <description>`. `## Ready` holds, in the order they will be done, the items the operator may assign without asking the user; only a person puts an item there or reorders it, never the team. The team adds what it finds to `## Backlog`. An assigned item moves to `## In progress` with its owner at the end of the line, `(@lead-dev)`. Integrated work moves to the top of `## Done` as `- [x]` with its commit.

`references/cmd_setup.md`, the `## Tasks` bullet gains: "With `TODO.md` as the tracker and none in the repo, copy the skill's `templates/TODO.md` to the repo root." `init` does not write it: `init` runs before the tracker is chosen.

Added to the owner's layout, and accepted by the owner:

- `## Backlog`: without it the team has nowhere to record found work except Ready, which only a person may fill. It sits above Done because Done grows.
- The owner suffix and the commit on a done line: they are the "owner" and "commit" the Workflow section already asks the tracker to hold.

Considered and left out: item numbers (the title is the handle; keep titles unique), a `bug:` prefix, pruning Done.

Tests: `templates/TODO.md` has the four headings in that order; the template brief's Tasks section names each of them. The test that reads the stock brief's "ready" sentence (#28) follows the new sentence.

## Step 4: public names and the migration stop

### Code (`scripts/ateam.py`)

```python
BRIEF = "FACTORY.md"
CONF = "factory.toml"
SAMPLE = "factory.toml.sample"
OLD = {"TEAM.md": BRIEF, "team.toml": CONF, "team.toml.sample": SAMPLE}  # a-team's names: found only to say how to rename them
```

A rename is *pending* when the old name is a file in the root and the new name does not exist there.

- `find_root`: the marker tuple becomes `(CONF, BRIEF, LEGACY, "team.toml")`, and `cmd_init` uses the same tuple where it decides between the found root and the repository's top level. So what marks a root today still does, except `TEAM.md`: it may be another document. Neither sample is a marker, as today.
- New `refuse_old_names(root)`: when any rename is pending, `SystemExit` with the message below, listing every pending one.
- Commands that need the files (`Config.__init__`, inside `if missing and require:`, before `refuse_legacy`): call it. So they stop only when `FACTORY.md` or `factory.toml` is missing. With both present they run, whatever else lies beside them: a stale file never stops a member's `inbox send`.
- `cmd_init`, next to `refuse_legacy(root)` and before anything is written: call it. `init` stops on any pending rename, the sample's too, since it would otherwise write a new sample beside the old one.
- `check_all`, beside the `LEGACY` note, for `team.toml` and `team.toml.sample` that still exist, on stderr:
  - new name missing (only the sample can get here): `note: <old> is not read. Rename it: mv <old> <new>`
  - new name present: `note: <old> is not read; <new> is. Compare them, then remove <old>.` That both exist shows the old one is ignored, not that it holds nothing the new one lacks.
- `TEAM.md` beside `FACTORY.md`: nothing is said, anywhere.
- `cmd_init` sources: `templates/FACTORY.md` and `templates/factory.toml`. Its `.gitignore` logic is unchanged and now adds `factory.toml`.
- Every message, docstring and `--help` string that names the files or `/a-team` follows the constants, the `LEGACY` message included; `render_team_brief` keeps the title "Team brief".

The stop, with only the lines that apply:

```
<root> holds files under their earlier names (a-team is now factory); nothing was written. Rename them:
  mv TEAM.md FACTORY.md
  mv team.toml factory.toml
  mv team.toml.sample factory.toml.sample
Then run `ateam.py init`: it adds factory.toml to .gitignore. Remove the team.toml line there and commit the renames.
If TEAM.md is another document, leave it and copy <skill>/templates/FACTORY.md to FACTORY.md instead.
```

The last line is printed only when `TEAM.md` is among the renames. Plain `mv` is right with or without Git, tracked or not.

| Project root holds | Commands that need the files | `init` |
|---|---|---|
| only new names | run | writes what is missing |
| `TEAM.md` or `team.toml` whose new name is missing | stop, every pending `mv` line | the same stop |
| both new required files, old sample, no new sample | run; `check`, `up`, `continue` print the rename note | stop |
| `team.toml` or the old sample beside its new name | run; the compare-then-remove note | runs |
| `TEAM.md` beside `FACTORY.md` | run, silent | runs, silent |
| `team-brief.md` | as today | as today |

Where the root is found, and where it is not:

- An old `team.toml` marks the root from any subdirectory, as it does today.
- `TEAM.md`, the old sample, or both without a `team.toml`, are seen from their own directory, and by `init` at the Git top level when the directory it starts in and its parents hold no marker.
- Not found: those files without a `team.toml`, no Git, command run from a subdirectory. The command then says no files exist; `init` there would write new files in that subdirectory. Accepted: outside Git, a project with no formation is not a case worth a marker that could seize another project's document.
- One deliberate change: `TEAM.md` no longer marks a root. Today a parent directory that holds only `TEAM.md` captures a nested repository; after this step the nested repository's own top level is used.
- Unchanged: a marker in a parent directory wins over a nested repository's own top level, and a sample alone never marks one.

### Text

- `SKILL.md`: `name: factory`, the description, the title, every `/a-team` and file name; "a-team's own commands" becomes "factory's own commands".
- `templates/`: rename the two files; `# Team` becomes `# Factory`; the comments in `factory.toml`.
- `references/`, `README.md`, `harnesses.toml` comments, `scripts/gate1-setup.sh` (it edits the written brief): file names, `/factory`, and "a-team" as the product ("a-team notes convention", "a-team launch settings", "a-team locates members", "a-team stopped", "not a-team") becomes "factory". "The team" as the agents stays.
- `references/cmd_setup.md`, after the `team-brief.md` bullet, a new one: "**`TEAM.md` or `team.toml` from a-team:** the script stops and prints the renames. Do them. Write no new file to get past it, except `FACTORY.md` from the template when `TEAM.md` is another document, as the message says."
- `README.md`: a short "Coming from a-team" paragraph: install the skill as `factory`; the first command in a project prints its renames.
- This repo's `.gitignore`: add `factory.toml`, keep `team.toml` until step 6.
- This repo's own `TEAM.md`, `team.toml.sample` and each person's `team.toml` are **not** touched: the running team reads them through the installed skill until step 6.

### Tests

1. Old names only: `check` exits non-zero with the three `mv` lines; the directory is byte-identical afterwards.
2. `init` in that project: the same stop; `.gitignore` untouched.
3. New brief and sample, personal `team.toml` old: only `mv team.toml factory.toml` is listed.
4. Both new required files, old sample only: `check` passes and prints the rename note; `inbox send` exits 0; `init` stops with the sample's `mv` line and writes no new sample.
5. `TEAM.md` alone: `init` stops and prints the "another document" line; after copying the template to `FACTORY.md`, `init` runs and leaves `TEAM.md` as it was.
6. `TEAM.md` beside all new files: `check` prints nothing about it.
7. Stale `team.toml` beside `factory.toml`: `check` passes with the compare-then-remove note; `inbox send` exits 0.
8. Old `team.toml` in the root, command run from a subdirectory: the stop names the root.
9. `TEAM.md` and the old sample at a Git top level, no `team.toml`, `init` run from a subdirectory: the stop names the top level, lists both `mv` lines, and nothing is written in the subdirectory.
10. A nested repository under a parent that holds `factory.toml`: `init` in the nested one picks the parent, as it does today. Under a parent that holds only an old sample, or only `TEAM.md`: `init` uses the nested repository's top level and writes there.
11. Fresh `init` writes the three new files and adds `factory.toml` to `.gitignore`.
12. The `team-brief.md` tests pass with the new names in their message.
13. Sweep: `SKILL.md`, `README.md`, `references/`, `roles/`, `templates/` and `harnesses.toml` contain no `TEAM.md`, `team.toml` or `/a-team` outside the two migration passages and the `a-team/<name>` branch line.

## Step 5: internal names

Decided by the owner. Without it the skill is called factory while every command an agent runs says `ateam.py`. Every member is regenerated at the cutover anyway.

- `git mv scripts/ateam.py scripts/factory.py` and `git mv scripts/test_ateam.py scripts/test_factory.py`; `SCRIPT` and the import name in the test.
- `.github/workflows/test.yml` keeps its file name. In it, the test it runs is `test_factory.py` and the Git user name is "factory CI".
- `scripts/gate1-setup.sh` keeps its file name. In it, the script it calls is `factory.py`, the usage line says "factory skill dir", and the `mktemp` prefix is `factory-gate1.`. The gate test's glob and its unrelated-cache fixture follow.
- `ATEAM_SETTLE` and `ATEAM_LIVE` become `FACTORY_SETTLE` and `FACTORY_LIVE`; no fallback, both are development switches.
- `TABS`: `factory operator`, `factory team`. Cosmetic: `place` finds the team tab through a member's pane, never by label, so an open tab keeps its old label until it is closed.
- Text: every `ateam.py`, `test_ateam.py` and tab label in `SKILL.md`, `README.md`, `references/`, `templates/`, `harnesses.toml` and the script's own messages, the `LEGACY` message and the step 4 stop included.

The cache is not moved. `Config.cache` becomes:

```python
base = pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache")
key = f"{self.root.name[:24]}-{hashlib.sha1(str(self.root).encode()).hexdigest()[:8]}"
old = base / "a-team" / key  # a project that ran under a-team keeps its sessions and unread messages where they are
return old if old.is_dir() else base / "factory-skill" / key
```

- A new project gets `factory-skill/<key>`. Only the cache carries the `-skill` suffix (the owner's choice); every other name is plain `factory`. A project with an `a-team/<key>` directory goes on using it: no step at the cutover, nothing orphaned, and the earlier script still finds it if the cutover is backed out.
- `check_all`: when both directories exist, `note: <new dir> is not used while <old dir> exists.` on stderr.
- `SKILL.md`, Before launch: "`~/.cache/factory-skill` (`~/.cache/a-team` for a project that ran under a-team)"; `references/cmd_continue.md` names `sessions.jsonl` under the same two.
- `README.md`, in "Coming from a-team": moving a project's directory to the new name is optional and a person's act: with no team running on that project, and only if the new directory is not there, `mkdir -p -m 700` the `factory-skill` directory under the cache base (`$XDG_CACHE_HOME`, else `~/.cache`), then `mv` the project's directory into it.

Tests: no earlier directory, so the cache is under `factory-skill`; an earlier directory with a noted session and an unread message, so `continue` resumes that session and `wait` prints that message, with the new project file names; both directories, so the earlier one is used and `check` prints the note; a launched member's `--add-dir` and inbox command carry the directory in use; the existing cache-path, tab-label and gate assertions with the new names; the sweep of step 4 extended to `ateam.py` and `ATEAM_`.

## Step 6: cutover

Cold. The owner acts in their own configuration; the operator in this repo. Nothing may call the old script between items 1 and 6: it reads the project's files before it can report a failed send, so a member's report would fail with no typed fallback.

1. **Operator, quiesce:** no ticket in progress; `master` clean and fast-forwarded; the tracker current, since recovery reads it. Tell every member to finish, report, and then send nothing until it is restarted. Act on the reports, acknowledge them with `wait --seen <last id>`, and stop the `wait` watcher. Give the owner the revision to install: `master`'s head, which must be step 5's merge.
2. **Owner, skill manifest (`extras/skills.toml`), two passes and then one configuration commit.** The sync works through the manifest in order and removes a disabled copy at once, so a-team must not go before factory is in place.
   - Before pass 1: `git status --short -- extras` in that repository prints nothing. Commit any pending change there first, so the last commit is exactly the install the team runs on now. The recovery and the back-out below return to that commit, and the sync refuses to remove a copy that differs from its lock. On 2026-11-18 that checkout held an uncommitted sync: the lock and files under `extras/skills/a-team`.
   - Pass 1: add `[factory]` with `repo = "github:pkieltyka/factory"`, `ref = "master"`, `path = "skill"` and no `exclude`. Leave `[a-team]` as it is. Update **only** `factory` (the update takes names), with the cooldown at 0 (`--hours 0`, the Makefile's present value): it takes the newest commit older than its cooldown, and an older one may predate `skill/` or the rename. Check that `extras/skills/factory/SKILL.md` exists and that `extras/skills-lock.json` records `factory` at exactly the revision of item 1 with `path` `skill`. If this pass fails, a-team is untouched: remove the `[factory]` entry and any `extras/skills/factory`, and stop.
   - Pass 2: mark `[a-team]` `disabled = true` and update **only** `a-team`: that removes its copy and lock entry. Deleting the entry instead would leave both.
   - Before the commit, anything wrong is undone by restoring `extras/skills.toml`, `extras/skills-lock.json` and `extras/skills/a-team` from the last commit and deleting `extras/skills/factory`. With the tree clean before pass 1, that undoes this cutover and nothing else.
   - Review the diff. Commit the manifest, the lock and both copy changes as one commit, and switch Home Manager only when the lock and both copies are as above. Check: `~/.claude/skills/factory/SKILL.md` exists and `~/.claude/skills/a-team` does not; the same under `~/.codex/skills` and `~/.agents/skills`. Remove the disabled entry in a later commit, after item 8.
3. **Owner, sandbox:** where the configuration grants `~/.cache/a-team`, also grant `~/.cache/factory-skill`. Keep the first: this project's cache stays there. The team never edits sandbox settings.
4. **Operator, proof of install:** run the new script's `check` in this repo. Expected: the step 4 stop with three `mv` lines, and nothing written.
5. **Operator, on the step's ticket branch:** `git mv TEAM.md FACTORY.md`; `git mv team.toml.sample factory.toml.sample`; `mv team.toml factory.toml`; remove the `team.toml` line from `.gitignore`. In `FACTORY.md`: the product name and script paths in Project, `pkieltyka/factory` in Tasks, `-F repo=factory` in the review-threads command, "factory notes convention". In `factory.toml.sample`: its comments and the members' `notes` (script names, repository).
6. **Owner, in the operator pane:** `/factory continue --restart`. If the harness does not list the new skill, restart the operator's session first. Every member comes back with standing instructions that carry the new paths.
7. **Verify:** `status` shows the message route; `check` passes; each member queues a one-line report; the suite passes from the checkout. This is the gate: item 8 follows only when all of it holds.
8. The step's pull request (item 5) is reviewed and merged as usual; the team now runs on the new skill.

### Back out

Two parts, used by every stage below.

- **Restore the install:** in the owner's configuration, revert the commit of item 2 and switch Home Manager (before that commit exists, item 2 says how). Do **not** run the update: it follows `master`, which no longer holds a skill named a-team at its root. The copies are files in that repository, so the revert alone puts back a-team's copy and lock exactly and removes factory's.
- **Restore this repo's files**, by where item 5 stands:
  - uncommitted: `mv factory.toml team.toml`; `git mv FACTORY.md TEAM.md`; `git mv factory.toml.sample team.toml.sample`; `git restore --source=HEAD --staged --worktree -- TEAM.md team.toml.sample .gitignore`; then `git switch master`.
  - committed on the ticket branch, not merged: `git switch master`, which has the earlier names (step 4 kept `team.toml` in `.gitignore`); `mv factory.toml team.toml`. The branch stays, unmerged.

By stage:

- **After 2, 3 or 4:** restore the install. The members were never restarted: tell them to go on, and start the watcher again.
- **After 5:** restore this repo's files, then the install, then as above.
- **After 6:** the members now run factory. Quiesce again as in item 1, with the new script. Then restore this repo's files, then the install, then `/a-team continue --restart` so members get the earlier paths back.
- **After 8:** the owner's decision, not a routine: item 7 has passed by then. The reverse of item 5 goes through a pull request prepared on a branch in a temporary worktree, so the shared checkout keeps the new names the running team reads until it merges. Then quiesce, fast-forward `master`, `mv factory.toml team.toml`, restore the install, `/a-team continue --restart`.

The cache needs no step either way (step 5).

Other people: the README paragraph of steps 4 and 5.

## Step 7: docs and the first recipes

- `docs/README.md`, the factory on one page: one paragraph on what it is (an owner-directed software factory: a team of agents in herdr plus the sections a project turns on); a table with one row per choice (section, built-in default, recommended choice and when, recipe); six lines on how a task flows from ready to merged. Rows now: Tasks (`TODO.md` built in; `gh` on GitHub; `todos` for a board), Pull requests (off; GitHub), Review (fast built in; deep with codegenie), Notes (off; the built-in convention with rg). Specs and Browser: "no tested recipe yet".
- `docs/getting-started.md`: what the person installs and signs in to (herdr, one harness); installing the skill; `/factory setup`, `up`, the first reviewed task, `continue`. It links `references/` and repeats none of their rules. `README.md` keeps its short entry and links both pages.
- Recipes in `skill/recipes/`, shipped with the skill so setup can read them, one tool per file, `<section>-<tool>.md`, about 30 lines, exactly these headings: `# <Section>: <tool>` (one or two sentences: what it is, when to choose it), `## Needs` (where to get it and sign in; the person installs; nothing installs or upgrades itself), `## Check` (one read-only command), `## FACTORY.md section` (one fenced block to paste, starting with the section's heading; it alone holds how members learn the tool, the commands, the limits and who marks an item ready), `## Tested with` (version, date).
- First recipes, from what this repo runs: `tasks-todos`, `pull-requests-github`, `review-codegenie` (its pasted section carries the lane sentence of step 1). Later, each when first exercised: `notes-qmd`, `specs-vibe-crafting`, `browser-agent-browser`, `usage-herdr-agent-usage`.
- `references/cmd_setup.md`: step 2 gains "The skill's recipes hold tested choices for the optional sections: propose one when its check passes on this machine, name the alternative, and keep a tracker or tool the project already uses." The "Edit `FACTORY.md`" paragraph gains "Paste the chosen recipe's section and fill its placeholders."
- A recipe never needs the script to know a tool. When one would, that is a feature ticket.

Test: every recipe has the five headings in order; its fenced block starts with `## ` and a section name `cmd_setup.md` lists; `docs/README.md` links every recipe, by `../skill/recipes/`.

## Step 8: tasks-gh and setup's proposal (#21)

- `tasks-gh.md`: GitHub Issues with `gh`; ready is a label a person sets; check `gh auth status`.
- `references/cmd_setup.md`, step 2: "For tasks, propose `gh` when the remote is GitHub and its check passes, otherwise `TODO.md`; `todos` is the board choice."
- Trial in a scratch project before the recipe says "tested": setup offers the three choices, and an operator assigns from the label. This team has not run a label-based tracker.

## Decided

By the owner, on this plan:

- **Recipes live in `skill/recipes/`**, not `docs/recipes/`: under #36 an installed copy is `skill/` alone, and setup must read a recipe to propose and paste it. The two pages for people stay in `docs/` and link to `../skill/recipes/` and `../skill/references/`.
- **Step 5 is in**: the script, the test, the two variables, the tab labels and the cache follow the name.
- **The new cache directory is `~/.cache/factory-skill`.** Nothing else carries the suffix.
- **`TODO.md`** keeps the Backlog section, the owner suffix on in-progress lines and the commit on done lines.

## Rejected

- **Reading both names for a while.** Two names for one file in every message and doc, and no end date.
- **Renaming files automatically in `init`.** `TEAM.md` may be someone's other document, and a personal file would move without its `.gitignore` line.
- **A `migrate` command.** Three `mv` lines; the stop prints them (the `team-brief.md` precedent).
- **Stopping when an old and a new file both exist.** It would stop members' sends over a stale file; the note covers it.
- **Moving the cache at the cutover, by hand or by a stop that prints the `mv`.** A whole-directory `mv` nests when the target exists, a per-project one needs its parent first, and a project already renamed never reaches a stop that hangs on a missing file. Using the earlier directory where it exists has no step and loses nothing.
- **Renaming this repo's own files in step 4.** The running team reads them through the installed skill; every member's send would fail the moment the branch is checked out.
- **A `## Review` section in the template that says "fast".** Two lines in every brief to state the default.
- **One `tasks.md` for all trackers.** Setup would read every tool to use one.

## Verified against the code

- `BRIEF`, `CONF`, `SAMPLE`, `LEGACY` and `TABS` are constants at `scripts/ateam.py:38-43`; `find_root` (line 56) uses `(CONF, BRIEF, LEGACY)`; `refuse_legacy` is called from `Config.__init__` and `cmd_init`; the `LEGACY` note is in `check_all`.
- `Config.cache` is `<base>/<root name, 24 chars>-<sha1 of the root path, 8 chars>`. The checkout stays at its path, so the key does not change.
- A member's inbox command, the Claude allowed-command pattern and the `--add-dir` grant are built from `SKILL` and `cfg.cache` at launch, so a restart regenerates them.
- `place` creates the team tab with `TABS["team"]` only when no member pane exists; `up` and `continue` rename the operator's tab.
- `cmd_init` writes the brief, the sample and the personal file, and decides the `.gitignore` line with `git check-ignore`.
- The owner's manifest: the skill's directory must equal the `name:` in its `SKILL.md` and the sync checks it; the `a-team` entry excludes `docs` and `notes`; Home Manager links each skill into `~/.claude/skills`, `~/.codex/skills` and `~/.agents/skills`. Its update handles only entries in the manifest, removes a copy and lock entry only for `disabled = true`, takes the newest commit on the ref older than `--hours` and never moves backwards; the copies and the lock are files in the owner's repository (`scripts/skills-sync.py`, read with @lead-dev). Read, not changed.
- `Config.__init__` runs before `inbox send` can report a failed send, so a member's send has no typed fallback when the project's files or the script are gone.
- `codegenie review` takes a branch, commit, commit range or pull request (`codegenie review --help`).
- GitHub resolves the earlier repository name in REST and in GraphQL (`gh issue view` and the review-threads query, run with `herdr-a-team` on 2026-11-18). This repo's brief keeps working until step 6.
- Counts outside `notes/` and `docs/plans/`: `TEAM.md` 46 and `team.toml` 84 mentions in 9 files each; `/a-team` 15 in 7; `ateam.py` 100 in 13.

## Open

- Tickets for steps 1, 2, 4, 5, 6 and 7, and whether #21 is step 8 alone. Operator and @pm.
- Does `/factory` appear in a running Claude Code session after the switch, or only in a new one? Operator, at step 6.
- Is anyone other than the owner running a-team? It decides how much the README paragraph must carry. Owner.
- If the owner moves the checkout to a directory named `factory`: the cache key, the default worktree directory and Codex's trusted-project entry change. Not in this plan.

## Risks

- A sync between steps 3 and 6 breaks the running team or fails. The hold is a rule for one person; step 6 is the only place it is lifted.
- The team renames the tool it runs on. The suite and the installed-copy test run at steps 3, 4 and 5, but the first run with real harnesses is step 6; the back-out is written per stage.
- An unrelated `TEAM.md` in a project stops the first `init` once, with the way past it in the message.
- A project keeps `~/.cache/a-team` until a person moves it, so two cache names exist on one machine for as long as that takes.
- Droid, Factory's CLI, keeps a `.factory/` folder at a project's root. `docs/getting-started.md` says the two are unrelated.
- Worktree mode of the pull-request workflow is still untested; this plan does not exercise it.
