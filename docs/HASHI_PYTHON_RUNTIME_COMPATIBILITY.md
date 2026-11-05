# HASHI Core Runtime and Function Worker Contract

Status: Core API 3 accepted for HASHI2; source/offline verification and live cold adoption are separate

Decision date: 2026-09-03; revised for Function Workers on 2026-09-04 and fast
transactional reboot on 2026-10-14

Owners: HASHI Core maintainers

The current process/ownership contract is [Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md).
HASHI2 evidence is [this migration record](HASHI2_MINIMAL_CORE_2026-09-07.md).
Earlier API 2 implementation and verification status is recorded in
[`HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md`](HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md).

## Core v5 role-qualified source candidate

The approved [architecture](CORE_RUNTIME_DECOUPLING_ARCHITECTURE_20261105.md),
[implementation order](CORE_RUNTIME_DECOUPLING_IMPLEMENTATION_20261105.md) and
[acceptance gates](CORE_RUNTIME_DECOUPLING_ACCEPTANCE_20261105.md) govern the
authorized v5 source candidate. This is not adoption of that candidate by any
installed instance. The coupled v4 baseline below remains historical evidence.

The existing runtime-contract owner now distinguishes protected code/protocol C,
the immutable Core-host environment CR, the immutable Functions environment FR,
and the Function-source artifact FG. CR permits zero application distributions;
FR must satisfy its complete exact lock. CPython/ABI/build policy is qualified
independently for each role. Current canonical inputs remain
`[tool.hashi.runtime]`, its `core-host-lock` and its Functions `standard-lock`;
there is no second package catalogue.

Exact equality remains mandatory within a role. The cross-role boundary
explicitly validates trusted role, source/protocol/API/schema compatibility and
the selected exact FR. It does not require CR and FR to share executable,
prefix, package set or policy digest. Both installations seal their environment
and base bytes and reject ambient distributions, startup injection, changed
packages and redirected paths. The existing unsealed v4 diagnostic treatment of
unrelated extra packages is not an exception to v5 sealed-role admission.

One protected launcher selects the absolute qualified interpreter for shared
Functions, Agent Workers, qualification and recovery. Private JSON IPC binds
nonce, OS-observed executable/parent/PID, runtime identity and FG before product
execution. Windows creates the owned Job and restricted handle list atomically
with the child. No active environment is upgraded in place. The operator-owned
selector is fixed relative to the Core prefix; it is not an Agent, Workzone,
environment-variable or prompt override. Platform installers materialize independent roles and re-observe relocated
Portable environments at the final target. Independent review and complete
artifact/OS acceptance still require Gate 5; source code alone is not that evidence.

A later compatible FR update or rollback changes FR and its admitted Functions
processes while C, CR and Core PID remain fixed. A CPython/stdlib/ABI change in
CR requires a separately qualified Core-runtime replacement and approved cold
cutover; unchanged protected source need not consume a Core-code major. Protected
source changes still require the authorized major migration and independent
same-tree review. Existing wire versions do not change merely to name a role.

Actual CR/FR IDs are projected separately in topology and detailed `/version`,
alongside the existing product/source/FG facts. Missing Core receipts remain
unknown. Public version facts omit interpreter and installation paths. Source,
prepared candidate, active route and live acceptance remain separate states.

The canonical installer also offers build-functions through
scripts/build_runtime_roles.py. It accepts a frozen reference and the existing
qualified Core manifest/digest, creates only a new FR in a fresh destination,
and revalidates the original CR unchanged. It returns a candidate descriptor;
it never writes the fixed selector, adopts the candidate, starts/stops Core,
or installs a service. Existing environments and program-source directories
are not valid output destinations.

The complete original-dependency business scope passed at source 030689c4
on native Windows and its push/PR CI. The independent-FR builder is exercised
by actual same-Core fixture-package replacement, rejection and rollback with
the existing durable-counter/no-replay assertions. This mechanism is not
PR137 security acceptance. Final role/build qualification, the five security
upgrades, independent review, complete packaging and operator adoption remain
separate gates. See the [Windows source checkpoint](CORE_RUNTIME_DECOUPLING_WINDOWS_CHECKPOINT_20261105.md)
for exact tested scope and retained failures.

