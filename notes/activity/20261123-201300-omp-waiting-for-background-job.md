---
created: 2026-11-23T20:13:00Z
author: operator
source: "#136, lead-dev's live acceptance on omp 19.0.1"
status: verified
kind: learning
---

omp 19.0.1 can show "Waiting for background job bg_N" after a native `async: true` Bash launch even when the model made no await or poll call. Read the tool transcript before calling it foreground drift. #134's first diagnosis ("omp awaits its own watcher") was this display.
