# HASHI Stable Core and Function Worker Architecture

Status: accepted and live-adopted on HASHI3; promotion remains gated

Original slim-Core decision: 2026-05-02

Worker-isolation revision: 2026-09-04

The exact source, offline and live evidence, installed Worker state, and
remaining operator-dependent canaries are recorded in
[`HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md`](HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md).

## Summary

`main.py` is a stable process kernel. Each running Agent lives in its own
Function Worker process behind a stable `AgentRuntimeHandle`. `/reboot` builds
and validates replacement Workers, then changes only those handles; it does not
reload modules or rebuild managers inside Core.

```text
                          immutable generation artifact
                                      |
                       probe -> candidate Worker READY
                                      |
Telegram / Workbench / API / Scheduler / HChat
                    |                 |
              stable HASHI Core -> AgentRuntimeHandle
                                      |
                         active per-Agent Worker
```

The old v3.2 design used `importlib.reload()` in the Core process and rebuilt
Manager/service objects. It could leave a mixed module generation after a late
failure. That mechanism and its recovery tests are retired.

## Core responsibilities

Core owns identities and shared authority that cannot safely be replaced from
inside a running Python process:

- CPython/dependency/ABI enforcement;
- process entry, instance lock, paths, signals and fatal shutdown;
- lifecycle and route locks;
- Telegram long polling plus Workbench and API ingress;
- Scheduler, background jobs and shared transport/service ownership;
- Function generation qualification and immutable artifacts;
- Worker spawn, IPC, target switching and crash recovery;
- cross-Agent routing and versioned persistence/protocol boundaries.

Core files are declared once in
`orchestrator.runtime_contract.CORE_SOURCE_PATHS`. The same manifest drives the
Core digest, protected-edit check and Function-generation exclusion.

`orchestrator.manager_registry.CORE_MANAGER_SPECS` constructs Core managers at
process startup. They are not reconstructed by `/reboot`:

```text
ConfigAdminManager
BackendPreflightManager
AgentLifecycleManager
ServiceManager
RebootManager
StartupManager
ShutdownManager
WhatsAppManager
SkillManager
```

Their live state belongs to the kernel and retains identity for the Core
lifetime. In particular, a Function reboot does not replace Workbench API, API
Gateway, Scheduler, delivery watcher, background jobs, instance lock or Core
runtime fingerprint.

## Function Worker responsibilities

One Worker contains one real `FlexibleAgentRuntime` and its replaceable
functional closure:

- backend adapters and model execution;
- slash commands and Agent-specific handlers;
- tools, skills and turn orchestration;
- Agent memory, media and voice behavior;
- Telegram application handlers and outbound delivery, but not long polling;
- Agent-local state and request queues.

The Worker receives a narrow `WorkerKernelFacade`. Shared operations—starting
another Agent, Scheduler recovery, API Gateway control, background jobs,
WhatsApp, HChat routing and cross-Agent messages—are requested from Core over
versioned JSON RPC. Core objects are never shared or pickled.

When new product behavior needs shared authority, extend the explicit RPC
contract. Do not move functional code into Core merely because direct Python
calls are easier.

## Stable Agent handle

Code outside a Worker sees an `AgentRuntimeHandle`, not
`FlexibleAgentRuntime`. The handle provides stable metadata and routes
supported operations to its current Worker.

Each handle owns:

- one route condition/gate;
- the current Worker client pointer;
- per-route in-flight count;
- current metadata and offline error;
- completion listeners for requests crossing IPC.

During cutover, new calls wait at the gate. Core first waits for calls already
using the old Worker, then quiesces that Worker. Pointer publication and gate
opening happen under the route locks.

Core owns one Telegram polling task and update offset per online Agent. It
serializes each `Update` to JSON and routes it through the handle. Candidate
Workers initialize their Telegram application and handlers but never call
`getUpdates`; therefore the stable poller cannot be duplicated and an update
cannot be consumed by a candidate before route commit.

The kernel-owned `runtimes` list retains identity because Workbench, Scheduler
and directories may hold it. Starting and stopping Agents mutate it in place.

## Generation qualification

`orchestrator.function_generation` performs four separate checks:

1. Discover operational entrypoints and their complete functional import
   closure, including literal lazy imports.
