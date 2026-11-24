---
created: 2026-11-24T14:48:00Z
author: operator
source: "#133 gate (b), lead-dev-2's trustedWorkspaces before/after (agy 1.5.13)"
status: verified
kind: learning
---

When agy 1.5.13 trusts a new folder (factory's #125 dialog answer at launch), it rewrites `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json` and can drop unrelated existing entries. In the #133 gate it removed the owner's `/home/peter/Dev/other/rodeo`. Snapshot that file before launching agy in an untrusted folder, and check it afterwards.
