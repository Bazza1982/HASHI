# HASHI v3.2 Core Slimming Plan

Status: accepted for `v3.2.0` on 2026-05-02. See the implementation status and validation record at the end of this document.

## Goal

Make `main.py` a minimal, stable process bootstrap shell.

All frequently changed behavior should move into hot-reloadable modules under `orchestrator/`, so future feature work can be picked up by `/reboot` without restarting the long-lived `main.py` process.

## Design Principles

- Keep process core minimal and boring.
- Move feature behavior into modular, replaceable managers.
- Preserve existing startup, shutdown, local-mode, and hot-reboot behavior while refactoring.
- Prefer backwards and forwards compatibility unless there is a clear reason to break it.
- Keep logging comprehensive enough for startup, shutdown, reboot, agent lifecycle, and service debugging.
- Make every phase independently testable and revertible.

## Current Problem

`main.py` is currently about 1570 lines and contains both process bootstrap and feature logic.

The `/reboot` hot reload path currently reloads project modules such as:

```python
("adapters.", "orchestrator.")
```

Logic that remains inside `main.py` is not hot-reloaded. This means changes to agent lifecycle, reboot behavior, service startup, WhatsApp control, config administration, and other logic still require restarting the whole `main.py` process.

## Desired Final Shape

`main.py` should keep only process-level responsibilities:

- Parse CLI arguments.
- Build bridge paths.
- Run onboarding gate.
- Configure minimal console encoding/logging bootstrap.
- Acquire and release the single-instance lock.
- Construct the orchestrator/kernel object.
- Run `asyncio.run(...)`.
- Capture top-level fatal crashes.
- Preserve final `os._exit(0)` behavior for stubborn runtime threads.

Original target size:

```text
main.py <= 200 lines
```

Accepted `v3.2.0` outcome: `main.py` is 337 lines. The final shape is a slim kernel wrapper rather than a pure bootstrap-only file.

Most behavior should live in hot-reloadable modules:

```text
orchestrator/bootstrap_logging.py
orchestrator/instance_lock.py
orchestrator/lifecycle_state.py
orchestrator/config_admin.py
orchestrator/backend_preflight.py
orchestrator/agent_lifecycle.py
orchestrator/reboot_manager.py
orchestrator/service_manager.py
orchestrator/startup_manager.py
orchestrator/shutdown_manager.py
orchestrator/whatsapp_manager.py
orchestrator/onboarding_gate.py
```

## Important Architecture Note

Do not simply move the whole `UniversalOrchestrator` class into `orchestrator/orchestrator_core.py` and stop there.

Reloading a module does not automatically update methods already bound to an existing long-lived object. The better design is to keep a thin kernel/orchestrator object and compose hot-reloadable managers:

```python
self.config_admin = ConfigAdmin(self)
self.backend_preflight = BackendPreflight(self)
self.agent_lifecycle = AgentLifecycleManager(self)
self.reboot_manager = RebootManager(self)
self.services = ServiceManager(self)
self.startup_manager = StartupManager(self)
self.shutdown_manager = ShutdownManager(self)
self.whatsapp_manager = WhatsAppManager(self)
```

On `/reboot`, reload modules and rebuild the managers. This allows future manager code changes to take effect without restarting `main.py`.

Manager rebuilds must be transaction-style:

1. Build all replacement managers into local variables.
2. If every manager initializes successfully, commit them to the kernel.
3. If any manager fails, keep the existing manager set alive and log the failed manager clearly.

Managers must not store direct references to other managers during `__init__`. Cross-manager calls must go through the stable kernel object, for example `self.kernel.config_admin`, so a later manager rebuild does not leave stale references behind.

Externally stored callbacks must also go through stable kernel indirection. Signal handlers, scheduler callbacks, Telegram handlers, and asyncio callbacks must not permanently capture old manager bound methods.

Long-lived service handles belong to the kernel, not to replaceable manager objects. Managers control those handles; they do not own their lifetime by default.

## Phase 0: Baseline

Purpose: record a clean safety baseline before structural refactoring.

Checks:

```bash
git status --short --branch
python3 -m py_compile main.py
pytest
curl http://127.0.0.1:18800/api/health
curl http://127.0.0.1:18801/health
```

