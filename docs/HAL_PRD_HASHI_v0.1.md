# HAL Runtime PRD — HASHI
**Version:** 0.1 | **Date:** 2026-11-23 | **Status:** design + staged implementation; NOT production-ready
**Language owner:** HASHI author. Normative grammar: [HAL_SPEC_v0.1.md](HAL_SPEC_v0.1.md).
**Paired Workbench branch:** `feat/hal-canvas-kasumi-retirement-20261123`.

## Product outcome and scope
HAL is an **optional, HASHI-owned, event-driven, deterministic automation interpreter**; Workbench is an optional Canvas authoring/visualization client. HAL must work without a Workbench tab, Workbench process or any specific Agent being online. Existing HASHI functions must work normally without HAL installed, enabled or reachable. Avoid feature creep: first release uses ONLY the vocabulary approved by the author; no WAIT/EVERY/REPEAT, generic JS/Python, custom authorization regime, independent agent/model loop or global scheduler.

### Vocabulary frozen by author
- Agents: `@agent_a`, `@agent_b` (meaning/alias unresolved), `@agent_any` (wildcard), `@agent_trigger` (bound per run), `@name@instance`/specific ID.
- Events: `#turn_finish`, `#turn_error`, `#turn_commentary`, `#system_idle`, `#hashideploy_finish`, `#reboot_ok`.
- HAL actions: `$prompt`, `$stop`. Existing HASHI native `/` commands may be dispatched through their real command interface only.
- Grammar: `if ... then ... [else ...]`; quoted prompt strings; `.` ends an independent workflow; `;` means the NEXT rule depends on the current workflow stage. `else` execution semantics remain unresolved; accept only if deterministic and author-approved, otherwise reject explicitly.
- Examples in the normative specification are **design examples**, not proof that corresponding HASHI events or commands exist.

## Functional ownership and layering
- **Owner:** independently replaceable HAL automation Function / optional adapter, engineering Layer 2 under `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`. HAL must NOT change, import into, monkeypatch or make initialization mandatory for Protected Core, PCM/PAO/HERV3, or existing Session API. If a general-purpose module is needed, keep it pure and dependency-free with no automatic startup.
- **Core-independent interpreter:** tokenizer/parser -> immutable typed AST -> validator -> event matcher -> per-execution context -> action dispatcher interface. No LLM to interpret grammar; parser must reject unsupported tokens with line/column diagnostics.
- **Event ingestion:** adapt existing owner-scoped `orchestrator/automation_events.py` durable event feed and Session APIs, with one source of event truth. Existing feed currently projects committed run.completed/run.failed only; do not claim other events are live. Gate activation of unsupported event names while allowing offline draft and validation.
- **Action dispatcher:** inject existing authenticated native command / Session Run admission adapters. Do NOT implement a second Agent executor. `/reboot max` exists in `orchestrator/command_specs.py`; verify its actual invocation and scope. `/hashideploy` was **not** verified in command catalogue: treat as illustrative until real adapter proven. Never equate an accepted command with a successful deployment/restart.
- **Storage:** HAL script revision, enabled state, event cursor, workflow execution ID, stage, source event identity, resolved Agent identity, command correlation and idempotent dispatch receipts. One durable owner per execution; avoid redundant stores or global locks unrelated to actual correctness. Store away from existing production Session data when testing.
- **API:** add small versioned HAL endpoints through an existing authenticated gateway; suggested `validate`, `list/get/upsert`, `enable/disable`, `runs/status` (exact routes are design, not yet approved API). Revision-aware writes prevent silent lost updates. Response must distinguish `supported` vs `unavailable`, `accepted` vs `completed`, `failed` vs `unknown`.
- **Runtime off-by-default unless author explicitly enables a saved HAL workflow**; zero enabled workflows => zero ongoing HAL polling/heartbeat/locks. No central readiness gate on ordinary HASHI operations.

