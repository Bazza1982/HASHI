# HASHI Layered Runtime Boundaries

Status: authoritative engineering-layer specification
Scope: HASHI core, HASHI functions, platform config, instance config
Decision: feature work must stay out of the immutable core unless explicitly authorized

Parent architecture: [HASHI System Architecture](../ARCHITECTURE.md)

## Summary

HASHI should be organized as four layers:

```text
Layer 1: HASHI core
Layer 2: HASHI functions
Layer 3: platform configuration
Layer 4: instance configuration
```

The core stays stable across platforms and instances. Feature changes should
land in replaceable shared/per-Agent Function processes or configuration layers.
Pulling from `main` must not erase local platform or instance configuration.

These are **engineering layers**, not HASHI's functional modules. The
orthogonal functional dimension is PCM, PAO, HER v2, and Frontend Connectors.
Every product capability must have one functional owner and one primary
engineering-layer placement. Most module behaviour belongs in Layer 2;
cross-module Core utilities may remain module-neutral only while they own no
product policy or duplicate authoritative state.

## Canonical Engineering Rule

> Design for high cohesion, low coupling, a single source of truth, and
> localized change.

HASHI applies that rule together with DRY, separation of concerns, SRP,
encapsulation, KISS, and YAGNI:

- One rule or piece of state has one authoritative owner.
- A normal feature change should have one primary implementation location and
  focused tests. Compatibility exports may derive from that owner, but must not
  copy its data.
- UI, business behavior, persistence, process bootstrap, platform adaptation,
  and instance adoption stay in separate modules.
- A module should have one principal reason to change. Related behavior stays
  together; unrelated behavior crosses a narrow public interface.
- Callers depend on public contracts, not another module's file layout or
  private state representation.
- Add an abstraction only when it removes current duplication or protects an
  existing boundary. Do not build speculative extension frameworks.

If a small requirement requires synchronized edits across many unrelated
files, treat it as **Shotgun Surgery**. Stop and first identify the missing
owner, registry, adapter, or persistence boundary. A compatibility view is
acceptable only when it is mechanically derived from the authoritative source.

Current authoritative owners include:

| Knowledge or lifecycle rule | Authoritative owner |
|---|---|
| Shared Engine/Model Provider compatibility baseline, models, effort, aliases, API-gateway eligibility | `orchestrator/flexible_backend_registry.py` |
| Instance model/effort opt-ins and effective Function-side choices | ignored `allowed_backends` configuration, resolved by `orchestrator/runtime_effort_options.py` |
| built-in slash handler, menu, help group, alias, sensitivity | `orchestrator/command_specs.py` |
| shared Function manager construction | `orchestrator/manager_registry.py` |
| Python, ABI, dependency and protected-Core identity | `orchestrator/runtime_contract.py` and `[tool.hashi.runtime]` |
| Function generation qualification and manifests | `orchestrator/function_generation.py` |
| per-Agent Worker lifecycle, IPC routing and crash recovery | `orchestrator/function_worker_supervisor.py` |
| transactional target switching and rollback | `orchestrator/reboot_manager.py` |
| reboot acceptance, outcome receipts and delivery recovery | shared PAO Functions: `reboot_manager.py`, `reboot_receipts.py`; [decision](HASHI_REBOOT_RECEIPTS.md) |
| reboot commands, result wording and Bot fallback | Frontend Functions: `runtime_reboot.py`, `reboot_ui.py`, `telegram_delivery_failover.py`, runtime language catalogs |
| function discovery, ordering, and public contract | `orchestrator/function_contract.py` |
| shared workspace `state.json` persistence | `orchestrator/workspace_state.py` |
| Core process supervision and instance lock/PID paths | `orchestrator/kernel_process.py`, `main.py` and `orchestrator/instance_lock.py` |
| compatibility port defaults | `orchestrator/runtime_defaults.py` |
| stable Remote port candidate/allocation policy | `orchestrator/stable_port_allocator.py` |
| npm-installed program's user-scoped instance registry, cwd/default selection and explicit program adoption | `tools/instance_registry.py`, projected by `scripts/hashi_instance_cli.py` |
| local instance identity and ports | ignored `agents.json` / `instances.json` |

This table records current physical fact owners. Conceptual ownership remains
governed by the Level 0 architecture. In particular, the current backend
registry contains compatibility representations of both Engine Providers and
Model Provider adapters; that physical shape must not erase the distinction.

When adding a fact covered by this table, extend its owner and derive consumer
views. Do not create another literal list or direct file writer. The shared
registry is part of the qualified Functions generation. An ordinary instance
model opt-in belongs in instance configuration and its existing resolver.
Shared compatibility changes update Function catalogues and their actual
consumers. Core never imports those product owners.

