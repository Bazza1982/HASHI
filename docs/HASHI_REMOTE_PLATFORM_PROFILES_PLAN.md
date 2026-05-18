# HASHI Remote Platform Profiles Plan

Status: design plan
Scope: Hashi Remote, WatchTower client integration, WSL/Windows/LAN routing
Decision: keep one `main`; split platform behavior into runtime profiles, adapters, and local instance configuration

## Why This Exists

Recent HASHI1/HASHI9/WatchTower debugging showed that Hashi Remote is now a
multi-platform control plane, not a single-machine helper. A fix that is safe
for one runtime can regress another when platform assumptions are hidden in
shared routing, probing, or bootstrap code.

Observed failure pattern:

- HASHI1 WSL changes can affect HASHI9 Windows routing because same-host
  WSL/Windows and same-host WSL/WSL cases are treated too similarly.
- Fixed default ports such as `8766` and `8767` make instances fight for
  ports. New installs should allocate stable random local ports and persist
  them in instance configuration.
- `/remote list`, `/peers`, hchat, and WatchTower can disagree because they
  currently rely on overlapping but not identical route and liveness models.
- WatchTower may have a listening port while the HTTP app is stuck, which is
  different from firewall failure or process absence.
- HASHI core, Remote sidecar, and WatchTower have different lifecycle owners,
  but diagnostics often collapse them into one "instance offline" status.

The goal is not to fork HASHI into separate Windows and Linux branches. The
goal is to make platform differences explicit, testable, logged, and
backwards-compatible inside one code line.

## Decision

Do not create permanent branches such as `main-windows` and `main-linux`.

Use:

```text
one main
multiple runtime profiles
platform-specific adapters
stable local instance configuration
shared protocol contracts
strict cross-platform contract tests
```

Shared protocol and registry semantics stay common. Platform-specific behavior
is moved into declarative profiles and small adapter modules.

## Runtime Profiles

Each running instance should expose an effective runtime profile. The profile is
the source of truth for route construction, health probing, lifecycle control,
and display policy.

Initial profile families:

| Profile | Purpose |
|---|---|
| `wsl_linux_primary` | HASHI core running in WSL/Linux, usually with Remote sidecar in WSL |
| `wsl_linux_peer_same_host` | additional WSL instance on the same Windows host, with unique ports |
| `windows_remote_sidecar` | Windows-native HASHI/Remote sidecar such as HASHI9 |
| `watchtower_windows_rescue` | Windows WatchTower service supervising or rescuing HASHI |
| `lan_peer_pc` | separate physical/virtual host reachable over LAN or Tailscale |
| `validation_ephemeral` | test/validation alias that must not pollute normal `/remote list` |

## Display And Terminal Profile Policy

Terminal display is platform configuration, not HASHI core behavior. The same
HASHI banner can render correctly or incorrectly depending on the Windows
terminal host, font, code page, and whether the process is running inside WSL.

Policy:

- Keep the full HASHI banner available as the canonical rich display.
- Use the full glyph profile only when the terminal host is known to support
  the required glyphs.
- Use a Latin-safe glyph profile for classic Windows console hosts that can
  render ANSI/block art but do not provide CJK/Japanese glyph fallback.
- Treat font configuration as local platform configuration. It must not be
  hard-coded into HASHI core.

Current validated local setup:

- Windows Terminal WSL profiles use `Noto Sans SC`.
- HASHI2 desktop start path launches WSL through Windows Terminal and sets
  `BRIDGE_BANNER_GLYPH_PROFILE=full`.
- HASHI WSL launchers default to full glyphs when `WT_SESSION` is present and
  latin-safe glyphs when running under classic conhost.

This preserves the original rich animation for capable terminals while avoiding
square replacement glyphs on terminals that lack CJK font support.

Minimum profile fields:

```yaml
instance_id: HASHI9
profile: windows_remote_sidecar
platform: windows
host_role: remote_sidecar
machine_id: stable-host-id
wsl_root_hint: null
remote_port: 35821
watchtower_port: null
workbench_hosts:
  - 127.0.0.1
  - 192.168.0.211
bind_policy: lan_or_loopback
advertised_routes:
  - kind: loopback
    host: 127.0.0.1
    port: 35821
  - kind: lan
    host: 192.168.0.211
    port: 35821
relay_required: false
health_probe_policy: windows_remote
stale_route_policy: no_wsl_port_fallback
lifecycle_adapter: windows_task_or_process
display_policy: production
```

Profiles should be generated from existing config and runtime discovery first;
hand-written overrides are allowed only where the platform cannot be inferred
reliably.

## Port Allocation Policy

The previous fixed-port model is a root cause of recurring Remote, Workbench,
and API conflicts. New installs should not assume that `8766`, `8767`, `18800`,
or any other fixed port is globally available.

