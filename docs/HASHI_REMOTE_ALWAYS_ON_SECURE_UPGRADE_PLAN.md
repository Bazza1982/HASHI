# HASHI Remote Always-On Secure Upgrade Plan

## Executive Summary

This plan upgrades Hashi Remote into an always-on, secure, OS-supervised side
program that continuously advertises each HASHI instance, publishes the active
agent directory, and remains available even when the main HASHI core process is
down.

The target shape is:

- Remote is enabled by default.
- Remote can be explicitly disabled and stays disabled until re-enabled.
- Remote continuously advertises reachable addresses, platform metadata, and
  active agents.
- Protocol handshakes require a shared security token.
- Remote runs consistently on Windows, WSL, Linux, and mixed same-host setups.
- Remote survives outside the HASHI core process and can be used for headless
  rescue and remote assistance.

This is not a replacement for the existing side-program and P2P protocol plans.
It is the operational hardening layer that makes those plans safe to run by
default.

Related documents:

- `HASHI_REMOTE_SIDE_PROGRAM_UPGRADE_PLAN.md`
- `HASHI_REMOTE_RESCUE_PROTOCOL.md`
- `HASHI_REMOTE_PROTOCOL_SPEC.md`
- `HASHI_REMOTE_P2P_UPGRADE_PLAN.md`
- `HASHI_REMOTE_FILE_TRANSFER.md`

## Current State

The repository already has the right foundation:

- `remote/main.py` runs Hashi Remote as an independent FastAPI/Uvicorn service.
- `remote/peer/lan.py` advertises and discovers peers over mDNS.
- `remote/peer/tailscale.py` provides overlay discovery.
- `remote/peer/registry.py` persists discovered peers into `instances.json`.
- `remote/protocol_manager.py` owns peer handshake, agent directory exchange,
  message routing, reply correlation, and liveness refresh.
- `remote/api/server.py` exposes health, peers, protocol, hchat, file transfer,
  pairing, terminal, and rescue endpoints.
- `bin/hashi-remote-ctl.sh` provides a Linux/WSL systemd user service helper.
- `bin/hashi_remote_ctl.ps1` provides a Windows Scheduled Task helper.
- Packaging templates already exist for systemd and Windows Task Scheduler.

The main gaps are operational and security related:

- Remote is not enabled by default.
- `/remote on` still starts Remote as a HASHI-owned child process, which is not
  rescue-grade.
- Supervisor mode exists but is optional rather than the preferred default.
- LAN mode can bypass token auth for protected endpoints.
- `/protocol/handshake` and `/protocol/message` are not currently protected by
  the same token gate as file transfer, terminal execution, and hchat relay.
- Discovery and liveness can publish stale state into `instances.json` when the
  sidecar is not running consistently.
- Port and route configuration can drift across same-host Windows/WSL instances.

## Goals

1. Make Hashi Remote default-on for normal HASHI installations.
2. Make Remote safe to expose on trusted LAN/Tailscale by requiring a shared
   security token for protocol trust.
3. Keep Remote independently supervised so it survives HASHI core crashes,
   terminal closure, agent reboot, and Workbench stalls.
4. Keep Remote explicitly disableable through config, CLI, and `/remote off`.
5. Make behavior consistent across Windows, WSL, Linux, and same-host mixed
   deployments.
6. Keep the core HASHI runtime minimal. Startup and lifecycle integration should
   live in focused Remote lifecycle modules rather than in a monolithic core.
7. Preserve backward compatibility where possible and fail closed where trust is
   unknown.
8. Add enough logging and diagnostics that peer visibility failures can be
   traced without guessing.

## Non-Goals

- Do not make Hashi Remote a public internet admin API.
- Do not allow unauthenticated discovery to become authenticated trust.
- Do not allow arbitrary remote shell execution by default.
- Do not require every peer to upgrade at the same time.
- Do not make local single-instance HASHI unusable if Remote fails.
- Do not store shared tokens in git-tracked files.
- Do not hide a disabled Remote by silently restarting it through a supervisor.

