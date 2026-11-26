---
status: complete
---

# Functional Spec: Factory v2

Factory v2 is the `/factory` skill rebuilt on herdr-core (`hc`). It keeps v1's product (roles, formation, brief, worktrees, the operator-led workflow) and replaces v1's herdr 0.9.5 plumbing with what `hc` provides: persistent terminals, agent state through OSC 7501, and `hc send` for typing into agents. Research: [v1 inventory](research/v1-inventory.md) · [hc capabilities](research/hc-capabilities.md) (live checks in its §10).

## 1. Goals and non-goals

Goals:
- One delivery path for every message and every harness: type it into the recipient's terminal. Arriving text is what wakes an agent, operator included. No watchers in the operator, no foreground waits, no per-harness wake rules.
- No agent, operator or member, ever starts, arms, re-arms or keeps a wait, watcher or background task for Factory, and nothing depends on a harness's background or foreground tool modes. Agents only run Factory commands that return within seconds; the keeper is Factory's own process in `hc` (§8.5).
- Agent state comes only from OSC 7501 as `hc` reports it. No screen reading except the startup-dialog check (§6.3).
- The team runs autonomously and continuously through ready work, and is never silent for long without the operator being told (§8a).
- The team shows up in any `hc` client (rodeo, the Herdr desktop app) because Factory writes its panes into the `herdr-app` layout doc.
- Fewer commands, fewer files and less script than v1, keeping v1's tested guarantees where they still apply.
- v1's `FACTORY.md` and `factory.toml` work unchanged.

Non-goals for v2.0: remote machines (`hc` machine links); the team dashboard (#2); project knowledge (#62); automatic recovery of interrupted work (§9.3 detects and reports only); herdr 0.9.5 (that is v1, on the `v1` branch).

## 1a. Built on OSC 7501

Every harness Factory supports reports its own state to `hc` through OSC 7501, and Factory relies on that alone for every decision about an agent's current runtime state. It never infers that state from the screen (except the startup-dialog check in §6.3), from process lists, or from harness-specific hooks. Other facts come from their own sources: which terminal is a member, from Factory's labels and `hc`'s terminal records; the owner's unsent text, from `hc`'s input tracking; replies, from messages; resume, from saved session IDs.

| Factory decision | OSC 7501 fact, as `hc` reports it |
|---|---|
| A terminal is an agent and may be typed into (§8.2) | It has a status (`status` is not null). |
| A member is ready for its brief (§6.3) | `idle`, and no startup dialog on screen. |
| Never type; queue instead (§8.2) | `blocked` (permission, question or login); `hc send` refuses it too. |
| A recipient can take a message (§8.5) | `idle`, `working`, `done` or `error`; not `blocked`. For a harness that takes messages only between turns (§8.3): not `working`. |
| A message was submitted (§8.5) | `hc send` typed and submitted it; whether the recipient then started working is recorded with it. This is a transport receipt: only the recipient acting on that message proves it was handled (§10). |
| A member hit trouble (§8.4) | `blocked` or `error`, or its terminal ended; the keeper sends the operator a note. |
| The team is quiet (§8a) | No member and not the operator `working`, nothing deliverable pending. |
| Shown in `status` (§5.2) | `state` and `turn`. |
| The operator is reachable (§7.4) | Its terminal is live, has a status, and `up` marked it, with no loss Factory has observed since. |

A harness counts as supported only when its OSC 7501 reports pass §10.1. One known gap: Codex reports no status while its trust dialog is open, so the startup-dialog check covers a terminal with no status as well as one that looks idle.

## 2. Concepts

- **Owner:** the person. Talks to the operator and may type into any pane.
- **Operator:** the agent the owner talks to, `@operator` by default, running in an `hc` terminal the owner opened in a client (a rodeo or Herdr app pane). It assigns work, reviews and integrates.
- **Member:** an agent Factory launches, with a stable name (`@lead-dev`, `@reviewer-2`), a role, a harness, a model and a checkout.
- **Team:** one operator and its members for one repository checkout. Several teams may share one `hc` session (§7.3).
- **Formation** (`factory.toml`, local) and **brief** (`FACTORY.md`, committed): v1's formats.
- **Message:** anything sent with `say`: reports to the operator, assignments to members, messages between members.
- **Keeper:** Factory's own process for a team (§8.5). It delivers every message, sends the operator notes about members, and notices when the team goes quiet. It is not an agent and runs no model.
- **`draft_wait`:** `hc`'s setting for how long it holds a message back while the owner has unsent text in the recipient's pane (default 60 seconds).

## 3. Requirements on the environment

- `hc` 0.1.20 or newer. Factory checks the version and fails with a clear message when `hc` is older or missing.
- The operator runs inside an `hc` terminal: any rodeo or Herdr app pane is one. Outside one, `up` refuses with "run the operator inside an hc terminal (rodeo or the Herdr app)". Offline commands (§5.1) work anywhere.
- Python 3.11+ standard library only; Git for worktrees; the harnesses the formation names.
- No herdr integrations, hooks or plugins. Factory never installs or rewrites any harness's global settings.

## 4. Configuration

- `FACTORY.md` and `factory.toml` keep v1's schema and meaning; a v1 file is a valid v2 file. If v2 needs a change, it is back-ported to v1.
- `factory.toml.sample` stays the project's portable suggestion; `init` copies it.
- Harness facts (model discovery, launch, resume and permission arguments) stay declarative, carried over from v1 and rechecked against installed versions.
- Runtime state (message log and queue, saved native session IDs) lives outside the repository, per team, readable only by the owner.

## 5. Commands

One script, mapped from `/factory <command>` by `SKILL.md` as in v1. A bare `/factory` routes as v1 does: to setup when `FACTORY.md` or `factory.toml` is missing, otherwise to `status`, then `up` for members that are not running, and recovery (§9.5) when unfinished work shows. Setup is a conversation with the operator, as in v1. `status` prints the Factory version actually loaded, since a user-level skill can shadow a project copy.

### 5.1 Offline (no `hc` needed)

| Command | Behavior |
|---|---|
| `init` | Writes missing `FACTORY.md`, `factory.toml.sample`, `factory.toml`; never replaces a file; adds `factory.toml` to `.gitignore`. As v1. |
| `harnesses` | Lists installed harnesses and their models. As v1. |
| `check [names]` | Validates the formation, harnesses, models and worktree preconditions without launching. When `hc` is present, also checks its version, prints `draft_wait` and warns when it is under 10 minutes. Exit 2 on any failure, naming member, problem and fix. |
| `inbox [N] [--for NAME]` | Prints the last N (default 10) messages addressed to the caller, or to NAME (default `operator` when the caller is not on the team), from the message log, with each one's current outcome. Read-only; works without `hc` running and after `down`. `status` and `inbox` find the team from the checkout when the caller's terminal is not on one. |
| `brief [NAME] [--send [all\|names]]` | Prints the team brief, or one member's standing instructions (v1's `prompt NAME`). `--send` resends the brief to live members and needs `hc`. |

