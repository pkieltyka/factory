# Factory v2 release candidate (Phase 10, #175)

**Status: candidate, not released.** The owner chooses the release version, creates and pushes the tag, publishes the release and changes skills-sync. Nothing here does any of those.

- **Candidate:** the reviewed implementation commit is `9b316ec` on branch `peter/175-phase-10`, with Factory `bbd75423bc1f` (`factory.py`'s own `VERSION`). Since `1dacdce` (Factory `9bd62cf9c632`), only the unstarted-delivery notice changed: every operator receipt is reported, and the notice points to `inbox` for the message. The native-discovery and cross-case evidence below ran at `e7c2f26`, Factory `7d4b075acdb5`. Changes since then: the unstarted-delivery notice, the multiline proof and report rejudging. No ID here is a published semver.
- **Environment:** hc 0.1.20, rodeo 0.2.1, Linux. Every run is on a private hc core, with rodeo on a second private core, and never in the owner's sessions.
- **Isolation:** QA terminals get no `HC_*`, `RODEO_*`, `HERDR*` or driving-harness session variables, a failing `herdr` first on PATH, and a scratch `FACTORY.md` naming the candidate script. User skill discovery is off where a harness has a switch.

## Harnesses × roles

Each cell links the generated report. A full harness claim needs every required case in both roles. The open rows below are each recorded with its reason. None is a Factory delivery failure, except where it says so.

| Harness | Version, model | As operator | As member |
|---|---|---|---|
| [Claude Code](claude.md) | 2.1.332, claude-fable-5-2 | all required rows pass (measurement-only rows that did not run are not required) | 4 owner-interrupt: the recorded limit for a deliberate owner Esc (functional spec §8.3) |
| [Codex](codex.md) | codex-cli 0.172.0, gpt-6.2-astra | 7 pending-restart: never reported blocked | 5 blocked, 7 pending-restart: never reported blocked |
| [pi](pi.md) | 1.6.1; operator openrouter/openai/gpt-6-astra, member openai/gpt-6-astra | 7 pending-restart: never reported blocked | 4 mid-turn: recorded pi behaviour (steering can leave the current task unfinished); 5 blocked, 7 pending-restart: never reported blocked |
| [omp](omp.md) | omp/19.2.1, openai-codex/gpt-6-astra | 6 operator-switch: no session variable; 7 upgrade: fail. Both messages were delivered and acted on once, but the unguarded `done` send landed in the owner's turn, and the owner's own task went unfinished (the #178 request 4 limit) | 6 member-switch: no session variable; 7 multiline: fail. The 29 KB message's receipt says delivered, but no turn started and no marker was acted on. It is a recorded limit: an idle omp does not submit a large message, which sits in its input until one more Enter. The keeper notes it to the operator, naming the message and the pane, and status shows an operator's own case. A longer submit gap is requested from hc (#178) |
| [OpenCode](opencode.md) | v2.4.1+6f05140, openrouter/openai/gpt-6-astra | 2 operator-commands: unproved (outputs only) | all required rows pass (measurement-only rows that did not run are not required) |
| [agy](agy.md) | 1.5.18, gemini-4-neon | 2 operator-commands: unproved (no readable record of its tool calls) | all required rows pass (measurement-only rows that did not run are not required) |

- **Delivery between turns** (keeper.md §9): Factory delivers to a working Codex or omp only between turns, in both roles. Other harnesses take messages mid-turn.
- **The #178 request 4 limit:** hc 0.1.20's `terminal.send` guard takes only `idle` or `working`. A recipient resting in `done` or `error` therefore gets an unguarded send. If the owner's Enter lands just before it, the send can arrive inside the owner's turn, which is what happened in omp's operator upgrade trial. This reopens when hc accepts a single-state `done` or `error` guard.
- **Trust entries:** every Claude Code and Codex trust entry the runs wrote is listed in [trust-entries.md](trust-entries.md) for the owner to remove. That includes the three Codex entries from the native-discovery check below, under `/tmp/factory-qa-disc-*/work`.

## Native discovery

Each check ran on a private core with the isolation above. The discoverable candidate is a wrapper copy of the skill at a different path from the script that `FACTORY.md` names. It writes a run-private marker to its own file and prints it, then hands over to the candidate. The owner prompt names no path or marker.

| Harness | Isolation and exposure | Discovery shown before typing | Line typed | Marker | Verdict |
|---|---|---|---|---|---|
| pi 1.6.1 | `--no-skills --skill <wrapper>/factory2` | pi's `[Skills]` list held only the wrapper's `SKILL.md`, none of the five installed copies | `/skill:factory2 status` | in its file; pi ran `<wrapper>/scripts/factory.py status` | pass |
| Codex 0.172.0 | per-path `skills.config` disables for all five installed copies; the wrapper at `<checkout>/.agents/skills/factory2` | Codex's own skill list showed `factory2` and no `factory` | `$factory2 status` | on screen and in its file; Codex ran `.agents/skills/factory2/scripts/factory.py status` | pass |
| omp 19.2.1 | — | none: `--no-skills` has no explicit skill-path option; `--skills` only filters by name, and `factory` would also match the installed copy | none | — | owner gap |
| Claude Code 2.1.332 | — | none: its launch switch turns off every skill command | none | — | owner gap |
| OpenCode v2.4.1 | — | none: the driver has no installed-copy disable | none | — | owner gap |
| agy 1.5.18 | — | none: the driver has no installed-copy disable | none | — | owner gap |

The candidate SHA was `e7c2f26` for both passes. The five installed copies are under `~/.nix-config/extras/skills`, `~/.claude/skills`, `~/.codex/skills`, `~/.agents/skills` and `~/.gemini/config/skills`. Discovering the real installed skill by name waits until the owner releases v2 and the installed copy is v2.

## Cross cases and the autonomy record

- **Cross cases:** `python3 tests/live.py qualify cross` at `e7c2f26` (Factory `7d4b075acdb5`), run `20261128T010300Z-2793607`. two-teams, multi-down, owner-pane and other-version all pass. The earlier record is in [hc-checks.md](hc-checks.md).
- **Phase 9 autonomy:** the local rerun passed 23/23 and the empty GitHub board 9/9 ([e2e-2026-11-27.md](e2e-2026-11-27.md)).
  - The normal GitHub board's final run passed 24/24: run `20261127T141356Z-824324`, source `4d0d77c`, Factory `31d3e84d827b`. Its record is local evidence (`github-final-4d0d77c.json`), not yet in the linked report.
  - The earlier GitHub rerun's 21/24 in that report is history.

## Owner release notes (proposed)

- **Requirements:** hc 0.1.20 or newer and rodeo, Python 3.11+, Git, and at least one signed-in harness: Claude Code, Codex, pi, omp, OpenCode or agy.
- **Messaging:** every message is stored before it is typed, and each one has an outcome you can check later with `inbox`. The keeper types messages into members' panes. It holds messages for a blocked or unbriefed member, and for a working Codex or omp. It also notes members that block, error or exit.
- **Operator:** all six harnesses ran as operator. Each took a member's idle report and handed off to a team-quiet note. OpenCode's and agy's own status and inbox output remains unproved.
- **Recovery:** `up` brings back stopped or lost members and resumes saved conversations where the harness allows. Every command Factory prints names `python3` and the running script's absolute path. A team command adds `--cwd` and the team's checkout once Factory knows it, so it works from any directory; before then it shows the placeholder `--cwd <team checkout>`.
- **Limits:** a deliberate Esc in a working Claude Code member's pane can lose a message delivered in that turn, so check `inbox` (leading.md). An idle omp can leave a large message unsent in its input, and Factory says so, naming the message and the pane. pi can leave a task unfinished when a message steers it. omp did the same before 8f40084, and now gets messages between turns. omp's upgrade trial (#178 request 4) and its 29 KB multiline trial failed, as the table says. There is the `done` or `error` guard limit above. Factory never verifies a harness's own permission prompts.

### Proposed README at release

- **Banner:** "Factory v2: a team of coding agents in hc and rodeo, led by the agent you already use." It would replace the pre-release banner, which stays until the owner publishes.
- **Install:** `npx skills add pkieltyka/factory -g`. This is pending. The command fetches GitHub master, so it can only be tested once this candidate is on master, from a fresh scratch HOME with no inherited installer settings. The record will be the remote master SHA, the installed destination and a file comparison with that commit. Until then the checked install-from-checkout instructions stay.

## Before release

- @architect-2 compares these claims with the evidence, @reviewer gives the exact-head verdict, and @operator merges.
- The separately scheduled final simplification follows, then these checks again at the release SHA.
- The owner decides the version, the tag, the release and skills-sync.
