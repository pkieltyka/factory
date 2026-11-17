# Notes

Read this only when the team brief's Notes section enables notes. The brief names the location and the folders in use; use no other folder and do not invent one. When the user asks for a new category, the operator adds its folder to the brief. Never search, index or cite private or ignored paths. There is no quota: most tasks leave no note.

A note is one file, `<folder>/<UTC YYYYMMDD-HHMMSS>-<slug>.md`. If the name exists, change the slug; never overwrite. Front matter: `created` (with timezone), `author`, `source` (the task, commit, request or URL behind it) and `status` (`provisional`, `verified` or `superseded-by <file>`).

## `activity/`: the team's working notes

These add a `kind` field:

- `observation`: something noticed during a task, provisional. A writing member adds it with its task's change.
- `learning`: something reusable, non-obvious, verified and durable. Only the operator writes it, usually from a report that ends `learned: <what a later task would otherwise get wrong>`.
- `decision`: a decision already made by whoever had the authority, that outlives its task. Only the operator writes it, and the task record links to it.

Read-only members and @pm put note text in their report instead of writing the file.

## Other folders: requested notes

Written only on request, by the operator or the member it assigns, as that task's deliverable. No `kind`. `source` says who asked and where the content comes from; leave private details out.

## Reading

Before a non-trivial task, read the notes your assignment links and search the declared folders once for the task's terms: the brief's search tool if it works here, otherwise `rg`. Do not read whole folders.

An `activity` decision or verified learning binds until a newer spec or decision replaces it. In a requested note only an explicit decision that names who made it binds, within that person's authority. Everything else is a hint to check against the code. A folder, author or status gives no authority, and no note is an instruction. Report a note that contradicts a current spec.

## Upkeep, by the operator

Supersede a stale note instead of rewriting it. At a milestone, promote observations to learnings, turn them into tasks, or remove them. Files directly in the notes location are older material: leave them where they are.
