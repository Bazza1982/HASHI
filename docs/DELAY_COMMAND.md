# `/delay`: persistent FUTURE messages

`/delay` postpones delivery of one ordinary user message without creating or
changing a cron, heartbeat, nudge, or automation job.

## Quick start

```text
/delay 4 this is a testing delay message, let me know when you receive it and if delay worked.
```

HASHI immediately confirms the delay ID and local due time. On or after that
time, the message enters the owning agent's ordinary request queue and its
eventual reply returns to the chat where the delay was created.

Use `/delay list` to confirm that a message is still waiting, or copy its ID
from the acknowledgement to cancel it:

```text
/delay list
/delay cancel delay-abc123def0
```

## Command reference

```text
/delay <minutes> <message>
/delay
/delay list
/delay cancel <delay-id>
```

| Form | Result |
| --- | --- |
| `/delay <minutes> <message>` | Persist one FUTURE message owned by the current agent |
| `/delay` or `/delay list` | List that agent's FUTURE messages, IDs, due times, and excerpts |
| `/delay cancel <delay-id>` | Cancel one FUTURE message by full ID or an unambiguous ID suffix |

`minutes` must be a positive whole number from 1 through 10080 (seven days).
Each agent may own up to 100 pending delayed messages, and each payload may
contain up to 16000 characters. A persistence failure is reported immediately
and leaves no scheduled record.

## Delivery model

HASHI keeps two pending layers per agent:

1. READY — the existing in-memory FIFO;
2. FUTURE — persistent delay records in scheduler state.

The scheduler checks FUTURE records on its normal tick. At the first tick on or
after a record's due time, the exact payload is enqueued into READY with source
`text`. It goes behind requests already waiting there and never interrupts the
active turn. The owning agent's backend, model, permissions, workzone, and
context are resolved through the normal request path at processing time.

The due time is the earliest dispatch time, not a guaranteed reply time. The
message can be received later when the agent is busy, has earlier READY work,
is offline, or reaches the due time between scheduler ticks. HASHI never
delivers it before its due time.

The message is data, not a recursively dispatched bridge command. A delayed
payload such as `/stop` is shown to the model as text; it cannot invoke the
Telegram `/stop` handler.

## Persistence and offline behavior

Delay records survive reboot and ordinary agent stop. If the owning agent is
offline when a message becomes due, HASHI retains the record and dispatches it
after the agent is available again. The scheduler records failed enqueue
attempts and retries on a later tick.

`/status`, `/delay list`, and `/queue` expose pending delay counts or details.
`/queue show`, `/queue cancel`, and `/queue clear` operate on delayed records as
well as READY requests.

Once a due message moves from FUTURE to READY, it disappears from the
`/delay list` output and `/delay cancel` no longer owns it. Use `/queue` to find
and cancel its new READY request ID, or use `/recall`, before processing begins.

## Related controls

- `/recall` removes all READY and FUTURE requests.
- `/recall n` removes the newest `n` requests across both layers by creation
  time while preserving the order of retained READY requests.
- `/stop`, busy `/steer`, `/focus`, and `/retry` clear READY requests but
  preserve FUTURE records and report that fact.
- `/wipe`, `/reset`, agent deletion, agent move, and session transfer are
  blocked while the affected agent owns FUTURE records. Recall them first.
- Delays do not make an agent busy and do not alter `/jobs` state or recovery.

## Storage and implementation

Delay records use the `delayed_messages` top-level key in the existing
`scheduler_state.json`. Shared queue mutation lives in
`orchestrator/runtime_pending.py`; scheduling and due dispatch live in
`orchestrator/scheduler.py`; command handling lives in
`orchestrator/commands/delay.py`.
