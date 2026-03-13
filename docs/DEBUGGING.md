# Debugging Guide

This document captures the debugging philosophy, failure patterns, and operational lessons learned while stabilizing `bridge-u-f`.

It is not product documentation. It is internal engineering memory for maintaining and hardening the system.

## Purpose

Use this guide to:

- debug the bridge systematically instead of guessing
- preserve knowledge learned from runtime failures
- check whether a bug in a fixed agent also affects flex agents
- keep a structured audit trail of recurring failure modes and proven fixes

## Debugging Philosophy

The core rule is simple:

- reduce ambiguity with better audit trails

When something fails, the target is not just to make it work again. The target is to make the next similar failure obvious, attributable, and faster to fix.

Practical principles:

- prefer concrete logs over inference
- log request lifecycle, not just exceptions
- separate backend failures from Telegram failures
- separate startup failures from runtime failures
- keep terminal output clean, but keep detailed file logs
- treat fixed-agent bugs as probes for flex-agent risk
- avoid broad behavioral changes when a narrow retry/recovery path will do

## System Model

There are two main runtime models:

- fixed agents: one backend permanently bound to one Telegram bot
- flex agents: one Telegram bot with a switchable active backend

There are also two operator surfaces:

- Telegram
- workbench frontend

Important shared rule:

- Telegram and workbench feed the same runtime queues and shared backend sessions

This means:

- a bug in a backend adapter can affect both frontends
- session resets, model changes, and retries affect the same underlying session state

## Where To Look First

### Bridge startup

Look at:

- `main.py`
- `logs/<agent>/<session>/events.log`
- `logs/<agent>/<session>/errors.log`

Questions:

- did the backend initialize?
- did Telegram preflight fail?
- did polling start?
- did the queue processor start?

### Telegram delivery issues

Look at:

- `logs/<agent>/<session>/telegram.log`
- `logs/<agent>/<session>/errors.log`

Questions:

- is this a network/polling problem?
- is there a bot conflict?
- did the backend actually finish, but send-back fail?

### Backend response issues

Look at:

- `logs/<agent>/<session>/events.log`
- `logs/<agent>/<session>/errors.log`
- backend-specific workspace artifacts like:
  - `workspaces/<agent>/codex_exec_events.jsonl`
  - `workspaces/<agent>/history.json`
  - `workspaces/<agent>/handoff.md`
  - `workspaces/<agent>/recent_context.jsonl`

Questions:

- did the subprocess launch?
- did it exit?
- was the output empty?
- was the session resume path corrupt?
- did prompt construction make the request too large or malformed?

### Flex continuity / switching issues

Look at:

- `orchestrator/flexible_agent_runtime.py`
- `orchestrator/flexible_backend_manager.py`
- `orchestrator/handoff_builder.py`
- `workspaces/<flex-agent>/handoff.md`
- `workspaces/<flex-agent>/transcript.jsonl`
- `workspaces/<flex-agent>/recent_context.jsonl`

Questions:

- was stale handoff reused?
- did hidden bootstrap text leak into persistent memory?
- was the backend switch done with or without context?
- did the new backend receive real user intent or wrapper instructions?

## Logging Expectations

Healthy runtime tracing should answer:

- what request was processed
- where it came from
- which backend handled it
- whether the subprocess started
- which PID was launched
- whether it exited or timed out
- whether the response was sent back

Useful lifecycle fields:

- request id
- source
- prompt length
- success/failure
- elapsed time
- error length
- child PID

## Known Failure Patterns

### 1. Telegram startup failure

Symptom:

- agent fails to come online during startup
- `httpx.ConnectError`

What it usually means:

- transient Telegram/TLS/network failure
- not necessarily a broken backend

Fix pattern:

- keep startup retries limited
- log Telegram preflight separately from backend init
- shut down backend cleanly if Telegram attach fails

### 2. Telegram polling conflict

Symptom:

- `Conflict: terminated by other getUpdates request`

What it means:

- another process is polling the same bot token

Fix pattern:

- kill stale processes
- if needed, revoke and replace the bot token

### 3. Windows encoding failures

Symptoms:

- `'charmap' codec can't encode character ...`
- `[Errno 22] Invalid argument`
- console breaks on emoji or Unicode

