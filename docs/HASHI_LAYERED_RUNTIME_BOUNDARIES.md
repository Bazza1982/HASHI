# HASHI Layered Runtime Boundaries

Status: design rule
Scope: HASHI core, HASHI functions, platform config, instance config
Decision: feature work must stay out of the immutable core unless explicitly authorized

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
  - main.py
  - orchestrator/kernel.py
  - orchestrator/reboot_manager.py
  - orchestrator/instance_lock.py
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

or set `HASHI_CORE_EDIT_AUTHORIZED=1` for that check. This guard is intentionally
non-invasive at first; it can later be wired into CI or pre-commit.

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
- Changes must be adopted by `/reboot min` or `/reboot max` whenever possible.
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
- assigned Workbench/API/Remote ports
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
