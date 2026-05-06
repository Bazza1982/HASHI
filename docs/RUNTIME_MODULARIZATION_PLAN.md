# HASHI Runtime Modularization Plan

Status: planning checkpoint. This document is intentionally non-invasive and
does not change runtime behavior.

## Goals

HASHI currently has two large runtime implementations:

- `orchestrator/agent_runtime.py`: legacy fixed-backend runtime, 5,411 lines.
- `orchestrator/flexible_agent_runtime.py`: current flexible runtime, 9,926 lines.

The live fleet now uses flexible agents. The long-term goal is to retire the
legacy fixed runtime and split the flexible runtime into smaller, modular,
testable components while preserving reboot behavior, audit mode, Anatta,
wrapper mode, Workbench, API Gateway, Hchat, transfer, media handling, and
existing Telegram command behavior.

## Current Findings

### Live runtime selection

`orchestrator/agent_lifecycle.py` builds runtimes as follows:

- `agent_cfg.type in {"flex", "limited"}` -> `FlexibleAgentRuntime`
- any other type -> `BridgeAgentRuntime`

Current `agents.json` contains 18 active agents, all with `type = "flex"`.
There are no active fixed agents in the current live configuration.

### Legacy runtime is still a dependency

Even if no live agent uses `BridgeAgentRuntime`, `agent_runtime.py` cannot be
deleted directly because it still provides shared symbols used elsewhere:

- `QueuedRequest`
- `_safe_excerpt`
- `_md_to_html`
- `_print_user_message`
- `_print_final_response`
- `_print_thinking`
- `resolve_authorized_telegram_ids`
- `_build_jobs_with_buttons`
- `_build_jobs_text`
- `_show_logo_animation`
- `AVAILABLE_GEMINI_MODELS`
- `AVAILABLE_OPENROUTER_MODELS`
- `AVAILABLE_CLAUDE_MODELS`
- `AVAILABLE_CODEX_MODELS`

Known imports include:

- `orchestrator/flexible_agent_runtime.py`
- `orchestrator/api_gateway.py`
- `tests/test_flexible_habits.py`
- `tests/test_wrapper_commands.py`
- legacy-specific tests for `BridgeAgentRuntime`

### Configuration still defaults to fixed

`orchestrator/config.py` currently treats a missing agent `type` as `"fixed"`.
That is a compatibility hazard. Removing the fixed runtime before changing this
default would cause older configs to keep selecting legacy behavior implicitly.

### File density issue

`FlexibleAgentRuntime` is no longer just a runtime loop. It now contains:

- lifecycle and initialization
- queueing and request metadata
- Telegram handler binding
- dozens of command handlers
- media handling
- Workbench/API/Hchat routing
- transfer and handoff flows
- habit, skill, workzone, memory, and status UI
- wrapper mode
- audit mode
- Anatta/post-turn observer hooks
- backend streaming and delivery
- background completion
- the main `process_queue` turn pipeline

This makes the file difficult to reason about and increases risk when changing
unrelated features.

## Non-Goals

The modularization should not:

- rewrite the runtime from scratch;
- change live agent behavior in one large patch;
- remove compatibility shims before references are cleaned up;
- move private command implementations into the public repo;
- merge unrelated HASHI2 scripts or soul deployment tools;
- make Anatta, audit mode, or wrapper mode mutually exclusive;
- break hot restart or `/reboot min` expectations.

## Design Principles

1. Keep the core runtime small.
2. Move feature code behind explicit module boundaries.
3. Preserve default no-op behavior for optional systems.
4. Prefer additive extraction before deletion.
5. Keep logs comprehensive at module boundaries.
6. Commit in small reversible checkpoints.
7. Keep legacy compatibility until tests and live config prove it is unused.

## Phase 0: Baseline and Safety Check

Purpose: make the current dependency graph explicit before moving code.

Actions:

1. Record current line counts and runtime selection behavior.
2. Confirm all active agents are `flex`.
3. Confirm no external scripts instantiate `BridgeAgentRuntime` directly.
4. Record all tests that still instantiate or import `BridgeAgentRuntime`,
   including:
   - `tests/test_agent_runtime_job_transfer.py`
   - `tests/test_usecomputer_mode.py`
   - `tests/test_fresh_context.py`