## Layer 1: HASHI Core

Purpose: stable process bootstrap, kernel state, and compatibility contracts.

Examples:

- `main.py`
- product-neutral process construction and lifecycle entrypoint
- single-instance lock
- crash/fatal exit handling
- immutable Function generation and Worker protocol contract
- generic child supervision, byte verification and JSON transport; shared
  services and Telegram/Backend API ingress belong to the Function process
- process protocol and runtime compatibility boundaries

Rules:

- Core files are protected.
- Feature changes must not edit core files by default.
- Core changes require explicit user authorization.
- Core changes require a focused plan, lightweight tests, and independent
  review before merge.
- Core should not know about product ports, terminal rendering, Windows/WSL
  product policy, or instance names. OS process/lock/stdio primitives stay here.

Core edit guard:

```text
Any agent attempting to edit protected core files must stop and ask for explicit
authorization unless the current task already names those files or says core
changes are allowed.
```

Canonical protected paths live only in
`orchestrator.runtime_contract.CORE_SOURCE_PATHS`.
`scripts/check_protected_core_changes.py`, the runtime fingerprint and the
function-generation exclusion set all derive from that tuple; documentation
must not duplicate a path list. It covers generic process entry, canonical
instance identity, OS locking, runtime/JSON protocol compatibility, artifact
verification, import purity and child supervision. Configuration interpretation,
terminal/logging presentation, Manager construction and Agent reboot policy
belong to Functions. Remote peers remain function/sidecar protocol
implementations and cross Core only through versioned messages.
The Windows helper (`tools.windows_helper` and
`tools.windows_use_mcp_client`) is one such external-runtime sidecar: its
modules are excluded from the in-process function generation and are launched
with their own declared `uv` dependency set.

Shared Function managers and services retain identity during Agent-only `/reboot`.
They are owned by a separately replaceable Function process, not by Core. The
explicit shared handoff keeps Core/lock identity but has a service gap; see
[Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md). Ordinary product changes never
require moving these owners back into the long-lived Core process.

The manifest is enforced through `AGENTS.md`, a local preflight and the optional
installed pre-commit hook. Default preflight includes staged, unstaged and
untracked changes; commit/branch checks also retain baseline protection:

```bash
python scripts/check_protected_core_changes.py
```

Pre-commit and branch checks should use the correct diff target:

```bash
# Check staged changes before commit.
python scripts/check_protected_core_changes.py --cached

# Check an entire branch against main.
python scripts/check_protected_core_changes.py --base main
```

If the user explicitly authorizes a core edit, rerun with:

```bash
python scripts/check_protected_core_changes.py --authorized
```

or set `HASHI_CORE_EDIT_AUTHORIZED=1` for that check. CI validates the manifest
and requires the `core-change-approved` pull-request label when protected paths
change.

## Layer 2: HASHI Functions

Purpose: replaceable product behavior in shared or per-Agent Function processes.
Agent behavior changes through a Worker `/reboot`; shared services require the
separate, explicitly scoped shared handoff.

Examples:

- runtime command handlers
- menus and Telegram/UI command surfaces
- request execution, backend adapters, tools and skills
- hchat delivery logic
- wrapper/audit/Anatta runtime features
- Agent-local memory, media and voice behavior
- Remote route planner and profile resolver when they execute inside an Agent

Rules:

- Feature work should land here by default.
- Every function-layer change must be adoptable through `/reboot min` for one
  Agent. `/reboot max` may also adopt it, but must never be a prerequisite.
- A function change without a verified targeted adoption path is incomplete
  and must not be promoted.
- A targeted reboot must never be widened or rejected because class members,
  signatures, fields, or other valid Python interfaces changed. Only an
  explicit `same` or `max` request may select multiple Agents.
- Workers may request a narrow shared capability through versioned JSON IPC; they
  may not receive or mutate Core Python objects.
- New behavior should be modular and swappable rather than added to stable
  unrelated ingress or a large shared manager.

## Layer 3: Platform Configuration

Purpose: OS/platform-specific local behavior that should survive `git pull`.

Examples:

- WSL vs Windows vs macOS terminal behavior
- display/encoding behavior
- Windows Terminal font/profile behavior for rich CJK banners
- shell command variants
- path translation rules
- Windows firewall diagnostics
- WSL host/guest bridge rules
- platform-specific service supervisors
- platform port allocation policy

Rules:

- Platform config must live in local config/state paths, not in tracked code
  defaults that get overwritten on pull.