### 5.2 Team commands (need `hc`)

| Command | Behavior |
|---|---|
| `up [names] [--fresh]` | Run by the operator. Labels the operator's terminal as this team's operator (§7.4). Validates every affected member first (exit 2, nothing changed). Then finds or creates the team tab (§7.1), starts the keeper (§8.5) if it is not running or runs another Factory version, creates worktrees, starts each member that is not running, resumes its saved native conversation unless `--fresh`, and sends its brief once it is ready (§6). Running members are left alone. Reports members flagged by §9.3. Exit 3 names any member that failed to start or to get its brief. |
| `down [names]` | Run by the operator. Saves each member's native session ID, ends its terminal and removes its pane and its finished `hc` record. Leaves the operator, worktrees, the message log and any pane the owner added to the team tab. Stopping the last member marks the team down; the keeper exits once nothing is pending for the operator (§8.5). |
| `status` | One row per configured member: role, harness, model, thinking, state (`starting`, `idle`, `working`, `done`, `blocked`, `error`, `gone`, `not started`; `starting` is a live member that reports no status yet), turn, and any §9.3 flag with Factory's last message to that member. Then pending messages by recipient with the age of the oldest, the keeper (running or not, its version, and how long since its last loop), and whether the operator is reachable. |
| `keeper` | Runs the team's keeper (§8.5). Internal: `up` starts it; `say`, `status` and `up` restart it as §8.5's "When it runs" says. A second copy exits at once. |
| `say NAME [NAME ...] --file PATH` | Sends a message from the caller (§8). Members always use `--file`; the operator may also pass quoted text. Names are separated by spaces or commas; `all` means every other live member. Run from a terminal that is not on a team (the owner's shell, or the operator before `up`), it refuses with "run up first". Returns within about ten seconds with a message ID and one outcome per recipient (§8.5). |

Removed from v1: `wait`, `--seen` acknowledgements, `--no-brief`, `--restart` (use `down NAME` then `up NAME`), the `prompt` command, and all herdr integration checks.

## 6. Launching members

### 6.1 Identity

Factory identifies every terminal it owns by team and member, recorded on the terminal itself, and addresses it by terminal ID after checking those. It never relies on an `hc` terminal name, so two teams in one session can both have a `@lead-dev`. Each member shows as `@<member>` in clients (its pane title and the name label rodeo displays). Factory never changes the `herdr.seen` marker clients keep.

### 6.2 Lifetime

Each member keeps running when clients close, when the owner detaches, and when the operator exits. Members have no `hc` parent: the keeper, not `hc`, tells the operator when a member becomes blocked, hits an error or exits (§8.4).

### 6.3 Readiness and startup dialogs

Factory sends a member its first message only when it is ready: it reports `idle`, and its screen shows none of that harness's known startup dialogs. The dialog check runs even when the member reports `idle` (v1 #125). Tested on `hc` 0.1.20: Claude Code and agy report `blocked` on their trust dialog; Codex reports no state. Where a harness has launch arguments that avoid the dialog, Factory uses them. Otherwise Factory answers a known dialog with its configured keys once, except agy's: accepting it can rewrite agy's global trusted-workspace list, so unless agy can be started without the dialog or with isolated settings (to check), Factory leaves it for the owner and reports the member as waiting on its trust dialog. A member still not ready by the launch timeout is reported by name and gets no brief.

### 6.4 Brief and ready line

A member gets its standing instructions through its harness's instruction channel (as v1), then the team brief as its first message. A new member answers the operator with a `ready` line; a resumed member answers with what it was doing. The operator assigns no work before that reply. Each reply is delivered on its own; it may start its own operator turn or, if the operator is mid-turn, steer that one. Batching them is not required.

## 7. Layout and teams

### 7.1 The team tab

`up` adds one tab, `team`, to the operator's own space in the `herdr-app` layout doc and tiles the members in it as panes, plus one narrow pane for the keeper. Later runs find that tab by the team it belongs to, not by its title, and add or remove only member panes: panes the owner moved or added are left where they are. When the last member pane goes, the tab is removed unless the owner added panes to it. The operator's pane, tab and focus are never moved. Layout writes never overwrite a client's concurrent edit. If the operator's terminal is not in any space, `up` creates a space for the team instead.

### 7.2 Clients

The team appears in every client of the session at once. Factory needs no client open, and closing or restarting a client does not affect the team.

### 7.3 Teams

A team is one repository checkout (its main worktree) in one `hc` session. Every lookup, message and stop is limited to the caller's team, so one team never sees, messages or ends another's terminals.

### 7.4 The operator

`up` marks the caller's terminal as the team's operator. `say` takes the sender and team from the calling terminal. The operator is **reachable** when `up` marked that terminal and, since then, it has stayed live and kept reporting an agent status. While it has no agent status, messages to it wait. It becomes unreachable until `up` marks it again only when its terminal ends, or when a message had to be queued for it as `gone` while it had no status; this keeps those messages in order. If the owner quits and restarts the harness in the same pane before any message was queued for it, the restarted harness is the same operator.

- `up` from a new terminal while the recorded operator is still reachable refuses: "@operator is running in <terminal>; use that pane".
- When the recorded operator is not reachable, `up` adopts the running members and the keeper's messages and notes go to the new operator.

## 8. Messaging

### 8.1 One path

`say` delivers every message by typing it into the recipient's terminal. The text arrives as a line `[from @<sender>]` followed by the message as written; Factory adds a final newline when the message lacks one, so a trailing `@name`, `$skill` or `/command` cannot swallow the submit (v1 #18, proven by §10.7). The log keeps the message exactly as written. Factory's `[from …]` line is the only sender tag; `hc` adds none to these messages. Notes from the keeper arrive the same way, as `[from factory]`.

### 8.2 Protection

- **The owner's unsent text:** `hc` holds a message while the owner has unsent text in the recipient's pane, for up to `draft_wait` (§2), then types it in after that text and submits both. Factory cannot see the draft, so its guarantee is `draft_wait`: `check` warns when it is under 10 minutes, and the docs tell owners to raise it. Holding a message without that limit needs an `hc` option to fail instead of typing; Factory asks herdr-core for it, and uses it to queue the message once it exists.
- **Not an agent:** Factory types only into a terminal that is on the team, reports an agent status, and is ready (a member that `up` has briefed, §6.4, or the reachable operator), checked right before each send. The brief itself is the exception: it is what makes a member ready. A terminal that is gone, finished, or back at a plain shell counts as `gone`; nothing is typed into it.
- **Known limit:** `hc` re-checks only `blocked` at the moment it types. If a harness quits while `hc` holds a message for `draft_wait`, `hc` can type that message into the shell left behind. Factory cannot prevent this on `hc` 0.1.20; it is an accepted risk, measured in §10.8, and part of the upstream request (§14).
- **A long `draft_wait`:** one forgotten word in the operator's pane delays every message and note by the full `draft_wait`, then is submitted with them. `status` shows the age of the oldest pending message so this is visible.
- **Blocked:** an agent waiting on a permission prompt or question is never typed into. The message is queued.

### 8.3 A recipient mid-turn

Messages are delivered while the recipient is `working`. Each harness handles text arriving mid-turn its own way, queuing it for the end of the turn or steering the current turn with it. That is accepted as long as nothing is lost and the owner's text is untouched; §10 records each harness's behavior. If a harness loses or garbles mid-turn text, that harness alone switches to delivery once it stops `working` (`idle`, `done` or `error`), built only if a harness fails §10.4.

### 8.4 Notes about members

The keeper sends the operator a short note, as an ordinary message, when a ready member (briefed, §6.4; `up` reports startup problems itself) becomes `blocked` (with the kind: permission, question or login), hits `error`, or its terminal ends without `down` (with the exit code when there is one). One note per change the keeper observes (it looks every second; a state that begins and clears between two looks gets no note); a member that stays blocked is not noted again. `down` sends none for the members it stops. No member note is sent to an operator that is not reachable; `up` and `status` report current states instead. Notes already sent are remembered in the team's state, so a restarted keeper does not repeat them, and notes a change it can still observe that happened while it was down (a state that began and cleared while it was down cannot be seen). `hc`'s own parent notes are not used.

### 8.5 The keeper, delivery and outcomes

- `say` never types. It writes the message to the team's message log (sender, recipients, text, its message file, and each recipient's outcome), durably, makes sure the keeper is running, waits up to about ten seconds for outcomes, and prints them with the message ID. A sender does nothing further with `sending` or `queued`, and never resends them: Factory will finish them.
- If any recipient is not on the team, or the input is invalid, the whole `say` is `refused` and nothing is stored. A member stopped by `down` or never started is not running: `say` to it is refused with "@x is not running", as in v1.
- **The keeper** delivers messages. It is the only thing that types messages, and it types only into a ready recipient (§8.2). It delivers each recipient's messages in the order they were sent, at most once, as soon as the recipient can take them (§1a), **with no agent having to act**. A recipient whose message is held does not delay messages to other recipients. Messages waiting on `gone` or `not ready` go out after the command that fixes them (`up`, or `brief --send`).
- Outcomes, per recipient:
  - `delivered`: typed in and submitted (a transport receipt, §1a), with whether the recipient then started working.
  - `sending`: handed to `hc`, not confirmed yet (held for `draft_wait`, or slow to start). It becomes `delivered` or `unconfirmed`; if `hc` refuses it before typing anything, it goes back to `queued` with the reason `hc` gave, or ends `dropped` if `down` stopped the recipient.
  - `queued`, with the reason: `waiting` (behind an earlier message to the same recipient, or not yet handed to `hc`), `blocked` (until it unblocks), `busy` (only for a harness that takes messages between turns, §8.3: until it stops `working`), `not ready` (a member whose brief failed or is `unconfirmed`: until a brief is `delivered`) or `gone` (lost without `down`, or the operator not reachable: until `up` brings it back).
  - `unconfirmed`: Factory may have typed it but could not confirm, for example because the keeper stopped mid-send. Final: never typed again automatically, and it does not hold up later messages. The sender decides.
  - `dropped`: `down` stopped the recipient while the message was queued, or while it was `sending` and nothing was typed. Final.
  - `not stored`: writing the message to the log failed or may have been partial. Nothing will be delivered, even if part of the entry later turns out to be on disk; the sender sees exactly what is known, and nothing suggests a blind resend.