## Target Architecture

```text
Operating System Supervisor
        |
        v
Hashi Remote Side Program
        |
        +-- continuous discovery advertisement
        +-- secure protocol handshake
        +-- active agent directory publication
        +-- peer liveness and route registry
        +-- hchat / protocol message routing
        +-- file transfer
        +-- rescue status/start controls
        +-- audit and diagnostics
        |
        v
HASHI Core Process
        |
        +-- agents
        +-- Workbench API
        +-- Telegram / WhatsApp / local runtime services
```

Remote owns remote reachability. HASHI core owns agent execution. HASHI can ask
Remote to start, stop, or report status, but HASHI core is not the only process
that can keep Remote alive.

## Configuration Model

### New Effective Settings

Remote lifecycle should resolve settings from these sources, in priority order:

1. CLI flags and environment variables.
2. `secrets.json` for sensitive values.
3. `agents.json` global settings.
4. `instances.json` local instance entry.
5. `remote/config.yaml`.
6. Safe defaults.

Proposed settings:

```json
{
  "global": {
    "remote_enabled": true,
    "remote_supervised": true,
    "remote_port": 8766,
    "remote_discovery": "both",
    "remote_lan_mode": false,
    "remote_max_terminal_level": "L2_WRITE"
  }
}
```

Sensitive settings belong in `secrets.json` or environment variables:

```json
{
  "hashi_remote_shared_token": "not-committed"
}
```

Environment equivalents:

```text
HASHI_REMOTE_ENABLED=1
HASHI_REMOTE_SUPERVISED=1
HASHI_REMOTE_SHARED_TOKEN=...
HASHI_REMOTE_DISCOVERY=both
HASHI_REMOTE_PORT=8766
HASHI_REMOTE_MAX_TERMINAL_LEVEL=L2_WRITE
```

### Default Values

```text
remote_enabled=true
remote_supervised=true
remote_discovery=both
remote_lan_mode=false
remote_max_terminal_level=L2_WRITE
```

`remote_lan_mode=false` is the important security change. The old LAN
auto-approve behavior is convenient for development, but it is too permissive
for an always-on service.

### Explicit Disable State

`/remote off` should persist an operator disable state under the resolved
HASHI root, not under the current working directory. Supervisor processes may
start with a different CWD from HASHI core, so all lifecycle code must resolve
this path once from an absolute `HASHI_ROOT`.

Effective path:

```text
<HASHI_ROOT>/state/remote_disabled.json
```

Suggested payload:

```json
{
  "disabled": true,
  "disabled_at": "2026-05-12T21:30:00+10:00",
  "disabled_by": "operator",
  "reason": "manual /remote off"
}
```

The Remote entrypoint should resolve:

```python
HASHI_ROOT = Path(os.getenv("HASHI_ROOT", configured_hashi_root)).expanduser().resolve()
DISABLED_STATE_PATH = HASHI_ROOT / "state" / "remote_disabled.json"
```

The supervisor helper, HASHI startup integration, and `/remote status` must all
use the same absolute path. Otherwise a supervisor can miss the disabled state
and restart Remote immediately after the user turns it off.

## Security Model

### Trust Boundary

Discovery is not trust.

A peer may advertise:

- instance id,
- display handle,
- candidate host addresses,
- remote port,
- protocol version,
- public capabilities.

But until secure handshake passes, it must not receive:

- active agent directory,
- protocol messages,
- hchat relay,
- file transfer,
- terminal execution,
- rescue controls.

### Shared Token Handshake

Add a shared-token proof to `/protocol/handshake`.

Recommended payload fields:

```json
{
  "from_instance": "HASHI2",
  "timestamp": 1778579000,
  "nonce": "random-128-bit-value",
  "auth_scheme": "hashi-shared-hmac-v1",
  "auth_digest": "hex-hmac"
}
```

Digest input must include stable, replay-resistant fields:

```text
method + path + from_instance + timestamp + nonce + canonical_payload_hash
```