## Deterministic semantics and PIT
1. `@agent_any` matches within a well-defined HASHI instance scope, and binds `@agent_trigger` to the **actual authenticated instance + Agent** at the instant the event is admitted. Each concurrent matching event gets a separate binding.
2. `#system_idle` is an edge (active -> idle), not a condition repeatedly polled while idle; avoid cold-boot replay and self-triggering reboot/deploy loops.
3. `;` creates a stateful dependency: a later `if` does not subscribe as an independent top-level trigger. Later events must be causally correlated to the current workflow execution and preceding issued action, not just match the same event name.
4. `.` closes the chain, making the following `if` a new independent top-level rule.
5. Native commands are never silently authorized by HAL; reuse existing command routing and operational permissions, without adding unnecessary new security gates. In test, never call production `/reboot` or `/hashideploy`.
6. Restart/crash: durable state must not confuse accepted with failed; reconcile uncertain run admission before replay, using existing HEA lessons. Duplicate event delivery must not duplicate side effects.
7. Unknown template variables remain literal or produce precise errors according to author-approved final lexical decision; never expand arbitrary `@...` text or dollar values. Never execute arbitrary code.
8. Invalid, disabled, unsupported or HAL-unavailable state affects HAL ONLY. No changes to normal Agent turn latency, startup, command execution, Workbench connectivity or UI.
9. Treat `else`, `$stop`, commentary granularity, cross-instance wildcard, timeout policy and exact deploy/reboot result events as **author decisions pending**, not developer-invented capabilities.
10. No fake green: syntax validity != subscription readiness != command dispatch != operational success.

## Existing HEA migration plan
- Inventory current Workbench-owned HEA rules, journal, delivery receipts and `automation_origin` protection. Do not delete data or interrupt active rules before stable equivalent HAL exists.
- Reuse HEA event contracts, idempotency/reconciliation patterns and tests, **not** its parallel rule editor/executor as a permanent second engine.
- Provide an explicit opt-in per-rule migration strategy (paused/draft first). One active executor per rule; prevent HAL and legacy HEA executing the same rule concurrently.
- Keep an export/rollback path. Old Workbench without HAL must still function; HASHI without Workbench must execute activated HAL scripts.

## Delivery phases — local agent
- **P0 (foundation):** review `ARCHITECTURE.md`, `AGENTS.md`, layered boundaries, testing policy, command source, HEA contract. Publish grammar tests and pure parser; no event dispatch/side effects.
- **P1 (opt-in dry run):** validate + compile + resolve wildcard + pure event matcher with fixture-driven tests; return explicit unsupported-event diagnostics.
- **P2 (isolated runtime):** inject real event adapter, durable per-workflow state, command/session adapters, correlated chain semantics, idempotent restart recovery. No changes to Core.
- **P3 (Workbench contract):** small API, stable error codes, source/connection capability discovery. Workbench absence no impact.
- **P4 (non-prod integration):** isolated HASHI + Workbench, no HASHI4, real API and browser E2E; confirm feature disabled path and Core fingerprint untouched.

## Acceptance matrix / evidence
- Syntax: `@agent_any`, `@agent_trigger`, `@rika@hashi4`, `#system_idle`, `$prompt`, `/reboot max`, chained `;` and independent `.`; invalid keywords/error location; quotes containing `;`/`.` safe.
- Functional: concurrent Agents don't cross-bind; repeated events no duplicates; unrelated `#reboot_ok` does not advance chain; restart resume works; stopped/disabled rules behave correctly; HAL unavailable leaves normal HASHI green.
- Benchmarks: zero enabled HAL = zero HAL polling and no measurable overhead on normal Agent paths. Do not add an always-on busy loop.
- Evidence: exact branch SHA; targeted tests and failures/skips; Protected Core guard; isolated process logs, accepted vs completed receipts; no assumed production test.
- Full PIT: Stupid Dependencies, UI (through Workbench), Logic, Switches, Tests, Copy, Safety, and Stupid Repetition.
- **Exit:** reviewed linked Draft PR, test receipts, named residuals; NO merge, HASHI4 restart or production deployment until separately authorized.

## Deferred, not authorized
Do not design more grammar, periodic schedulers, generalized scripting, network-wide event discovery, supervisor replacement, or security admission systems. The language author personally decides additional vocabulary.