Expected result:

- Worktree is clean before each phase.
- Full tests pass.
- Current running HASHI health endpoints are known.
- Any current warnings are documented separately from refactor regressions.

## Phase 1: Extract Bootstrap Infrastructure

Low-risk extraction.

Create:

```text
orchestrator/bootstrap_logging.py
orchestrator/instance_lock.py
orchestrator/lifecycle_state.py
```

Move:

- Console encoding setup.
- `ColorFormatter`.
- Console output filter.
- Bridge audit line writing.
- Bridge audit emit helper.
- `InstanceLock`.
- Orchestrator state read/write helpers.
- Startup/shutdown state marking.
- Shutdown metadata formatting.

Keep behavior identical.

Validation:

```bash
python3 -m py_compile main.py orchestrator/bootstrap_logging.py orchestrator/instance_lock.py orchestrator/lifecycle_state.py
pytest
```

Suggested commit:

```text
Refactor bootstrap infrastructure out of main
```

## Phase 2: Extract Config Administration and Backend Preflight

Create:

```text
orchestrator/config_admin.py
orchestrator/backend_preflight.py
```

Move config admin:

- Raw config load/write.
- Get all agents.
- Set agent active/inactive.
- Delete agent from config.
- Add agent scaffold to config.
- Configured agent names.
- Startable agent names.

Move backend preflight:

- OpenRouter key lookup.
- CLI backend availability checks.
- Flex/fixed agent availability partitioning.

Validation:

```bash
pytest tests/test_commands.py tests/test_flexible_habits.py
pytest
```

Suggested commit:

```text
Move config admin and backend preflight out of main
```

## Phase 3: Extract Agent Lifecycle

This is the highest-value hot-reboot boundary.

Create:

```text
orchestrator/agent_lifecycle.py
```

Move:

- Runtime construction.
- Runtime startup failure cleanup.
- Telegram preflight.
- Runtime startup.
- Telegram connection attempt and local-mode fallback.
- Start agent.
- Stop agent.
- Runtime teardown.
- Shutdown all agents.

Design:

```python
self.agent_lifecycle = AgentLifecycleManager(self)
```

The kernel keeps shared state:

- `self.runtimes`
- `self._lifecycle_lock`
- `self._agent_locks`
- `self._startup_tasks`
- `self.skill_manager`
- `self.global_cfg`
- `self.secrets`

The manager owns the behavior.

Requirements:

- Preserve Telegram failure to local-mode behavior.
- Preserve backend retry behavior.
- Preserve startup bootstrap enqueue behavior.
- Preserve WhatsApp startup notification hooks.
- Use local imports for runtime classes so `/reboot` picks up changes.

Validation:

```bash
pytest tests/test_codex_cli.py tests/test_commands.py tests/test_remote_peer_status.py
pytest
```

Suggested commit:

```text
Move agent lifecycle into hot-reloadable manager
```

## Phase 4: Extract Runtime Services

This phase intentionally comes before `RebootManager`, so the reboot sequence can rebuild and adopt all hot managers end to end.

Create:

```text
orchestrator/service_manager.py
```

Move:

- Workbench API start/stop.
- API Gateway start/stop.
- Scheduler create/recreate/stop.
- Agent directory creation.
- Runtime service health logging.

Design:

```python
self.services = ServiceManager(self)
```

Ownership rule:

- The kernel keeps the live Workbench API, API Gateway, and scheduler handles.
- `ServiceManager` operates on those handles and can adopt them after manager rebuild.
- `/reboot min` should not unnecessarily restart Workbench/API Gateway.
- Scheduler may be recreated on reboot because it is already designed as a managed background task.

Goals:

- Scheduler changes can be picked up by `/reboot`.
- Workbench and API Gateway lifecycle is isolated.
- `main.py` no longer knows service startup details.

Validation:

```bash
curl http://127.0.0.1:18800/api/health
curl http://127.0.0.1:18801/health
pytest tests/test_browser_gateway_phase1.py tests/test_remote_peer_status.py
pytest
```

Suggested commit:

```text
Move runtime service management out of main
```

## Phase 5: Extract Reboot Manager

Create:

```text
orchestrator/reboot_manager.py
```