`canonical_payload_hash` is defined as the SHA256 hex digest of the exact HTTP
request body bytes received on the wire:

```python
canonical_payload_hash = hashlib.sha256(request_body_bytes).hexdigest()
hmac_input = "\n".join([
    method.upper(),
    path,
    from_instance.upper(),
    str(timestamp),
    nonce,
    canonical_payload_hash,
])
```

Do not compute this hash from an ad hoc `json.dumps()` of a Python dictionary.
The request body bytes are the transport-level canonical form and avoid
cross-version JSON serialization drift. This same definition must be mirrored in
`HASHI_REMOTE_PROTOCOL_SPEC.md` before Phase 1 code starts.

Use HMAC-SHA256 with the shared token. Do not send the token itself.

The receiver validates:

- token configured locally,
- timestamp inside the fixed `±300s` protocol window,
- nonce not recently used,
- digest matches,
- `from_instance` is not self,
- optional allowlist policy if configured.

Nonce replay protection:

- Store nonces in an in-memory per-Remote-instance TTL set.
- TTL is `2 * timestamp_window`, which is `600s` when the window is `±300s`.
- HASHI1 and HASHI2 on the same host use separate nonce stores because each
  Remote process has a separate instance id and port.
- On Remote restart, the nonce store is lost. This is acceptable for Phase 1
  because the fixed timestamp window limits replay exposure, but it must be
  logged as an intentional tradeoff and covered by tests.

If validation fails, the peer receives:

```json
{
  "status": "handshake_reject",
  "reason": "auth_failed"
}
```

### Protocol Message Authentication

Protect `/protocol/message` with the same HMAC envelope. A valid old handshake
should not be enough forever; each message should be authenticated or signed
with a short-lived session derived from the shared token.

Pragmatic phase-1 implementation:

- require HMAC on every `/protocol/handshake`;
- require HMAC on every `/protocol/message`;
- keep unauthenticated `/health` public but sanitized;
- make unauthenticated `/peers` return only `{"count": N}` and no peer entries;
- require valid shared-token auth for the full `/peers` list;
- keep file, terminal, rescue, and hchat endpoints behind existing token auth,
  then migrate them to the same shared auth helper.

### Public vs Trusted Health

`GET /health` can remain unauthenticated, but it should expose only public
metadata when no token is supplied:

- ok,
- instance id,
- display name,
- remote port,
- platform,
- protocol version,
- public capabilities,
- high-level peer count.

Authenticated health may include:

- full peer list,
- active agents,
- network candidates,
- liveness errors,
- supervisor state.

### Backward Compatibility

Mixed-version behavior should be explicit:

- If no token is configured locally, Remote should start in "untrusted
  discovery-only" mode unless an operator explicitly enables legacy LAN mode.
- If a peer does not support `hashi-shared-hmac-v1`, mark it as
  `handshake_rejected` with reason `auth_required`.
- If `remote_lan_mode=true`, allow legacy behavior only when explicitly
  configured. Log a warning at startup and in `/protocol/status`.

## Continuous Advertisement and Agent Directory

Remote should continuously publish:

- instance id,
- display handle,
- platform,
- host identity,
- environment kind,
- remote port,
- workbench port,
- protocol version,
- capabilities,
- address candidates,
- active agent snapshot version.

Agent directory publishing should not rely on a one-time startup snapshot.

### Refresh Triggers

Remote should refresh and advertise local agent metadata when:

- Remote starts;
- `agents.json` changes;
- HASHI core starts or stops;
- Workbench health changes;
- an agent becomes active/inactive;
- a peer handshakes;
- periodic refresh interval elapses.

Suggested intervals:

```text
discovery reannounce: 30s
agent snapshot refresh: 15s or on file mtime change
peer handshake revalidation: 30s
peer liveness refresh: 15s to 30s
stale threshold: 75s
offline threshold: 150s
```

### Snapshot Format

Minimum agent entry:

