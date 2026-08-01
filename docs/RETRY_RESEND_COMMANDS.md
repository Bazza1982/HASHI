# Telegram recovery commands: `/retry` and `/resend`

**Status:** Implemented and regression-covered

**Date:** 2026-07-28

## Purpose

`/retry` and `/resend` recover from different failure modes:

- `/resend` repeats the last successful visible output when delivery, copying,
  or reading failed. It does not call a model or change context.
- `/retry` recovers from a stuck, failed, or stale model turn. It creates a
  clean context, restores bounded recent continuity, and runs the last request
  again.

Both commands are available in flexible and fixed runtimes and appear in the
Telegram bot command menu.

## Choosing the right command

| Command | Active execution | Waiting queue | Context | Model work |
| --- | --- | --- | --- | --- |
| `/resend` | Unchanged | Unchanged | Unchanged | None |
| `/retry` | Stopped when active | Cleared | Replaced with a clean context plus bounded handoff | Last request runs again |

Use `/resend` when the answer already exists. Use `/retry` when the model needs
to perform the request again.

## `/resend`

### Usage

```text
/resend
```

HASHI replays the same stored visible text to the chat where `/resend` was
issued. The output may come from a normal model turn or a Bridge request such as
HChat.

`/resend` does not:

- stop the active backend;
- clear queued requests;
- reset a CLI or API context;
- invoke a model;
- fall back to rerunning the last prompt.

The last successful visible output is persisted independently from the last
retryable request, so it remains available after a runtime restart. Internal
retry-handoff acknowledgements are never allowed to replace it.

If no previous output can be found, HASHI reports that nothing is available and
changes no runtime state.

## `/retry`

### Usage

```text
/retry
```

The normal command takes no argument. For compatibility with older command
forms, `/retry prompt`, `/retry req`, and `/retry request` use the same recovery
flow. `/retry response` changes nothing and directs the operator to `/resend`.

### Recovery sequence

HASHI:

1. captures and persists the last retryable request before changing the
   backend;
2. prevents a second recovery flow from starting concurrently;
3. marks an active turn as an intentional `user_retry` interruption and stops
   its backend process;
4. clears requests still waiting in the agent queue and clears stale transfer
   state;
5. applies the appropriate clean-context behavior:
   - CLI backend: `/new` semantics and a new backend session where supported;
   - API backend: `/fresh` semantics, with recent turns cleared and saved
     memories preserved but not injected automatically;
6. builds a bounded handoff from up to 10 recent exchanges and 6,000 words;
7. queues that handoff as an internal, non-visible control turn, then queues the
   original request again.

The Telegram acknowledgement reports the clean-context mode, number of removed
queued requests, continuity result, and new request ID.

### Failure boundaries

- If no retryable request exists, HASHI changes nothing.
- If clean-context creation fails, recovery stops and the request is not
  rerun.
- If the optional handoff cannot be built or queued, HASHI still reruns the
  request and reports that continuity restoration was unavailable.
- If the request itself cannot be queued after reset, HASHI reports that the
  context was reset but the rerun did not start.
- An intentional backend kill caused by `/retry` is not displayed as a
  `Backend error`.

## Persistent recovery state

Each agent stores recovery state at:

```text
<workspace>/state/retry_state.json
```

The file has two independent snapshots:

- `last_prompt`: the most recent retryable request, recorded before model
  execution so a failed or interrupted turn remains recoverable;
- `last_output`: the most recent successful visible model or Bridge output.

Writes use an atomic temporary-file replacement. If the state file is missing
or unreadable, HASHI falls back to in-memory state and compatible transcript
history where available.

Startup, system, session-reset, ordinary handoff, and internal retry-handoff
control turns cannot replace the saved retry prompt. The internal
`retry-handoff` source is also excluded from future handoffs and from Wrapper,
Audit, Dual Brain, Memory+, and Anatta processing, preventing recovery metadata
from becoming user conversation.

## Examples

### Repeat an answer without model work

```text
User: /resend
```

HASHI sends the previous visible output again. The active session and queue are
untouched.

### Recover a stuck request

```text
User: Review the deployment and update the runbook.
Agent: The backend becomes stuck.
User: /retry
```

HASHI stops the stale execution, removes later queued requests, creates a clean
backend context, restores recent bounded continuity, and reruns the deployment
review request.

## Implementation map

| Piece | Location |
| --- | --- |
| Persistent snapshots and bounded retry handoff | `orchestrator/runtime_retry.py` |
| Shared command handlers and recovery orchestration | `orchestrator/runtime_control.py` |
| `/new` and `/fresh` reset primitives | `orchestrator/runtime_session.py` |
| Shared command and bot-menu metadata | `orchestrator/command_specs.py` |
| Flexible runtime integration | `orchestrator/flexible_agent_runtime.py`, `orchestrator/runtime_pipeline.py` |
| Fixed runtime compatibility | `orchestrator/legacy/bridge_agent_runtime.py` |
| Recovery regression coverage | `tests/test_retry_command.py` |

## Related

- [Task-control commands: `/focus` and `/recall`](FOCUS_RECALL_COMMANDS.md)
- [`/steer` detailed reference](STEER_COMMAND.md)
- [Bridge operational command catalog](AGENT_FYI.md)
- [Root README command table](../README.md#commands)
