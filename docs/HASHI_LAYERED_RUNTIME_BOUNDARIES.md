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
land in hot-reloadable functions or configuration layers. Pulling from `main`
must not erase local platform or instance configuration.

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
| Engine/Model Provider compatibility entries, models, effort, aliases, API-gateway eligibility | `orchestrator/flexible_backend_registry.py` |
| built-in slash handler, menu, help group, alias, sensitivity | `orchestrator/command_specs.py` |
| initial/hot manager construction | `orchestrator/manager_registry.py` |
| hot-reload discovery, ordering, and source preflight | `orchestrator/hot_reload.py` |
| shared workspace `state.json` persistence | `orchestrator/workspace_state.py` |
| instance process lock and PID paths | `orchestrator/pathing.py` |
| compatibility port defaults | `orchestrator/runtime_defaults.py` |
| stable Remote port candidate/allocation policy | `orchestrator/stable_port_allocator.py` |
| local instance identity and ports | ignored `agents.json` / `instances.json` |

This table records current physical fact owners. Conceptual ownership remains
governed by the Level 0 architecture. In particular, the current backend
registry contains compatibility representations of both Engine Providers and
Model Provider adapters; that physical shape must not erase the distinction.

When adding a fact covered by this table, extend its owner and derive consumer
views. Do not create another literal list or direct file writer.

## Layer 1: HASHI Core

Purpose: stable process bootstrap, kernel state, and compatibility contracts.

Examples:

- `main.py`
- kernel construction and process lifecycle entrypoint
- single-instance lock
- crash/fatal exit handling
- manager rebuild transaction contract
- shared protocol schemas and compatibility boundaries

Rules:

- Core files are protected.
- Feature changes must not edit core files by default.
- Core changes require explicit user authorization.
- Core changes require a focused plan, lightweight tests, and independent
  review before merge.
- Core should not know about platform-specific ports, terminal display quirks,
  Windows/WSL details, or instance names.

Core edit guard:

```text
Any agent attempting to edit protected core files must stop and ask for explicit
authorization unless the current task already names those files or says core
changes are allowed.
```

Canonical protected paths live in
`scripts/check_protected_core_changes.py::PROTECTED_CORE_PATHS`. The list below
is a human-readable copy and must not be treated as the source of truth:

```yaml
protected_core_paths:
  - __main__.py
  - main.py
  - orchestrator/config.py
  - orchestrator/instance_lock.py
  - orchestrator/pathing.py
  - orchestrator/manager_registry.py
  - orchestrator/hot_reload.py
  - orchestrator/reboot_manager.py
  - orchestrator/startup_manager.py
  - orchestrator/shutdown_manager.py
  - remote/protocol_manager.py
  - remote/peer/base.py
```

Hot-reloadable manager implementations are Layer 2 unless they define or mutate
the kernel/process contract. For example, `orchestrator/service_manager.py` is a
hot-reloadable function-layer manager; its public contract with kernel-owned
service handles is protected, but ordinary implementation changes such as adding
a new managed service should not require full core authorization.

The manifest is enforced by agent instructions first and by a local preflight
check:

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

Purpose: hot-reloadable behavior that can change with `/reboot`.

Examples:

- orchestration managers
- runtime command handlers
- menus and Telegram/UI command surfaces
- scheduler and superloop behavior
- hchat delivery logic
- wrapper/audit/Anatta runtime features
- Remote route planner and profile resolver, once extracted from legacy core

Rules:

- Feature work should land here by default.
- Every function-layer change must be adoptable through `/reboot min` for one
  Agent. `/reboot max` may also adopt it, but must never be a prerequisite.
- A function change without a verified targeted adoption path is incomplete
  and must not be promoted.
- A targeted reboot must never be widened or rejected because class members,
  signatures, fields, or other valid Python interfaces changed. Only an
  explicit `same` or `max` request may select multiple Agents.
- Managers may use kernel-owned handles but must not silently replace them.
- New behavior should be modular and swappable rather than hard-coded into one
  large runtime object.

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

## Hot-change contract

Tracked feature and adoption behavior should be usable after `/reboot` whenever
the process bootstrap contract itself did not change:

1. Resolve the requested lifecycle scope once; targeted modes must contain
   exactly one immutable target, and malformed input must not fall back to all.
2. Compile all loaded project sources before stopping an agent.
3. Reject the reboot without touching running agents if preflight fails.
4. Reload dependencies before consumers and fail fast on the first reload
   error; never continue into a mixed manager rebuild silently.
5. Build the complete manager bundle before installing any replacement.
6. Restart only the previously selected agents.
7. Recreate warm services—Backend API, enabled API Gateway, scheduler,
   delivery watcher, and background jobs—only after a successful reload.
8. Keep the process lock, kernel identity, and live WhatsApp transport outside
   the warm-service refresh.

Cold process restart is not an allowed function-change adoption or recovery
path. Process bootstrap, lock implementation, and native supervision are core
boundaries rather than function-layer changes. A proposed change to one of
those boundaries must include an explicit warm-handoff mechanism before it can
be promoted. `orchestrator.hot_reload.PROCESS_IDENTITY_MODULES` excludes the
already-held process lock and path-identity objects so `/reboot` cannot falsely
claim to have replaced them.

Hot reload discovery is also rooted to the checked-out project. A third-party
module whose name happens to start with `tools.` or `orchestrator.` must never
be reloaded.

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
- function layer touched: `/reboot min` or targeted hot-reload check;
- platform config touched: at least one WSL/Windows/macOS-relevant fixture;
- instance config touched: migration test preserving existing local values;
- port allocation touched: collision, persistence, and legacy migration tests.
- every pull request: `.github/workflows/architecture-boundaries.yml`.
