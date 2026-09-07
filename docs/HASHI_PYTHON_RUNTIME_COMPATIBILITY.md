# HASHI Core Runtime and Function Worker Contract

Status: accepted and live-adopted on HASHI3; promotion and external canaries remain gated

Decision date: 2026-09-03; revised for Function Workers on 2026-09-04

Owners: HASHI Core maintainers

Current implementation and verification status is recorded in
[`HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md`](HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md).

## Decision

HASHI Core owns one mandatory runtime. Functional code runs in replaceable,
per-Agent Worker processes created by that Core. Functional code may use the
Core contract; it may not choose a Python version, mutate Core, or share Python
objects across the process boundary.

The machine-readable authority is `[tool.hashi.runtime]` in `pyproject.toml`:

```text
implementation:    CPython
approved Python:   3.12.13
source range:      >=3.12,<3.13
standard lock:     constraints/standard-py312.lock
Core API:          2
Function API:      2
Worker model:      per-agent-process
Worker protocol:   1
generation schema: 2
```

Python 3.10 and 3.11 are unsupported. Python 3.13 remains a candidate until
the complete standard, media, voice, OCR, vector, WhatsApp, Remote, Workbench,
API Gateway and native-extension matrix passes and a planned Core migration is
approved.

Source compatibility covers the 3.12 minor line. A deployed Core starts only
on the approved patch and exact standard dependency lock. Changing even the
patch version changes the runtime fingerprint and is a planned Core migration.

## Non-negotiable invariant

> A Function generation can either become READY and replace the selected
> Worker, or be discarded. It must never modify the running Core or the active
> Worker generation.

There is no supported state called “Core environment polluted; cold restart
required.” The architecture prevents that state:

- Core never calls `importlib.reload()` on active project modules;
- candidate code is imported only in disposable probe and Worker processes;
- Core communicates with Workers through versioned JSON values, never pickle
  or shared Python objects;
- a failed candidate is terminated while existing route handles keep pointing
  to the previous Worker;
- an unexpected active Worker exit is recovered from its last immutable
  generation without replacing Core.

A Core process can still fail because of an operating-system failure, hardware
failure, or a defect in Core itself. That is a Core incident, not a Function
reboot recovery mechanism.

## Runtime identity

Before importing functional code, Core records an immutable fingerprint:

```text
implementation and exact Python version
cache tag and platform/native-extension ABI
operating system, machine architecture and pointer width
resolved interpreter and virtual-environment prefix
runtime-policy and dependency-set digests
protected Core source digest
Core API and Function API
Worker model, protocol and generation schema
```

`orchestrator.runtime_contract` loads and enforces this contract before normal
project imports in `main.py`. Every probe and Worker recomputes the fingerprint
with the same executable and must exactly match Core.

The standard lock defines required production packages. Extra packages may be
installed for an approved optional profile, but the effective installed set is
part of `dependency_digest`; Core and every Worker must therefore see the same
environment.

## Ownership boundary

### Stable Core

Core owns only process and shared-resource authority:

- process entry, runtime enforcement, paths and instance lock;
- lifecycle locks, fatal shutdown and external supervision boundary;
- Telegram long polling, Workbench/API ingress and shared
  background/scheduler services;
- immutable generation qualification and artifacts;
- Worker process supervision, route gates, crash recovery and `/reboot` scope;
- cross-Agent routing and shared service capability RPC;
- stable protocol and persistence-schema boundaries.

The authoritative file manifest is
`orchestrator.runtime_contract.CORE_SOURCE_PATHS`. Core-source edits invalidate
the running fingerprint and cannot be adopted by `/reboot`.

Core service objects remain alive during a Function reboot. A feature-specific
operation reached through Workbench, API Gateway, Scheduler, HChat, background
jobs or another ingress must cross `AgentRuntimeHandle` into the selected
Worker. New product behavior must not be added to the stable ingress merely to
avoid defining an IPC method.

### Function generation

Each Agent Worker owns its replaceable behavior, including:

- `FlexibleAgentRuntime`, commands and request execution;
- backend adapters and provider routing;
- tools and skills used inside an Agent turn;
- message rendering, Agent-specific Telegram handlers and outbound delivery;
- Agent-local memory, media, voice and workbench execution behavior;
- function-layer transports reached through stable Core capabilities.

`orchestrator.function_generation` starts from the declared operational
entrypoints, expands every static or literal dynamic import in the functional
namespace, compiles each source, includes non-Python assets, and hashes the
result. Protected Core modules are excluded.

The resulting content-addressed artifact lives below
`state/function_generations/<sha256>`. A Worker reads functional imports from
that artifact through exact manifest routing; arbitrary `sys.path` shadowing is
forbidden. The live checkout is reverified before commit so an edit between
probe and cutover cannot enter service.

### External sidecars

Platform helpers such as `tools.windows_helper` and
`tools.windows_use_mcp_client` have independent runtimes. They cross a
versioned JSON/HTTP boundary and are neither Core imports nor Function Worker
modules. Their Python/dependency policy must be declared and tested by the
sidecar itself.

## Worker protocol

