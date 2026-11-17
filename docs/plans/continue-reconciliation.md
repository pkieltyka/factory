# Continue as reconciliation: A1, A2, A3 and Gate 1

Issues: A1 #4, A2 #5, A3 #6, Gate 1 #7 (depends on #4-#6). Agreed in the joint answers of @architect and @lead-dev.

## Decision

A project must be recoverable from `TEAM.md`, the tracker and Git alone. Sessions and member recollection only make recovery faster.

- **A1** makes `ateam.py continue` work when no member pane is left, and stops asking a member with no memory what it was doing.
- **A2** turns `references/cmd_continue.md` into a reconciliation procedure: an order of evidence, a rule for which source wins, and what the operator must do before it assigns work.
- **A3** puts review state on the existing task record, so a lost session loses no open finding and an approval is tied to what was reviewed.
- **Gate 1** is a rerunnable cold-recovery scenario with fixed pass criteria. It is the only proof for the prose in A2 and A3.

Nothing here adds a state file, a TOML key or a new command.

## Rejected alternatives

| Alternative | Why not |
|---|---|
| Checkpoint file (`STATUS.md`), or assignments and verdicts in `sessions.jsonl` | A second copy of what the tracker and Git already hold; it goes stale, and A2 would have to distrust it. `sessions.jsonl` stays a disposable cache of session references. |
| Bare `continue` always brings back every member of `team.toml` that is not running | Changes today's behaviour after a partial launch (`up reviewer pm`): it would start members nobody asked for. Only the case with no member pane at all changes; the others get a hint line. |
| Use `sessions.jsonl` to decide who was on the team | The cache is append-only history and may be gone (Gate 1 has none). `team.toml` is the team. |
| Automatic fresh start when a resume fails | Hides a cause that can be fixed. The explicit `--fresh` retry stays. |
| Bind a shared-checkout approval to a diff hash (hashing `git diff HEAD` with `git hash-object --stdin`) or to the base commit | The hash covers unrelated dirty files and misses untracked ones; the base commit moves with every tracker commit (seen while building the Gate 1 fixture). Rule instead: approved work that was never committed is reviewed again after a recovery. |
| A separate channel or log for review findings | The task record already has a verdict and one next-step field. The reviewer's required fixes become that next step. |
| Convention text in `TEAM.md` for reconciliation | It is the operator's procedure, not project policy; it belongs in the operator's reference. |

## A1 (#4) Cold continue

Files: `scripts/ateam.py` (`cmd_continue`, `send_brief`), `scripts/test_ateam.py` (`ContinueTests`), `references/cmd_continue.md` (text is in A2, one file edit for both).

### Behaviour

Selection of members, in `cmd_continue`:

| Call | Members with a pane | Selected | Output line |
|---|---|---|---|
| `continue a b` | any | `a`, `b` (unchanged) | none new |
| `continue` | at least one | members with a pane (unchanged) | when `team.toml` has members without a pane: `no pane, not continued: @x, @y. To bring them back: ateam.py continue x y` |
| `continue` | none | every member in `team.toml` (new) | `no member panes in this herdr session: continuing every member in team.toml` |
| `continue` | none, and `team.toml` has no members | exit 1 | ``no members in team.toml; add one, then run `ateam.py up` `` on stderr |

The old exit (``no member panes from team.toml in this herdr session; start the team with `ateam.py up` ``) is removed.

In the new cold case only, label the operator's tab as `up` does, because cold `continue` takes the place of `up`:

```python
if os.environ.get("HERDR_TAB_ID"):
    herdr("tab", "rename", os.environ["HERDR_TAB_ID"], TABS["operator"], check=False)
```

Everything after selection is unchanged: preflight for every member that needs a launch (exit 2), resume the pane's session or the last noted one of the same harness unless `--fresh`, a new pane when the old one is gone, exit 3 for failed starts or undelivered briefs, and the `--fresh` hint after a failed resume.

A member launched in this call **without** a resume reference is *fresh*. A member that failed to launch is not listed as fresh.

### The closing line of the re-brief