- A member that `up` starts or restarts gets its brief before any message queued for it; until the brief is `delivered` the member is not ready and its queue waits. If the brief fails or is `unconfirmed`, `up` reports it and the member's queue stays held as `not ready`; `brief --send NAME` that ends `delivered` makes the member ready and releases its queue in order (that brief goes ahead of the queue, and, because the member never confirmed its first brief, it asks for the `ready` or pickup line again), and `down NAME` then `up NAME` also works (the queue is then `dropped`, and `down` says how many).
- Messages queued for the operator as `gone` go out, oldest first, after a new `up` marks it reachable. `down` marks queued messages for the members it stops `dropped` and says how many.
- **The keeper process.** `up` starts it as `factory.py keeper`: one per team, in its own `hc` terminal, identified by a team label and not by its lack of an agent status. It is not an agent and never reports OSC 7501, so clients show no agent state, toasts or sounds for it, and nothing ever types into it. It runs no model. When it runs is below. It ignores typed input; Ctrl+C stops it.
- **Liveness and version.** The keeper records a heartbeat and its Factory version. Its heartbeat keeps going while a send is held for `draft_wait`, so a long hold never looks like a hang. When the installed Factory version changes, the keeper finishes any send in progress and replaces itself in place with the new version, so an idle team is never left without one. A hung keeper (stale heartbeat) is stopped by its recorded terminal ID, never by name, before a new one starts; a send it had in progress becomes `unconfirmed`. A second keeper for the same team exits at once.
- **When it runs.** The team is up from `up` until `down` stops the last member. While the team is up, `say`, `status` and `up` start the keeper again when it is gone, hung or another version; if its pane is gone, they start it without a pane and `up` puts the pane back. After the last member's `down`, the keeper exits once nothing is pending for the operator, and a command starts it again only to finish messages still pending. `status` on a stopped team leaves it stopped. If the keeper dies while every agent is idle, nothing restarts it until the next command; that would need a supervisor and is out of scope.
- **What its pane shows:** one line per event, appended, never redrawn, short enough for a narrow pane, with no message text (the text is in the log, `inbox` shows it). Its startup, reconnects and shutdown; every outcome change, as `16:42:07 #42 @operator -> @lead-dev delivered` or `16:42:09 #43 @reviewer -> @operator queued: blocked`; every note, as `16:50:00 note #44 @lead-dev exited (1) -> @operator` or `16:58:12 quiet 2m, note #45 -> @operator`. Its terminal title shows counts, changed only when they change (`keeper · 2 pending`, `keeper · quiet 3m`). No spinner and no separate log file: the message log is the record and the pane displays it.
- When the operator is not reachable, `say` to it answers "operator not running; message queued".