Port policy:

```text
allocate once, persist locally, then reuse
```

This means "random at first allocation", not "random on every boot". Random
ports that change on every restart would break discovery, hchat, Remote
handshake, supervisors, and WatchTower client configuration.

Recommended behavior:

- build candidates from a configurable broad pool, then filter out OS ephemeral,
  Windows excluded, already-bound, and already-assigned ports;
- on Linux/WSL, read `/proc/sys/net/ipv4/ip_local_port_range`;
- on Windows, inspect `netsh interface ipv4 show excludedportrange protocol=tcp`
  and `netsh int ipv4 show dynamicport tcp`;
- hold a machine-local allocation lock during check-then-persist;
- probe candidate ports before assignment;
- persist the selected port in instance configuration atomically;
- never overwrite an existing assignment on `git pull`;
- if a persisted assigned port is occupied, fail with an actionable error
  rather than silently reallocating;
- expose the effective assigned ports in `/protocol/status` and diagnostics;
- keep legacy fixed ports only as migration probes for older peers.

Recommended local state:

```yaml
instance:
  instance_id: HASHI1
  port_allocations:
    workbench_api:
      port: 43172
    api_gateway:
      port: 45218
    hashi_remote:
      port: 46793
```

Migration rule:

- existing deployments may keep their current ports until explicitly migrated;
- new deployments should allocate stable random ports;
- stale-route repair may probe legacy defaults for compatibility, but must not
  rewrite a peer away from its persisted assigned port unless the new route is
  verified and the profile policy allows repair.
- allocator reset tooling must exist before this becomes default-on, e.g.
  `hashi port reset <service>` and `hashi port reset --all`.

## Boundaries

### Shared Core

The following remain platform-neutral:

- remote protocol message schema
- handshake state machine
- shared-token HMAC behavior
- peer registry data model
- peer liveness state names
- agent directory snapshot schema
- file transfer and attachment capability semantics
- audit event names and correlation IDs

### Platform Adapters

The following move behind profile-aware adapters:

- route candidate construction
- same-host detection
- WSL host/guest bridge handling
- Windows firewall and listener diagnostics
- Remote sidecar start/stop/status
- WatchTower status/start/status-log operations
- stale route repair policy
- `/remote list` filtering and explanation text

No adapter should mutate peer registry state without a reason code and an audit
event.

## Adapter Design

Suggested module shape:

```text
remote/
  platform_profiles.py
  route_planner.py
  health_probe.py
  lifecycle_adapters/
    base.py
    linux_wsl.py
    windows_sidecar.py
    watchtower_windows.py
    lan_peer.py
  display_policy.py
```

The core Remote manager should call profile-neutral interfaces:

```python
profile = profiles.resolve(local_config, runtime_facts)
routes = route_planner.build_candidates(local_profile, peer_profile, peer_record)
probe_result = health_probe.probe(peer_profile, routes, budget=probe_budget)
lifecycle = lifecycle_adapters.for_profile(peer_profile)
```

The adapters must not hard-code instance names such as `HASHI1`, `HASHI2`, or
`HASHI9`. Instance-specific behavior belongs in config/profile data.

## Route Policy

Route choice must distinguish these cases:

| Case | Correct route behavior |
|---|---|
| same WSL distro / same host | loopback with unique Remote ports |
| WSL to Windows same host | explicit WSL-to-Windows bridge or LAN candidate, never blind loopback |
| Windows to WSL same host | explicit Windows-to-WSL reachable route, not a guessed WSL loopback |
| separate LAN PC | LAN/Tailscale route, optional default-port repair |
| validation alias | hidden from normal production list unless diagnostics request it |

Legacy fixed-port fallback must become a migration-only behavior. The desired
steady state is that every peer advertises and persists its assigned Remote
port. Route repair should prefer verified profile/instance config over any
hard-coded default. Hard-coded default probes are allowed only to recover older
peers during migration, and every rewrite must be reason-coded and audited.

## Health Probe Policy

Health probing must be asynchronous or moved off the event loop. A stuck or slow
offline peer must not block the local Remote HTTP service.

Required safeguards:

- global probe budget per rebuild
- per-peer timeout by profile
- concurrency limit
- stale candidate cap
- last-known-good route preservation
- audit event for every route rewrite
- clear distinction between process absent, port closed, HTTP timeout, auth
  failure, handshake failure, and app-level unhealthy

Important debugging distinction:

```text
listener exists + /health timeout != firewall failure
listener absent                  != app hung
hchat succeeds + /remote list red != target unreachable
```

## WatchTower Boundary

WatchTower is a separate program. HASHI should treat it as an external
rescue/control service, not as a normal HASHI agent and not as a normal Remote
peer.

Profile: `watchtower_windows_rescue`