```json
{
  "agent_name": "rika",
  "agent_address": "rika@hashi2",
  "display_name": "里香",
  "is_active": true,
  "updated_at": 1778579000,
  "source": "agents.json"
}
```

If HASHI core is down, Remote should still advertise the last known agent
directory with:

```json
{
  "directory_state": "stale_core_offline",
  "last_core_seen_at": 1778578800
}
```

That allows remote assist tools to distinguish "Remote is alive but HASHI core
is down" from "the whole machine is unreachable".

## WSL and Windows Indifference

The upgraded Remote must treat platform differences as routing data, not as
special cases scattered through tools.

### Required Network Profile

Every Remote should publish a normalized local network profile:

```json
{
  "host_identity": "a9max",
  "environment_kind": "wsl",
  "address_candidates": [
    {"host": "127.0.0.1", "scope": "same_host", "source": "loopback"},
    {"host": "192.168.0.211", "scope": "lan", "source": "interface_scan"},
    {"host": "100.64.100.6", "scope": "overlay", "source": "tailscale"}
  ]
}
```

Route selection should happen in one Remote-owned resolver:

1. same-host loopback when host identity matches and port is local;
2. WSL host/guest bridge candidates;
3. LAN address;
4. Tailscale address;
5. configured fallback host;
6. stale cached route only as a last resort.

### Port Ownership

Same-host multi-instance deployments must not reuse the same Remote port.

Common layout:

```text
HASHI1 Remote 8766, Workbench 18800
HASHI2 Remote 8767, Workbench 18802
HASHI9 Remote 8768, Workbench 18819
INTEL  Remote 8766, Workbench 18802
MSI    Remote 8767, Workbench 8779
```

The repeated INTEL/MSI ports are safe only because they are on different hosts.
Across a LAN, `host:port` is not a peer identity; `instance_id` is the canonical
key. Route code and registry merge code must never collapse peers solely because
they share the same port. Same-host validation should catch collisions only
among instances with the same host identity.

The startup path should validate that:

- configured port is free;
- local `agents.json` agrees with local `instances.json`;
- the instance's own port does not collide with another same-host instance;
- cross-host peers can reuse ports safely because the host differs;
- port mismatch is logged as a clear actionable error.

### Windows Firewall and Binding

Windows helper scripts should optionally configure or verify inbound firewall
rules for the selected Remote port.

The default bind host can remain `0.0.0.0`, but diagnostics should report:

- actual listener address,
- Windows firewall status when detectable,
- whether LAN peers can connect,
- whether WSL peers need loopback or LAN routing.

## Lifecycle Model

### Startup

HASHI startup should call a small lifecycle module, not embed supervisor logic in
the core bootstrap:

```text
orchestrator/remote_lifecycle.py
```

Responsibilities:

- read effective Remote config;
- check persistent disabled state;
- install supervisor when configured and missing;
- start supervisor when configured and stopped;
- fall back to child process only in development or unsupported environments;
- log all decisions.

### Shutdown

Normal HASHI shutdown should not stop supervised Remote.

Rules:

- supervised Remote survives HASHI core shutdown;
- child Remote may stop with HASHI core;
- `/remote off` stops supervised Remote and writes disabled state;
- `/remote on` clears disabled state and starts supervisor or child process.

### Reboot

`/reboot min` and `/reboot max` should not disturb supervised Remote unless
Remote code/config changed and the operator explicitly requests Remote restart.

Add optional command:

```text
/remote restart
```

## Command and UI Behavior

### `/remote status`

Status should show:

- enabled/disabled state;
- supervisor mode: supervised, child, manual, unavailable;
- process status and PID;
- port and bind host;
- token mode: shared-token, legacy-lan, discovery-only;
- discovery backend;
- peer counts by live status;
- stale config warnings;
- last startup error;
- log path.

### `/remote list`

Peer blocks should distinguish:

- `online`: secure handshake accepted and recently seen;
- `untrusted`: discovered but not authenticated;
- `stale`: previously trusted but not recently refreshed;
- `offline`: route failed beyond threshold;
- `rejected`: auth/protocol rejected.