### 8.6 The message log

Carried over from v1's inbox, which worked well:
- Every message gets an ID that only increases and is never reused, even after a restart, and a UTC timestamp.
- The log is append-only and also records the keeper's notes: a message as written is never changed; outcome changes are recorded after it, so the history survives a message being dropped or an outcome changing.
- An append is reported only after it is durable. A failure before storing anything, a partial write, and an uncertain sync are told apart; after an uncertain update Factory checks what became visible before trying again. If the disk reports an error after an entry is completely written and the entry cannot be removed, the message counts as stored and is delivered; `say` warns that it may not survive a power loss and must not be resent.
- A damaged log (a cut-off last entry, invalid entries, IDs out of order) is refused with a diagnosis before anything is written, never silently repaired or discarded. `status` still shows the members' states.
- Only the owner can read it. It survives `down`.

### 8.7 Message files

Each member has its own message folder, which Factory gives it. A member writes each message to a new, uniquely named file there and passes it with `--file`. The keeper removes such a file once the last recipient reaches `delivered`, whenever that happens; with any other final outcome the file stays, so the sender can check and decide. A file outside the sender's own message folder (any other `--file` path) is never removed. On `up` after a stop or crash, Factory matches leftover message files to their message IDs and outcomes, and lists only the ones not delivered to everyone, including files that were never logged, so the operator can check before resending or discarding them. A file whose message was delivered to everyone is removed, not listed.

