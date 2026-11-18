---
created: 2026-11-18T17:56:27Z
author: lead-dev
source: "#57 incident report and owner decision relayed by operator on 2026-11-18; #54 merged as e77fcd7"
status: provisional
kind: observation
---

The #57 report describes an inbox stall at 11:58 Toronto on 2026-11-18. The operator restarted its watcher from its own wrapper one second after message 288 arrived. The watcher returned immediately with the message, but the wrapper discarded the output and the operator ended its turn with no watcher. Messages 288 and 289 stayed unread for about 45 minutes, until the owner asked; a manual status check showed the count.

The merged #54 guidance requires a harness background task, forbids a scripted wait-and-ack loop and blocking polls, and says to keep one watcher running. The unread-message path still prints pending messages and returns immediately; it does not acknowledge them. These instructions address the reported operator handling, but do not detect a missing watcher. It is not established whether the operator had loaded and followed the revised guidance at the time of the incident.

The owner declined a guard, new process, tab or command. If this recurs after the revised skill is installed and leading.md re-read, record how the watcher was launched, its returned output, and what the operator did with that output before ending its turn.
