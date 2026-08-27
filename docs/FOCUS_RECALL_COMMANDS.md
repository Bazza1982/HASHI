# Telegram task-control commands: `/focus`, `/recall`, and `/delay`

**Status:** Implemented and regression-tested

**Updated:** 2026-08-18

**Original focus/recall commit:** `da48df5`

## Purpose

`/focus`, `/recall`, and `/delay` solve three different queue-control problems:

- `/focus` corrects the **scope of the active or most recent task** and tells the
  agent to continue working.
- `/recall [count]` removes **READY or FUTURE requests that have not started
  yet** while leaving the current task alone.
- `/delay <minutes> <message>` creates a persistent FUTURE request that joins
  the normal READY FIFO when its due time arrives.

All three commands are available in every supported Flex Agent execution mode
and appear in the Telegram bot command menu.

## `/focus`

### Usage

```text
/focus
```

`/focus` takes no arguments. It is a one-off reminder to return to the original
user-requested scope. It is not a request to stop, pause, cancel, finish early,
or provide only a status report.

### When the agent is busy

HASHI:

1. captures the original task and current backend;
2. marks the active turn as an intentional `user_focus` interruption;
3. stops the active backend turn without reporting the intentional kill as a
   backend error;
4. preserves existing files, artefacts, tool results, session state, and other
   in-scope progress;
5. clears other queued requests;
6. queues an immediate continuation restricted to the original task.

The continuation is instructed to take the next concrete in-scope action and
keep working until the original result is complete or a genuine blocker
requires new user authority or an external state change.

### When the agent is idle

- If a recent task exists, HASHI queues the same scope-corrected continuation
  for that task.
- If no recent task can be found, HASHI reports that nothing was queued.

### Repeated use

Repeated `/focus` calls unwrap the earlier focus prompt and preserve one clean
copy of the original task. The control instructions do not accumulate in
nested wrappers.

## `/recall [count]`

### Usage

```text
/recall
/recall 1
/recall 2
/recall 100
```

The waiting queue has two logical layers:

- **READY**: the existing in-memory FIFO consumed by the agent runtime;
- **FUTURE**: persistent `/delay` records that are not visible to the agent and
  do not count as busy until the scheduler moves them into READY.

Behavior:

| Command | Result |
| --- | --- |
| `/recall` | Remove every READY and FUTURE request still waiting for this agent |
| `/recall 1` | Remove the newest waiting request across both layers |
| `/recall 2` | Remove up to the newest two requests across both layers |
| `/recall n` | Remove up to the newest `n` across both layers, where `n` is any positive whole number |

If `n` is larger than the queue, HASHI removes all waiting requests without
error. Requests that remain in the queue retain their original first-in,
first-out order.

### What `/recall` does not do

`/recall` does not:

- interrupt or stop the active task;
- shut down, reset, or reinitialize the backend;
- write an active-turn interruption marker;
- affect cron jobs, heartbeats, nudges, deterministic automations, or messages
  sent after the command;
- change the separate legacy auto-restore runtime setting.

The reply reports how many READY and delayed requests were actually withdrawn.
An empty combined queue is a successful no-op.

## `/delay`

### Usage

```text
/delay 5 send me a message to say hi
/delay list
/delay cancel delay-abc123def0
```

The first form accepts a positive whole number of minutes, from 1 through
10080 (seven days), followed by a non-empty text message. HASHI persists the
record immediately and returns its ID and local due time.

At the first scheduler tick on or after the due time, HASHI appends the exact
payload to the owning agent's normal FIFO with source `text`. The current task
is not interrupted and existing READY requests remain ahead of it. Backend,
model, workzone, permissions, and context are resolved normally when the due
request is processed, so all execution backends share the same behavior.

Delay records live in scheduler state but are independent from `/jobs` data.
They survive reboot and agent stop; if an agent is offline at the due time, the
record remains FUTURE and is dispatched after that agent starts again. Wipe,
reset, deletion, move, and session transfer are blocked while the affected
agent owns delayed messages, preventing orphaned or surprising later work.

The payload is not recursively parsed as a Telegram command. For example,
`/delay 5 /stop` sends the text `/stop` to the model after five minutes; it does
not invoke HASHI's `/stop` handler.

### Invalid counts

The count must contain exactly one positive decimal whole number. These are
rejected without changing the queue:

```text
/recall 0
/recall -1
/recall two
/recall 1 2
```

Positive counts may be arbitrarily large. A value beyond the queue length has
the same effect as recalling the whole current queue.

## Choosing the right command

| Command | Active task | Waiting queue | What happens next |
| --- | --- | --- | --- |
| `/stop` | Interrupted and saved durably | READY cleared; FUTURE preserved | A later plain `continue`, `resume`, or `继续` resumes the saved task |
| `/steer <direction>` while busy | Interrupted | READY cleared; FUTURE preserved | Continue with the added direction and preserved progress |
| `/focus` | Re-focused through an immediate continuation | READY cleared when busy; FUTURE preserved | Continue only within the original scope until done or genuinely blocked |
| `/recall [count]` | Continues untouched | All or newest `count` across READY+FUTURE removed | Current task keeps running |
| `/delay <minutes> <message>` | Continues untouched | Adds one FUTURE request | Request joins READY when due |

Use `/focus` when the agent has drifted beyond the requested scope. Use
`/recall` when the current work is correct but one or more later prompts should
not run.

## Examples

### Narrow a drifting task

```text
User: Implement the parser fix.
Agent: Starts considering an unrelated refactor.
User: /focus
```

The parser task continues with its current in-scope progress, while the
unrelated refactor is not extended.

### Withdraw only the latest queued request

Assume the active task is running and the waiting queue contains:

```text
request A
request B
request C
```

After `/recall 1`, request C is removed. Requests A and B keep their original
order, and the active task continues.

After `/recall 2`, requests B and C are removed. Request A remains, and the
active task continues.

## Implementation map

| Piece | Location |
| --- | --- |
| Shared handlers, focus wrapper, queue withdrawal | `orchestrator/runtime_control.py` |
| Persistent delay state and due dispatch | `orchestrator/scheduler.py` |
| Shared READY/FUTURE queue operations | `orchestrator/runtime_pending.py` |
| Delay command | `orchestrator/commands/delay.py` |
| Flex Agent command method | `orchestrator/flexible_agent_runtime.py` |
| Shared command and bot-menu bindings | `orchestrator/runtime_command_binding.py` |
| Focus tests | `tests/test_focus_command.py` |
| Recall tests | `tests/test_recall_command.py` |

## Validation

The implementation was covered by the focused `/focus`, `/recall`, and
`/steer` regression suite, linting, Python compilation, and `git diff --check`.
The `/recall n` behavior was also confirmed through a live user test before
commit `da48df5` was published.

## Related

- [`/steer` detailed reference](STEER_COMMAND.md)
- [Bridge operational command catalog](AGENT_FYI.md)
- [Root README command table](../README.md#commands)
