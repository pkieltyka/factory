---
status: draft
issue: 172
phase: 7
branch: peter/172-skill-text
owners: ["@architect", "@lead-dev"]
---

# Phase 7: Skill text

Base `c416bd4`. Plan reviewer: @architect-2. Writer: @lead-dev. Follower: @lead-dev-3. Runs beside Phase 6 (#171); §6 says what waits for it.

Read first: [cli_skill.md](../components/cli_skill.md) §2 and §4, [functional_spec.md](../functional_spec.md) §1 to §5, §8.5 (outcomes), §8a and §9.5 to §9.6, [keeper.md](../components/keeper.md) §5 and §6 (note texts), [phase_6.md](phase_6.md), and `skill/scripts/factory.py`'s `main`, `cmd_status`, `_up`, `_down` and `hc_check` for the exact words the commands print.

## 1. Decision and scope

The files agents and owners read still describe v1: herdr, `wait`, watchers, acknowledgement, background and foreground operators, `--restart`. This phase makes every one of them describe what `factory.py` on `master` does, deletes the rest, and adds tests that keep text and commands from drifting apart again.

**The rule for every file** (cli_skill.md §4): delete what described herdr 0.9.5, waits, watchers and acknowledgements; add only what an agent must do differently. Every line an agent reads costs context: prefer a shorter file. Write for a reader with no v1 memory: no "no longer", "replaces", "used to" or "v2" in `skill/`; say what is.

| File | Change |
|---|---|
| `skill/SKILL.md` | Rewritten (§3.1) |
| `skill/references/leading.md` | "Receive and inspect" replaced; "Autopilot" becomes "Autonomous operation"; status and note tables (§3.2) |
| `skill/references/cmd_continue.md` | Rewritten around `up`, `down`, `inbox` and conversations (§3.3) |
| `skill/references/cmd_setup.md` | Ready-check question, `draft_wait`, permissions wording (§3.4) |
| `skill/references/pull-requests.md` | Two sentences (§3.5) |
| `skill/templates/member-prompt.md` | The outcome sentence for storage failures (§3.6) |
| `skill/templates/FACTORY.md` | `## Workflow` becomes cli_skill.md §4's default (§3.7) |
| `skill/templates/factory.toml` | Operator comment; three commented keys (§3.8) |
| `skill/recipes/tasks-todos.md`, `tasks-gh.md` | A "Ready check" section each (§3.9) |
| `skill/scripts/factory.py` | Two strings in `render_team_brief` (§3.10). No behaviour change |
| `README.md`, `docs/README.md`, `docs/getting-started.md` | Rewritten for `hc` and rodeo (§4) |
| `tests/test_factory.py` | `SkillTextTests`; the link check covers `skill/` (§5) |
| `skill/roles/*.md`, `references/notes.md`, `recipes/review-codegenie.md`, `recipes/pull-requests-github.md`, `templates/TODO.md`, `harnesses.toml` | No change expected; §5's tests decide |

**Not in this phase:** any command behaviour; qualification results and claims about which harness is qualified as operator (Phases 8 and 10 write those); the release banner's final form and skills-sync (Phase 10, the owner's); this repository's own `FACTORY.md` (the operator's).