Where it appeared:

- Claude CLI output
- typing/status messages
- console printing paths

Fix pattern:

- prefer UTF-8-safe file-based prompt passing
- harden console output
- avoid raw Unicode-heavy argv where possible

### 4. Gemini empty `-p` / malformed prompt invocation

Symptom:

- `Not enough arguments following: p`

Where it appeared:

- Gemini fixed and flex paths
- especially on Windows when prompts became multiline or large

Root cause:

- `.cmd` argument parsing with large or multiline prompt payloads

Fix pattern:

- use stdin transport when prompt is multiline or large
- keep a small placeholder in argv

### 5. Gemini resumed session corruption

Symptom:

- `Unable to submit request because it has an empty inlineData parameter`

What it means:

- Gemini resume state likely still referenced a broken media attachment / session artifact

Fix pattern:

- detect the specific error
- disable resume and recover
- use `/new` for immediate manual recovery

### 6. Codex chunk/separator limit failure

Symptom:

- `Separator is not found, and chunk exceed the limit`

Where it appeared:

- `codex-coder`
- especially on the scheduled memory-maintenance cron

What it means:

- Codex hit an internal chunking/parser limit
- often not because the visible prompt alone is huge, but because prompt + resumed thread + file-reading instructions together became too heavy

Fix pattern:

- cap prompt size
- preserve separator structure
- for the `02:00` `codex-coder` cron:
  - retry once after 2 minutes
  - disable resume only for that retry
  - log and announce the retry clearly

Additional operational note:

- the same chunk-limit failure can also appear in flex when `sakura` is on `codex-cli` and receives a large raw prompt
- in flex, keep Codex free of wrapper context, but still run raw prompts through the prompt-budget cap before dispatch
- the fixed runtime now applies the same prompt-budget cap before dispatch as well; if `codex-coder` still shows this error before restart, treat it as stale-runtime recurrence rather than a missing patch
- the flex runtime (FlexibleAgentRuntime) now also implements the same targeted 120s retry logic for scheduler tasks hitting this limit, matching the fixed-agent behavior
- on Windows, large wrapped Codex prompts can also fail earlier with `The command line is too long.` because `codex.cmd` goes through `cmd.exe`; route large or multiline Codex prompts over stdin in both fixed and flex paths

### 7. Codex wrapper/handoff misinterpretation

Symptom:

- Codex answers the wrapper instructions instead of the user request
- e.g. `No NEW REQUEST was included`

Root cause:

- flex prompt wrapper and handoff material were being treated as literal user-facing instructions

Fix pattern:

- keep handoff/bootstrap context hidden where appropriate
- ensure visible Codex turns get the real user message
- do not recycle wrapper text into persistent handoff memory

### 8. Flex stale handoff contamination

Symptom:

- backend switch picks up wrong or old context

Root cause:

- old `handoff.md`
- bootstrap/system turns leaking into transcript memory

Fix pattern:

- distinguish switch-with-context from switch-only
- clear handoff/recent context on plain backend switch
- regenerate handoff only when explicitly requested

### 9. OpenRouter closed client lifecycle bug

Symptom:

- `Cannot send a request, as the client has been closed`

Root cause:

- adapter reused a closed `httpx.AsyncClient`

Fix pattern:

- recreate client if missing or closed
- reset client reference on shutdown

### 10. Workbench startup and supervision fragility

Symptoms:

- backend/frontend zombie processes
- stale PID files
- port conflict confusion
- workbench startup affecting bridge expectations

Fix pattern:

- keep workbench optional
- PID/log/health-based supervision
- stale PID cleanup
- bridge must survive workbench failure

### 11. Workbench admin command endpoint returns HTTP 500

Symptom:

- `POST /api/admin/command` fails with HTTP 500
- `POST /api/admin/smoke` may also fail when `include_commands=true`
- chat-only smoke may still pass

Root cause:

- local command capture stored raw `reply_text`/`_send_text` kwargs in `messages[].meta`
- some kwargs are Telegram objects (non-JSON-serializable)
- `web.json_response(...)` raises during serialization

Fix pattern:

- sanitize captured meta to JSON-safe primitives before returning
- convert unknown objects to `repr(...)` in admin-local testing helper
- if 500 persists immediately after patch, check whether runtime has restarted and loaded new code

## Fixed Agents vs Flex Agents

### Why fixed agents still matter

Fixed agents are useful because they:

- expose backend-specific failures more directly
- are simpler to reason about
- serve as baseline backend probes
- can remain good stable workers for narrow roles

### Why flex agents matter more

Flex agents are strategically more important because they:

- realize the vendor-indifferent architecture
- concentrate orchestration value in one runtime
- benefit directly from lessons learned in fixed-agent debugging

### Rule of thumb

When a bug is found in a fixed agent, ask:

- does the same adapter exist under flex?
- does the same resume path exist under flex?
- does the same media path exist under flex?
- does the same scheduler path exist under flex?

If yes, verify flex explicitly.

## Debugging Workflow

When something breaks:

1. Identify the agent and exact request time.
2. Check `events.log` and `errors.log` for that agent session.
3. Determine the failure class:
   - startup
   - polling/network
   - subprocess launch
   - subprocess timeout
   - session corruption
   - send-back failure
   - handoff/memory contamination
4. Check whether the same failure path exists in:
   - fixed runtime
   - flex runtime
5. Prefer a narrow fix:
   - targeted retry
   - backend-specific fallback
   - one-time recovery path
6. Improve logs while fixing.
7. Record the pattern here if it was non-trivial.

## Scheduler-Specific Guidance

Scheduler tasks are useful, but they are high-risk for “quiet failures” because they often run without an operator watching in real time.

For scheduler tasks:

- log the concrete task id
- log the agent
- log the request id
- log retry scheduling explicitly
- log retry outcome explicitly
- prefer one retry only for transient backend-specific failures
- inspect `maintenance.log` in the active agent session for scheduler/heartbeat audit entries
- keep heartbeat summaries tagged with `[task_id]` so runtime logs can be correlated back to the scheduler definition

Special note:

- `codex-coder-memory-journal-0200` now has a targeted retry path for the Codex chunk-limit error

## Workbench-Specific Guidance

Workbench and Telegram are not separate sessions.

If behavior looks inconsistent:

- confirm whether the action came from Telegram or workbench
- inspect transcript order
- verify that a command from one surface changed the shared session seen by the other

Workbench bugs are often:

- API startup/supervision issues
- transcript/render issues
- stale frontend polling state

They are less often backend bugs themselves.

## Known Good Practices

- keep terminal output clean enough to watch live behavior
- keep detailed logs in files
- log retries, not just failures
- prefer one explicit recovery path over many blind retries
- avoid changing default session semantics unless the failure pattern proves it necessary
- keep flex behavior intentional around context transfer

## Known Technical Debt (Low Priority)

Identified during comprehensive code review on 2026-03-10. None are urgent — documented here for future reference.

### 12. Synchronous SQLite in async event loop

**Where:** `orchestrator/bridge_memory.py`

All database calls use synchronous `sqlite3` inside async code, which technically blocks the event loop during I/O.

**Why no rush:** The database is tiny (conversation history only), operations complete in <1ms, and there's no concurrent write pressure. This only becomes a problem at scale (thousands of messages per second) or if the DB file grows to hundreds of MB. Current usage is nowhere near that. A proper fix would involve switching to `aiosqlite`, which is a non-trivial refactor touching every DB method.

### 13. Unbounded memory table growth

**Where:** `orchestrator/bridge_memory.py`

The `messages` table grows indefinitely with no pruning or rotation. Old conversation history is never cleaned up.

**Why no rush:** At current usage rates (tens of messages per day per agent), the DB won't reach problematic size for months. A fix requires a design decision — time-based pruning? row-count cap? archival? — and should be done thoughtfully rather than rushed. When it matters, add a `PRAGMA auto_vacuum` and a periodic cleanup that preserves recent context.

### 14. Flex runtime missing transcript logging

**Where:** `orchestrator/flexible_agent_runtime.py`

The flex runtime doesn't call `log_transcript()` after responses, unlike the fixed runtime in `agent_runtime.py`. This means flex agent conversations don't appear in the workbench dashboard transcript view.

