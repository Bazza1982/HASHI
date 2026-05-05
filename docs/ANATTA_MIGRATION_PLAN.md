# Anatta Migration Plan

This document describes how to migrate the experimental Anatta architecture
from HASHI2 into HASHI1 without merging the HASHI2 worktree directly. The goal
is to preserve HASHI1's slim, modular runtime while allowing Anatta and audit
mode to run at the same time.

## Current Finding

Anatta should not be merged directly from HASHI2 into HASHI1.

HASHI2 contains useful Anatta work, but the branch also contains unrelated
launch-script changes, old lifecycle edits, a separate workspace command
registry, and a backend-manager change that conflicts with HASHI1's audit and
wrapper sidecar requirements. The migration should cherry-pick Anatta concepts
and modules, then reconnect them to HASHI1's current runtime APIs.

## Goals

- Add Anatta as an optional, workspace-scoped capability.
- Keep HASHI1 core minimal and hot-reboot friendly.
- Let Anatta run in `off`, `shadow`, and `on` modes.
- Let Anatta and audit mode run together without requiring wrapper mode.
- Let audit mode inspect the visible personality impact of Anatta-influenced
  core replies.
- Keep Anatta failures isolated: no failed observer task should block user
  response delivery.
- Preserve backwards compatibility when Anatta is absent or disabled.

## Non-Goals

- Do not merge the whole HASHI2 branch.
- Do not port HASHI2's `flexible_backend_manager.generate_ephemeral_response`
  change. HASHI1 needs cross-backend ephemeral sidecars for audit and wrapper
  support.
- Do not port HASHI2's launch-script changes as part of this migration.
- Do not introduce a second command registry. Any Anatta commands should use
  HASHI1's existing `orchestrator.command_registry` path.
- Do not make wrapper mode a dependency for Anatta. Wrapper can remain off.

## Desired Architecture

Anatta should be implemented as two generic runtime extensions:

- a pre-turn context provider, used only when Anatta is in `on` mode
- a post-turn observer, used when Anatta is in `shadow` or `on` mode

The core runtime should know only about generic protocols, not concrete Anatta
classes. Workspace configuration decides whether Anatta is loaded.

Example workspace config:

```json
{
  "observers": [
    {
      "factory": "orchestrator.anatta.post_turn_observer:build_post_turn_observer",
      "enabled": true
    }
  ]
}
```

Example Anatta mode config:

```json
{
  "mode": "shadow"
}
```

Mode behavior:

| Mode | Pre-turn prompt section | Post-turn annotation | User-visible impact |
|---|---|---|---|
| `off` | No | No | None |
| `shadow` | No | Yes | None |
| `on` | Yes | Yes | Yes |

## Audit Compatibility

Anatta and audit mode should compose in the main response pipeline instead of
wrapping each other.

Recommended foreground order:

1. Receive user turn.
2. Build prompt extra sections:
   - workzone section
   - active habit sections
   - Anatta pre-turn sections, only when Anatta mode is `on`
3. Generate the core assistant response.
4. Deliver the normal visible response. If wrapper mode is disabled,
   `visible_text == core_raw`.
5. Record core transcript and memory using the existing HASHI1 policy.
6. Schedule Anatta post-turn observation, if enabled.
7. Schedule audit follow-up, if audit mode is enabled for the source.

This ordering lets audit mode review the actual assistant text that the user
sees after Anatta has influenced the core prompt. Audit mode should audit the
behavior and output, not replace or hide Anatta. Wrapper mode is not required.

The audit follow-up should keep receiving:

- `core_raw`
- `visible_text`
- telemetry from streaming/tool execution
- source metadata

It should not require the full private Anatta injection text. If audit needs
extra context later, add a small telemetry field such as `anatta_mode` and
`anatta_injected=true`, not the full internal Anatta prompt body by default.

## Source Policy

Anatta should default to user-facing conversational sources only. It should not
inject into system, startup, bridge maintenance, scheduler, or internal control
traffic unless explicitly enabled later.

Recommended default:

- provide pre-turn context for normal user conversation sources when mode is
  `on`
- observe normal user conversation sources when mode is `shadow` or `on`
- skip bridge, startup, system, scheduler, and other automation/control sources