## 8a. Autonomous operation

The team works through ready tickets on its own, following the workflow in `FACTORY.md` (direction from the owner; tickets by @pm; a plan by an architect; build by developers; review; pull request and CI by the developer who wrote it; merge by the operator; then the next ticket). The skill's `FACTORY.md` template carries this workflow as the default hand-off, editable per project.

**Guarantee:** while the operator is reachable and the keeper runs, a quiet team gets one note for the operator once the quiet grace has passed and the operator can take a message, delivered as §8.5 describes; once per quiet period, not repeatedly, and not again after a keeper restart.

### 8a.1 The team-quiet note

When no member and not the operator is `working`, and nothing is deliverable or `sending`, for the quiet grace (default about 2 minutes, configurable), and the operator is reachable and can take a message (not `blocked`), the keeper sends it one note. The quiet and ready-work notes are never queued: if the operator cannot take one, the keeper checks again later, so a stale note is never queued. The keeper checks that the team is still quiet right before handing a note to `hc`; a note `hc` then holds for `draft_wait` can still arrive after work has resumed, and the operator treats it as information, not an instruction. The note: "team quiet", with one line per member giving its state, how long since it stopped, the last message it received (ID and sender) and the last it sent (ID and recipient). A member whose last received message is newer than anything it sent probably owes a reply; the operator judges. The keeper does not track assignments and does not read message text. It sends the note again only after real activity: a member `working` again, or a message other than the keeper's own being delivered. The operator's own turn answering the note does not count, so an operator that rightly does nothing (waiting on the owner, an empty board) is told once.