2. Compile, hash and order Python sources; hash functional non-Python assets.
3. Launch a disposable isolated probe with the running Core executable.
4. Rebuild and compare the manifest after the probe.

The probe enforces the exact Core runtime, imports the full closure under an
import-purity guard, validates public cross-module identities, and returns a
versioned receipt. It cannot write files, launch processes/threads/tasks,
change environment or signal state, or open a network connection during
candidate import.

Core copies verified bytes into:

```text
state/function_generations/<generation-sha256>/
```

A Worker resolves only manifest-listed modules from that tree. Non-listed Core
imports continue to resolve from the stable source tree. Existing artifacts
are verified before reuse; source or artifact tampering is rejected.

## Worker lifecycle

The Worker state machine is:

```text
BOOTING -> READY -> ACTIVATING -> ACTIVE
                              -> DRAINING -> QUIESCED
                              -> STOPPING -> STOPPED
```

READY requires the real Agent configuration, backend initialization, complete
function import and contract checks. ACTIVE owns request execution. DRAINING
closes intake, stops Agent-local polling and waits for the queue, foreground
generation and tracked background work to become idle. QUIESCED can either be
resumed after rollback or retired after commit.

## Transactional reboot

`RebootManager` resolves targets before candidate work:

- `min`: requesting Agent only;
- a number: exactly that configured/running Agent;
- `same`: currently running Agents;
- `max`: currently running Agents under a full rollout request.

Invalid or offline targets fail closed and never become “all Agents.”

For a valid request:

1. qualify one candidate generation;
2. prepare all selected Workers to READY;
3. close only selected handles and drain their in-flight routes;
4. quiesce selected old Workers;
5. reverify the runtime and source manifests;
6. activate all candidates and verify capabilities;
7. acquire all selected handle locks;
8. replace all pointers without an await point;
9. open the gates, publish topology and terminate old Workers.

All failures before step 8 discard every candidate and resume all old Workers.
No Core module, manager, service or unselected Agent changes. This is the
architectural rollback; there is no attempt to reverse mutated `sys.modules`.

After step 8, the new route generation is committed. A diagnostic or topology
publication error is reported as a post-commit observability fault and cannot
be mislabeled as a rollback.

## Multi-Agent isolation

Every Agent has an independent process, artifact pointer and route gate.
`/reboot min` therefore works in a multi-Agent Core without interrupting the
others. A staged rollout may temporarily report:

```text
function_generation.generation_id = mixed
```

Workbench health lists each Agent's generation, Worker PID, phase, acceptance
state and process liveness so this is explicit rather than inferred.

Broad cutover prepares and activates all candidates before publishing any
route. Pointer publication is all-or-none under all selected locks.

## Worker crash recovery

An unexpected Worker exit fails its outstanding request listeners and closes
only that route. Core tries up to three times to start the same Agent from the
same verified immutable artifact. Success updates the stable handle and
broadcasts topology. Exhaustion marks that route explicitly failed; other
Agents and Core services remain online.

Recovery does not use the mutable checkout and cannot silently advance a
generation.

## Startup and shutdown

Cold startup performs the one-time Core construction, qualifies a Function
generation, and starts selected Agent Workers with bounded concurrency. Shared
Core services start after Agent selection succeeds.

Normal shutdown first closes and drains Agent routes, terminates all Workers,
then stops shared Core services and releases the instance lock. The external
supervisor owns Core replacement.

Changing Python, dependencies, Core sources or protocol/API versions is a
planned Core migration. HASHI3 may use one controlled cold restart during this
pilot; production should use blue/green Core handoff where continuous service
is required.

## Required engineering rules

- No in-process project module reload.
- No duplicate Core or Function ownership manifests.
- No Python objects or pickle across Worker IPC.
- No live dependency installation.
- No candidate import side effects.
- No target widening.
- No pointer publication before every selected candidate is healthy.
- No Core cold restart as Function failure recovery.
- Core ingress delegates feature execution through a stable handle.
- Tests assert observable isolation, rollback and liveness outcomes.

The normative runtime values and promotion gate are in
`HASHI_PYTHON_RUNTIME_COMPATIBILITY.md`. Layer ownership and edit authority are
in `HASHI_LAYERED_RUNTIME_BOUNDARIES.md`.
