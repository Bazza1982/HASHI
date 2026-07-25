# Telegram task-control commands: `/focus` and `/recall`

**Status:** Implemented and user-tested

**Date:** 2026-07-24

**Commit:** `da48df5`

## Purpose

`/focus` and `/recall` solve two different problems while work is in progress:

- `/focus` corrects the **scope of the active or most recent task** and tells the
  agent to continue working.
- `/recall [count]` removes **requests that have not started yet** while leaving
  the current task alone.

Both commands are available in flexible and fixed runtimes and appear in the
Telegram bot command menu.

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

Behavior:

| Command | Result |
| --- | --- |
| `/recall` | Remove every request still waiting in this agent's queue |
| `/recall 1` | Remove the newest waiting request |
| `/recall 2` | Remove up to the newest two waiting requests |
| `/recall n` | Remove up to the newest `n`, where `n` is any positive whole number |

If `n` is larger than the queue, HASHI removes all waiting requests without
error. Requests that remain in the queue retain their original first-in,
first-out order.

### What `/recall` does not do

`/recall` does not:

- interrupt or stop the active task;
- shut down, reset, or reinitialize the backend;
- write an active-turn interruption marker;
- affect cron jobs, scheduled future work, or messages sent after the command;
- change the `recall` memory skill managed through `/skill recall`.

The reply reports how many queued requests were actually withdrawn. An empty
queue is a successful no-op.

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
| `/stop` | Interrupted | Cleared | No automatic continuation |
| `/steer <direction>` while busy | Interrupted | Cleared | Continue with the added direction and preserved progress |
| `/focus` | Re-focused through an immediate continuation | Cleared when busy | Continue only within the original scope until done or genuinely blocked |
| `/recall [count]` | Continues untouched | All or newest `count` removed | Current task keeps running |

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
| Flexible-runtime command method | `orchestrator/flexible_agent_runtime.py` |
| Fixed-runtime command method and menu | `orchestrator/legacy/bridge_agent_runtime.py` |
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