Move:

- Project module reload logic.
- Hot restart orchestration.
- Restart target selection.
- Hot restart banner coordination.
- Manager rebuild transaction.
- Scheduler recreation after module reload.

Design:

```python
self.reboot_manager = RebootManager(self)
```

Upgrade hot reload scope carefully:

```text
adapters.*
orchestrator.*
browser_gateway.*
```

Do not add all `transports.*` blindly. WhatsApp will be isolated in Phase 6 and then can receive its own explicit warm-restart path.

On `/reboot`:

1. Stop selected agents.
2. Reload project modules.
3. Build replacement managers without mutating the kernel.
4. If all manager initialization succeeds, atomically commit the replacement manager set.
5. If any manager fails, keep the old managers and abort the reboot with a clear log.
6. Reload config.
7. Restart selected agents.
8. Recreate scheduler through `ServiceManager`.
9. Keep `main.py` alive.

Validation:

```bash
/reboot min
/reboot max
pytest
```

Add a hot-reload canary during implementation:

- Make a trivial log change in a manager.
- Run `/reboot min`.
- Verify the new log line appears.

This proves the code change was actually picked up by hot reload.

Suggested commit:

```text
Move hot reboot flow into reloadable manager
```

## Phase 6: Extract WhatsApp Manager

Create:

```text
orchestrator/whatsapp_manager.py
```

Move:

- Load WhatsApp config.
- Start WhatsApp transport.
- Stop WhatsApp transport.
- Send WhatsApp text.
- Send local-mode startup notification through WhatsApp.

Important:

WhatsApp must leave `main.py` core. It is a major feature surface and cold-restarting the whole bridge for WhatsApp changes is not acceptable for the v3.2 architecture goal.

At the same time, WhatsApp may hold long-lived connections and runtime threads. The split must be:

- The kernel keeps the live WhatsApp transport object/handle.
- `WhatsAppManager` controls start, stop, send, config persistence, and notifications.
- Rebuilding `WhatsAppManager` must not garbage-collect or accidentally shut down the live transport.
- Transport implementation hot reload is not automatic. Add an explicit WhatsApp warm-restart path after the manager extraction is stable.

This removes WhatsApp control logic from core while keeping connection ownership safe.

Validation:

- WhatsApp disabled startup remains unaffected.
- WhatsApp enabled startup behavior is unchanged.
- Shutdown watchdog behavior is preserved.
- Rebuilding managers does not disconnect an already-running WhatsApp transport.
- Full tests pass.

Suggested commit:

```text
Move WhatsApp transport control out of main
```

## Phase 7: Slim the Main Orchestrator Run Loop

Goal: reduce `UniversalOrchestrator.run()` from a large procedural block into a high-level flow.

Possible final shape:

```python
async def run(self):
    await self.bootstrap()
    await self.start_initial_agents()
    await self.services.start()
    await self.run_event_loop()
```

Optional modules:

```text
orchestrator/startup_flow.py
orchestrator/shutdown_flow.py
orchestrator/event_loop.py
```

Validation:

```bash
pytest
/reboot min
/reboot max
```

Suggested commit:

```text
Slim orchestrator run loop into high-level flow
```

## Phase 8: Final Main.py Slimming

Final `main.py` should look like a process shell:

```python
if __name__ == "__main__":
    args = parse_args()
    paths = build_bridge_paths(...)
    run_onboarding_gate(paths)
    configure_console()
    lock = InstanceLock(paths.lock_path)
    try:
        lock.acquire()
        orchestrator = UniversalOrchestrator(paths, ...)
        asyncio.run(orchestrator.run())
    finally:
        lock.release()
    os._exit(0)
```

Validation:

```bash
python3 -m py_compile main.py
pytest
bash -n bin/bridge-u.sh
```

Suggested commit:

```text
Slim main.py to process bootstrap only
```

## Required Validation After Every Phase

Run:

```bash
python3 -m py_compile main.py orchestrator/*.py
pytest
git status --short --branch
```

When lifecycle or reboot behavior changes, also verify:

```bash
/reboot min
/reboot max
curl http://127.0.0.1:18800/api/health
curl http://127.0.0.1:18801/health
```

## Success Criteria