- Platform config should be generated or migrated, not hand-edited in core.
- Pulling `main` should not reset Windows-specific or WSL-specific settings on
  an installed instance.
- Platform adapters should read platform config at runtime and report the
  effective config in diagnostics.

Display example:

- The full HASHI startup banner is a function-layer renderer.
- Whether WSL uses the full CJK glyph profile or a latin-safe profile is
  platform configuration.
- Windows Terminal font selection, such as using `Noto Sans SC` for WSL
  profiles, is local platform configuration and must not require core changes.
- Classic console fallbacks should avoid glyphs that render as square
  replacement boxes.

## Layer 4: Instance Configuration

Purpose: per-instance identity and local state that must not be flushed by
updates.

Examples:

- `instance_id`
- assigned Backend API/API Gateway/Remote ports
- local machine identity
- local bind hosts
- active agent set
- Remote shared token references
- WatchTower address, if this instance talks to an external WatchTower
- generated profile overrides
- local aliases and retired/validation display policy

Rules:

- Instance config is local and should be ignored by git unless it is a template.
- Pulling `main` must not change the instance's identity or assigned ports.
- Missing instance config may be bootstrapped, but existing config must be
  preserved.
- Runtime should fail with actionable diagnostics instead of silently falling
  back to a conflicting default.

### Per-instance process ownership

The npm-facing registry is a Layer 3 selector, not a replacement for Layer 4
identity. It maps a local name to one `code_root` and `bridge_home`, plus cwd
bindings, a default, and the explicitly adopted installed-program version.
`agents.json` remains authoritative for the runtime `instance_id` and service
ports. Registration of an existing Git instance is therefore read-only: it
records those facts but never regenerates the identity, copies secrets, or
rewrites workspaces.

The registry itself lives outside the installed program and is scoped to the
current Windows, WSL-distribution, Linux, or macOS environment. Selection order
is explicit name, longest cwd binding, default, sole instance, then interactive
choice; non-interactive ambiguity fails closed. npm upgrade/uninstall cannot
delete or migrate this registry or any bridge home. Managed recursive deletion
requires an exact confirmation plus a matching instance marker beneath the
managed data root; external/Git roots are never purge targets.

The process lock is scoped to the instance's local `bridge_home`, under:

```text
<bridge_home>/state/instance/process.lock
<bridge_home>/state/instance/process.pid
```

Only a duplicate process for the same instance may be blocked or stopped.
HASHI1, HASHI2, HASHI9, and any other configured instances may run concurrently
on one computer when each has its own `bridge_home`. The path intentionally
does not change when `instance_id` is renamed, because the same local files
must never be served by two processes. Launchers and control scripts must
resolve these paths through `orchestrator.pathing`; they must not scan and kill
every `main.py` process or assume a repository-wide `.bridge_u_f.pid`.

The lock file is persistent. The operating-system file lock, not file
existence, is authoritative. This avoids the unlock/delete inode race.

## Function-change contract

Tracked Agent behavior should be usable after `/reboot` whenever the process
bootstrap contract itself did not change. Shared-service changes use the broad
handoff defined in the Minimal Core decision; they do not widen `/reboot`:

1. Resolve the requested lifecycle scope once; targeted modes contain exactly
   one immutable target and malformed input never falls back to all.
2. Compile and hash the complete functional source/asset closure.
3. Import and validate it in an isolated probe with the exact Core runtime.
4. Materialise a content-addressed immutable artifact.
5. Spawn one candidate Worker per selected Agent and require a READY receipt.
6. Reject any pre-READY failure without closing an active route.
7. Close only the selected stable route gates, drain their old Workers, and
   reverify runtime, Core and source fingerprints.
8. Activate every candidate, then replace all selected handle pointers under
   their route locks with no await point between the first and last mutation.
9. Open the gates together, publish topology, and retire the old Workers.
10. If anything before pointer commit fails, terminate every candidate, resume
    the prior Workers, and reopen the same routes.
11. Keep Core PID, instance lock, runtime fingerprint, shared Function managers, Backend API,
    API Gateway, scheduler, background jobs and unselected Agent handles intact.

Cold process restart is not an allowed Function-change adoption or recovery
path. Process bootstrap, runtime policy, generic process control and native supervision
are Core boundaries. `orchestrator.runtime_contract.CORE_SOURCE_PATHS` is the
single exclusion and fingerprint manifest; there is no second reload list.
Unexpected Worker failure is recovered from its last immutable generation and
never by reloading Core.