Core and a Worker use a private duplex byte pipe carrying UTF-8 JSON envelopes:

```text
version + kind + request id/method/params
version + kind + response id/ok/result|error
version + kind + event/payload
```

Protocol version, message kind, required and unexpected fields, finite numbers
and a 16 MiB frame limit are validated at both ends. Paths are serialized as
text. Python instances, exceptions, callables and arbitrary objects are
rejected rather than pickled.

The Worker lifecycle is:

```text
BOOTING -> READY -> ACTIVATING -> ACTIVE -> DRAINING -> QUIESCED -> STOPPED
              \-------------------------------- failure ------------> discarded
```

READY means the Worker has matched the Core runtime, verified the immutable
artifact, imported the complete functional closure, validated public
cross-module identities, constructed the real Agent runtime and initialized
its backend. It does not yet own the active Core route.

Telegram `getUpdates` offset and long polling remain in Core. Each update is
converted to JSON and delivered through the stable handle. While a route gate
is closed, the Core poller holds the update until commit or rollback; a
candidate cannot fetch, acknowledge or process it early.

## Transactional `/reboot`

For `min`, a number, `same`, or `max`, Core performs:

1. Resolve an immutable target set. Invalid input fails without widening it.
2. Qualify one generation in an isolated process.
3. Materialize and verify its immutable artifact.
4. Spawn one READY candidate Worker for every selected Agent.
5. Close only those stable route gates and wait for in-flight Core calls.
6. Quiesce the selected old Workers and their Agent-local ingress.
7. Reverify runtime, Core and generation fingerprints.
8. Activate all candidates and require their health receipts.
9. Acquire every selected route lock, replace all Worker pointers without an
   await point, then open the gates together.
10. Publish topology and retire the old Workers after commit.

Before step 9, any failure terminates all candidates and resumes every old
Worker that was quiesced. Core objects, unselected Agents, services and route
pointers retain identity. After step 9, diagnostic publication failures are
logged but cannot falsely report that an already committed pointer swap was
rolled back.

`/reboot min` and `/reboot N` work in a multi-Agent Core because every Agent has
an independent Worker and stable handle. Different Agents may intentionally
run different generation IDs during a staged rollout; health output reports
that aggregate state as `mixed`.

## Failure and recovery rules

- Candidate import, contract, construction or readiness failure: discard it;
  old Workers are never gated.
- Drain or activation failure: discard every candidate in that transaction,
  resume old Workers and reopen the same routes.
- Candidate loses a previously working Telegram capability: reject it and
  resume the old Worker.
- Source or Core changes after qualification: reject before pointer commit.
- Unexpected active Worker exit: close only that Agent route, start the same
  immutable generation up to three times, then either restore it or leave the
  route explicitly failed.
- Core/Python/dependency/ABI change: reject `/reboot`; use a planned Core
  migration.

No failure silently expands a target set, changes another Agent, or turns an
unknown state into a claimed rollback.

## Core migration

These require a new Core rather than `/reboot`:

- Python patch/minor or interpreter implementation;
- virtual environment, dependency lock or native ABI;
- any protected Core source;
- Core API, Function API, Worker protocol or generation schema.

On HASHI3, one controlled cold restart is permitted to load this architecture.
Production should use an external supervisor and a blue/green handoff when
zero downtime is required. A Core migration is planned deployment work, never
the fallback for a failed Function generation.

## Test contract

Tests protect outcomes rather than the retired implementation. Required
assertions include:

1. policy, exact Python patch, locks and all fingerprint fields are enforced;
2. Core enforcement is the first project import in `main.py`;
3. Core and Function manifests are disjoint and their declared closures exist;
4. a real isolated probe imports the full functional closure;
5. artifacts are content-addressed, immutable and reject tampering;
6. IPC rejects non-JSON values, wrong versions, malformed fields and oversized
   frames;
7. READY failure never gates or stops an active Worker;
8. targeted reboot changes only its Agent in a multi-Agent Core;
9. broad reboot publishes all selected routes together or restores all old
   routes;
10. Core and Core-service object identity survives repeated Function reboots;
11. a source edit after probe cannot commit;
12. active Worker death recovers the same artifact or fails that route closed;
13. Workbench health exposes Core contract plus per-Agent Worker PID,
    generation, phase, acceptance and liveness;
14. two live HASHI3 `/reboot` switches succeed under the same Core PID.

Tests of `importlib.reload()` order, partial module replacement, rebuilt Core
Managers, warm-service recreation or legacy mixed-class repair are obsolete
and must be removed with those APIs.

## Promotion gate

HASHI3 may be promoted to HASHI1/HASHI2 only after:

- focused runtime, generation, protocol, supervisor, lifecycle, service and
  reboot suites pass;
- the curated Core gate and explicit offline product suite pass;
- protected-Core and runtime-authority checks pass;
- a real HASHI3 cold start reports CPython 3.12.13 and Worker protocol 1;
- targeted and repeated live reboots preserve the Core PID and Core services;
- an injected candidate failure is shown to leave the active generation live;
- launchers, CI, containers and portable builders agree with
  `[tool.hashi.runtime]`.