- `main.py` is slim enough to act as a stable process bootstrap/kernel wrapper. Original target was less than or equal to 200 lines; accepted `v3.2.0` outcome is 337 lines.
- Full test suite passes.
- `/reboot min` picks up agent lifecycle changes.
- `/reboot max` picks up scheduler/runtime/backend changes.
- Workbench API remains healthy after reboot.
- API Gateway remains healthy after reboot when enabled.
- HASHI can still cold-start normally.
- Startup, shutdown, and reboot logs remain clear enough to debug failures.
- WhatsApp control logic is outside `main.py` core.
- Rebuilding managers does not accidentally tear down live WhatsApp transport state.
- WhatsApp transport hot reload has an explicit warm-restart path or is documented as transport-restart-only.
- Future feature modules are plug-and-play rather than hardcoded in a large process core.

## Recommended Commit Sequence

```text
Refactor bootstrap infrastructure out of main
Move config admin and backend preflight out of main
Move agent lifecycle into hot-reloadable manager
Move runtime service management out of main
Move hot reboot flow into reloadable manager
Move WhatsApp transport control out of main
Slim orchestrator run loop into high-level flow
Slim main.py to process bootstrap only
```

## v3.2.0 Implementation Status

Updated: 2026-05-02.

Accepted for release line `v3.2.0`.

Completed structural commits:

- `d01be3b` - `Refactor bootstrap infrastructure out of main`
- `96f062f` - `Move config admin and backend preflight out of main`
- `f63ff29` - `Move agent lifecycle into hot-reloadable manager`
- `c10d8de` - `Move runtime services into hot-reloadable manager`
- `7255967` - `Move hot restart orchestration out of main`
- `4f64407` - `Move WhatsApp lifecycle into hot-reloadable manager`
- `c92a31d` - `Preserve runtime list identity when stopping agents`
- `ed42242` - `Move bridge file logging into bootstrap`
- `d07c60e` - `Move initial startup orchestration out of main`
- `67b0fb3` - `Move shutdown and onboarding boundaries out of main`

Current shape:

- `main.py` reduced from about 1570 lines to 337 lines.
- Feature behavior moved into hot-reloadable managers for config administration, backend preflight, agent lifecycle, runtime services, reboot, startup, shutdown, WhatsApp, and skill management.
- Long-lived service and transport handles remain on the kernel.
- Manager rebuild is transaction-style: build replacements first, commit only after construction succeeds.
- `stop_agent()` now preserves `self.kernel.runtimes` list identity, so Workbench API and AgentDirectory do not hold stale list references after `/reboot min`.
- `SkillManager` is now rebuilt during hot manager rebuild.
- `StartupManager` is rebuilt with the rest of the manager set for consistency, although initial agent startup only runs during cold start.

Validation completed:

```text
python3 -m py_compile main.py orchestrator/*.py
pytest
203 passed, 2 warnings
```

Live validation completed on 2026-05-02:

```text
/reboot min: passed
/reboot max: passed
cold restart: passed
Workbench API health: ok
API Gateway health: ok when enabled
12 agents online after /reboot max
```

The accepted `/reboot max` run showed:

- `Hot restart begin (mode=max, requester=zelda, ...)`.
- 12 agents stopped with `reason=hot-restart:max`.
- 12 agents restarted and reached `ONLINE (backend + Telegram)`.
- Scheduler was recreated with reloaded code and restarted.
- Workbench API returned `ok: true` with 12 agents.
- API Gateway returned `status: ok` on the active gateway port.
- Post-reboot log scan found no `ERROR`, `CRITICAL`, `Traceback`, `failed`, or `LOCAL MODE` entries.

Known non-blocking warning:

```text
Scheduler task stop timed out or was cancelled
```

This warning is acceptable for the current release because the scheduler was immediately recreated and started successfully after the warning.

Remaining closeout:

- Keep `docs/HASHI_SLIM_CORE_ARCHITECTURE.md` current when adding or removing managers.
- Decide in a later release whether to reduce `main.py` further toward the original 200-line target, or continue accepting the 337-line kernel wrapper shape.
- Document WhatsApp transport implementation hot reload as transport-restart-only unless a dedicated warm-restart path is added.