This keeps Anatta focused on personality and relationship effects in real
conversation while avoiding hidden changes to operational messages.

## Migration Phases

### Phase 0: Baseline and Rollback Point

1. Confirm HASHI1 worktree is clean.
2. Record current HEAD as the rollback point.
3. Run targeted baseline tests:

```bash
pytest -q tests/test_command_registry.py tests/test_audit_mode.py tests/test_agent_runtime_job_transfer.py
pytest -q tests/test_wrapper_commands.py tests/test_wrapper_mode.py tests/test_flexible_backend_state.py
```

Expected outcome: a known-good HASHI1 baseline before any Anatta code enters
the repo.

### Phase 1: Port Anatta Core Modules Without Runtime Integration

Cherry-pick the Anatta domain modules from HASHI2:

- `orchestrator/anatta/models.py`
- `orchestrator/anatta/config.py`
- `orchestrator/anatta/memory.py`
- `orchestrator/anatta/aggregation.py`
- `orchestrator/anatta/relationship.py`
- `orchestrator/anatta/llm_interpreter.py`
- `orchestrator/anatta/composer.py`
- `orchestrator/anatta/bootstrap.py`
- `orchestrator/anatta/bridge_adapter.py`
- `orchestrator/anatta/layer.py`

Do not integrate these modules into runtime flow yet.

Bring over focused tests first:

- `tests/test_anatta_semantic_calibration.py`

Then run:

```bash
pytest -q tests/test_anatta_semantic_calibration.py
python -m py_compile orchestrator/anatta/*.py
```

Expected outcome: Anatta core is importable and testable while inactive.

### Phase 2: Add Generic Observer Interfaces

Add generic protocols, not Anatta-specific hooks:

- `orchestrator/post_turn_observer.py`
- `orchestrator/post_turn_registry.py`

The registry should load observer factories from workspace config and pass:

- `workspace_dir`
- `bridge_memory_store`
- `backend_invoker`
- `backend_context_getter`
- optional observer options

Add tests for:

- missing config returns no observers
- invalid config logs warnings and continues
- disabled observers are ignored
- valid factory loads successfully

Expected outcome: the core runtime can load optional observers without knowing
about Anatta.

### Phase 3: Integrate Generic Hooks Into HASHI1 Runtime

Modify HASHI1's current runtime manually. Do not copy HASHI2's old runtime file.

Add runtime state:

- `_post_turn_observers`
- `_pre_turn_context_providers`

Add lifecycle method:

- `reload_post_turn_observers()`

Call it during initialization and during `/reboot min` or equivalent runtime
reload paths.

Add pre-turn hook before `context_assembler.build_prompt_payload(...)`:

```python
extra_sections = self._workzone_prompt_section() + habit_sections
extra_sections += await self._build_pre_turn_context_sections(item)
```

Add post-turn hook after the core response is available and memory policy has
selected the assistant text:

```python
self._schedule_post_turn_observers(...)
```

The hook should receive the actual assistant text for the turn. When wrapper is
off, this is the same visible response. When wrapper is on, keep HASHI1's
current memory/core policy explicit and test it.

Preserve workspace files during reset/wipe:

- `post_turn_observers.json`
- `anatta_config.json`

Expected outcome: generic observer infrastructure exists, but Anatta remains
optional and config-gated.

### Phase 4: Add Anatta Observer Adapter

Port:

- `orchestrator/anatta/post_turn_observer.py`

Adapt it to HASHI1's source policy and logging conventions.

Required behavior:

- `shadow`: records post-turn annotations, no prompt injection
- `on`: provides pre-turn context and records post-turn annotations
- `off`: does nothing
- failed Anatta background task logs a warning and does not affect user delivery
- state cache has a TTL and max size

Expected outcome: Anatta can be enabled per workspace through config.

### Phase 5: Reconcile Commands and Diagnostics

Do not port HASHI2's `extension_command_registry.py` as-is.

Instead, adapt Anatta command support to HASHI1's existing command registry:

- add a public or private `/anatta` runtime command only if needed
- keep diagnostics in `tools/anatta_diagnostics.py`
- ensure diagnostics do not expose sensitive memory content by default

Optional command behavior:

```text
/anatta status
/anatta shadow
/anatta on
/anatta off
```