Today one closing goes to every recipient. It becomes per member:

- Resumed in this call, or already running and not relaunched: the existing text, unchanged (`We are picking up where we left off. ...`).
- Fresh:

  ```text
  You are starting in a new session and know nothing of the work so far. Re-read `TEAM.md` and do not start anything yet: @<operator> will send your task, its criteria and the evidence so far. No reply needed.
  ```

  (`TEAM.md` is the `BRIEF` constant; `<operator>` is the operator's name.)

After the brief is sent and before `report_failed`, when there are fresh members, print one line:

`fresh sessions, no earlier context: @x, @y. Send each its task, criteria and the evidence so far.`

Who is re-briefed does not change: with names, those members minus the failed ones; without names, every running member.

### Interfaces

`send_brief(cfg, team, operator, names=None, closing=None)`: `closing` may now be a `{member name: text}` dict as well as one string or `None`. With a dict, each target gets `closing.get(name)`; a missing name gets the default closing. `message_for` and `render_team_brief` are unchanged (they still take one string). `cmd_up` and `cmd_brief` call it as today.

`cmd_continue` passes `{name: fresh_text if name in fresh else pickup_text for name in cfg.members}`.

No new flag, no parser change, no change to exit codes.

### Tests (`ContinueTests`, fake herdr, `Env(UI)`: lead-dev, frontend-dev, reviewer, pm)

1. `test_no_member_panes_and_no_cache_starts_the_whole_team_fresh`. No `up` first; the cache directory has no `sessions.jsonl`. Run `continue`. Assert: exit 0; stdout has the `no member panes ...` line; `agent start` was called for all four and none carries a resume argument; one `tab create` labeled `a-team workers`; `e.calls("tab", "rename")` contains the operator tab with `a-team operator`; all four prompts contain `starting in a new session` and `No reply needed` and not `picking up where we left off`; stdout has `fresh sessions, no earlier context: @lead-dev, @frontend-dev, @reviewer, @pm`.
2. `test_no_member_panes_resumes_noted_sessions`. `up --no-brief`; `set_session` for lead-dev (codex `cx-1`) and pm (pi path); `status` (notes them); `e.close(...)` all four panes. Run `continue`. Assert: lead-dev's argv starts `["resume", "cx-1"]` and pm has `--session` with its path; frontend-dev and reviewer have no resume argument; lead-dev's and pm's prompts contain `picking up where we left off` and not `No reply needed`; the other two contain `starting in a new session`; the fresh line names exactly `@frontend-dev, @reviewer`.
3. `test_some_panes_gone_continues_only_members_with_panes`. `up --no-brief`; `e.kill("lead-dev")`; `e.close("reviewer")`. Run `continue`. Assert: lead-dev is started again, reviewer is not; stdout has `no pane, not continued: @reviewer. To bring them back: ateam.py continue reviewer` and not the `no member panes` line; frontend-dev and pm (still running) get the pick-up closing.
4. Extend `test_closed_pane_or_changed_harness_starts_fresh`: the prompts to pm and reviewer contain `starting in a new session`; stdout has the fresh line with both.
5. Extend `test_restart_checks_before_closing_and_reapplies_settings`: after `continue lead-dev --fresh --restart` the last prompt contains `starting in a new session`; after the plain `--restart` (resumed) it contains `picking up where we left off`.
6. `test_continue_without_members_says_so`. `Env(formation({}))`, `continue`: non-zero exit, stderr has `no members in team.toml`.
7. Unchanged and still passing: `test_brings_members_back_in_their_panes_and_sessions` (resumed members keep the pick-up closing, `No reply needed` absent), `test_failed_resume_says_how_to_start_fresh`, `test_up_and_continue_fail_when_brief_delivery_fails`.

Done when `python3 scripts/test_ateam.py` passes.

## A2 (#5) Reconciliation procedure

Files: `references/cmd_continue.md`, one line each in `SKILL.md` and `README.md`, and one hint line in `scripts/ateam.py` (`cmd_status`) with its test.

### `references/cmd_continue.md`

Replace the opening paragraph and code block with:

````markdown
Use after agents exit, herdr restarts or you lose context, including when no member and no session is left. `TEAM.md`, the tracker and Git must be enough to recover the project; sessions only make it faster.

```bash
ateam.py status
ateam.py continue                # members with panes, or the whole team when none has one; re-briefs everyone running
ateam.py continue lead-dev       # selected member, even if its pane closed
ateam.py continue --restart      # reapply settings to running members
ateam.py continue lead-dev --fresh --restart  # new session
```
````

In the paragraph that follows the code block, after the sentence ending `or no session was noted.` insert:

```markdown
A fresh member is told to wait for its assignment, and `continue` lists the fresh ones.
```

Replace the paragraph `Members are asked to report what they were doing and their next step. Compare replies with Git and the tracker; reassign missing context for fresh sessions. Then follow [leading.md](leading.md).` with this section, placed before `## Change a running member's settings`:

```markdown
## Reconcile before assigning

Read in this order: `TEAM.md`, `team.toml` and project plans; the tracker; Git (`git status`, `git log --oneline -10`, `git worktree list`, each worktree's `git -C <path> status` and `git diff <main>...<branch>`, and PRs and CI when the project has them); last, members' replies. Where they disagree, Git and CI outrank the tracker, and the tracker outranks anyone's memory, yours included. Have the tracker's owner correct a stale record; do not act on it.

- Match every uncommitted change to an in-progress item before any member writes. A change no item explains is not built on, committed or discarded: ask the user.
- A verdict covers only the commit or paths it names. Ask for a new review when the record has no verdict or no checks, when the branch moved past the reviewed commit, or when approved work was never committed.
- Tell the user where the project stands: each open item with its state, evidence and next action, then anything unexplained or contradictory. Then carry on; stop only for those and for decisions that are the user's.
- Send each fresh member its task again: goal, criteria, branch or files, the evidence so far and the required fixes still open. Resumed members report what they were doing; check it against the above.

Then follow [leading.md](leading.md).
```

The six bullets between them (pane reuse, worktree branch, Codex directory, herdr restart, failed resume, failed brief) stay as they are.

### `SKILL.md`

In the sentence that starts ``Bare `/a-team`:``, replace `No members → up;` with:

`No members → continue when the tracker or Git shows unfinished work, otherwise up;`

Without this, a new herdr session routes to `up` and never reaches the reconciliation reference. (`SKILL.md` has an uncommitted edit for #3 on another line; this does not overlap.)

### `README.md`

Replace this sentence:

```markdown
`/a-team continue` resumes noted sessions.
```

with:

```markdown
`/a-team continue` brings the team back, resuming noted sessions, and works out where the project stands from `TEAM.md`, the tracker and Git.
```

### `scripts/ateam.py`: the hint at the end of `status`

Bare `/a-team` runs `status` first, and tool output outranks the prose in `SKILL.md`. Today `cmd_status` ends, when no member has a pane, with a line that sends the operator to `up` and claims more than `status` knows (it sees panes, not history). Replace only that print:

```text
No member of team.toml has been started. Next: `ateam.py check`, then `ateam.py up`.
```

with (`team.toml` is the `CONF` constant):

```text
No member of team.toml has a pane in this herdr session. A team that ran before: `ateam.py continue`. A new team: `ateam.py check`, then `ateam.py up`.
```

No logic change, and `status` does not consult `sessions.jsonl` (the cache may be gone). Test: in `scripts/test_ateam.py`, a `status` run before any `up` asserts the new line, including `ateam.py continue`.

## A3 (#6) Review state on the task record

Files: `roles/reviewer.md`, `roles/pm.md`, one line in `references/leading.md` (for teams without a pm, where the operator keeps the record). Prose only. `templates/TEAM.md` is unchanged: its workflow already names verdict and next step.

What changes in the flow: the reviewer's verdict also goes to whoever owns the tracker, once per review round. Nothing else moves.

### `roles/reviewer.md`

Replace this bullet:

```markdown
- `status: approve` or `status: changes required`, exact commit/diff, and required fixes. Send the verdict to the requester.
```

with:

```markdown
- `status: approve` or `status: changes required`; what you reviewed (the commit, or for uncommitted work the changed paths); the checks you ran and their results; the required fixes. Send the verdict to the requester and, if someone else owns the tracker (@pm when present, otherwise {operator}), to them too.
```

### `roles/pm.md`

Replace the bullet `- Updates when work starts, finishes, blocks or exposes a bug. When status changes, rewrite the whole task record so status, verdict and its single next-step field agree; remove superseded instructions.` with:

```markdown
- Updates when work starts, is reviewed, finishes, blocks or exposes a bug. When status or verdict changes, rewrite the whole task record so status, verdict and its single next-step field agree; remove superseded instructions.
- A verdict is recorded with what was reviewed (commit, or changed paths) and the checks run. After `changes required` the next step is the reviewer's required fixes, in full.
```

### `references/leading.md`

Replace `- Tell @pm about assignments, decisions and completion. Each durable task record needs owner, criteria, branch/commit, verdict and next step. Maintain this yourself without a pm.` with:

```markdown
- Tell @pm about assignments, decisions and completion; reviewers send it their verdicts. Each durable task record needs owner, criteria, branch/commit, verdict (with what was reviewed and the checks run) and next step (after `changes required`: the required fixes). Maintain this yourself without a pm.
```

Check: `test_every_role_has_the_same_four_parts` still passes (no heading changes).

## Gate 1 (#7) Cold recovery experiment

A behavioural test of the operator following A1-A3, on real herdr and real harnesses. It is not part of `test_ateam.py`.

### Fixture

#7 adds `scripts/gate1-setup.sh` with exactly this content (verified: it builds, the three tests pass, `slugify("hello world", max_length=6)` returns `hello-`, commit `0f3a9c1` does not exist):

```bash
#!/usr/bin/env bash
# Gate 1 fixture: a small repo left mid-task, with no team running and no session cache.
# Usage: gate1-setup.sh <a-team skill dir> [parent dir]     (re-running rebuilds it from scratch)
set -euo pipefail
SKILL=$(cd "$1" && pwd)
GATE=${2:-${TMPDIR:-/tmp}}/ateam-gate1
rm -rf "$GATE" "${XDG_CACHE_HOME:-$HOME/.cache}"/a-team/ateam-gate1-*
mkdir -p "$GATE"
cd "$GATE"
git init -q -b main
g() { git -c user.name=gate1 -c user.email=gate1@example.invalid -c commit.gpgsign=false "$@"; }

cat > README.md <<'EOF'
# slug

Turns text into URL slugs. Tests: `python3 -m unittest`.
EOF
cat > slug.py <<'EOF'
def slugify(text):
    return text.lower()
EOF
cat > test_slug.py <<'EOF'
import unittest

from slug import slugify


class SlugTests(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(slugify("Hello"), "hello")


if __name__ == "__main__":
    unittest.main()
EOF
python3 "$SKILL/scripts/ateam.py" init --cwd "$GATE" > /dev/null
python3 - <<'EOF'
import pathlib, re
p = pathlib.Path("TEAM.md")
project = "slug: a one-function Python library that turns text into URL slugs. Standard library only. Tests: `python3 -m unittest`."
p.write_text(re.sub(r"(## Project\n\n)<!--.*?-->", lambda m: m.group(1) + project, p.read_text(), count=1, flags=re.S))
EOF
printf '# Tasks\n\nNothing recorded yet.\n' > TODO.md
g add -A
g commit -q -m "Start slug"

# T1: done, reviewed and committed.
cat > slug.py <<'EOF'
import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
EOF
cat > test_slug.py <<'EOF'
import unittest

from slug import slugify


class SlugTests(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(slugify("Hello"), "hello")

    def test_joins_words(self):
        self.assertEqual(slugify("Hello, World"), "hello-world")


if __name__ == "__main__":
    unittest.main()
EOF
g add -A
g commit -q -m "T1: slugify joins words with hyphens"
T1=$(git rev-parse --short HEAD)

# The tracker as the team left it. T4 is the planted contradiction: no such commit, no such code.
cat > TODO.md <<EOF
# Tasks

## T1 Slugify joins words: done
- owner: @lead-dev
- criteria: \`slugify("Hello, World")\` returns \`hello-world\`; tests pass.
- commit: $T1
- verdict: approve by @reviewer on $T1; ran \`python3 -m unittest\`, 2 passed
- next: none

## T2 max_length: doing
- owner: @lead-dev
- criteria: \`slugify(text, max_length=n)\` returns at most n characters and never ends with \`-\`; without max_length nothing changes; tests cover it.
- work: uncommitted in the main checkout: \`slug.py\`, \`test_slug_length.py\`
- verdict: changes required by @reviewer on those paths; ran \`python3 -m unittest\`, 3 passed
- next: @lead-dev makes the required fixes, then @reviewer re-reviews:
  1. \`slug.py:8\`: \`slugify("hello world", max_length=6)\` returns \`hello-\`; strip the trailing \`-\` after cutting.
  2. Add a test where max_length cuts right after a word.

## T3 Command line: todo
- owner: none yet
- criteria: \`python3 -m slug "Hello World"\` prints \`hello-world\`.
- next: start after T2 is done; it changes the same function.

## T4 Strip accents: done
- owner: @lead-dev
- criteria: \`slugify("Crème")\` returns \`creme\`.
- commit: 0f3a9c1
- verdict: approve by @reviewer on 0f3a9c1; ran \`python3 -m unittest\`
- next: none
EOF
g add TODO.md
g commit -q -m "Tracker"

# T2: reviewed, changes required, never committed.
cat > slug.py <<'EOF'
import re


def slugify(text, max_length=None):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if max_length is None:
        return slug
    return slug[:max_length]
EOF
cat > test_slug_length.py <<'EOF'
import unittest

from slug import slugify


class LengthTests(unittest.TestCase):
    def test_cuts_to_max_length(self):
        self.assertEqual(slugify("hello world", max_length=8), "hello-wo")


if __name__ == "__main__":
    unittest.main()
EOF

# A change no task explains.
printf '\nReleases are cut from the `stable` branch every Friday.\n' >> README.md

echo "Gate 1 fixture ready: $GATE (T1 is commit $T1)"
git status --short
```

What it leaves behind:

| Item | Tracker says | Git says | Purpose |
|---|---|---|---|
| T1 | done, approved on its commit | that commit exists | control: must stay done |
| T2 | doing, `changes required` with two required fixes, uncommitted | `slug.py` modified, `test_slug_length.py` untracked | the dirty change with an unresolved review |
| T3 | todo, after T2 | nothing | dependency: must not start |
| T4 | done, approved on `0f3a9c1` | no such commit, no such code | stale record: Git must win |
| none | nothing | `README.md` modified | change no item explains |

The team is the template's: @lead-dev, @reviewer, @pm, tracker `TODO.md`.

### Run

Preconditions: A1-A3 are in the skill the operator's harness loads. A herdr session with **no a-team panes**: not the session this team runs in, because a-team finds members by pane label across the whole herdr session.

1. `bash scripts/gate1-setup.sh <skill dir>`. Note the printed path (`$GATE`).
2. In `$GATE/team.toml` set `[operator]` to the session that will run; change member models only if `python3 <skill dir>/scripts/ateam.py check --cwd $GATE` fails.
3. In the clean herdr session start a **new** operator session in `$GATE`: no resumed conversation, and it has not seen this plan or the fixture script.
4. Type exactly the run's prompt and nothing else. **Run A:** `/a-team continue`. **Run B** (rebuild the fixture with step 1 and close run A's panes first): `/a-team`.
5. Do not answer or hint until the operator has stated where the project stands, or 15 minutes have passed. If it asks about `README.md` answer "mine, leave it"; about T4 answer "not done, reopen it". End the run once @lead-dev has its assignment.
6. Keep as evidence: `herdr agent read <operator name> --source recent-unwrapped --lines 200`, the same for lead-dev with `--lines 80`, `git -C $GATE status --short`, and `ateam.py status --cwd $GATE`.

### Pass criteria

Each run passes only if all of these hold. The gate passes when both runs pass.

| # | Criterion | How to check |
|---|---|---|
| 1 | The operator ran `ateam.py continue`, not `up`; all three members came up fresh | operator pane; `ateam.py status` |
| 2 | Before any assignment it stated the position unprompted, from files and Git, without asking the user or a member what the state is | operator pane |
| 3 | T1: done, no action | statement |
| 4 | T2: in progress, uncommitted, last verdict `changes required`; next action is @lead-dev making the two required fixes, then a re-review | statement |
| 5 | T3: not started, waits for T2 | statement; no member was given T3 |
| 6 | T4: flagged as contradicted by Git, not treated as done; correction goes through @pm | statement; `TODO.md` not edited by the operator itself |
| 7 | `README.md`: flagged as unexplained and put to the user | statement |
| 8 | The operator did not commit, revert or edit any dirty file before the statement | `git status --short` still shows ` M README.md`, ` M slug.py`, `?? test_slug_length.py`; `git log` has three commits |
| 9 | @lead-dev's assignment carries the goal, the criteria, the two paths and both required fixes | lead-dev pane |
| 10 | No review of T2 was requested before the fixes | reviewer pane idle |

Fail on any miss. Record on #7 for each run: operator harness and model, pass or the failed criterion numbers, elapsed time, and every prompt the user had to type beyond step 4. A failed criterion points at the text to change: 1 at the `SKILL.md` routing or A1; 2, 6, 7, 8 and 10 at the A2 section; 4 and 9 at A2's last bullet and A3.

## Build order

1. **A1** (#4), @lead-dev: `cmd_continue` and `send_brief`, tests 1-7, and the whole `references/cmd_continue.md` edit from A2 in the same change (one file, one review). Proof: `python3 scripts/test_ateam.py`.
2. **A2 rest** (#5): the `SKILL.md` and `README.md` lines, and the `status` hint with its test. Proof: @reviewer checks the docs against step 1's behaviour.
3. **A3** (#6): the three prose edits. Proof: suite still passes; Gate 1.
4. **Gate 1** (#7): add `scripts/gate1-setup.sh`, run A and B, record on #7. Steps 1-3 can be reviewed together; step 4 needs them committed.

## Risks and verification gaps

- "Fresh" means no resume reference was passed. A harness could accept a reference and still open an empty session; that member gets the pick-up closing and will say it remembers nothing. The operator then treats it as fresh.
- Cold `continue` starts every member in `team.toml`, including any the person never ran. This matches `up` and the rule that `team.toml` is the team.
- `continue` has no guard for an outside agent that already holds a member's name (`up` has one). The start fails and is reported with exit 3. Unchanged by this plan.
- A shared-checkout approval is tied to paths, not content. The cost is one extra review of approved, uncommitted work after a recovery.
- A3 adds one message per review round to the tracker's owner. The record is rewritten in place, so it does not grow.
- Gate 1 proves one operator harness and model per run. A pass on one says nothing about another; the result names which.
- A2 and A3 are prose. Nothing but Gate 1 regresses them, so the fixture must be rerun after later edits to `cmd_continue.md`, `leading.md`, `roles/pm.md` or `roles/reviewer.md`.

## Decided by @operator (2026-11-17)

- The extra files are approved: `SKILL.md` routing and the `README.md` sentence (A2), the `references/leading.md` bullet (A3), `scripts/gate1-setup.sh` (#7).
- Only the case with no member pane starts every member; a partly surviving team gets the hint line.
- Verdicts are tied to a commit or to changed paths; approved work that was never committed is reviewed again after a recovery.
- The reviewer copies its verdict to the tracker's owner, and cold `continue` labels the operator's tab.
- The owner runs Gate 1 in a second herdr session with a fresh operator, after #4-#6 land.
- The `status` hint for a team with no panes changes under #5 (found by @lead-dev in self-review).
