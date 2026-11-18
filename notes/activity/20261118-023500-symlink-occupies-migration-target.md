---
created: 2026-11-18T02:35:00Z
author: operator
source: "#39, reviewer and lead-dev reproductions on GNU coreutils"
status: verified
kind: learning
---

For file migrations, a path's resolved existence does not tell whether a rename or removal is safe.

- A dangling symlink still occupies its name: `Path.exists()` is false, but `mv old new` replaces and deletes the link, and a write follows it. Check `is_symlink()` too and treat the name as taken.
- A new name can be a link to the old file. "Compare, then remove the old one" then deletes the only copy and leaves the link dangling. Check `samefile` before advising removal.