Expected responsibilities:

- stay alive when HASHI core is down
- expose fixed health/status/log/start endpoints
- supervise cold restart when explicitly authorized
- report whether the controlled HASHI core is running

Non-goals:

- do not appear as a normal hchat-capable instance unless explicitly configured
- do not share Remote sidecar liveness semantics blindly
- do not let a listening WatchTower port imply that its HTTP app is healthy
- do not reserve HASHI core or Remote ports for WatchTower inside this repo
- do not start WatchTower as an embedded HASHI sidecar

Operationally, a WatchTower record should show two separate statuses:

```text
watchtower_service: online | http_timeout | port_closed | auth_failed
controlled_hashi: running | stopped | starting_or_stuck | unknown
```

## Display Policy

`/remote list` should present production peers by default and avoid mixing
ephemeral validation records into the normal operator view.

Display classes:

| Class | Examples | Default display |
|---|---|---|
| production | HASHI1, HASHI2, HASHI9, INTEL, MSI | show |
| rescue | WATCHTOWER, MSI-WT | show in rescue section |
| validation | WATCHTOWER_VALIDATE, WATCHTOWER_VALIDATE2 | hide unless `--all` or diagnostics |
| retired | old aliases | hide unless `--all` |

Every red/offline row should include an actionable reason if known:

```text
offline: remote_port_closed
offline: health_http_timeout
offline: handshake_timed_out
offline: stale_registry_route
offline: validation_alias_hidden
```

## Logging And Audit Requirements

Every profile-sensitive decision must produce structured logs. Minimum fields:

```json
{
  "event": "remote.route.selected",
  "request_id": "optional-correlation-id",
  "local_instance": "HASHI1",
  "peer_instance": "HASHI9",
  "local_profile": "wsl_linux_primary",
  "peer_profile": "windows_remote_sidecar",
  "candidate_count": 3,
  "selected_route": {"kind": "lan", "host": "192.168.0.211", "port": 35821},
  "reason": "profile_preferred_lan_for_wsl_to_windows",
  "elapsed_ms": 41
}
```

Required event families:

- `remote.profile.resolved`
- `remote.route.candidates_built`
- `remote.route.selected`
- `remote.route.rewrite_skipped`
- `remote.route.rewrite_applied`
- `remote.health.probe_started`
- `remote.health.probe_result`
- `remote.lifecycle.status_checked`
- `remote.display.filtered`
- `watchtower.health.result`
- `watchtower.controlled_hashi.status`

Logs should make it possible to answer:

- which profile was used?
- which route candidates were considered?
- why was a stale route repaired or not repaired?
- did a slow peer block the local Remote service?
- was an entry hidden because it is validation/retired?

## Compatibility

Backward compatibility rules:

- Existing `instances.json` and `remote_live_endpoints.json` remain readable.
- Missing profile fields should default to conservative legacy behavior.
- New profile fields should be additive.
- Unknown profile values should fail open for display but fail closed for route
  mutation.
- Legacy peers that do not advertise profiles should be classified using
  observed platform, route, port, host identity, and config hints.

Forward compatibility rules:

- Persist profile version.
- Preserve unknown profile fields.
- Include profile summaries in `/protocol/status`.
- Keep protocol messages tolerant of older peers that do not send profile data.

## Test Matrix

Any change to Remote registry, route bootstrap, peer liveness, or WatchTower
integration must cover this matrix before release:

| Scenario | Required evidence |
|---|---|
| HASHI1 WSL -> HASHI2 WSL same host | unique loopback ports preserved |
| HASHI1 WSL -> HASHI9 Windows same host | correct WSL/Windows route selected |
| HASHI9 Windows -> HASHI1 WSL | Windows-to-WSL route works or fails with clear reason |
| HASHI1 WSL -> INTEL/MSI LAN peer | LAN route and stale default-port repair behave correctly |
| WatchTower HTTP app hung | shown as HTTP timeout, not generic offline |
| Remote sidecar absent | shown as port closed/listener absent |
| hchat succeeds while `/remote list` red | detected as registry/display inconsistency |
| validation aliases exist | hidden from normal list, visible in diagnostics |
| many offline peers | local Remote `/health` and `/peers` remain responsive |

Lightweight unit tests should cover profile resolution and route planning.
Integration tests should use fake HTTP peers for timeout, auth failure, and
handshake failure. Live smoke tests should be reserved for real Windows/WSL/LAN
validation.

## Rollout Plan

### Phase 0: Document And Freeze Current Lessons

- Land this plan.
- Link it from the docs index and README Remote diagnostics.
- Do not change runtime behavior in this phase.

### Phase 1: Profile Resolver

