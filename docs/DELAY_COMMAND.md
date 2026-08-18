# `/delay`: persistent FUTURE messages

`/delay` postpones delivery of one ordinary user message without creating or
changing a cron, heartbeat, nudge, or automation job.

## Commands

```text
/delay <minutes> <message>
/delay list
/delay cancel <delay-id>
```

`minutes` must be a positive whole number from 1 through 10080 (seven days).
Each agent may own up to 100 pending delayed messages, and each payload may
contain up to 16000 characters.

## Delivery model

HASHI keeps two pending layers per agent:

1. READY — the existing in-memory FIFO;
2. FUTURE — persistent delay records in scheduler state.

The scheduler checks FUTURE records on its normal tick. At the first tick on or
after a record's due time, the exact payload is enqueued into READY with source
`text`. It goes behind requests already waiting there and never interrupts the
active turn. The owning agent's backend, model, permissions, workzone, and
context are resolved through the normal request path at processing time.

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