### `/remote off`

Expected behavior:

1. write persistent disabled state;
2. stop supervised service or child process;
3. update status output;
4. do not let supervisor restart it;
5. log operator, timestamp, and reason when available.

### `/remote on`

Expected behavior:

1. clear persistent disabled state;
2. validate token mode:
   - if a shared token is configured, start in trusted-auth capable mode;
   - if no shared token is configured, start in discovery-only mode;
   - `/remote status` must display
     `token_mode: discovery_only (no token configured)` and warn that trusted
     protocol messaging, full peers, file transfer, and rescue controls are not
     available;
3. install/start supervisor if configured;
4. fall back to child only when supervisor unavailable;
5. run health check;
6. show exact route and log path.

## Logging and Audit Requirements

Add structured logs for:

- effective config resolution;
- supervisor install/start/stop decisions;
- persistent disable and enable events;
- token mode warnings;
- handshake accepted/rejected/timed out;
- auth failures without logging token material;
- nonce replay detection;
- route candidate probes;
- agent snapshot changes;
- port collision detection;
- Windows firewall diagnostics;
- WSL route selection.

Suggested files:

```text
logs/hashi-remote-supervisor.log
logs/hashi_remote_security.jsonl
logs/hashi_remote_routes.jsonl
logs/hashi_remote_discovery.jsonl
logs/remote_rescue_audit.jsonl
```

Do not log shared tokens, bearer tokens, raw HMAC keys, or full Authorization
headers.

## Implementation Plan

### Phase 0: Baseline Diagnostics

Purpose: make current failures easy to see before changing behavior.

Work:

- Add a read-only diagnostic helper for effective Remote config.
- Add a route/port validation check for same-host multi-instance deployments.
- Improve `/remote status` output with supervisor, token, port, and route
  warnings.
- Add tests for stale/inactive registry reporting.

Validation:

- Unit tests for config resolution and warning generation.
- Manual status check on HASHI1/HASHI2/HASHI9/INTEL layout.

### Phase 1: Shared Token Auth

Purpose: make always-on safe.

Work:

- Add `remote/security/shared_token.py`.
- Load shared token from environment or `secrets.json`.
- Implement HMAC signing and verification helpers.
- Define `canonical_payload_hash` as SHA256 over exact request body bytes in
  both implementation and `HASHI_REMOTE_PROTOCOL_SPEC.md`.
- Define the fixed timestamp window as `±300s`.
- Add a per-instance in-memory nonce TTL store with `600s` retention.
- Require authenticated handshake for trusted state.
- Require authenticated `/protocol/message`.
- Sanitize unauthenticated `/health`.
- Make unauthenticated `/peers` count-only; full peer entries require auth.
- Add explicit legacy LAN mode warnings.

Validation:

- Good token accepts handshake.
- Missing token rejects handshake.
- Wrong token rejects handshake.
- HMAC verification fails if request body bytes change after signing.
- Replay nonce rejects handshake.
- Timestamp outside `±300s` rejects handshake.
- Old peer is marked `auth_required` instead of online.
- `/health` unauthenticated returns public metadata only.
- `/peers` unauthenticated returns count only.

### Phase 2: Default-On Lifecycle

Purpose: make Remote start consistently.

Work:

- Add `orchestrator/remote_lifecycle.py`.
- Add effective settings for `remote_enabled` and `remote_supervised`.
- Start/ensure Remote during HASHI startup when enabled.
- Do not stop supervised Remote during HASHI shutdown/reboot.
- Keep child-process `/remote on` as fallback.
- Add persistent disabled state.

Validation:

- Fresh HASHI startup starts Remote by default.
- `/remote off` stops Remote and survives restart.
- `/remote on` clears disabled state.
- Missing shared token starts Remote in discovery-only mode with an explicit
  status warning.
