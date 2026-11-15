# `/a-team setup`: form the team

Guide the user to a team for this repo and write it to `team-brief.md` at the repo root. Incremental: if a brief exists, start from it and change only what the user wants.

## 1. Look before asking

- Find the repo root (`git rev-parse --show-toplevel`). Note the stack from root markers (`go.mod`, `package.json`, `pyproject.toml`, `Cargo.toml`, ...) and whether there is a UI. Note any existing tracker: a `TODO.md`, GitHub Issues in use (`gh issue list --limit 5`), a Linear or todo CLI the user mentioned.
- If `team-brief.md` exists, run `ateam.py show` and ask what they want to change. Then jump to step 4.
- Run `ateam.py harnesses`: which harnesses are installed and which recommended models each can use. Recommend only what it reports as available. If a harness the user wants is missing or cannot list its models, show its fix line now.

## 2. Ask (a few questions, one round)

1. **The project, in two or three sentences:** what it is, who it is for, and the stack if you could not tell.
2. **Kind:** backend, frontend, full-stack, or other (CLI, library, data, infra). Propose your guess from step 1.
3. **Size:**
   - *default:* lead-dev (Codex gpt-6-astra), reviewer (Codex gpt-6-luna), pm (pi deepseek/deepseek-v4.1-flash); add frontend-dev (Claude Code claude-opus-5-5) only for a UI. The orchestrator uses Claude Code Opus or Fable. Propose this formation first; keep the current lead session unless the user requests a change.
   - *lean:* lead-dev and reviewer, with the lead keeping the tracker
   - *standard:* lead-dev, one or two developers for the kind, reviewer, pm, and a QA member for the kind
   - *full:* architect, product, lead-dev, backend-dev, frontend-dev, designer, reviewer, pm, qa-frontend, qa-backend
4. **Model mix:**
   - *mixed* (recommended): the strongest models where judgment matters, cheaper ones for volume
   - *Claude-first*
   - *open models:* GLM, DeepSeek or Kimi via OpenRouter
   - *local:* models the user serves themselves, run through pi or omp

   Also ask whether cost or speed matters more.
5. **Permissions:** not a question. Tell the user that members use unattended permission modes intended for a sandbox or VM; existing harness sandbox settings can still apply. If they say it is not, set `permissions = "guarded"` (`cmd_up.md`).
6. **Worktrees:** will developers build separate things in parallel? Then each of those gets a git worktree of its own. Developers working on the same thing share the main checkout.
7. **Tracking (when there is a pm):** where @pm keeps tasks, owners, status and bugs:
   - a `TODO.md` file in the repo
   - GitHub Issues, and optionally a GitHub Project board, via `gh`
   - Linear, via its MCP tools or CLI
   - the user's own todo CLI (ask its name)
   - none

Use the harness's question tool if it has one, with your recommendation first.

## 3. Propose the formation

Show a table: @name, role, harness, model, thinking, and one line of why. Start from the template's members (`templates/team-brief.md`) and the models `ateam.py harnesses` reported as available. Rules of thumb:

- Start from the default formation above. Optional catalog roles are added for concrete work, not automatically.
- Tip for custom teams: a reviewer on a different model family from the code's author can catch different mistakes. The default reviewer stays on Codex Luna; it differs from the Claude frontend developer, but not the Codex lead developer.
- designer, qa-frontend and frontend-dev need a browser tool in their harness (a browser MCP, Playwright or `agent-browser`): they judge and test the running app. Put them on a harness that has one, and ask the user if you cannot tell.
- With a product member, it writes each feature's acceptance criteria. Without one, you do, in the task.
- Thinking: high for the architect, reviewer and designer; medium for developers; low for the pm. The highest levels on Claude and Codex are refused by default (`never` in `harnesses.toml`).
- Names are stable: the first member is `@backend-dev`; later ones are `@backend-dev-2`, `@backend-dev-3`, each with `role = "backend-dev"`. Never rename an existing member.
- If the user chose a mixed team, spread models across families. Leave the lead's model as it is: the lead is you.

No architect is required by default. The orchestrator owns architecture decisions; lead-dev proposes or challenges non-trivial designs before building. Add an architect for larger systems.

Adjust until the user agrees.

## 4. Write and check

1. With no brief yet, run `ateam.py init`. It writes the template, with every role defined, to the repo root. Then edit `team-brief.md`:
   - **Settings:**
     - keep only the chosen members (the template also offers a catalog), and set each one's required harness, model and thinking;
     - add `notes` saying what each member owns on this project;
     - set `worktree = true` on the members that get their own worktree;
     - set `tracker` to the user's answer in words a member can act on, e.g. `"GitHub Issues on owner/repo via gh issue"`, or remove it for none;
     - set `[teams] default` to the chosen roster.
   - **Prose:**
     - rewrite *Role details* and *Workflow* for this team: name the real members, say who pairs with whom, how @pm uses the tracker, and the commit rule;
     - finish with `## Project`: two or three sentences on what we are building, for whom, and the stack.

   Keep it short.
2. Run `ateam.py check`. If a member fails, show the problem and fix lines and ask: fix the setup, or change the member? Do not substitute on your own. Repeat until every member is `ok`.
3. Run `ateam.py brief` and show the user the brief as members will receive it.

## 5. Finish

- Suggest committing `team-brief.md`.
- Next: `/a-team up`.
