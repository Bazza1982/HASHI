# HASHI Python Runtime and Function Generation Contract

Status: accepted for the HASHI3 pilot

Decision date: 2026-09-03

Owners: HASHI Core maintainers

Scope: process bootstrap, Python and dependency ABI, function-layer adoption,
rollback, packaging, CI, and release gates

## Decision

HASHI Core owns one runtime. Function code may target that runtime; it may not
select, replace, or relax it.

The current production contract is:

```text
implementation: CPython
Python:          3.12.13 (approved production runtime)
compatibility:   >=3.12,<3.13
standard lock:   constraints/standard-py312.lock
Core API:        1
Function API:    1
```

The machine-readable authority is `[tool.hashi.runtime]` in `pyproject.toml`.
Packaging metadata, launchers, containers, portable builds, documentation and
CI must agree with that section. `orchestrator.runtime_contract` enforces it;
tests reject duplicated version claims that drift.

Supported source code targets the 3.12 minor line, but a production Core starts
only on the approved security patch. Moving to another 3.12 patch is a planned
Core migration because the interpreter and dependency fingerprint change.

Windows and macOS portable packages use the approved
`python-build-standalone` release for the exact security patch. This avoids a
false dependency on python.org's Windows embeddable archives, which are no
longer published for source-only 3.12 security releases. Portable packages use
the bundled `ensurepip` and install the same standard lock as every other
deployment; legacy `_pth` mutation and downloaded `get-pip.py` are retired.

Python 3.10 and 3.11 are unsupported. Python 3.13 is a candidate only. It must
pass the full standard, media, voice, OCR, vector, WhatsApp, Remote, Workbench,
API Gateway and native-extension profile matrix before a planned Core API
migration can change the canonical minor.

## Runtime identity

At process entry, before any function module is imported, Core first verifies
the exact Python patch and every package in the standard lock, then records:

```text
implementation
Python full and major.minor versions
sys.implementation.cache_tag
platform ABI (`SOABI` on POSIX, native-extension suffix on Windows)
operating system and machine architecture
pointer width
resolved interpreter executable
resolved virtual-environment prefix
installed-distribution digest
protected Core source digest
Core API and Function API versions
```

That immutable record is the `core_runtime_id`. Every candidate staging worker
must produce an exact match. A difference is a release/deployment error, not a
hot-reboot error.

Platform helpers that run as separate processes are not in-process function
modules. In particular, `tools.windows_helper` and
`tools.windows_use_mcp_client` run in their own Windows `uv` environment and
communicate through their versioned JSON/HTTP boundary. Core must not import
or materialise those sidecars during `/reboot`; their launcher and protocol
contracts are tested separately.

Examples that require a planned Core migration are:

- any Python patch or minor change, including 3.12.13 to another patch;
- replacing the virtual environment or interpreter executable;
- adding, removing or upgrading a dependency while Core is running;
- changing platform ABI, CPU architecture or pointer width;
- editing a protected Core source file;
- changing Core API or Function API.

`/reboot` must reject all of those before it stops an Agent. It must never
suggest that retrying an in-process reload can repair them.

## Core and function ownership

Core owns:

- process entry and the runtime check;
- instance lock and path identity;
- process-wide lock identities used by overlapping function generations;
- lifecycle state and fatal shutdown;
- the manager manifest;
- function-generation staging, commit and rollback;
- reboot scope resolution;
- startup and shutdown coordination;
- cross-process/cross-instance protocol boundaries.

The authoritative Core file manifest is
`orchestrator.runtime_contract.CORE_SOURCE_PATHS`. The protected-core change
check imports this tuple instead of maintaining a second list.

Function space includes loaded project modules below `adapters`, `tools`,
`orchestrator`, `flow`, `nagare`, `remote`, and `transports`, except the Core
manifest. The staging worker expands the initial active set to its complete
cold-import closure. Contract validation resolves every registered backend and
the WhatsApp transport surface, so an inactive adapter or transport cannot
evade the release gate.

## Transactional `/reboot`

The former implementation used `importlib.reload()` on live module objects.
After module 34 succeeded and module 35 failed, Python offered no reliable way
to undo the first 34 mutations. Restoring old Manager pointers therefore did
not restore the old code generation. That path is retired and now fails
closed.

The replacement state machine is:

```text
DISCOVER
   -> fingerprint and compile source manifest
   -> ISOLATED PROBE
   -> materialise fresh module objects
   -> validate all cross-module identities
   -> build the complete Manager bundle
   -> READY
   -> quiesce exactly the selected Agent lifecycle scope
   -> verify the source manifest again
   -> atomically install module bindings and Manager bundle
   -> start selected Agent(s)
   -> refresh and health-check warm services, including enabled transports
   -> COMMITTED
```

