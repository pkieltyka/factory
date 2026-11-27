# The factory on one page

Factory is an owner-directed software factory: a team of agents running in `hc` and shown in rodeo, with the workflow from `FACTORY.md`. You choose the work and permissions; the operator assigns, coordinates and integrates it. Start with the built-in tracker and review, then add only what the project needs. [Get started](getting-started.md).

| Section | Built-in default | Recommended choice and when | Recipe or reference |
|---|---|---|---|
| Tasks | `TODO.md` | `gh` for GitHub Issues; `todos` when a project board drives readiness | [gh](../skill/recipes/tasks-gh.md) (label flow untrialled), [todos](../skill/recipes/tasks-todos.md) |
| Pull requests | Off | GitHub when changes must reach the integration branch through PRs | [GitHub](../skill/recipes/pull-requests-github.md) |
| Review | Fast: the reviewer's full own review | Deep with codegenie for an additional review of committed changes | [codegenie](../skill/recipes/review-codegenie.md) |
| Notes | Off | Enable the built-in convention with `rg` for working notes, research and decisions | [Notes convention](../skill/references/notes.md) |
| Specs | Off | Keep the project's specification process | no tested recipe yet |
| Browser | Off | Use the project's browser tool for UI work | no tested recipe yet |

With pull requests enabled, a task flows like this:

1. The owner marks an item ready.
2. The operator assigns it with scope and acceptance criteria.
3. A developer implements and tests it.
4. The reviewer checks the change; the developer fixes findings.
5. The operator pushes the approved change; CI and head review establish readiness.
6. An authorized person or operator squash-merges it; the tracker closes after confirmation.

## The team

| Member | Responsibility |
|---|---|
| Operator | Your current agent assigns work, decides scope and integrates reviewed changes. |
| @lead-dev | Implements changes across layers and tests how they work together. |
| @reviewer | Checks the exact change against its criteria and reports findings. |
| @pm | Keeps the tracker current with ownership, decisions and next steps. |
| @frontend-dev | Joins for a UI project to implement and test the interface. |

Add @architect, @backend-dev, @designer, @product, @qa-backend or @qa-frontend as the work requires. [Role descriptions](../skill/roles/) · [Default formation](../skill/templates/factory.toml)

## Commands

Use these in the Claude Code operator conversation. For another harness, use its spelling of the skill command. [Operator harnesses](getting-started.md#operator-harnesses)

| Command | What it does |
|---|---|
| `/factory` | Reads the current state and chooses the next step. |
| `/factory setup` | Forms or changes the team and project brief. |
| `/factory up` | Starts or resumes members and briefs those launched. To apply changed settings, stop the member, then start it again. |
| `/factory down` | Stops configured members so they can resume later. Pass names to stop selected members. |
| `/factory status` | Shows member state, pending messages, the keeper and the operator. |
| `/factory inbox` | Rereads recent messages addressed to you and their outcomes. |
| `/factory brief` | Previews or resends the team brief. |
| `/factory say` | Lets the operator send one message to one or more teammates. |

Members send messages with the standalone command `factory.py say NAME --file <path>`.