5. Document transcript filename divergence before retiring fixed runtime:
   - legacy fixed runtime writes `conversation_log.jsonl`
   - flexible runtime writes `transcript.jsonl`
   - `ticket_manager.py` currently assumes `transcript.jsonl`
6. Add or update tests that protect current flex behavior before refactoring.
7. Keep existing dirty/unrelated work out of this migration.

Suggested checks:

```bash
git status --short
wc -l orchestrator/agent_runtime.py orchestrator/flexible_agent_runtime.py
rg "BridgeAgentRuntime|from orchestrator\.agent_runtime|QueuedRequest" orchestrator tests
pytest -q tests/test_command_registry.py tests/test_audit_mode.py tests/test_flexible_runtime_observers.py
```

Exit criteria:

- Current runtime selection is documented.
- Shared imports from `agent_runtime.py` are known.
- Bridge-runtime test dependencies are listed before shims are moved or removed.
- Transcript filename assumptions are documented before retirement work begins.
- No behavior changes have been made.

## Phase 1: Extract Shared Runtime Primitives

Purpose: make `FlexibleAgentRuntime` independent from legacy runtime helpers.

Create small modules:

- `orchestrator/runtime_common.py`
  - `QueuedRequest`
  - `_safe_excerpt`
  - `_md_to_html`
  - console print helpers
  - `resolve_authorized_telegram_ids`

- `orchestrator/runtime_jobs.py`
  - `_build_jobs_with_buttons`
  - `_build_jobs_text`

- `orchestrator/runtime_display.py`
  - `_show_logo_animation`

- `orchestrator/model_catalog.py`
  - `AVAILABLE_GEMINI_MODELS`
  - `AVAILABLE_OPENROUTER_MODELS`
  - `AVAILABLE_CLAUDE_MODELS`
  - `AVAILABLE_CODEX_MODELS`

Then update:

- `flexible_agent_runtime.py`
- `api_gateway.py`
- tests that import `QueuedRequest`

Keep `agent_runtime.py` temporarily re-exporting the moved symbols so older
imports continue to work.

Suggested commit:

```text
Extract shared runtime primitives
```

Exit criteria:

- `FlexibleAgentRuntime` no longer imports shared helpers from
  `orchestrator.agent_runtime`.
- API Gateway imports models from `model_catalog`.
- Legacy tests still pass through the compatibility exports.

## Phase 2: Quarantine the Legacy Fixed Runtime

Purpose: make legacy runtime visibly separate from the active flex runtime.

Actions:

1. Move `BridgeAgentRuntime` into a legacy module, for example:
   - `orchestrator/legacy/bridge_agent_runtime.py`
2. Keep `orchestrator/agent_runtime.py` as a compatibility shim:
   - re-export `BridgeAgentRuntime`
   - re-export any remaining compatibility symbols
   - include a clear deprecation comment
3. Update `agent_lifecycle.py` to import the legacy runtime from the new path.
4. Log a warning if a fixed runtime is instantiated.
5. Add config validation that reports agents missing an explicit `type`.
   This belongs with Phase 2 so there is no window where quarantined legacy code
   and silent `"fixed"` fallback combine unexpectedly.

Suggested commit:

```text
Quarantine legacy fixed runtime
Warn on implicit fixed agent type
```

Exit criteria:

- Flex runtime does not depend on legacy runtime.
- Fixed runtime still works if explicitly configured.
- Legacy usage is visible in logs.
- Missing agent `type` fields are reported by validation or startup warnings.

## Phase 3: Make Flex the Explicit Default

Purpose: remove accidental legacy selection while preserving compatibility.

Current risk:

- Missing `type` currently means `"fixed"`.

Recommended path:

1. Update docs to require explicit `type`.
2. Change missing `type` behavior from silent `"fixed"` to warning-first if
   this was not fully handled during Phase 2.
3. Later, change the default to `"flex"` only after compatibility review.
4. Defer any `HASHI_ENABLE_LEGACY_FIXED_RUNTIME=1` feature flag until Phase 7
   retirement work. Adding a flag too early creates operational overhead before
   there is an active fixed-runtime user to protect.