### 8a.2 The ready check

The owner's local `factory.toml` may set a read-only ready-check command (for example a `todos` query for the Todo column). It runs only when the owner's own file sets it, never from the committed `FACTORY.md`; `factory.toml.sample` may show one only as a comment, so `init` copying the sample never enables it. The keeper runs it in the main checkout every few minutes, only while the team is quiet, the quiet note has been tried (`delivered` or `unconfirmed`), and the operator is reachable, with a time limit, without holding up delivery. The contract:
- exit 0 with non-empty output means ready work; its output (trimmed, first lines) goes into the note;
- exit 1, or exit 0 with empty output, means no ready work;
- any other exit, or the time limit, means unknown: one line in the keeper's pane, no note, nothing changed.

It wakes the operator with "ready work" when work appears after a result of no work, or when the output differs from the last one reported; a result of no work clears the last report, so a ticket that leaves Todo and comes back wakes the team again. The owner should make the command print stable output (ticket IDs, no times). Without a ready check, a ticket moved to Todo while the team is quiet waits until the owner tells the operator. The keeper never calls the tracker otherwise and knows nothing about it.

### 8a.3 Ways the team can get stuck, and what handles each

| Stall | Signal | Rule | Who acts |
|---|---|---|---|
| A member finishes its turn without reporting | The team goes quiet | Team-quiet note (§8a.1) | The operator asks the member, or reassigns |
| The operator ends its turn early, or two members wait on each other | The team goes quiet | Team-quiet note | The operator |
| Team idle while tickets wait in Todo | Quiet, then the ready check | Ready-work note (§8a.2) | The operator takes the next ticket |
| A member blocked on a permission prompt, question or login | `blocked` | Note (§8.4) | The operator resolves it, or asks the owner when only the owner can |
| A member rate-limited, or its model or provider failing | `error` or `blocked` (login) | Note (§8.4) | The operator reassigns, or retries after the provider's delay |
| A member crashes or is lost | Terminal ended | Note (§8.4); flagged interrupted (§9.3) | The operator runs `up NAME` or reassigns |
| A message cannot be delivered | `queued` with its reason | §8.5; oldest age in `status` | The keeper delivers it when the reason clears |
| A review loop | Three rounds on one ticket | Workflow rule in `FACTORY.md` | The operator decides |
| CI that never finishes | The developer's bounded wait runs out | The developer reports | The operator decides |
| A decision only the owner can make | The operator asks the owner | The operator carries on with other ready work meanwhile | The owner |

Not handled, and said so in the docs: the operator gone (messages queue until a new `up`); the keeper or `hc` dead while every agent is idle (until the next command); members exchanging messages without making progress; and an operator that, after a quiet note, works alone and stops without messaging anyone, which nothing re-arms until a member works or a message is delivered (the ready check still runs).

## 9. Lifecycle and recovery

### 9.1 Persistence

Members survive clients closing, the operator exiting and the owner detaching. `up` after a client restart finds them running and does nothing.

### 9.2 A member's harness dies

Whether its harness exits or is lost, the member counts as `gone`: messages to it queue, and `status` and `up` flag it (§9.3). A member's terminal is never replaced by a plain shell. `up NAME` restarts it, in the same pane when that pane still exists (it may get a new terminal ID), resuming its conversation.

### 9.3 Interrupted work (#129)