**Why no rush:** Flex agents use `handoff_builder` for context transfer instead of transcript logs. The missing logging only affects the workbench UI display — agents still function correctly. Adding it is straightforward (call `log_transcript` after `generate_response`) but low impact since the workbench dashboard is a convenience tool, not a critical path.

### 15. Backend init retry logic

**Where:** `main.py` — `_try_init_backend()`

Backend initialization uses a `--version` check that either passes or fails with no retry. A transient failure (e.g., CLI not yet on PATH after install) would permanently skip the agent for that session.

**Why no rush:** The `--version` check rarely fails transiently — if a CLI isn't installed, retrying won't help. The current fail-fast behavior is actually reasonable: it surfaces misconfiguration immediately rather than hiding it behind retries. If transient failures become observed (e.g., slow PATH resolution on Windows), a single retry with a short delay would suffice.

## Future Additions

This document should grow when new failure classes appear.

Good future sections:

- media failure matrix by backend
- scheduler failure matrix by backend
- startup failure matrix
- workbench operator runbook
- release-hardening checklist

## Stress Testing Record — 2026-03-11

### Case: `telegram.error.BadRequest: Chat not found` (agent: `agent-dev`)

Severity: **Medium** (channel-delivery branch issue, not core execution failure)

Observed evidence:
- `agent-dev` handled regular API requests successfully (`source=api`, backend success true).
- Failures clustered on auto/system-triggered sends (`source=system` and `source=fyi`).
- Stack traces show failure at send stage (`bot.send_message`) with `Chat not found`.

Interpretation:
- This is **not** a global agent-dev runtime/backend failure.
- It is a **target chat resolution/routing mismatch** for specific automatic delivery paths.
- User-facing implication: task may execute successfully but notification/reply on that branch is dropped.

Testing protocol decision:
- Keep this issue **inside testing scope**.
- Do **not** apply out-of-band hotfix outside the active stress-testing process.
- Continue testing around it; log recurrence frequency and affected sources.
- Include final remediation recommendation in end-of-test report.

## Key Findings Summary (to date)

Scope: active stress-testing cycle on 2026-03-11 (3-hour protocol, stopped early by operator after sufficient data).

### A) Test harness stability finding

- Initial stress runner failed immediately due to Windows console encoding (`UnicodeEncodeError` from emoji log prefixes under cp1252).
- Fix applied in test harness: logging switched to encoding-safe prefixes + ASCII fallback.
- Result: runner can stay alive and continue execution paths without console-encoding crashes.

### B) Dominant error class observed

- Most frequent issues were Telegram transport errors:
  - `httpx.ConnectError`
  - `httpx.ReadError`
- Severity: **Low–Medium** (usually transient; affects delivery reliability more than backend generation correctness).
- Impact: intermittent missed/delayed outbound sends, noisy error logs, reduced confidence in pass/fail if not separated from backend status.

### C) Branch-specific delivery defect (confirmed)

- `agent-dev` produced successful backend results for normal API prompts, while failing on system/fyi-triggered sends with:
  - `telegram.error.BadRequest: Chat not found`
- Severity: **Medium**.
- Interpretation: route/target-chat resolution defect in specific automatic send branches, not a global runtime/backend failure.

### D) Backend-level intermittent failures (non-dominant)

- Observed occasional backend execution errors (e.g., `claude-cli exited with code 1 (no stderr)`, gemini unknown backend error lines).
- Severity: **Medium** (requires retry-aware handling and better branch attribution in reports).
- Pattern: less frequent than transport failures; should be tracked separately from Telegram/network faults.

### E) Testing-process decision applied

- For this phase, issues were treated inside protocol (no out-of-band production hotfixing).
- Continue/stop decisions based on data sufficiency rather than waiting for full-duration completion.

### Recommended next protocol focus

1. Keep transport-vs-backend error separation explicit in reports.
2. Add per-source delivery assertions (`api` vs `system` vs `fyi`) to catch branch regressions earlier.
3. Add retries with capped attempts for transient Telegram failures, but preserve raw first-failure evidence.
4. Prioritize fix validation for `Chat not found` on automatic send branches before next long soak run.