- Add a profile resolver that reads existing config/runtime facts.
- Expose the effective profile in `/protocol/status`.
- Add tests for WSL primary, WSL same-host peer, Windows sidecar, WatchTower,
  LAN peer, and validation aliases.

### Phase 1B: Stable Port Allocator

- Add a local port allocator for Workbench API, API Gateway, and Hashi Remote.
- Allocate uncommon random ports once and persist them in instance config.
- Treat fixed ports as legacy migration hints, not permanent defaults.
- Add OS-aware filtering for Linux/WSL ephemeral ranges and Windows excluded
  ranges.
- Add a machine-local allocation lock so concurrent same-host first boots cannot
  select the same port.
- Fail with an actionable error if a persisted port is occupied; do not silently
  reallocate.
- Add `hashi port reset <service>` and `hashi port reset --all` before enabling
  allocator defaults.
- Add collision, persistence, lock, occupied-persisted-port, and migration tests.

Windows/HASHI9 impact:

- persist allocations in a Windows-native local config path for Windows-native
  HASHI9;
- do not write WSL paths into HASHI9's platform or instance config;
- verify `netsh` excluded/dynamic range filtering before assigning HASHI9 ports.

Rollback:

- revert the allocator commit;
- run `hashi port reset --all` only after confirming peers have been moved back
  to known-good legacy ports or a previous instance config backup is restored.

### Phase 2: Route Planner Extraction

- Move route candidate construction into one profile-aware planner.
- Preserve existing behavior by default.
- Add reason-coded decisions and audit logs.
- Add tests for same-host WSL, WSL-to-Windows, LAN peer, and stale route repair.

Windows/HASHI9 impact:

- HASHI9 must resolve through the `windows_remote_sidecar` profile/adapter;
- HASHI9 routes must not use WSL loopback unless an explicit Windows-to-WSL
  bridge route is configured and verified.

Rollback:

- revert route planner extraction;
- restore the previous `instances.json` / `remote_live_endpoints.json` backup if
  route repair wrote bad candidates.

### Phase 3: Non-Blocking Health Probes

- Ensure peer probing cannot block the local Remote HTTP event loop.
- Add probe budgets, timeout classes, and concurrency limits.
- Add regression tests where multiple offline peers do not make `/health` or
  `/peers` hang.

### Phase 4: Display Policy Cleanup

- Classify production, rescue, validation, and retired records.
- Hide validation aliases from normal `/remote list`.
- Add actionable offline reasons.

### Phase 5a: WatchTower Legacy Audit

- Produce an explicit list of legacy WatchTower docs, config comments, aliases,
  and code paths proposed for deletion or archival.
- Classify each item as `delete`, `archive`, `keep_client`, or `needs_migration`.
- Submit the list for Zelda/user approval before deleting anything.

Prerequisite:

- no deletion happens in Phase 5a.

Rollback:

- not applicable; audit-only.

### Phase 5b: WatchTower Adapter And Legacy Deletion

- Split WatchTower service health from controlled HASHI core health.
- Report HTTP timeout, port closed, auth failure, and core state distinctly.
- Keep rescue controls behind existing auth/terminal level gates.
- Delete or archive legacy code/docs that imply WatchTower runs inside this
  HASHI repo, after an audit confirms current `/restart` client behavior still
  talks to the external WatchTower service.

Prerequisite:

- HASHI9 must be running the `watchtower_windows_rescue` adapter and passing the
  WatchTower health probe matrix before any legacy deletion.

Windows/HASHI9 impact:

- HASHI9 WatchTower control must continue through the external WatchTower
  client path during and after deletion;
- do not remove `/restart` client integration unless the replacement adapter is
  live and verified.

Rollback:

- restore the Phase 5a audited files from git;
- switch HASHI9 back to the previous WatchTower client config if the new adapter
  fails.

### Phase 6: Cross-Platform Contract Gate

- Add a required test group for Remote platform behavior.
- Document the release gate in README and troubleshooting docs.
- Require independent review for route/registry/liveness changes before release.
- Wire `python scripts/check_protected_core_changes.py --cached` into a
  pre-commit hook or equivalent local release gate for environments without CI.

Rollback:

- disable the hook locally if it blocks an emergency authorized fix, then run
  the check manually with `--authorized` and record the reason.

## Exit Criteria

This work is complete when:

- one `main` supports WSL, Windows sidecar, WatchTower, and LAN peers through
  explicit profiles;
- new installs use stable random local ports persisted in instance config;
- legacy fixed ports are compatibility probes only;
- route rewrites are profile-aware, reason-coded, and audited;
- offline/stale peers cannot block the local Remote service;
- `/remote list`, `/peers`, and hchat use compatible liveness/route models;
- validation aliases no longer pollute normal operator views;
- WatchTower service health and controlled HASHI health are reported separately;
- the cross-platform test matrix is part of the release gate.