`up` and `status` flag a member **interrupted** when it was lost or exited without `down`, by name, with `hc`'s last known state for it and Factory's last message to it.

Factory does not read harness transcripts and does not resend work; the operator decides. The keeper's notes (§8.4) and the team-quiet note (§8a) tell the operator while it runs.

### 9.4 Resume

`down` and `up` keep each member's native conversation unless `--fresh`. Factory captures each harness's session ID in a way qualified per harness (§10), saves it per team so two teams never resume each other's members (v1 #108), and resumes with the harness's own arguments. Factory learns each member's current conversation from the member itself: harnesses export it to the commands they run (Claude Code `CLAUDE_CODE_SESSION_ID`, Codex `CODEX_THREAD_ID`, agy `ANTIGRAVITY_CONVERSATION_ID`; pi and omp keep each member's conversations in its own session directory and continue the newest; OpenCode by its launch flag), so every `say` a member runs records it, and a conversation switched to inside the harness is followed from that member's next `say`. A switch followed by `down` before the member sends anything is not seen. A harness with no way to learn its conversation starts fresh after `down`, with a notice; the brief, tracker and worktree are the recovery guarantee. If a session ID cannot be saved, the member is left running and `down` says so instead of losing the conversation. A damaged saved entry is skipped and reported, not trusted. A saved conversation from a different harness or an incompatible model is not resumed: the member starts fresh, with a warning. Standing instructions are rebuilt at every launch, so a moved checkout never leaves a member with stale ones.

### 9.5 Returning to unfinished work

As in v1: when the operator comes back to unfinished work, it first reconciles the brief, the tracker, Git and worktree changes, review verdicts and CI, leaves work it cannot explain untouched, and gives the owner a summary before reading any member's reply. A cold-recovery test fixture checks this, as v1's Gate 1 did.

### 9.6 Workflow carried over from v1

- A `[from @name]` line tells who sent a message; it grants no authority. Authority comes from the roles and the owner.
- Resending the brief by hand asks for no reply, except to a member whose first brief was never confirmed (§8.5), which is asked for its `ready` or pickup line. A member that joins for the first time can be announced to the existing members, who are not asked to report ready again.

## 10. Harness support and qualification

All six harnesses (Claude Code, Codex, pi, omp, OpenCode, agy) are supported as members and as operator. Each is qualified by live tests on a separate `hc` session viewed in rodeo, never the owner's own sessions. Each test uses a unique message and checks that the recipient acts on it, rather than only that Factory reported success. For each harness, in both roles:
1. OSC 7501 states are correct through a real turn, and the harness's startup dialog is caught (§6.3).
2. A report to an idle operator starts a turn that acts on it, with no human input.
3. A report while the owner has unsent text: held for `draft_wait`; with a long `draft_wait` the text survives byte-for-byte and the report lands after it is sent or cleared.
4. A message mid-turn: behavior recorded (queued or steered), nothing lost; and the owner interrupting the operator while a message is pending.
5. A blocked member: nothing typed; the operator gets the keeper's note; a message queued to it is delivered after it unblocks, with no Factory command run.
6. Resume after `down`/`up` (session ID captured), and after a thread switch in the operator.
7. A message ending in `@name`, `$skill` or `/command` with no final newline; a long multi-line message; two senders at once; a keeper restarted while messages are pending, without repeating a note or losing a stored one; a quiet note held for `draft_wait` while a member resumes work; a keeper replaced after a version change.
8. A member whose harness dies: nothing is typed into what is left. An operator that quits its harness but leaves the pane open while a message is pending: later messages queue until `up`. An operator quit and restarted in the same pane before any message was queued for it: the next `say` is delivered to the restarted harness. A brief that is `unconfirmed`: the queue is held as `not ready` until `brief --send NAME`. Measured and recorded, not required: a recipient that quits during `draft_wait` (§8.2 known limit).
9. A `guarded` member can still reach `hc` and Factory's state, and can restart the keeper (v1 #123, #141).
10. To an idle recipient, `say` normally reports `delivered` within its wait (measured; the wait is lengthened if not). `say` returns within about ten seconds while `hc` holds its message for `draft_wait`; the sender's process ending afterwards neither loses nor repeats the message. A keeper killed mid-send leaves that message `unconfirmed`, never retyped.

The cross-team cases run once: two teams with the same member names in one session; `down` of several members sends no notes; owner-added panes in the team tab survive `up` and `down`; a keeper of another version is replaced.