### Functions security maintenance on the split architecture

The Functions version authority selectively adopts aiohttp 3.14.4, certifi
2026.10.19, multidict 6.9.1, Pillow 13.0.0 and yarl 1.25.1 from PR137.
Core's empty application lock, protected source, APIs and runtime stay unchanged.
constraints/functions-py312.hashes.lock and Portable's requirements.lock are
derived by scripts/compile_functions_locks.py; their exact 50/55 package closures
must match the canonical Functions pins. Installation requires wheel hashes and
builds a fresh directory, never upgrades a running interpreter.

scripts/qualify_functions_dependency_handoff.py accepts an existing qualified
pair and a Functions-only builder candidate. It launches one new isolated
zero-application-dependency Core controller, checks real package ownership,
performs upgrade, failed-candidate rollback, downgrade and a second upgrade,
and preserves the same synthetic durable effect counter. --prepare-product
also qualifies and prepares the complete identical FG under both FRs without
activating connectors, real providers or services. Its evidence is independent
of business tests and production adoption; the installed selector is untouched.

### Complete distribution and installer trust

Standard npm/source preparation uses a new copied CPython build/sidecar base,
a disposable pip tool environment, hash-required empty-target Functions install,
and the canonical role builder. Nothing is installed in an existing Core/FR.
Sidecar preparation precedes role sealing and cannot create caches in a running
or sealed Core base. The installer catalogue selects the management FR; initial
Core launch accepts only the paired, qualified Core interpreter. An Agent's
management-Python environment variable is not Core selection authority.

Portable carries separate independently pinned runtime/core and runtime/functions
trees. The public relative capsule binds observed CR/FR IDs, full file closures,
the complete FG, public committed-source evidence and independent sidecar
closures. It contains no build-machine interpreter or receipt paths. The
installer verifies the complete existing SHA256SUMS coverage, copies atomically,
preserves data permissions and data lineage, protects program/runtime/selector
trust roots, then re-observes final paths before success. Generated absolute
qualification records live outside both immutable interpreters. A failure
restores the previous complete installation; relocation never rewrites sealed
environment bytes to make an old receipt appear valid.

A Git checkout still requires its exact clean committed source. A Git-less
package requires a privately bound FR plus the fixed installer-owned
hashi-function-publication.json beside that FR. Every requested source/asset
must match its published bytes and executable mode. A package-local self-asserted
FUNCTION_SOURCE.json alone grants no runtime authority. The tool write-denial
policy covers both publication records and the installation selector. No
fallback to a package record is allowed for a dirty or broken Git checkout.
Publication stays outside the interpreter seal so FG and FR remain independent.

Enterprise images build through scripts/build_enterprise_image.py: only a clean
committed public export enters the context. Standard and optional Functions
packages receive an exact platform wheel hash lock. Sidecars are prepared with
a disposable build interpreter before the CR/FR copies are sealed. Main uses
the absolute Core -I -B command, management/health uses FR, and installed source
and runtime trees are read-only. The image workflow probes both actual roles
with networking disabled; a successful source check is not a production canary.

## Core v4 coupled baseline

The remaining sections describe the installed v4 coupling and its historical
acceptance. Their same-interpreter/full-fingerprint and Core-dependency-lock
statements do not override the role-qualified v5 candidate above. Historical
restart permissions below grant no authority for the current migration.

### Decision

HASHI Core owns one mandatory runtime. Functional code runs in replaceable,
shared Function and per-Agent Worker processes supervised by that Core. Functional code may use the
Core contract; it may not choose the Core Python version, mutate Core, or share Python
objects across the process boundary. Optional/native Function dependencies run
in isolated sidecars over typed data protocols and are never installed into the
interpreter of a running Core.

The machine-readable authority is `[tool.hashi.runtime]` in `pyproject.toml`:

```text
implementation:    CPython
approved Python:   3.12.13
source range:      >=3.12,<3.13
standard lock:     constraints/standard-py312.lock
Core API:          3
Function API:      3
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

### Non-negotiable invariant

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

### Runtime identity

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
with the same executable. ABI, runtime policy, protected Core, API and protocol
fields must exactly match Core.

The standard lock defines the Core runtime's required production packages. Its
exact package versions must be present in Core, the isolated candidate probe
and the new Worker. `dependency_digest` still records the complete installed
set for diagnosis, but unrelated additional distributions do not reject a
Function generation. A missing or changed locked package always does. Native
or conflicting Function dependencies still live in an isolated sidecar
selected by platform/instance configuration and communicate with a Function
Worker through a bounded, versioned data protocol.

The repository-local virtual environment may belong to a running Core, so the
project declares `[tool.uv] managed = false`. Project-aware `uv run`, `uv lock`,
and `uv sync` must not implicitly create or synchronize that environment.
Diagnostics use the existing approved interpreter directly; isolated helpers
use `uv run --no-project` or an explicitly separate environment.

Python release packaging is isolated for the same reason. Setuptools may
rewrite project-local `*.egg-info` metadata even when no dependency changes.
Release artifacts therefore use `scripts/build_python_release.py`, which builds
an exact Git `HEAD` export in a temporary directory. A packaging command must
never run against a checkout owned by a live Core: ignored metadata is still
visible to Python distribution discovery and can otherwise manufacture a false
runtime drift that blocks Function `/reboot`.

Windows has no system IANA timezone database. The standard dependency contract
therefore installs the hash-locked `tzdata` package on Windows only, so
timezone-sensitive Scheduler and Wiki evidence uses the same named zones as
Linux/WSL without embedding a fixed-offset fallback in Core.

### Ownership boundary

#### Stable Core and replaceable Functions

Core retains runtime enforcement, its OS instance lock, generic process lifetime,
verified immutable artifact loading and versioned JSON transport. It does not
host application managers, models, scheduler policy, API execution, UI or
frontend ingress. Those run in a pinned shared Function process with isolated
Agent children. Shared replacement has an explicit broad operational scope;
Agent-only `/reboot` never implicitly replaces shared services.

The import boundary is checked including lazy dependencies. Changing a model,
UI renderer or provider policy cannot require Core protection to be bypassed.
The full process contract and handoff limits are in the Minimal Core decision.

### Worker protocol

Core/shared-Functions and shared-Functions/Agent-Worker links use private duplex
byte pipes carrying UTF-8 JSON envelopes:

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

Qualification and process readiness have distinct owners. The disposable
isolated probe imports the complete functional closure and validates the public
cross-module contract once per new generation. The protected bootstrap verifies
the immutable artifact bytes once for each actual process. After that boundary,
READY means the Worker has matched the runtime, loaded its natural runtime
closure, constructed the real Agent runtime and initialized its backend. It does
not repeat the generation-wide import/contract test or full artifact scan, and
does not yet own the active shared Function route.

Telegram `getUpdates` offset and long polling live in shared Functions. Each update is
converted to JSON and delivered through the stable handle. While a route gate
is closed, the shared poller holds the update until commit or rollback; a
candidate cannot fetch, acknowledge or process it early.

### Transactional `/reboot`

For `min`, a number, `same`, or an explicit group, the shared Function supervisor performs:

1. Resolve an immutable target set. Invalid input fails without widening it.
2. Require every file in the Function manifest to be clean in local Git HEAD,
   record that exact source commit, then qualify the generation in an isolated
   process. Dirty files outside the manifest do not block qualification.
3. Reuse a sealed qualified generation and content-addressed artifact when its
   runtime and exact Function bytes still match. If clean Git HEAD moved while
   those bytes stayed identical, re-hash the accepted manifest once and bind a
   refreshed receipt to the current commit; do not repeat the isolated probe.
   The artifact retains its original creation provenance.
4. Keep old routes open while spawning one READY candidate Worker for every
   selected Agent.
5. Re-read live activity. If a target became busy, discard candidates and keep
   the old routes unchanged.
6. Fence only selected routes, wait for accepted calls and quiesce the selected
   old Workers.
7. Check the lightweight generation seal immediately before activation.
8. Activate all candidates and require their identity and health receipts.
9. Acquire every selected route lock, replace all Worker pointers without an
   await point, then open the gates together.
10. Publish topology and retire the old Workers after commit.

`/reboot max` publishes one shared replacement request while every old Agent
route, API and Scheduler remains available. Core's existing handoff qualifies
and prepares the candidate first, then performs the short quiesce/commit window.
Only in that window are new requests held for the successor. It replaces shared
Functions and running Agent Workers while Core and Remote keep their own
lifecycles.

Before step 9, any failure terminates all candidates and resumes every old
Worker that was quiesced, then reopens only the selected routes. Core objects, unselected Agents, services and route
pointers retain identity. After step 9, diagnostic publication failures are
logged but cannot falsely report that an already committed pointer swap was
rolled back.

`/reboot min` and `/reboot N` work in a multi-Agent instance because every Agent has
an independent Worker and stable handle. Different Agents may intentionally
run different generation IDs during a staged rollout; health output reports
that aggregate state as `mixed`.

### Failure and recovery rules

- Candidate import, contract, construction or readiness failure: discard it;
  reopen selected routes to their unchanged old Workers.
- Drain or activation failure: discard every candidate in that transaction,
  resume old Workers and reopen the same routes.
- Candidate loses a previously working Telegram capability: reject it and
  resume the old Worker.
- Source or Core changes after qualification: reject before pointer commit.
- Staged, unstaged or untracked files inside the Function manifest: reject the
  candidate; unrelated dirty files outside that manifest do not block it.
- Unexpected active Worker exit: close only that Agent route, start the same
  immutable generation up to three times, then either restore it or leave the
  route explicitly failed.
- Core/Python/locked Core dependency/ABI change: reject `/reboot`; use a planned
  Core migration. Unrelated extra packages are diagnostic information, not a
  rejection condition.

No failure silently expands a target set, changes another Agent, or turns an
unknown state into a claimed rollback.

### Core migration

These require a new Core rather than `/reboot`:

- Python patch/minor or interpreter implementation;
- Core virtual environment, Core dependency lock or native ABI;
- any protected Core source;
- Core API, Function API, Worker protocol or generation schema.

The historical HASHI3 v4 adoption allowed one controlled cold restart for that
specific migration. It grants no restart or cutover permission for v5 or any
current instance. Production adoption requires separate operator approval and a
complete verified rollback pair. A Core migration is planned deployment work,
never the fallback for a failed Function generation.

### Test contract

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
10. Core and shared-service object identity during Agent-only replacement survives repeated Function reboots;
11. a source edit after probe cannot commit;
12. active Worker death recovers the same artifact or fails that route closed;
13. Workbench health exposes Core contract plus per-Agent Worker PID,
    generation, phase, acceptance and liveness;
14. two live HASHI3 `/reboot` switches succeed under the same Core PID.
15. unchanged-generation cache hits perform no isolated probe, materialization
    or full-closure import;
16. PID reuse with a new nonce and exact Agent/generation remains a valid
    replacement, while a nonce or route mismatch fails closed;
17. heartbeat execution remains available during broad preparation and startup
    recovery emits a due heartbeat at most once.

Tests of `importlib.reload()` order, partial module replacement, rebuilt Core
Managers, warm-service recreation or legacy mixed-class repair are obsolete
and must be removed with those APIs.

### Promotion gate

`scripts/check_function_worker_runtime.py` may be given a validated live
service-endpoint snapshot with `--service-endpoints` when a configured CLI
backend needs the Backend API route to construct its Tool Gateway.  The probe
rejects a snapshot owned by another instance and republishes the validated
endpoints only inside its isolated `bridge_home`.  This qualifies a real
backend and Worker through READY; it does not start a shared service, activate
the Worker, call a model, or establish live adoption.  Endpoint health remains
a separate observable check.

HASHI3 may be promoted to HASHI1/HASHI2 only after:

- focused runtime, generation, protocol, supervisor, lifecycle, service and
  reboot suites pass;
- the curated Core gate and explicit offline product suite pass;
- protected-Core and runtime-authority checks pass;
- a real HASHI3 cold start reports CPython 3.12.13 and Worker protocol 1;
- targeted and repeated live reboots preserve the Core PID and shared services for Agent-only updates;
- an injected candidate failure is shown to leave the active generation live;
- launchers, CI, containers and portable builders agree with
  `[tool.hashi.runtime]`.