Expected outcome: Anatta can be operated without adding a parallel command
system.

### Phase 6: Audit Plus Anatta Acceptance Tests

Add tests that prove Anatta and audit mode compose:

- Anatta `shadow` + audit mode:
  - no Anatta prompt section is injected
  - Anatta post-turn observation is scheduled
  - audit follow-up is scheduled
- Anatta `on` + audit mode:
  - Anatta pre-turn section is added before core generation
  - user-visible response is delivered without wrapper
  - audit receives the core/visible response influenced by Anatta
  - Anatta post-turn observation is scheduled
- Anatta disabled:
  - no prompt section
  - no observer task
  - existing audit behavior unchanged
- source filtering:
  - normal user conversation can run Anatta
  - bridge/system/startup/scheduler sources are skipped
- failure isolation:
  - Anatta observer exception logs warning
  - audit still schedules
  - user response still sends

Recommended targeted test command:

```bash
pytest -q \
  tests/test_anatta_semantic_calibration.py \
  tests/test_post_turn_registry.py \
  tests/test_flexible_runtime_anatta_audit.py \
  tests/test_audit_mode.py \
  tests/test_command_registry.py
```

Expected outcome: Anatta and audit mode are explicitly compatible.

### Phase 7: Controlled Workspace Trial

Use one isolated test workspace before enabling live agents.

Create:

```json
{
  "observers": [
    {
      "factory": "orchestrator.anatta.post_turn_observer:build_post_turn_observer",
      "enabled": true
    }
  ]
}
```

Start with:

```json
{
  "mode": "shadow"
}
```

Trial sequence:

1. `/reboot min`
2. Send a normal user conversation turn.
3. Confirm user response is unchanged.
4. Confirm Anatta tables are created in the bridge memory database.
5. Confirm an Anatta annotation is recorded.
6. Enable audit mode for the same agent.
7. Repeat the turn.
8. Confirm audit follow-up still runs.
9. Switch Anatta to `on`.
10. Confirm personality impact is visible in the core response.
11. Confirm audit reviews that visible/core response.

Expected outcome: shadow mode proves safe recording first; on mode proves
Anatta personality impact and audit compatibility.

## Validation Matrix

| Area | Check |
|---|---|
| Import safety | `python -m py_compile orchestrator/anatta/*.py` |
| Anatta semantics | `pytest -q tests/test_anatta_semantic_calibration.py` |
| Observer loading | `pytest -q tests/test_post_turn_registry.py` |
| Runtime integration | `pytest -q tests/test_flexible_runtime_anatta_audit.py` |
| Existing audit | `pytest -q tests/test_audit_mode.py` |
| Commands | `pytest -q tests/test_command_registry.py` |
| Sensitive content | `git grep` for real tokens, credentials, private paths |
| Dirty worktree | `git status --short` |

## Rollback Plan

Every phase should be a separate commit.

Immediate disable options:

- set `anatta_config.json` to `{ "mode": "off" }`
- set the observer config entry to `"enabled": false`
- remove `post_turn_observers.json` from the workspace

Code rollback options:

- revert only the latest phase commit when the failure is isolated
- revert all Anatta migration commits to return to the Phase 0 baseline

The default behavior must remain no-op when no Anatta config exists.

## Recommended Commit Boundaries

1. `docs: plan Anatta migration`
2. `Add inactive Anatta core modules`
3. `Add post-turn observer registry`
4. `Wire observer hooks into flexible runtime`
5. `Add Anatta observer adapter`
6. `Add Anatta diagnostics and command`
7. `Validate Anatta with audit mode`

## Open Decisions

- Whether Anatta should observe HChat conversations. Recommendation: keep it
  disabled for HChat initially unless a specific agent workflow needs it.
- Whether Anatta sidecar interpretation should use the active core model or a
  configured sidecar model. Recommendation: use HASHI1's existing ephemeral
  backend invoker first, then add config later if needed.
- Whether audit reports should include Anatta internals. Recommendation: audit
  output behavior first; expose only small Anatta telemetry fields unless a
  debugging mode is explicitly enabled.
- Whether `/anatta` should be public or private. Recommendation: start private
  or admin-only until the mode switch semantics are stable.