- `/reboot min` does not kill supervised Remote.
- If supervisor is unavailable, status explains fallback.
- Peer agent directories are marked
  `directory_state: snapshot_may_be_stale` until Phase 3 continuous refresh is
  implemented.

### Phase 3: Continuous Advertisement

Purpose: keep registry and agent directory fresh.

Work:

- Add agent snapshot watcher based on `agents.json` mtime and optional core
  health state.
- Reannounce discovery metadata periodically.
- Include agent snapshot version in advertised metadata.
- Re-handshake when agent snapshot changes.
- Mark directory state as fresh, stale, or core offline.

Validation:

- Agent activation change appears in peer directory without Remote restart.
- Core down leaves Remote online with stale directory state.
- Peer registry updates `instances.json` only with fresh liveness.
- Phase 2's `snapshot_may_be_stale` marker is replaced by fresh/stale/core
  offline states.

### Phase 4: WSL/Windows Route Unification

Purpose: make same-host and cross-host routing predictable.

Work:

- Centralize route candidate construction in Remote.
- Validate same-host port ownership.
- Prefer loopback for true same-host peers.
- Prefer LAN/Tailscale for cross-host peers.
- Add Windows firewall diagnostics to PowerShell helper.
- Add WSL host/guest bridge diagnostics.

Validation:

- HASHI1 WSL to HASHI2 WSL routes over loopback with unique ports.
- HASHI1 WSL to HASHI9 Windows uses the correct reachable route.
- INTEL Windows to HASHI9 Windows uses LAN route.
- Port conflicts produce actionable errors.

### Phase 5: Headless Remote Assist

Purpose: make Remote useful when HASHI core is down.

Work:

- Ensure supervised Remote can start without Workbench.
- Keep rescue status/start independent of core.
- Add authenticated remote assist commands for status, logs, and fixed HASHI
  start.
- Keep arbitrary terminal execution behind existing auth level gates.
- Document operational runbooks.

Validation:

- Kill HASHI core; Remote remains reachable.
- `GET /control/hashi/status` reports core offline.
- Authenticated `POST /control/hashi/start` starts core when L3 is enabled.
- L2 default blocks start.
- `/remote status` reports `rescue_start_enabled: false (requires L3)` when
  the default L2 level is active.
- Supervisor install/onboarding clearly asks the operator whether rescue start
  should be enabled with `L3_RESTART` before a core outage occurs.

### Phase 6: Documentation and Migration

Purpose: make rollout safe for existing machines.

Work:

- Update README Remote section.
- Update install docs for Linux/WSL and Windows.
- Add migration notes for shared token setup.
- Add troubleshooting guide for invisible peers.
- Document rollback steps.

Validation:

- New install guide starts Remote default-on.
- Existing install can opt out.
- Mixed-version warning is clear.

## Test Plan

### Unit Tests

- shared token load precedence;
- HMAC signing and verification;
- timestamp skew rejection;
- nonce replay rejection;
- unauthenticated public health redaction;
- trusted health full detail;
- persistent disabled state;
- supervisor command construction;
- same-host route candidate ordering;
- port collision warnings;
- agent snapshot mtime refresh;
- handshake reject reasons.

### Integration Tests

- start Remote under test root with token;
- two local Remote instances with different ports handshake successfully;
- wrong-token peer is visible but untrusted;
- `/protocol/message` rejects missing auth;
- upgraded HASHI1 sees an old peer without HMAC as untrusted/auth_required;
- legacy hchat compatibility still works for an old peer when explicitly
  enabled during rolling deployment;
- `/remote off` prevents supervisor restart;
- HASHI core down while Remote stays alive;
- rescue status works without Workbench.

### Manual Acceptance Matrix

```text
HASHI1 WSL      -> HASHI2 WSL      same host, unique ports
HASHI1 WSL      -> HASHI9 Windows  same machine, WSL/Windows boundary
HASHI1 WSL      -> INTEL Windows   LAN peer
INTEL Windows   -> HASHI9 Windows  LAN peer
HASHI9 Windows  -> HASHI1 WSL      Windows to WSL
```

