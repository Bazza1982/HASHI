# HASHI Slim Core Architecture

Status: accepted for the `v3.2.0` release line on 2026-05-02.

This document records the major structural change that moved HASHI from a large `main.py`-centered bridge into a slim process core with hot-reloadable orchestration managers.

## Summary

`main.py` is now a process bootstrap and kernel wrapper rather than the primary home for feature behavior.

Before the v3.2 release line, `main.py` contained process startup, configuration administration, backend preflight, agent lifecycle, service management, hot reboot orchestration, startup, shutdown, WhatsApp control, and several logging boundaries in one large file. That made many code changes impossible to adopt through `/reboot`, because code that stayed inside the already-running `main.py` process was not reloaded.

In `v3.2.0`, frequently changed behavior was moved into modules under `orchestrator/`, and the long-lived kernel rebuilds those managers during hot reboot after project modules are reloaded.

## Current Shape

`main.py` keeps process-level responsibilities:

- CLI argument parsing.
- Bridge path construction.
- Onboarding gate invocation.
- Minimal logging and console bootstrap.
- Single-instance lock ownership.
- Kernel object construction.
- Top-level `asyncio.run(...)` lifecycle.
- Fatal crash capture and final process exit behavior.

Hot-reloadable behavior now lives in manager modules:

```text
orchestrator/skill_manager.py
orchestrator/config_admin.py
orchestrator/backend_preflight.py
orchestrator/agent_lifecycle.py
orchestrator/service_manager.py
orchestrator/reboot_manager.py
orchestrator/startup_manager.py
orchestrator/shutdown_manager.py
orchestrator/whatsapp_manager.py
```

The kernel constructs these managers and exposes compatibility wrapper methods for older call sites. New code should prefer the manager boundary directly when adding behavior in the same domain.

## Kernel-Owned State

Long-lived runtime state stays on the kernel. Replaceable managers control this state but do not own it.

Kernel-owned live handles include:

```text
workbench_api
api_gateway
scheduler
scheduler_task
whatsapp
agent_directory
runtimes
_lifecycle_lock
_agent_locks
_startup_tasks
global_cfg
secrets
```

This rule prevents a manager rebuild from accidentally tearing down service handles, losing agent list identity, or garbage-collecting an active transport.

## Manager Rules

Managers are controllers. They should:

- Store `self.kernel`.
- Read shared state from `self.kernel.*` at call time.
- Avoid caching other managers during `__init__`.
- Call sibling managers through the kernel, for example `self.kernel.agent_lifecycle`.
- Avoid owning long-lived transports or background service handles unless the kernel explicitly transfers that ownership.

This is important because `/reboot` replaces manager instances. Any direct reference to an old manager can become stale after a rebuild.

## Hot Reboot Flow

`/reboot` is coordinated by `RebootManager`.

The accepted flow is:

1. Select restart targets from the requested mode.
2. Compile loaded project sources before stopping any agent.
3. Stop selected agents only after preflight succeeds.
4. Reload project modules in the centralized dependency order and fail fast.
5. Build the manager set from `manager_registry.py` into a temporary bundle.
6. Commit the full replacement manager set only if every constructor succeeds.
7. Reload config and secrets as needed.
8. Restart selected agents.
9. Refresh warm services through `ServiceManager`: Workbench API, enabled API
   Gateway, scheduler, delivery watcher, and background jobs.
10. Keep the long-lived process, instance lock, kernel identity, and WhatsApp
    transport alive.

The manager rebuild is transaction-style. If any replacement manager fails to initialize, the kernel keeps the old manager set alive and the failure is logged clearly.
If source preflight fails, no agent is stopped. If a live reload fails after
agents were stopped, HASHI restores those agents where possible, leaves warm
services untouched, and reports the reload or runtime-contract mismatch. The
operator repairs that mismatch and retries `/reboot`; cold process restart is
not an adoption or recovery path for function changes.

## Reboot Modes

`/reboot min` always restarts only the requesting Agent while reloading project
code, configuration, managers, and warm services. `/reboot N` applies the same
flow to exactly the numbered Agent. Public class additions, signature changes,
and other valid Python edits are not grounds to widen or reject a targeted
reboot. The rebuilt target receives fresh runtime objects; non-target Agents
remain online and their active work is not interrupted.

Target selection is locked before source preflight. Only an explicit
`/reboot` (same) or `/reboot max` request may select multiple Agents. Missing,
invalid, or unknown targeted input is rejected with no lifecycle or reload side
effects; it never falls back to all Agents.