**Decided here**
- **The template's workflow is cli_skill.md §4's table with two review rounds, not three.** The owner approved the leaner workflow for this repository after that table was written (two rounds, then the operator decides), and both the plan reviewer and the writer asked for the template to follow it. So "after three rounds" becomes "after two rounds" in cli_skill.md §4, in functional_spec.md §8a.3's review-loop row and in `cmd_setup.md`, in the same change. The rest of the table stays: one item in progress at a time is the safe default for a default formation that shares one checkout, and the table already lets an item's tasks run in parallel when each writer has a worktree. This repository's pairing rule and single-owner merge bar are its own and do not go into the template. The operator confirmed the owner’s two-round default.
- **A storage failure is not a refusal.** `nothing sent` means nothing was stored: fix and send again. `not stored` and `not confirmed stored` lines say themselves whether to resend, and some say not to. The member prompt and `leading.md` stop telling a sender to "send again" after `not stored` (§3.6). This corrects cli_skill.md §4's paragraph, which is updated in the same change, as is that section's note-table row for `ready work` (§2).
- **No qualification claims in the text.** Where v1's text sorted operator harnesses into background and foreground, the new text says nothing about modes: there are none. It does not say "all six are qualified" either.
- **The CI wait in `pull-requests.md` stays a bounded command**, and loses its "native background task" sentence, so the text never suggests a background task where Factory's own rule forbids one for Factory.
- **An owner decision does not stop independent work.** functional_spec.md §8a.3: the operator asks the owner and carries on with other ready work meanwhile. v1's Autopilot stopped everything at the first blocked item; cli_skill.md §4's "keep its stop rules" is read as keeping the limits (what is always the owner's, the sandbox), not that rule, and its sentence is corrected in this change (§3.2). A project or an owner that wants the stricter rule writes it in `FACTORY.md` or the goal, and the operator follows it.
- **A note is information that may be late** (§2). Every "Do" in the note table starts from the current tracker and `status`, and a member brought back is reconciled before it is given work. cli_skill.md §4's note table is updated to the same wording in this change.

**Rejected**
- *Keeping a short "what changed from v1" note in `SKILL.md`.* Agents do not need history, and the owner's direction is v2 only.
- *Describing the keeper's internals to the operator.* The operator needs four facts: messages are typed into panes, what each outcome means, what each note asks of it, and what `status` shows.

## 2. Facts the text must agree with

One list, so every file says the same thing. Each item is checked against the code before it is written.

**Environment.** `hc` 0.1.20 or newer and rodeo. The operator runs in an `hc` terminal, which a rodeo pane is; outside one `up` refuses. `rodeo [--session <name>]` opens rodeo on an `hc` session; its detach key is prefix then `q`; terminals keep running in `hc`. No integrations, hooks or plugins.

**Commands** (the parser): `init`, `harnesses`, `check [names]`, `up [names] [--fresh] [--timeout S]`, `down [names]`, `status`, `brief [NAME]`, `brief --send [all | names]`, `say NAME [NAME…] (--file PATH | TEXT)`, `inbox [N] [--for NAME]`. `keeper` is Factory's own and is never run by an agent. Every command but `say` and `keeper` takes `--cwd`.

**Messages.** `say` stores a message; the keeper types it into the recipient's pane as a line `[from @name]` and the text. It may start a turn or steer the current one. A note from Factory arrives as `[from factory]` and is information, not a teammate's request. Nobody starts a wait, a watcher or a background task for Factory. `inbox [N]` rereads the last messages addressed to the caller, with each one's outcome; it works after compaction and after `down`.

**Outcomes of `say`** (one line per recipient, after `message N`):

| Line | The sender does |
|---|---|
| `delivered` | Nothing: it was typed into their pane. |
| `sending`, `queued (<reason>)` | Nothing, and never resends: Factory finishes it. Reasons: `waiting`, `blocked`, `not ready` (no confirmed brief yet), `gone` (lost; for the operator, not reachable). |
| `unconfirmed (<why>)`, `dropped (<why>)` | It may not have arrived. A member tells the operator which message and recipient and resends only when asked; the operator checks the recipient and decides. |
| `nothing sent: <reason>` | Nothing was stored. Fix what the line says, then send again. |
| `not stored: …`, `not confirmed stored: …` | Do what the line says. When it says not to resend, do not; a member tells the operator. |

**`up`.** Run from the operator's pane. It checks everything it will start first (exit 2, nothing changed); marks this pane as the operator; starts the keeper; starts each member that is not running, in the `team` tab, one pane per member titled `@name`, with a narrow `keeper` pane; resumes each member's conversation unless `--fresh`; sends each its brief once it is ready. Running members are left alone. Exit 3 names each member that did not start, is not ready or has no delivered brief, with the command that fixes it (`factory.py up NAME`, `factory.py brief --send NAME`, or `factory.py status` while a brief is still on its way). Known startup dialogs are answered, except agy's trust dialog, which the owner accepts in the member's pane.

**`down`.** Run from the operator's pane. Stops the named members, or all configured ones; `up` resumes their conversations. It says how many queued messages it dropped. A member removed from `factory.toml` that still runs is named with the `hc kill` command for its terminal, exit 1.

**Changing a running member.** `down NAME`, then `up NAME`; add `--fresh` for a new conversation.

**`status`.** The loaded Factory version and script path, the `hc` version, the team; one row per member with `State` (`not started`, `starting`, `idle`, `working`, `done`, `blocked`, `error`, `gone`), `Turn` and `Terminal`; an `interrupted` line for each `gone` member; `pending:` by recipient with the oldest age and reason, and how to read the operator's; the `keeper:` line; `no pane:` for members without one; a line for a member with no confirmed brief; `operator: reachable` or not.

**Conversations.** `up` tries the member's saved compatible conversation and says so when it starts a new one instead. Which conversation is saved differs by harness, so the text promises no more than that. A member in a new conversation knows nothing of the work so far; whether it needs its task again is decided by reconciling (below), not assumed. After `hc` restarts, every member that was running is `gone` and reported interrupted: run `up`. A member stopped with `down` stays stopped across a restart and is not reported.

**`draft_wait`.** `hc` holds a message while the owner has unsent text in the recipient's pane, for `draft_wait` seconds (default 60), then types it after that text and submits both. `check` warns below 600 and names the config file. The operator tells the owner the one line to add (`draft_wait = 600`) and never edits `hc`'s config.

**Autonomous operation** (the default; phase_6.md): the team works through ready items on its own. Notes from Factory, and what the operator does:

| Note | Do |
|---|---|
| `@x is blocked (…)` | Resolve it if you can; ask the owner when only they can. |
| `@x hit an error` | Reassign, or retry after the provider's delay. |
| `@x exited` / `was lost` | `factory.py up x`. When it reports back, reconcile its pickup report with the tracker and your inbox before you give it its task again or reassign: a resumed member may already be on it. |
| `team quiet` | The note may be late: check `status` and the tracker first. Then ask whoever still owes you a report. Otherwise take the next ready item. Otherwise end your turn; do not answer the note. |
| `ready work` | The note may be late and its list is in no order: check the tracker, ownership and work in progress, then take the next ready item, as the workflow says. |

A note can be late: `hc` may hold it behind text the owner is typing, so it describes the team as it was. It is a reason to look, never an instruction.

The ready check is optional and the owner's: three top-level keys in the owner's own `factory.toml` (`ready_check`, `ready_check_every`, `quiet_grace`), never in `FACTORY.md` or the sample. Its contract: exit 0 with output means ready work; exit 1, or exit 0 with empty output, means none; anything else is unknown, including exit 2 with nothing printed. The output only has to be stable: sorted IDs are enough, and it carries no priority.

**Not handled, and said so** (functional_spec.md §8a.3): the operator gone (messages queue until a new `up`); the keeper or `hc` dead while every agent is idle (until the next command); members exchanging messages without progress; an operator that works alone after a quiet note and stops without messaging anyone. And the known limit of §8.2: a harness that quits while `hc` holds a message can have it typed into the shell left behind.

## 3. The skill

### 3.1 `SKILL.md`

- Front matter `description`: "Form, launch and lead a software team in herdr-core (hc) across Claude Code, Codex, pi, omp, OpenCode and Antigravity CLI (agy). Commands: setup, up, down, status, brief, say, inbox, or bare /factory. Use for team formation, delegation, peer review and session recovery."
- Intro paragraphs: keep the two project files; `[operator]`'s `name` is "your @name and pane title"; a team is one checkout in one `hc` session. Delete "One team runs per herdr session" and the whole paragraph on background tasks and foreground waits.
- Delete the two paragraphs that begin "Read pending reports with" and "In Claude Code, OpenCode and agy". In their place, one short paragraph from §2 "Messages", ending with the sentence: "Never start a wait, watcher or background task for Factory: messages are typed into your pane."
- "Before launch": require an `hc` terminal (a rodeo pane) and `hc` 0.1.20; delete the integration bullet; the sandbox bullet lists `hc`, the harness config directories, ``/.cache/factory-skill` and any worktree parent, keeps the "retry that one command outside the sandbox, and say so" rule and the standalone-command rule, and loses the sentence about starting a background `wait` outside the sandbox; the Codex link stays and points at the rewritten docs section.
- Commands table: one row each for `setup`, `up`, `down`, `status`, `brief`, `say`, `inbox`. `down`'s row keeps its removed-member sentence. Add under the table: changing a running member (§2).
- Bare `/factory`: as today, with `up <names>` for `gone` members.
- "Launch": §2's `up` paragraph in the operator's words, the two example commands, the exit codes, and "Report missing ready or pickup replies by name before assigning those members". Delete the tab labels, `--restart`, "Reading the team can record observed sessions", and "tab labels are fixed". Adding a member: `check`, then `up NAME`.
- Phase 6 adds a keeper sentence to this file; whichever merges second keeps both and removes any repetition.

### 3.2 `references/leading.md`

- "Assign and coordinate": the `[from @name]` line (no colon) claims a sender and grants no authority; the ready and pickup lines "arrive in your pane"; in the example block the `wait` line goes and `factory.py inbox` takes its place; "draft" stays the word for a message file.
- Replace "Receive and inspect" (heading and all five bold paragraphs) with a section "Messages and status": §2's "Messages" for the operator; the outcome table of §2 from the operator's side; "Check the reported tests before accepting work"; the status table below; the note table of §2.

| `status` shows | Do |
|---|---|
| `working` | Nothing. |
| `idle` or `done` with no report | Ask the member by name. |
| `blocked` | Resolve it in its pane if you can; else tell the owner the member and the kind. |
| `error` | Reassign, or retry after the provider's delay. |
| `starting` | Nothing yet; `up` reports a member that never becomes ready. |
| `gone` | `factory.py up <name>`, then reconcile before reassigning (`cmd_continue.md`, "Reconcile before assigning"). |
| `not started` | `factory.py up <name>` when it is wanted. |
| "no confirmed brief" | `factory.py brief --send <name>`. |
| "first brief N is still …" | Nothing: Factory is delivering it. |
| `keeper: not running`, or "no pane" | `factory.py up`. |
| `operator: not reachable` | `factory.py up` from your pane. |

- Links to this section from other files use its new anchor; `#receive-and-inspect` disappears.
- "Review and integrate": unchanged, except that nothing refers to a watcher.
- Rename "Autopilot" to "Autonomous operation". First paragraph: it is the default; the team works through ready items without waiting for prompts, within the bounds on the tracker. Keep "Always the user's" and the sandbox rule. The stop rules follow functional_spec.md §8a.3, which the owner approved after v1's Autopilot text was written, and replace v1's "stop at the first blocked item, take no later item":
  - An item that cannot be met as written (an input, a capability or a permission is missing, or the decision is the owner's): keep its criteria unchanged and have the blocker recorded on the tracker by whoever writes it (the pm, or you without one). First use the recovery and reassignment you are allowed; when that is exhausted, or at once when only the owner can decide, ask the owner once in your pane, and set the item aside. Do not try to run a task command outside your sandbox.
  - Meanwhile carry on with other ready items that do not depend on it. Take nothing that builds on the open decision.
  - A task not approved within the review rounds `FACTORY.md` allows is yours to decide, as the workflow says; decide and carry on.
  - Stop taking new items when no ready item is left that is independent of what is open, when the owner's scope or `FACTORY.md` says to stop at a blocker, or when a member the remaining work needs cannot be recovered or replaced.
  Delete the `herdr notification` sentence; on stopping, say why and what comes next in your pane. Add two bullets: a `team quiet` note that finds nothing to do ends the turn without a reply; the ready check is the owner's to turn on (§2).
- "Keep state current": restart for changed settings is `down <name>` then `up <name>`; a fresh conversation is `up <name> --fresh` after `down <name>`; delete "A watcher may report those closures"; "Do not close or retitle panes or tabs unless authorized. Factory finds members by labels on their terminals, not by titles."

### 3.3 `references/cmd_continue.md`

- Opening: use after agents exit, `hc` restarts or you lose context. The example block: `status`; `up`; `up lead-dev`; `down lead-dev` then `up lead-dev` (apply changed settings); `up lead-dev --fresh` (a new conversation, for a member that is not running).
- One paragraph from §2 "Conversations", plus: `up` names each member it starts or resumes; a failed resume is retried once in a new conversation and says so; `--fresh` skips the saved conversation; to keep a stopped member out, name the others or remove it from `factory.toml`.
- Bullets: after an `hc` restart every member is `gone` and reported interrupted, run `up`; exit 3 names the members and the fixing command; a member with no confirmed brief gets `brief --send <name>`; messages survive `down` and a restart, `factory.py inbox [N]` rereads yours and `status` shows what is pending; a cold `up` lists message files that were never delivered to everyone, to check before resending or discarding.
- Delete: the pane-reuse bullet, the Codex working-directory bullet (still true, not the operator's concern), the herdr-restart bullet, integration warnings, the `sessions.jsonl` path, the acknowledged-inbox bullet.
- "Reconcile before assigning": kept, with its reading order (members' replies last) and its summary to the user before any member's reply is read (functional_spec.md §9.5); the messages come from `factory.py inbox`. Add: after `up NAME` for a member that was lost, reconcile its pickup report, the tracker and your inbox before reassigning, because a resumed member may already be doing that task.
- "Change a running member's settings": §2's two commands. Coordinate active work first.

### 3.4 `references/cmd_setup.md`

- Step 2's one round of questions gains: whether Factory may run a read-only ready check for this person, and with which command. If yes it goes in that person's `factory.toml` as §3.8's keys, never in `FACTORY.md` or `factory.toml.sample`. Offer the recipe's command (§3.9) when the tracker is `todos` or `gh`, and for `TODO.md` the command of §3.8's comment.
- "Write and validate": `[operator]` records "this pane's harness". After `factory.py check`: when it warns about `draft_wait`, tell the owner the line and the file it names; never edit `hc`'s config. The Workflow bullet names the defaults as they now are: one item at a time, one writing task per shared checkout, two review rounds.
- "Permissions": "Full Claude members can reach `hc`'s socket inside an inherited sandbox. Guarded exempts the standalone `factory.py say` and `factory.py inbox`." agy: its trust dialog is the owner's to accept in the member's pane; `up` reports the member as waiting on it and names `brief --send` for afterwards. The existing agy sentences about its permission preset stay.

### 3.5 `references/pull-requests.md`

- "Worktrees": `factory.py down <name>` then `factory.py up <name>` in place of `up <name> --restart`; "if the conversation cannot resume there, `up` starts a new one and says so".
- "Checks": "keep one wait per pushed head: a command with a time limit that fits your harness". Delete "Use a native background task where available" and the word "watcher" throughout the bullet.

### 3.6 `templates/member-prompt.md`

Replace the sentence "`nothing sent` or `not stored`: fix what the line says, then send again." with: "`nothing sent`: nothing was stored; fix what the line says, then send again. `not stored` or `not confirmed stored`: do what the line says, and when it says not to resend, tell {operator}." Nothing else in the file changes. cli_skill.md §4's quoted paragraph gets the same sentence.

### 3.7 `templates/FACTORY.md`

`## Workflow` becomes cli_skill.md §4's default, word for word after that section's round count is changed to two (§1): the opening sentence, the seven-row table and the three bullets. `## Tasks` and `## Notes` stay.

### 3.8 `templates/factory.toml`

- The `[operator]` `name` comment: "the operator agent's @name and pane title; fixed while the team runs".
- With the other commented top-level keys, above `[operator]` (TOML needs top-level keys before the first table):

```toml
# quiet_grace = 120       # seconds the team may be quiet before Factory tells the operator (30 or more)
# ready_check = '''out=$(awk '/^## /{r=($0=="## Ready")} r&&/^- \[ \]/' TODO.md) || exit 2; [ -n "$out" ] && printf '%s\n' "$out" || exit 1'''
#                         # optional and yours alone: a read-only command, run while the team is quiet. Exit 0 with
#                         # output: ready work; exit 1, or exit 0 with no output: none; any other exit: unknown.
#                         # Leave it out of factory.toml.sample.
# ready_check_every = 300 # seconds between ready checks (60 or more)
```

  They stay comments, so `init` never enables a ready check. The example is a TOML literal string (three single quotes), so the shell line needs no escaping. It keeps `awk`'s own failure apart from "none": a missing or unreadable `TODO.md` exits 2 (unknown), not 1. A pipe into `grep .` would hide that. Both this line and @lead-dev's stricter variant (items matched as `- [ ] (N)`, a TOML basic string) were run through `tomllib` and `sh`: missing file 2, empty Ready 1, an item in another section 1, a Ready item 0 with its line. The writer commits whichever it tested last; the test in §5 holds either to those four results.

### 3.9 Recipes

Each of `tasks-todos.md` and `tasks-gh.md` gains a section `## Ready check` between `## FACTORY.md section` and `## Tested with`: one sentence saying it goes in the person's own `factory.toml` as `ready_check`, and one command in inline code (no fence: a recipe keeps exactly one fenced block). The command prints the ready items' IDs, sorted, one per line (a title may follow; nothing that changes between runs, and no order of priority), exits 1 when there are none, and exits with another code when the tool itself failed, so a network error is not read as "no work". Shapes the writer has already verified read-only against this repository: for `gh`, `gh issue list --state open --label 'READY_LABEL' --json number --jq …` gives a stable list of issue numbers, mapped by the shell to exit 1 when it is empty and exit 2 when `gh` fails; for `todos`, `todos board --json --fresh` with the repository and project given explicitly, selecting the named column's open cards by number from `.columns[].status` and `.cards[].state` and `.number`, with a missing column, a truncated board or a tool error mapped to exit 2. Each recipe gives the full one-line command with its names in capitals, and "Tested with" records what was run and what it returned for "some", "none" and "failed".

### 3.10 `factory.py`

In `render_team_brief` (lines 3321 and 3344): "each in its own pane" and "which is also their pane's title". These are the only product strings in the script that still say herdr. The tests that assert the brief's text follow.

## 4. README and docs

- **`README.md`.** The banner stays until the owner tags v2 (Phase 10), reworded: `master` is Factory for herdr-core; v1 for herdr 0.9.5 lives on the `v1` branch and is what to install until v2 is tagged. "What's new" in three bullets that are true of the built system: agents report their own state through OSC 7501; one delivery path, with Factory's keeper typing each message into the recipient's pane and nobody running a wait; the team keeps going on its own and tells the operator when it goes quiet. Delete "Everything below describes v1 until v2 replaces it" and make everything below describe `master`: panes are `hc` terminals shown in rodeo; "herdr for panes and messaging" becomes "`hc` for terminals, rodeo to see them"; Quickstart starts "From your project, in a rodeo pane"; tests are "fake harness and fake `hc` tests, a private real `hc` core, real Git checks and the Gate 1 fixture"; delete the herdr-select tip.
- **`docs/README.md`.** "a team of agents in `hc`, shown in rodeo". Commands table: remove `/factory wait` and `--restart`; `/factory status` "shows each member's state, pending messages, the keeper and the operator"; add `/factory inbox` "rereads recent messages addressed to you".
- **`docs/getting-started.md`.**
  - "Before you start": `hc` 0.1.20 or newer, rodeo, Python 3.11+, Git, one harness. No integration paragraph. One short paragraph on `draft_wait` (§2) with the line to add.
  - "Operator harnesses": delete the background and foreground paragraph; keep the install-path table; "The session where you run setup stays the operator when you run `up`". The agy paragraph becomes: Factory does not accept agy's trust dialog; accept it in the member's pane, then `/factory brief --send <name>`.
  - "Codex operator access": the same advice with the prefixes that exist (`status`, `inbox`, `say`; `up` and `down` kept separate); the example pair uses `inbox`; "denied access to `hc`"; delete `herdr integration status`.
  - "Form the team": `rodeo --session first-factory` from the project directory, start the harness in its pane, `/factory setup`.
  - "Launch": `up` adds a `team` tab with one pane per member titled with its @name and a narrow `keeper` pane; your own pane and tab are left as they are.
  - "Ready items and autonomous work": the team works through ready items on its own; what stops it; the quiet note; the optional ready check, yours to set in `factory.toml`. The paragraph on what always needs the owner stays.
  - "Stop and come back": `/factory down`; detach with prefix then `q`, the team keeps running in `hc`; `/factory up` to bring members back, `--fresh` for new conversations; after `hc` stops or restarts every member is reported interrupted and `/factory up` restarts them. Delete the `herdr session stop` and `--restart` sentences. The writer checks each `hc` or rodeo command it names against that tool's `--help` on this machine and names none it has not run.
  - New short section "What Factory does not handle": §2's last paragraph.

## 5. Tests

In `tests/test_factory.py`. The text files are the unit under test; no `hc` is needed.

**`SkillTextTests`**
- `test_no_removed_commands_or_stale_instructions`: in every `.md` and `.toml` under `skill/`, and in `README.md` and `docs/*.md` (not `docs/qualification/`), none of the things that no longer exist: the commands `factory.py wait`, `/factory wait`, `factory.py prompt`; the flags `--seen`, `--restart` and `--no-brief` anywhere, and `--interval` on a `factory.py` or `/factory` command (the pull-request recipe's `gh pr checks --interval 60` is legitimate); `HERDR_` variables; `herdr integration`, `herdr session`, `herdr --session`, `herdr notification`; and the word `herdr` anywhere it is not `herdr-core`, `herdr-app` or part of a URL, except README's banner line about the `v1` branch, matched exactly. It bans removed commands and names, not ordinary words: "wait", "watcher", "background task" and "acknowledgement" stay legal, because the text needs them (the rule in `SKILL.md`, the CI wait in `pull-requests.md`, "send content, not acknowledgements"). Whether such a word is used rightly is the follower's reading in step 6, not a test.
- `test_commands_named_in_the_text_exist`: every `factory.py <word>` in those files is a parser command, and every `/factory <word>` is one of `setup` or a parser command other than `keeper`, `init`, `harnesses` and `check`.
- `test_flags_named_in_the_text_exist`: within each inline-code span or fenced command line that starts with `factory.py <command>` or `/factory <command>`, every `--flag` appears in that command's `--help`. Only that span or line is inspected, so another tool's flags elsewhere on the same line (`gh`, `git`) are not mistaken for Factory's.
- `test_status_words_in_leading_are_the_real_ones`: the state words in `leading.md`'s status table are a subset of the eight states, and each phrase quoted from `status` ("no confirmed brief", "no pane", "operator: not reachable", "keeper: not running") is a string `cmd_status` can print (found in `factory.py`).
- `test_outcome_words_are_the_real_ones`: `leading.md` and the member prompt name every outcome in `OUTCOMES`, and `nothing sent`, `not stored` and `not confirmed stored`; each of those three is a string `cmd_say` prints.
- `test_template_formation_has_no_active_autonomy_key`: `templates/factory.toml` loads with `ready_check` None and the default grace and period, and its three lines are comments placed before the first table. (This is cli_skill.md's `test_init_sample_has_no_active_ready_check`.)
- `test_template_ready_check_command`: uncommented, loaded as TOML and run by `sh -c`: in a directory with the template `TODO.md`, exit 1 and no output; with an item only in another section, exit 1; with one item under `## Ready`, exit 0 and that line; with no `TODO.md`, exit 2.
- `test_template_workflow_is_the_spec_default`: the `## Workflow` section of `templates/FACTORY.md` equals the block in cli_skill.md §4.
- `test_team_brief_says_pane_not_herdr`.
- After #171 merges (§6): `test_note_table_matches_the_keeper`: each note name in `leading.md`'s note table (`is blocked`, `hit an error`, `exited`, `was lost`, `team quiet`, `ready work`) is a string the keeper writes.

**Existing classes**
- `DocumentationTests.test_relative_links_and_headings` also walks `skill/SKILL.md` and `skill/references/*.md`, `skill/recipes/*.md`, so a link to a removed heading fails.
- `RecipeTests`: the heading list allows an optional `## Ready check` before `## Tested with`, still requires exactly one fenced block (the pasteable `FACTORY.md` section), and for the two task recipes requires the `## Ready check` section with exactly one inline command.
- `MemberPromptTests`: also asserts `not confirmed stored`.
- `InstalledCopyTests`: unchanged; it already runs `harnesses`, `init`, `check` and `brief NAME` from a copy of `skill/` alone.
- Any test that asserts text this phase changes (the brief's sentences, the sample formation) is updated in the same commit as the text.

## 6. What waits for Phase 6

Written now against keeper.md §5 and §6 and phase_6.md, and checked again after #171 merges and this branch is rebased: the note table and its test; the words "team quiet" and "ready work"; the three `factory.toml` keys and their minimums; `SKILL.md`'s keeper sentence; the "Not handled" list. If #171 changes a note's wording, the text follows the code.

## 7. Build order, risks and questions

One commit per step; the suite green at each.

1. `SkillTextTests` with the checks that can already pass on the files this plan leaves alone, the link check over `skill/`, and `RecipeTests`' heading rule. The removed-commands test is committed last (step 7), when it can pass.
2. `SKILL.md`.
3. `leading.md`, `cmd_continue.md`, `pull-requests.md`.
4. `cmd_setup.md`, the templates, the recipes, `render_team_brief`; cli_skill.md §4's outcome paragraph.
5. `README.md` and `docs/`.
6. Read every changed file once from top to bottom as the agent or owner who will read it, and cut what repeats.
7. `test_no_removed_commands_or_stale_instructions`; run both suites.
8. After #171 merges: rebase, §6's check, the note-table test; then both suites and the private `hc` and rodeo smoke (`python3 tests/live.py smoke`), which every phase since 5b ends with, before asking for review.

Risks and gaps:
- **The text is checked by tests for words, not for sense.** A sentence can pass every test and still mislead an operator. The follower reads each file as its reader; the Phase 8 qualification runs with a real operator are the real test.
- **Phase 6 is not merged.** Note names and the autonomy keys may still move (§6).
- **Recipes' ready-check commands** are only as good as the run the writer records; the `todos` one depends on that tool's current flags.
- **No claim about operator harnesses.** Until Phases 8 and 10, the docs do not say which harnesses are qualified as operator.

The operator confirmed the owner’s two-review-round default for the template and spec.
