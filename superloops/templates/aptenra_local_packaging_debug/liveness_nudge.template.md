SYSTEM: Aptenra packaging Superloop idle continuation.

Loop: `{{LOOP_ID}}`

You are being nudged because Zelda is idle while this loop is still active.
This loop must keep making concrete progress until Aptenra is installed and
both installed Aptenra and Workbench shortcuts launch successfully, or round
30 is formally blocked. Waiting, reporting status, or repeating a blocker is
not a terminal result.

Before doing anything, read:

- `superloops/loops/{{LOOP_ID}}/state.json`
- `superloops/loops/{{LOOP_ID}}/taskboard.json`
- `superloops/loops/{{LOOP_ID}}/issues.json`
- `superloops/loops/{{LOOP_ID}}/waits.json`
- recent `events.jsonl`
- the active round record
- the Aptenra Packaging Failure Journal

Always ask:

> What mistake did I make last time, and how will I avoid it in the most
> straightforward way this round?

Rules:

1. Do not merely report status. While neither terminal condition is true, take
   at least one concrete in-scope action that advances diagnosis, repair,
   media construction, installation, or installed dual-launch verification.
2. Do not mark a pending task `in_progress` merely because the loop was idle.
   The orchestrator must inspect its dependencies and then explicitly start it.
3. If one task is already `in_progress`, continue it with the smallest concrete
   safe action. If an internal gate fails, diagnose and repair it inside the
   loop; do not turn it into an invented user-input wait.
4. If no task is in progress, verify prior dependencies and required evidence,
   then explicitly start the next permitted task in the same continuation.
5. Any recurrence of a known PFJ signature invalidates the candidate
   immediately; repair the failed regression gate before another build.
6. Provider secrets are not required for this installation-and-dual-launch
   loop. Never request, copy, decrypt, import, or wait for provider credentials.
7. A candidate succeeds only after actual visible `/usecomputer` installation
   and successful user-visible launches from both installed shortcuts.
8. After a failed installation or launch, freeze evidence, update the Journal,
   uninstall that exact candidate, prove cleanup, then create the next round.
9. Never stop, modify or uninstall the original Debug Runtime.
10. If progress evidence has not changed for 15 minutes, open a
    `loop_stalled` issue and immediately take the smallest safe recovery action.
11. Do not disable this nudge, emit a completion marker, or end with a
    status-only response while the loop is below round 30 and installed
    dual-launch success has not been observed.
12. Emit `NUDGE_COMPLETE:{{NUDGE_ID}}` only after installed Aptenra and
    Workbench both launch successfully, or round 30 is formally blocked.
