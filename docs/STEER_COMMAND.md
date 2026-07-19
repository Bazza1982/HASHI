# Telegram `/steer` — mid-task course correction

**Status:** Implemented and smoke-tested  
**Date:** 2026-07-19  
**Commits:**

| Commit | Change |
| --- | --- |
| `a28ece3` | Add global `/steer` (flex + fixed + Telegram menu) |
| `ba8952f` | Idle: plain new request (no mid-task wrapper) |
| `f39473c` | Suppress false `❌ Backend error` on intentional kill |

## What it is

`/steer <direction>` is a **global** Telegram command available on **all backends and models** (flex and fixed runtimes). It is like `/stop` followed by a new instruction, with one critical difference:

- **Progress is kept** — interim thinking, workspace files, artefacts, tool results, CLI session state, and partial answers are **not** discarded.
- **No session reset** — the agent continues from the current state unless the new direction explicitly requires a wipe/reset.

Telegram bot menu label: **Stop and continue with new direction**.

## Usage

```text
/steer also include unit tests for the auth module
/steer use the existing OAuth helper instead of a new client
```

Empty `/steer` replies with usage help (busy vs idle behavior summarized).

## Busy vs idle

| Agent state | What happens | Prompt the model sees | Queue `source` | Interrupt backend? |
| --- | --- | --- | --- | --- |
| **Busy** (generating, or queue non-empty) | Stop current turn immediately, clear queued messages, re-init backend best-effort, enqueue continuity follow-up | Full mid-task **steer wrapper** + original task context | `steer` | Yes |
| **Idle** | Enqueue direction only — **no** mid-task wrapper | Your direction text **as-is** | `text` | No |

### Busy — mid-task wrapper

The model receives a prompt built by `build_steer_prompt()` in `orchestrator/runtime_control.py`:

```text
[HASHI /steer — mid-task course correction]
The user interrupted the previous turn to add direction. This is NOT a new blank task.
Requirements:
1. Stop the previous approach only where it conflicts with the new direction.
2. KEEP all interim progress already made: workspace files, artefacts, tool results, CLI session state, partial answers, and thinking already produced.
3. Do NOT call session-reset flows, wipe workspaces, or discard completed sub-steps unless the new direction explicitly requires it.
4. Continue from the current state and incorporate the additional direction below.

Active backend/engine at interrupt: <backend>

Additional direction / requirement from the user:
<your direction>

--- Original task context (for continuity; do not restart from zero) ---
<original prompt, max 12000 chars>
--- End original task context ---
```

- `Active backend/engine…` is omitted when no backend name is available.
- Original task block is omitted when no current/last prompt can be captured.
- Original prompt longer than **12000** characters is truncated with `…[original task truncated]`.

Typical Telegram ack (busy):

```text
🧭 Steered: stopped <backend>, cleared N queued, continuing with new direction (req-…).
```

### Idle — plain new request

When the agent is idle, HASHI does **not** wrap the text. The direction is enqueued like a normal chat message.

```text
You send:   /steer also include unit tests
Model gets: also include unit tests
```

Typical Telegram ack (idle):

```text
🧭 Agent was idle — queued your text as a new request (…) (no steer wrapper).
```

## `/steer` vs `/stop`

| | `/stop` | `/steer <direction>` (busy) | `/steer <direction>` (idle) |
| --- | --- | --- | --- |
| Kill active backend | Yes | Yes | No |
| Clear queue | Yes | Yes | No |
| Continue with new work | No | Yes (wrapped) | Yes (plain text) |
| Keep artefacts / session | Yes (nothing discarded by design) | Yes | N/A |
| False Backend error suppressed | Yes (`user_stop`) | Yes (`user_steer`) | N/A |

## False Backend error suppression

Intentional process kills (for example Grok CLI **exit code `-9` / SIGKILL**) previously showed:

```text
❌ Backend error (grok-cli) | req-…
Flex Backend Error (grok-cli): Grok CLI exited with code -9
```

That is **not** a failure for `/stop` or busy `/steer`. The pipeline now:

1. Calls `mark_user_interrupt(runtime, "user_stop" | "user_steer")` **before** shutdown.
2. On non-zero exit, `consume_user_interrupt` matches the current request.
3. Logs a soft interrupt and **does not** deliver `❌ Backend error` to Telegram for that turn.

You should see the stop/steer acknowledgement, not a red Backend error line.

## Coverage

| Surface | Status |
| --- | --- |
| Flexible agents (`FlexibleAgentRuntime`) | ✅ `cmd_steer` → `runtime_control.cmd_steer` |
| Fixed / legacy agents (`bridge_agent_runtime`) | ✅ same shared handler |
| All backends via `backend.shutdown()` / `backend_manager` | ✅ |
| Telegram `BotCommand` menu | ✅ `steer` |
| Command binding table | ✅ `CommandBinding("steer", "cmd_steer")` |

## Implementation map

| Piece | Location |
| --- | --- |
| Command handler, busy/idle branch, wrapper builder | `orchestrator/runtime_control.py` |
| Interrupt mark / consume | `mark_user_interrupt`, `consume_user_interrupt` |
| Flex pipeline error suppress | `orchestrator/runtime_pipeline.py` |
| Lifecycle double-notify avoid | `orchestrator/runtime_lifecycle.py` |
| Fixed-runtime exit suppress | `orchestrator/legacy/bridge_agent_runtime.py` |
| Menu + bindings | `orchestrator/runtime_command_binding.py` |
| Tests | `tests/test_steer_command.py` |

## Smoke checklist

1. **Busy steer** — start a long turn, send `/steer testing steer now`. Expect: ack, mid-task wrapper on the next turn, **no** `❌ Backend error … -9`.
2. **Idle steer** — with agent idle, send `/steer hello`. Expect: plain prompt `hello`, ack says “no steer wrapper”.
3. **Empty steer** — `/steer` alone shows usage (busy vs idle described).
4. **Artefacts** — busy steer during a multi-step task should continue without wiping workspace files.

## Related

- Operator one-liner: [AGENT_FYI.md](AGENT_FYI.md) (`/steer` under Important Commands)
- Changelog: [CHANGELOG.md](../CHANGELOG.md) (Unreleased — Added / Fixed)