Every pre-READY failure becomes `DISCARDED`; no Agent is stopped. The isolated
probe runs with the exact Core executable and environment prefix and rejects a
different runtime, dependency set, Core source or API version.

Candidate materialisation uses new module objects. It never edits an active
module object with `importlib.reload()`. Canonical bindings and project package
dictionaries are restored while the candidate is inactive. Candidate imports
are guarded against file writes, process creation, and thread or asynchronous
task creation. A function module with import-time operational side effects is
not reloadable and must be refactored.

At cutover, Core recomputes the complete runtime fingerprint and verifies the
source digest again to close the edit-between-probe-and-commit window. If
activation, Agent startup, transport replacement, or warm-service health fails,
Core restores the previous module map and Manager bundle, tears down any new
target runtime, restarts the target on the previous generation, and rebuilds
the prior service generation. A failed candidate is never a reason to
cold-restart Core.

Old and new Agent generations may briefly overlap during a targeted cutover.
Any lock protecting shared persisted state must therefore come from the
Core-owned `orchestrator.process_resources` registry. Replaceable module-global
lock maps are forbidden because they split synchronization across generations.

## Target scope

`min` and numeric modes resolve to exactly one immutable target. `same` and
`max` are the only broad modes. Invalid input never falls back to all Agents.

The HASHI3 pilot makes function-generation commit atomic at process scope. A
targeted cutover is therefore permitted only when its target is the sole live
Agent. In a multi-Agent process, `min` and numeric modes fail before staging or
stopping anything; `same` and `max` quiesce the complete live set and may
commit one process generation. This fail-closed rule prevents an unselected
old runtime from later importing a module from the new canonical generation.

True targeted switching in a multi-Agent production instance requires an
independent Function Worker for each Agent. Until that boundary exists and its
qualification suite passes, the Core must reject the operation rather than
widen its scope or pretend the mixed-generation state is safe.

## Dependency generations

`requirements.txt` remains the human-maintained input range. The tested
standard deployment installs `constraints/standard-py312.lock`, generated by:

```bash
uv pip compile requirements.txt --python-version 3.12 --universal \
  --output-file constraints/standard-py312.lock
```

Updating the lock is a dependency/Core migration:

1. regenerate it intentionally;
2. rebuild a clean environment;
3. run every applicable profile;
4. record the new dependency digest;
5. replace Core through a planned cold or blue/green handoff.

Installing packages into a live Core environment is forbidden. The candidate
worker will detect the digest change and reject `/reboot`.

## Required tests

The release gate must assert outcomes, not implementation trivia:

1. approved CPython 3.12.13 and the standard dependency lock satisfy the policy;
2. another implementation/minor, platform ABI, architecture, dependency digest,
   Core digest, or API level is rejected;
3. the runtime guard precedes every function import in `main.py`;
4. every registered backend resolves and the complete active import closure
   succeeds in an isolated process;
5. provider modules are ordered before consumers and stale cross-generation
   class/function/module bindings are rejected;
6. syntax/import/contract/Manager-construction failure does not stop an Agent;
7. source change after probe cannot commit;
8. target stop failure never activates a candidate;
9. target start, transport, and warm-service health failures roll back code,
   Managers, Agent, and the service generation;
10. repeated complete generations can be adopted without Core restart;
11. protected Core modules and process-owned file locks retain object identity
    across function generations;
12. a real HASHI3 cold boot, `/reboot min`, API health check and second
    `/reboot min` succeed under the same Core PID.

Tests whose only assertion was the order or partial progress of
`importlib.reload()` are obsolete and must be deleted. A passing count from a
bounded suite is not evidence of compatibility unless these contract tests are
inside that suite.

## Deployment and recovery

There are only two supported operations:

| Change | Operation |
|---|---|
| Function source, same runtime/Core contract | transactional `/reboot` |
| Python, dependency generation, Core source/API, native ABI | planned Core migration |

A planned Core migration may use one cold restart on a development instance.
A zero-downtime production deployment should start and health-check a new Core
under an external supervisor before transferring ports/queues and draining the
old Core. Neither operation is an emergency repair for a polluted process;
the function-generation design must prevent that state.

## HASHI3 pilot exit criteria

HASHI3 may be used to validate this contract because its Core can be restarted
without disrupting HASHI1 or HASHI2. Promotion requires:

- all contract and bounded product tests pass;
- the protected-core authorization check passes;
- launchers, CI, container and portable builders select CPython 3.12.13;
- HASHI3 uses its own environment rather than another instance's virtualenv;
- two real generation switches succeed under one Core PID;
- injected candidate failure leaves the live Agent and service health intact;
- multi-Agent lazy-import isolation is either proven or moved to per-Agent
  Function Workers.