The exact Python and generation rules are normative in
`docs/HASHI_PYTHON_RUNTIME_COMPATIBILITY.md`. A Python, dependency, platform-ABI,
protected-Core source, Core API, or Function API change is a planned Core
migration and is rejected by `/reboot` before lifecycle cutover.

Function discovery is rooted to the checked-out project. A third-party module
whose name happens to start with `tools.` or `orchestrator.` must never enter a
generation artifact.

## Stable Random Port Allocation

Fixed default ports have repeatedly caused HASHI instances and APIs to fight
over ports. The new rule is:

```text
Allocate uncommon local ports intentionally, randomly, and once.
Then persist the assignment in instance configuration.
```

This is not "random on every boot". Ports must be stable after first allocation
so discovery, hchat, Remote, and external supervisors can rely on them.

Recommended allocator behavior:

1. Read existing instance config.
2. If a service already has an assigned port, probe it and keep it unless the
   operator explicitly resets it.
3. If the assigned port is occupied by another process, fail with an actionable
   error. Do not silently pick a new port.
4. If no port exists, acquire a machine-local allocation lock before probing.
5. Build candidates from a configurable broad pool, then remove OS ephemeral
   ranges, Windows excluded ranges, already-bound ports, and ports already used
   by this HASHI instance config.
6. Persist the chosen port atomically before starting the service.
7. Emit an audit event with service, port, pool, lock path, and reason.

The allocator must be OS-aware:

- Linux/WSL: read `/proc/sys/net/ipv4/ip_local_port_range` and avoid the active
  ephemeral range.
- Windows: inspect excluded ranges with
  `netsh interface ipv4 show excludedportrange protocol=tcp` and dynamic ranges
  with `netsh int ipv4 show dynamicport tcp`.
- macOS: use `sysctl net.inet.ip.portrange.*` when available and always probe
  before assignment.

The candidate pool should be configurable. A reasonable first implementation is
to consider `20000-65000`, remove OS-reserved/ephemeral/excluded ranges, then
choose randomly from what remains. Avoid claiming a universal fixed "safe"
range because the safe set is host-specific.

Example persisted instance config:

```yaml
instance:
  instance_id: HASHI9
  port_allocations:
    workbench_api:
      port: 43172
      assigned_at: "2026-05-18T17:00:00+10:00"
      reason: "initial_random_allocation"
    api_gateway:
      port: 45218
      assigned_at: "2026-05-18T17:00:00+10:00"
      reason: "initial_random_allocation"
    hashi_remote:
      port: 46793
      assigned_at: "2026-05-18T17:00:00+10:00"
      reason: "initial_random_allocation"
```

Legacy fixed ports such as `8766`, `8767`, and `18800` should become migration
hints only, not permanent assumptions. They may be probed for backwards
compatibility while older peers are still deployed, but new installs should
prefer stable random assignments.

If a persisted port is occupied:

```text
ERROR: persisted port 43172 for workbench_api is occupied by another process.
Action: stop the conflicting process or run `hashi port reset workbench_api`
after confirming the migration impact.
```

The reset command is part of the port allocator rollout and must be implemented
before the allocator becomes default-on.

## WatchTower Boundary

WatchTower is already a separate program. HASHI docs and code should treat it
as an external rescue service, not as a component embedded in this repository.

HASHI may keep:

- client code that calls WatchTower status/log/start endpoints;
- docs describing how HASHI talks to external WatchTower;
- local instance config that stores the WatchTower address and auth reference.

HASHI should remove or archive:

- docs that imply this repo is the WatchTower runtime;
- config comments that reserve ports for WatchTower inside HASHI;
- legacy code that starts WatchTower as if it were an in-repo HASHI sidecar;
- test aliases that appear as production Remote instances.

Deletion must be done through a separate audit pass so we do not remove the
current `/restart` client path that correctly talks to external WatchTower.

## Pull-Safety Requirement

After `git pull`, an installed instance must still know:

- who it is;
- which ports it owns;
- which platform it runs on;
- how to start its local functions;
- how to reach its configured Remote and WatchTower peers.

If the update cannot preserve that, startup must stop with a clear migration
message rather than booting into a wrong identity or conflicting port.

## Release Gate

Changes that touch these boundaries require focused checks:

- protected core touched: explicit user authorization + independent review;
- function layer touched: isolated probe plus `/reboot min` Worker switch;
- platform config touched: at least one WSL/Windows/macOS-relevant fixture;
- instance config touched: migration test preserving existing local values;
- port allocation touched: collision, persistence, and legacy migration tests.
- every pull request: `.github/workflows/architecture-boundaries.yml`.