Python module dictionaries and warm services are process-global, so the source
reload itself is shared even when lifecycle interruption is local. This is not
a reason to force all Agents through a restart. A function-layer change is
complete only when it can be adopted through `/reboot min`; `/reboot max` is an
operator-selected rollout action, not a compatibility prerequisite.

`/reboot max` restarts all running agents and reloads project code.

Both modes rebuild hot managers. Workbench API and an enabled API Gateway are
warm-recreated so their request handlers adopt reloaded code; the scheduler,
delivery watcher, and background jobs are also recreated. Their fields remain
kernel-owned even though the service objects stored in those fields are
replaced.

## Live Handle Boundaries

Workbench API, API Gateway, scheduler task state, WhatsApp transport, and the
agent directory are kernel-level fields. Managers can start, stop, recreate, or
send through them. Manager construction alone must not destroy a live service;
the explicit post-reload warm-service refresh owns that lifecycle.

The `runtimes` list is also identity-sensitive. External holders, including the Workbench API and agent directory, may hold a reference to that list. Agent stop logic must mutate the list in place rather than replacing it:

```python
self.kernel.runtimes[:] = [
    rt for rt in self.kernel.runtimes if rt.name != agent_name
]
```

## HER v2 Assurance Hot-Reload Boundary

HER v2 Reviewed and Assured execution is implemented in hot-reloadable adapter,
runtime, prompt, policy, and Tool Registry modules. Reviewed (`xhigh`) performs
one read-only independent Review, optional Primary-Agent remediation, and one
closure Review. Assured (`max`) adds a `VERIFYING` lifecycle state and at most
three latest-state Verification attempts.

Reviewers remain read-only. Verifiers receive only the additional authority to
run validation commands in the authoritative workspace; they cannot remediate
or widen delegation. Claims are accepted only when they cite exact completed
receipts bound to the current stage, provider attempt, invocation, and tool
call. Configured recipes or direct argv checks inherit the HASHI process's
filesystem, environment, `HOME`, and network authority. Their timeout grows
from cumulative Execution duration and cannot be shortened by the verifier.

These modules and schemas are adopted through the ordinary targeted reboot
flow. Adding lifecycle states, routes, response fields, or public helper
interfaces does not authorize widening `/reboot min` or `/reboot N`; only the
selected Agent lifecycle is interrupted while shared Python modules and warm
services reload.

## Startup And Shutdown

`StartupManager` owns cold-start orchestration. It starts initial agents and then starts runtime services. It is rebuilt during hot reboot for consistency, but the initial startup path only runs during cold start.

`ShutdownManager` owns final full shutdown. The process run loop dereferences `self.shutdown_manager` at call time, so a rebuilt shutdown manager is used after a hot reboot. Signal handlers remain bound to stable kernel methods, not replaceable manager methods.

## WhatsApp Boundary

WhatsApp control logic is outside `main.py` in `WhatsAppManager`, but the live WhatsApp transport object remains on the kernel.

Manager rebuild does not warm-restart the WhatsApp transport implementation. Changes to manager-level WhatsApp control can be adopted by `/reboot`; changes that require replacing the underlying transport still require an explicit transport restart or a cold restart.

## Validation Record

Accepted validation for the `v3.2.0` release line:

```text
python3 -m py_compile main.py orchestrator/*.py
pytest
203 passed, 2 warnings
```

Live checks completed on 2026-05-02:

```text
/reboot min: passed
/reboot max: passed
cold restart: passed
Workbench API health: ok
API Gateway health: ok when enabled
12 agents online after /reboot max
```

The `/reboot max` acceptance run confirmed:

- 12 agents stopped successfully.
- 12 agents restarted and reached `ONLINE`.
- Scheduler was recreated with reloaded code and restarted.
- Workbench API returned `ok: true`.
- API Gateway returned `{"status":"ok","engines":["codex-cli"]}` when checked on the active gateway port.
- No post-reboot `ERROR`, `CRITICAL`, `Traceback`, `failed`, or `LOCAL MODE` entries were found.

Known non-blocking warning:

```text
Scheduler task stop timed out or was cancelled
```

This warning was followed by successful scheduler recreation and startup, so it is not an acceptance blocker.

## Residual Notes

- `main.py` is intentionally still above the original aspirational 200-line target. The accepted `v3.2.0` shape is a slim kernel wrapper rather than a pure bootstrap-only file.
- `StartupManager` is rebuilt during hot reboot but only used during cold start. This is harmless and keeps the manager set complete.
- Transport implementation hot reload for WhatsApp remains explicit-restart-only.
- Future manager additions must be registered once in
  `orchestrator/manager_registry.py`; cold construction and hot rebuild derive
  from that manifest.