For each pair:

- `/remote list` shows peer online after secure handshake;
- active agents are visible;
- hchat route check succeeds;
- protocol message delivery succeeds;
- file stat/push requires auth;
- core-down rescue status remains available.

## Rollout Strategy

1. Implement token auth behind a feature flag.
2. Deploy to HASHI1 only in compatibility mode.
3. Deploy to HASHI2 and HASHI9 with shared token.
4. Enable default-on supervisor on one peer at a time.
5. Complete Phase 3 continuous advertisement within 7 days of any Phase 2
   production rollout, or roll Phase 2 back to manual start. Phase 2 without
   Phase 3 is acceptable only as a short transition because agent directories
   are marked `snapshot_may_be_stale`.
6. Turn off legacy LAN auto-auth.
7. Validate INTEL and MSI cross-host discovery.
8. Update docs and make default-on the normal path.

## Rollback Strategy

Remote default-on changes must be reversible:

- `remote_enabled=false` disables startup integration.
- `HASHI_REMOTE_ENABLED=0` overrides config.
- `bin/hashi-remote-ctl.sh uninstall` removes Linux/WSL supervisor.
- `bin/hashi_remote_ctl.ps1 uninstall` removes Windows Scheduled Task.
- `remote_lan_mode=true` can temporarily allow legacy LAN auth during emergency
  mixed-version debugging, but should log warnings.

Protocol rollback:

- Keep legacy `/hchat` compatibility path.
- Keep `/remote on` child-process path.
- Keep older peers visible as untrusted/discovery-only rather than deleting
  them from the registry.

## Risks and Mitigations

### Risk: Default-on exposes a wider attack surface

Mitigation:

- shared-token handshake required by default;
- LAN auto-auth disabled by default;
- privileged endpoints keep auth level gates;
- unauthenticated health is redacted.

### Risk: Token mismatch makes all peers invisible

Mitigation:

- show `auth_failed` peer state;
- log reject reason without token material;
- provide `remote doctor` diagnostics.

### Risk: Supervisor restarts Remote after user turns it off

Mitigation:

- persistent disabled state;
- supervisor helper checks disabled state before start where possible;
- `/remote status` reports disabled state explicitly.

### Risk: Same-host Windows/WSL routes choose the wrong address

Mitigation:

- central route resolver;
- host identity matching;
- port ownership validation;
- route probe logs.

### Risk: Mixed-version peers fail unpredictably

Mitigation:

- explicit capabilities;
- `auth_required` and `unsupported_protocol` states;
- legacy compatibility path only when explicitly enabled.

## Open Decisions

1. Shared token storage:
   - `secrets.json` only,
   - environment only,
   - or both with environment priority.
2. Whether a missing token should prevent Remote startup or start in
   discovery-only mode.
3. Whether supervisor install should happen automatically on first startup or
   only after an explicit command.
4. Whether Windows firewall rule creation should be automatic or diagnostic
   only.

Recommended defaults:

- support both `HASHI_REMOTE_SHARED_TOKEN` and `secrets.json`;
- public-redacted `/health`;
- unauthenticated `/peers` returns only `{"count": N}`;
- authenticated `/peers` returns full peer entries;
- missing token starts discovery-only with warning;
- supervisor install requires explicit first-time operator approval, but once
  installed it starts by default;
- Windows firewall starts diagnostic-only, then add opt-in auto-fix.

## Acceptance Criteria

The upgrade is complete when:

- Remote starts by default on supported installations.
- Remote can be turned off and stays off.
- Remote survives HASHI core shutdown.
- Secure handshake requires the shared token.
- Wrong-token peers cannot receive agent directories or messages.
- Active agent lists refresh without restarting Remote.
- HASHI1/HASHI2/HASHI9/INTEL style deployments show correct peer status.
- WSL/Windows same-host routes are deterministic and logged.
- Remote rescue status works when Workbench is down.
- Tests cover auth, lifecycle, discovery, routing, and rescue behavior.
