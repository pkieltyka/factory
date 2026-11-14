# `/devteam setup`: form the team

Guide the user to a team for this repo and write it to `.devteam/team-brief.md`. Incremental: if a brief exists, start from it and change only what the user wants.

## 1. Look before asking

- Find the repo root (`git rev-parse --show-toplevel`). Note the stack from root markers (`go.mod`, `package.json`, `pyproject.toml`, `Cargo.toml`, ...) and whether there is a UI.
- If `.devteam/team-brief.md` exists, run `devteam.py show` and ask what they want to change. Then jump to step 4.
- Run `devteam.py harnesses`: which harnesses are installed and which recommended models each can use. Recommend only what it reports as available. If a harness the user wants is missing or cannot list its models, show its fix line now.

## 2. Ask (a few questions, one round)

1. **The project, in two or three sentences.** What it is, who it is for, and the stack if you could not tell.
2. **Kind:** backend / frontend / full-stack / other (CLI, library, data, infra). Propose your guess from step 1.
3. **Size:**
   - *lean*: lead-dev, reviewer, pm
   - *standard*: lead-dev, one or two developers for the kind, reviewer, pm, and a QA for the kind
   - *full*: architect, product, lead-dev, backend-dev, frontend-dev, designer, reviewer, pm, qa-frontend, qa-backend
4. **Model mix:**
   - *mixed* (recommended): strongest models where judgment matters, cheaper ones for volume
   - *Claude-first*
   - *open models*: GLM, DeepSeek, Kimi via OpenRouter
   - *local*: models the user serves, through pi or omp
   Ask too whether cost or speed matters more.

Use the harness's question tool if it has one, with your recommendation first.

## 3. Propose the formation

Show a table: @name, role, harness, model, thinking, and one line of why. Rules of thumb:

| Role | Default pick | Why |
|---|---|---|
| architect | Claude Fable, high | the hardest judgment calls |
| product | Claude Opus, medium | product sense, writing |
| lead-dev | Codex gpt-6.1-sol or gpt-6-luna, high | strong, steady implementer |
| backend-dev | GLM 5.3 via pi, medium; or Codex Luna | volume coding at lower cost |
| frontend-dev | Claude Opus, medium | UI judgment |
| designer | Claude Sonnet, high | critique, read-only |
| reviewer | a model family different from the main developers, high | different blind spots catch more bugs |
| pm | DeepSeek V4.1 Flash or a local model, low | bookkeeping |
| qa-frontend | Claude Sonnet, medium | browser tools work best in Claude Code |
| qa-backend | GLM 5.3 Flash via pi, medium | curl and test runs |

- Thinking: high for architect, reviewer and designer; medium for developers; low for pm and runner. Never max or ultra on Claude or Codex (`never_thinking`).
- Two of a role get numbered names: `backend-dev-1`, `backend-dev-2`, with `role = "backend-dev"`.
- Spread models across families when the user said mixed. Keep the lead's model as it is: it is you.

Adjust until the user agrees.

## 4. Write and check

1. With no brief yet, run `devteam.py init`; it writes the template with every role defined. Then edit `.devteam/team-brief.md`:
   - **Settings:** keep only the chosen members, set harness/model/thinking, add `notes` for what each one owns on this project, and set `[teams] default` to the chosen roster.
   - **Prose:** rewrite *Role details* and *Workflow* for this team. Name the real members, state who pairs with whom, who owns `TODO.md`, and the commit rule. Keep it short.
2. Run `devteam.py check`. If a member fails, show the problem and fix lines and ask: fix the setup, or change the member? Do not substitute on your own. Repeat until every member is `ok`.
3. Run `devteam.py brief` and show the user the brief as members will receive it.

## 5. The project brief (offer it)

Offer to draft `.devteam/project-brief.md` from what the user told you:
- what we are building and for whom
- scope and non-goals
- constraints and key decisions
- where things live in the repo
- first milestones and tasks, with owners

Write it for agents who have never seen this conversation. If the user runs `/spec`, point to its project folder instead of repeating it.

## 6. Finish

- Suggest committing `.devteam/team-brief.md` and `.devteam/project-brief.md`; `state/` is already ignored.
- Next: `/devteam up`.
