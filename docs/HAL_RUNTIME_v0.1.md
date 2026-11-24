# HAL v0.1 Runtime — HASHI Implementation Notes

**Status:** staged implementation, NOT production-ready. Language grammar is
author-owned (see `HAL_SPEC_v0.1.md`); this document only describes how the
authorised vocabulary executes.

## Architecture

HAL is an optional Layer-2 Function with zero effect when unused:

- **`hal/language.py`** — author-owned deterministic compiler (unchanged by the
  runtime work; already shipped on this branch).
- **`hal/event_source.py`** — maps the author vocabulary onto the ONE existing
  durable event source (`orchestrator/automation_events.py`). No second event
  system exists.
- **`hal/runtime.py`** — pure planning engine: entry matching, per-execution
  `@agent_trigger` binding, `.`/`;` semantics, correlation, honest failure
  planning. No I/O.
- **`hal/store.py`** — private SQLite file at `bridge_home/hal/hal_v1.sqlite`,
  deliberately outside the Session database. Rules, workflow executions,
  dispatch receipts, consumed-event ledger, per-owner feed cursor.
- **`hal/dispatch.py`** — execution adapters. Prompts ride the existing
  `runtime.enqueue_api_text` admission with a `hal:<exec>:<stage>` idempotency
  key; native commands ride `try_execute_slash_command_text`. HAL never
  implements a second executor.
- **`hal/service.py`** — runtime loop + Workbench API handlers, hosted by the
  existing `WorkbenchApiServer` process.

## Event availability (truthful, never faked)

| HAL event | Source | Status |
|---|---|---|
| `#turn_finish` | real feed `run.completed` | available |
| `#turn_error` | real feed `run.failed` | available |
| `#system_idle` | derived active→idle edge from committed Session evidence | available |
| `#turn_commentary` | no commentary events in the durable feed | **unavailable** |
| `#hashideploy_finish` | no deployment-completion event source | **unavailable** |
| `#reboot_ok` | no reboot-completion event source | **unavailable** |

Validation reports per-stage availability and never pretends an unavailable
event can trigger. A workflow may be enabled when its **entry** event is
available; stages waiting on unavailable events stay `active` with an honest
detail (`waiting for #reboot_ok (no reboot-completion event source exists)`).

## Semantics decisions (all evidence-based, no new vocabulary)

1. **No history replay.** Enabling a rule anchors the per-owner feed cursor at
   enable time; only events committed afterwards can trigger.
2. **Recursion guard.** Runs dispatched by automation carry `hal:` or `hea:`
   idempotency prefixes; the feed marks them `automation_origin` and entry
   matching skips them. Dependent stages may still correlate their own
   dispatched run.
3. **`;` correlation.** A dependent stage advances only when the triggering run's
   idempotency key equals the previous stage's dispatch key. Unrelated runs —
   including other deployments — never advance the chain. System-event stages
   (`#system_idle`, `#reboot_ok`, ...) carry no per-workflow correlation
   evidence and therefore never auto-advance.
4. **`@agent_trigger` binding.** Bound per execution from the triggering event's
   canonical identity (`@name@instance`), never through a shared global. On
   system events there is no trigger Agent; a prompt referencing
   `@agent_trigger` fails that stage honestly.
5. **`#system_idle` edge.** Emitted only when a non-automation run terminal
   event arrives and the Session store shows no active runs, once per
   transition batch (per-page latch + run-identity latch cleared when activity
   resumes). It never fires on a permanently idle instance without a real
   transition, and automation runs cannot re-arm it.
6. **Native commands.** Executed through the existing slash-command path with
   the trigger Agent's runtime (or, for system events, the deterministic first
   runtime by sorted name). Native commands have no idempotency key: a failed
   or ambiguous outcome is **never auto-retried** and is recorded honestly for
   manual verification.
7. **Prompt retries.** Prompt dispatch is idempotent via the run idempotency
   key; transient failures retry with the same key until accepted.
8. **Crash recovery.** Receipts are written before dispatch; replays reconcile
   stage/state divergence without double execution. Command receipts left
   `unknown` by an interruption are resolved as failed-with-note, never re-run.
9. **Zero cost when disabled.** With no enabled rules there is no loop task and
   no polling. The loop exits when the last rule is disabled. Enabled rules
   cost one bounded feed check per ≤5s (the existing HEA long-poll precedent),
   plus event-driven wake.

## API (authenticated Workbench API, owner-scoped, admin-only)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/hal/status` | availability + enabled-rule count + per-event availability |
| POST | `/api/v1/hal/validate` | compile + availability diagnostics (never persists) |
| GET | `/api/v1/hal/rules` | rule list with validation summaries |
| POST | `/api/v1/hal/rules` | create/update (revision-aware, 409 on conflict) |
| POST | `/api/v1/hal/rules/{id}/enable` | enable (entry event must be available) |
| POST | `/api/v1/hal/rules/{id}/disable` | disable |
| GET | `/api/v1/hal/runs` | workflow executions |
| GET | `/api/v1/hal/runs/{exec_id}` | execution detail with receipts |

All endpoints reuse the existing admin authentication (`_v1_owner_id` +
`_check_admin_auth`), the same trust boundary as the HEA event feed. Request
bodies are bounded (64 KiB scripts, 413 before unbounded reads).

## Boundaries honoured

- Protected Core untouched (verified by
  `scripts/check_protected_core_changes.py --base origin/main`).
- No new process, supervisor, scheduler, permission layer, polling worker or
  timeout was introduced.
- The only change outside `hal/` and its tests is additive: the existing event
  feed now also reports `run_idempotency_key` and marks `hal:`-prefixed runs
  as automation-origin, and `WorkbenchApiServer` registers HAL routes plus one
  guarded startup hook. HAL import failures leave the gateway fully functional.