**Autonomy tests.** Automated: a fake agent that reports OSC 7501 states and plays scripted behaviour (reports, forgets to report, works forever, blocks, crashes), with a controlled clock, covering every keeper-handled row of §8a.3 (the review, CI and owner rows are workflow rules, checked in the live run), proving the team-quiet note never loops, that `status` leaves a stopped team stopped, and the ready check's A → none → A case. CI runs them against a fake `hc`, as v1's tests used a fake herdr, unless a pinned `hc` release can be installed there; the same tests run locally against a real separate `hc` core. Live, end to end, with no human input after `up`: first a throwaway repository with a local bare remote and a file tracker, where a branch reviewed and merged locally stands in for a pull request and a local test script for CI; then the private `pkieltyka/factory-test` repository with its GitHub board, real pull requests and CI. Two or three small Todo tickets; one member made to end its turn without reporting, one member's harness killed. It passes when every ticket is planned, built, reviewed, merged with CI green and closed within a time budget, with no `unconfirmed` message, and the log shows the notes and the recovery. A second run with an empty board gives exactly one team-quiet note, then silence for 15 minutes; a ticket moved to Todo afterwards is picked up through the ready check. Results are recorded in the repository with the harness, `hc` and loaded Factory versions tested. The automated tests also keep v1's checks of the installed skill copy, the docs and recipes, and cold recovery, and run in CI.

## 11. Prior work and challenges

| v1 problem | v2 requirement |
|---|---|
| Waking the operator differed per harness: background tasks for Claude Code, OpenCode and agy, foreground waits for Codex, pi and omp; queue experiments removed (#148–#161, #163) | One path: typed delivery wakes every harness (§8.1), qualified per harness (§10). |
| Reports could sit unread: a discarded watcher return left the inbox unattended for 45 minutes (#57); a message appended as the last waiter exited was stranded (#148) | No watcher in the operator; the keeper types everything, delivers queued messages on its own and runs for the team's whole life (§8.5). |
| A report could clobber the owner's draft; a trailing mention swallowed Enter (#22, #18) | `draft_wait`, warned on by `check`; an upstream ask to never type into a draft (§8.2); delivery that a trailing token cannot swallow (§8.1). |
| Agent state from screen parsing and per-harness integrations; transient frames misread | State only from OSC 7501 (§1). |
| Startup trust dialogs looked idle (#125, PR #128) | Dialog check even when idle (§6.3). |
| Team silently stalled after the session was killed; no operator-liveness signal (#129) | Interrupted members flagged (§9.3); notes about members (§8.4); the team-quiet note (§8a.1); "operator not running" on `say` (§8.5); reachability in `status` (§5.2). |
| Durable messaging and run IDs deferred (#14) | Durable log, message IDs and per-recipient outcomes, including `sending` and `unconfirmed` (§8.5). Task worktrees, enforced gates and cross-host claims stay deferred. |
| Killing watchers by process name hit other teams (#158) | No watchers in the operator; everything limited to the caller's team (§7.3). |
| Resume could pick another session's member (#108) | Session IDs saved per team (§9.4). |
| Quoting broke inline messages (#104) | Members always send with `--file` (§5.2). |
| Scratch runs rewrote shared harness settings; agy trust rewrote its global list | No integrations, nothing global written (§3); never accept agy's dialog for the owner (§6.3). |

## 12. What v2 cuts from v1

- `wait`, watcher handoff rules, foreground and background operator modes and their per-harness docs.
- Inbox acknowledgements (`--seen`) with their watermark and history compaction; `inbox` only reads the log.
- herdr integration checks and repair; every screen read except §6.3; joining panes and labels to discover the team.
- `prompt` (now `brief NAME`), `--no-brief`, `--restart`, and inline `say` text from members.
- Draft retry bookkeeping beyond "keep the file unless every recipient got it".

## 13. Constraints

- Tests and experiments never touch the owner's own `hc` sessions; live work uses a separate session viewed in rodeo.
- No secrets in messages, logs or notes. Runtime state is readable only by the owner.
- Members default to `full` permissions, which assume a safe machine; `guarded` as in v1.

## 14. Upstream request

Factory asks herdr-core for two `hc send` options: (1) fail with a distinct result instead of typing when the recipient has unsent human input, so Factory can queue the message and never touch the owner's text; (2) a guard checked at the moment `hc` types that the recipient still reports an agent status (not only `blocked`), so a held message never lands in a shell. Until they exist, `draft_wait` and the known limit in §8.2 stand.