Suggested commits:

```text
Warn on implicit fixed agent type
Document explicit agent runtime types
```

Exit criteria:

- No current config depends on implicit fixed type.
- Fixed runtime cannot be selected accidentally.

## Phase 4: Split Flexible Runtime by Feature Area

Purpose: reduce file density without changing behavior.

Recommended extraction order:

1. Observer hooks
   - Move Anatta/post-turn provider scheduling into `runtime_observers.py`.
   - Keep default no-config behavior as no-op.

2. Delivery and display
   - Move `send_long_message`, typing indicators, HTML conversion, streaming
     placeholders, and Telegram delivery helpers into `runtime_delivery.py`.

3. Media handling
   - Move document/photo/voice/audio/video/sticker handlers into
     `runtime_media.py`.

4. Remote and transfer features
   - Move remote peer status, remote start/stop, move/transfer, and handoff
     helpers into `runtime_remote.py` and `runtime_transfer.py`.

5. Wrapper helpers
   - Move wrapper polishing, wrapper context extraction, wrapper verbose trace,
     and wrapper delivery placeholders into `runtime_wrapper.py`.

6. Audit helpers
   - Move audit telemetry, audit evidence writing, audit follow-up scheduling,
     and audit model-selection helpers into `runtime_audit.py`.
   - Keep audit separate from wrapper code because audit evidence is a higher
     sensitivity boundary and should remain easy to review independently.

7. Workzone, habit, skill, and status helpers
   - Move these into focused modules with narrow public functions.

Implementation style:

- Prefer plain helper modules that accept a runtime/context object.
- If mixins are used, treat them as transitional and keep inheritance shallow.
- Do not move `process_queue` until the surrounding helpers are extracted.

Exit criteria:

- Each extraction commit moves one feature area only.
- Existing tests pass after each commit.
- Logs at module boundaries remain clear.

## Phase 5: Replace Command Handler Density with Command Modules

Purpose: make commands plug-and-play and hot-reboot friendly.

Implementation note:

- Static flexible-runtime Telegram handler binding now lives in
  `orchestrator/runtime_command_binding.py`.
- Built-in command menu metadata also lives in
  `orchestrator/runtime_command_binding.py`.
- `FlexibleAgentRuntime.bind_handlers()` and `get_bot_commands()` remain as
  compatibility shims.
- Public/private runtime command modules are still loaded through
  `orchestrator.command_registry`.
- `/reboot min` stops and starts the target runtime after reloading project
  modules, so static handlers and command menu metadata are refreshed on the
  rebuilt runtime. Already-bound handlers on a still-running Telegram
  `Application` are not mutated in place.

HASHI already has `orchestrator.command_registry` and private command loading.
Built-in commands should gradually use the same pattern.

Actions:

1. Define built-in command modules grouped by domain:
   - lifecycle: `/start`, `/stop`, `/terminate`, `/reboot`, `/new`, `/fresh`,
     `/wipe`, `/reset`, `/clear`
   - backend/core: `/mode`, `/backend`, `/model`, `/effort`, `/core`
   - wrapper/audit: `/wrap`, `/wrapper`, `/audit`
   - Anatta: `/anatta`
   - memory/workzone/habit/skill
   - remote/move/transfer/handoff
   - Hchat/COS/WA/WOL
   - status/help/token/usage
2. Keep Telegram callback handlers grouped with their command modules where
   practical.
3. Keep `bind_handlers()` as a small registry binder rather than a long list of
   inline command registrations.
4. Specify the reload contract for command modules:
   - static Telegram handler binding may remain startup-only;
   - command metadata and implementation modules should be reloadable where
     safe;
   - if `/reboot min` is expected to pick up a command change,
     `bind_runtime_commands()` or its replacement must run during the reboot
     path, not only during cold initialization.

Exit criteria:

- Adding a built-in command does not require editing the main runtime class.
- Private and built-in command loading share compatible abstractions.
- `/reboot min` can pick up command module changes where safe.

## Phase 6: Slim the Turn Pipeline

Purpose: make `process_queue` understandable and testable.

Introduce a `RequestPipeline` or `TurnProcessor` with explicit stages:

1. receive/dequeue
2. source filtering and internal-source policy
3. session primer and transfer prefix handling
4. context assembly
5. prompt assembly
6. backend generation
7. wrapper/audit/Anatta orchestration
8. transcript and memory persistence
9. delivery
10. error handling and maintenance logging

Each stage should:

- accept an explicit context object;
- emit structured debug logs;
- fail in a controlled way;
- be unit-testable outside Telegram.

Exit criteria:

- `process_queue` becomes an orchestration wrapper rather than a large
  implementation body.
- Audit, wrapper, and Anatta can run together without implicit ordering bugs.
- Queue processing can be tested with fake backends and fake delivery.

## Phase 7: Retire Fixed Runtime

Purpose: remove `BridgeAgentRuntime` only after it is demonstrably unused.

Retirement requirements:

1. No active agents use fixed runtime for a sustained trial period.
2. `rg "BridgeAgentRuntime"` has no production references except a legacy shim
   or archival docs.
3. Workbench and API Gateway no longer assume fixed transcript names.
4. Config validation blocks accidental fixed runtime selection.
5. CI covers flexible lifecycle, reboot, queue processing, wrapper, audit, and
   Anatta.

Possible final states:

- preferred: remove `BridgeAgentRuntime` from active source;
- conservative: keep it in `orchestrator/legacy/` behind an explicit feature
  flag only if a real operator need appears during retirement;
- archival: keep a historical copy outside runtime import paths.

Exit criteria:

- `orchestrator/agent_runtime.py` is under 200 lines as a shim or removed.
- No active code imports shared primitives from legacy runtime.

Implementation note:

- HASHI uses the conservative final state first: `BridgeAgentRuntime` remains in
  `orchestrator/legacy/` and `orchestrator/agent_runtime.py` remains a short
  compatibility shim.
- Config loading rejects omitted `type` values and blocks explicit
  `type: "fixed"` unless `HASHI_ENABLE_LEGACY_FIXED_RUNTIME=1` is set.
- Offline Workbench and agent-directory metadata must not infer fixed runtime
  from a missing `type`; missing type is invalid configuration.

## Phase 8: Acceptance Metrics

Target outcomes:

- `flexible_agent_runtime.py` under 2,500 lines after first modularization pass.
- Longer-term target: under 1,500 lines.
- `agent_runtime.py` under 200 lines as a compatibility shim, or removed.
- No flex code imports from `orchestrator.agent_runtime`.
- Built-in commands are registry-backed.
- Optional systems are default no-op without config.
- All major runtime modes remain compatible:
  - normal flex
  - wrapper mode
  - audit mode
  - Anatta
  - Anatta + audit mode
  - Workbench local mode
  - API Gateway
  - Hchat and transfer

Recommended checks:

```bash
python -m py_compile orchestrator/*.py orchestrator/anatta/*.py
pytest -q tests/test_command_registry.py
pytest -q tests/test_audit_mode.py tests/test_anatta_audit_compatibility.py
pytest -q tests/test_flexible_runtime_observers.py
pytest -q tests/test_agent_runtime_job_transfer.py
pytest -q tests/test_wrapper_commands.py tests/test_wrapper_mode.py
```

## Risk Register

High risk:

- moving `process_queue` too early;
- rewriting `bind_handlers()` in one large patch;
- changing config default from fixed to flex without migration;
- deleting legacy runtime while it still owns shared helpers.

Medium risk:

- transcript filename assumptions between fixed and flex runtimes;
- Workbench/API Gateway references to old runtime symbols;
- tests that instantiate runtime classes via `__new__`;
- callback handlers split away from their command state.

Low risk:

- stale docs mentioning fixed runtime;
- helper names remaining private-style after extraction;
- legacy tests requiring compatibility shims longer than expected.

## Recommended Immediate Next Step

Start with Phase 1 only:

1. Extract shared primitives out of `agent_runtime.py`.
2. Update flex/API/tests imports.
3. Keep compatibility re-exports.
4. Run targeted tests.
5. Commit the extraction.

This reduces coupling immediately and creates the safe foundation needed before
quarantining or deleting any legacy runtime code.
