# HASHI Remote Side Program Upgrade Plan

## Executive Summary

Hashi Remote should become a first-class side program for HASHI: a small,
supervised, long-running companion process that can outlive HASHI core, preserve
cross-instance communication, and perform controlled rescue actions when a peer
HASHI instance is down.

The current code already has the right foundation:

- `remote/main.py` can run `python -m remote` as an independent FastAPI service.
- `remote/api/server.py` already exposes health, peers, hchat, protocol message,
  terminal execution, and file transfer endpoints.
- `/remote on` can start Remote from inside HASHI for convenience.
- `tools/hchat_send.py` and `tools/remote_file_transfer.py` already know how to
  talk to Remote peers.

The gap is operational ownership. If Remote is only started by HASHI, then a
HASHI crash can also remove the rescue channel. The upgraded design makes Remote
supervised by the operating system and makes HASHI core a client of Remote, not
Remote's parent.

## Goals

- Keep all existing Hashi Remote functions working:
  - discovery,
  - `/health`,
  - `/peers`,
  - `/hchat`,
  - `/protocol/*`,
  - `/terminal/exec`,
  - `/files/push`,
  - `/files/stat`,
  - pairing and LAN auth behavior.
- Let Hashi Remote survive HASHI crashes, `/reboot`, terminal closure, and core
  process exits.
- Provide a clean remote rescue protocol to status/start HASHI core on another
  PC without opening arbitrary shell execution.
- Keep backward compatibility with older Hashi Remote instances that only know
  legacy `/hchat` or lack rescue endpoints.
- Make rollout staged, reversible, and safe under mixed-version LAN conditions.

## Non-Goals

- Do not make Remote a public internet admin surface.
- Do not allow unauthenticated arbitrary process control.
- Do not require all peers to upgrade simultaneously.
- Do not remove `/remote on`; keep it as a convenience/development path.
- Do not make HASHI core depend on Remote for local single-machine operation.

## Current Problem

The current operational model has two modes:

1. `python -m remote` can run independently if started manually.
2. `/remote on` starts Remote as a child of a running HASHI agent runtime.

Mode 2 is convenient but not rescue-safe. If HASHI core crashes, is killed as a
process tree, or loses its launcher session, the child Remote process may also
die. Even if Remote stays alive, legacy `/hchat` delivery still depends on the
local Workbench API, so agent delivery fails while HASHI core is down.

The result is a remote management paradox: the service that should rescue HASHI
is often owned by the HASHI process that needs rescuing.

## Target Architecture

```text
Operating System Supervisor
        |
        v
Hashi Remote Side Program
        |
        +-- discovery / peer registry / protocol routing
        +-- hchat compatibility relay
        +-- file transfer
        +-- rescue status/start protocol
        +-- optional terminal delegation
        |
        v
HASHI Core Process
        |
        +-- agents
        +-- Workbench API
        +-- API Gateway
        +-- Telegram / WhatsApp / local runtime services
```

Hashi Remote owns remote availability. HASHI core owns agent execution. Remote
can start HASHI core when authorized, but HASHI core should not be the only
process capable of starting Remote.

## Process Ownership Model

### Required Production Shape

Remote must be launched by an external supervisor:

- Linux/WSL:
  - preferred: `systemd --user`,
  - acceptable: `supervisord`, `tmux`, or a small shell supervisor.
- Windows:
  - preferred: Task Scheduler running at user logon,
  - acceptable: NSSM or a PowerShell background service wrapper.
- Development:
  - manual `python -m remote --no-tls --hashi-root <repo>`.

### HASHI-Owned `/remote on`

Keep `/remote on` for:

- local testing,
- quick operator enablement,
- old-machine compatibility,
- sidecar restart during normal development.

But documentation and status output should label it as:

```text
convenience child process, not rescue-grade supervision
```

### Remote-Owned HASHI Start

Remote should expose fixed rescue commands:

```text
GET  /control/hashi/status
POST /control/hashi/start
```

These commands do not accept arbitrary shell text. They use known local launcher
paths only:

- Linux/WSL/macOS: `bin/bridge-u.sh --resume-last`
- Windows: `bin/bridge_ctl.ps1 -Action start -Resume`
- fallback Windows: `bin/bridge-u.bat --resume-last --no-pause`

## Safety Model

Remote rescue is restart-class control, so it must be gated separately from
normal file transfer and messaging.

### Authorization Levels

Use existing `AuthLevel`:

- `L0_READ_ONLY`: health, peer status, rescue status.
- `L1_READ_FILES`: diagnostics.
- `L2_WRITE`: file push and normal write-level operations that do not start or
  restart long-lived HASHI processes.
- `L3_RESTART`: HASHI start/restart/rescue.
- `L4_SYSTEM`: OS reboot/shutdown, always disabled.

`POST /control/hashi/start` must require `L3_RESTART`.

### Default Posture

Default Remote remains:

```yaml
security:
  max_terminal_level: "L2_WRITE"
```

This preserves current behavior and blocks rescue start by default.

To enable rescue on trusted machines:

```yaml
security:
  max_terminal_level: "L3_RESTART"
```

For any non-LAN or Tailscale-wide use, turn off LAN auto-auth and use pairing:

```yaml
security:
  lan_mode: false
```

## Protocol Surface

### Existing Endpoints To Preserve

These endpoints remain compatible:

```text
GET  /health
GET  /peers
GET  /protocol/status
POST /protocol/handshake
GET  /protocol/agents
POST /protocol/message
POST /hchat
POST /terminal/exec
POST /files/push
GET  /files/stat
POST /pair/request
GET  /pair/status
POST /pair/approve
POST /pair/reject
```

### New Rescue Endpoints

```text
GET  /control/hashi/status
POST /control/hashi/start
```

Status response should include:

- `hashi_running`
- `state`: one of `running`, `offline`, `stale_pid`, `starting_or_stuck`
- `workbench_url`
- `workbench_health`
- `pid_file_exists`
- `pid`
- `pid_alive`
- timestamp
- optional `remote_supervisor` metadata when available.

Start response should include:

- `started`
- `already_running`
- `pid`
- `command`
- `log_path`
- post-start status.

### Future Supervisor Endpoints

Only after a supervisor install flow exists:

```text
GET  /control/remote/supervisor/status
POST /control/remote/supervisor/install
POST /control/remote/supervisor/restart
```

These should be local-admin only or require explicit pairing approval. They are
not required for first rollout.

## Backward Compatibility

Mixed-version LANs must remain stable. Every peer should be classified by
capability, not assumed upgraded.

### Capability Detection

Use `/health`, `/protocol/status`, and handshake capabilities:

- `remote_basic`: `/health` and `/peers`
- `legacy_hchat`: `/hchat`
- `protocol_v2`: `/protocol/message`
- `file_transfer`: `/files/push` and `/files/stat`
- `rescue_control`: `/control/hashi/status`
- `rescue_start`: `/control/hashi/start` allowed and L3-enabled

If a probe returns 404, mark capability false. Do not treat 404 as a peer
failure.

Capability data must have an explicit freshness rule:

- Direct endpoint probes are authoritative for the current request.
- Cached peer capabilities from handshake/registry are advisory.
- `tools/remote_rescue.py` should probe rescue endpoints live before showing a
  start option.
- Future `/protocol/message` preference should use cached capabilities only
  within a short TTL, then re-probe or force a fresh handshake before assuming
  structured protocol support.

### Compatibility Matrix

| Local Remote | Peer Remote | Behavior |
|---|---|---|
| new | new | Use full protocol, file transfer, rescue if authorized |
| new | old | Use legacy `/hchat`; hide rescue controls; keep file transfer only if endpoint exists |
| old | new | New peer must still accept legacy `/hchat` |
| old | old | Existing behavior unchanged |

### Legacy `/remote on`

Old peers may still start Remote through HASHI. New peers must not require the
remote process to advertise supervisor metadata.

### Legacy `/hchat`

Keep `/hchat` indefinitely as the compatibility ingress. New protocol routing
may prefer `/protocol/message`, but `/hchat` remains available for older peers.

## Deployment Components

### 1. Remote Control Scripts

Add:

```text
bin/hashi-remote-ctl.sh
bin/hashi_remote_ctl.ps1
```

Commands:

```text
install
uninstall
start
stop
restart
status
logs
```

These scripts manage the OS supervisor, not HASHI core.

### 2. Service Templates

Add templates:

```text
packaging/systemd/hashi-remote.service
packaging/windows/hashi-remote-task.xml
```

The generated command should be equivalent to:

```text
<venv-python> -m remote --hashi-root <repo> --no-tls --discovery lan
```

with optional:

```text
--max-terminal-level L3_RESTART
```

only when rescue is intentionally enabled.

Supervised Remote processes must advertise that they are supervisor-owned by
passing either:

```text
--supervised
```

or:

```text
HASHI_REMOTE_SUPERVISED=1
```

Remote should surface this through `/health` or `/protocol/status` as
`remote_supervisor.mode=supervised`. A Remote started by `/remote on` should
report `remote_supervisor.mode=child`; a reachable process without metadata
should report `external_unknown`.

### 3. Client Helper

Add:

```text
tools/remote_rescue.py
```

Commands:

```text
python tools/remote_rescue.py status HASHI1
python tools/remote_rescue.py start HASHI1 --reason "core down"
python tools/remote_rescue.py capabilities HASHI1
```

The helper should:

- use `instances.json` and peer registry data,
- try HTTPS then HTTP like existing hchat/file-transfer tools,
- treat 404 as missing capability,
- show precise errors for auth, offline, and disabled L3 start.

### 4. HASHI Runtime Integration

Update `/remote status` to distinguish:

- `supervised`: OS-owned side program.
- `child`: started by current HASHI runtime.
- `external/unknown`: Remote reachable but current runtime does not own it.
- `offline`: no Remote process/API.

Update `/remote on` output:

```text
Started as child process. For rescue-grade Remote, install side program supervisor.
```

## Phased Upgrade Plan

### Phase 0: Contract And Documentation

Deliverables:

- side program upgrade plan,
- rescue protocol document,
- safety model,
- compatibility matrix,
- operator checklist.

Acceptance:

- docs explain why `/remote on` is not rescue-grade;
- docs define how to enable/disable L3 rescue.

### Phase 1: Rescue Protocol Contract

Deliverables:

- `GET /control/hashi/status`
- `POST /control/hashi/start`
- `L3_RESTART` gate
- fixed launcher command only
- rescue start log file
- focused tests

Acceptance:

- `L2_WRITE` Remote rejects start with 403;
- `L3_RESTART` Remote can attempt fixed HASHI launch;
- status works even when HASHI core is down;
- existing `/hchat`, `/files/*`, and `/protocol/*` tests still pass.

### Phase 2: Supervisor Scripts

Deliverables:

- Linux/WSL `hashi-remote-ctl.sh`
- Windows `hashi_remote_ctl.ps1`
- install/uninstall/start/stop/status/logs
- service/task templates
- `--supervised` flag or `HASHI_REMOTE_SUPERVISED=1`
- Remote health/status metadata that reports supervisor ownership

Acceptance:

- killing HASHI core does not kill Remote;
- closing the HASHI terminal does not kill Remote;
- OS reboot/logon starts Remote when configured;
- `/remote status` detects supervised Remote as external/supervised.

### Phase 3: Client Tooling

Deliverables:

- `tools/remote_rescue.py`
- capability probing
- live endpoint probing for `rescue_control` and `rescue_start`
- capability cache TTL / refresh policy
- status/start UX
- auth/token handling
- JSON output mode for automation.

Acceptance:

- a healthy peer can detect down HASHI core on another PC;
- a healthy peer can request start when the target allows L3;
- missing rescue endpoints degrade to a clear "peer does not support rescue"
  message.
- stale cached capabilities cannot make the tool attempt `/protocol/message` or
  rescue start against a peer that no longer advertises support.

### Phase 4: Runtime UI Integration

Deliverables:

- `/remote status` shows supervisor mode and rescue capability.
- `/remote list` shows rescue-capable peers.
- optional Telegram buttons for "check HASHI core" and "request start" only when
  capability and auth allow it.

Acceptance:

- no rescue buttons shown for legacy peers;
- no start button shown for L2-only peers;
- operator gets a clear log path after start.

### Phase 5: Remote-Owned Messaging Hardening

Coordinate with `HASHI_REMOTE_P2P_UPGRADE_PLAN.md`:

- prefer `/protocol/message` when available;
- keep `/hchat` as legacy ingress;
- persist in-flight state under `~/.hashi-remote`;
- resume or abandon in-flight messages safely after sidecar restart;
- define capability cache TTL and first-use fresh-probe behavior;
- never depend on prompt text for protocol safety.

Acceptance:

- sidecar restart does not create duplicate replies;
- mixed-version peers do not loop;
- local hchat remains unchanged.

## Operator Runbooks

### Install Rescue-Grade Remote

1. Ensure repo and venv are valid.
2. Configure `remote/config.yaml`.
3. Decide whether this machine should allow L3 rescue.
4. Install supervisor:

```bash
bin/hashi-remote-ctl.sh install
bin/hashi-remote-ctl.sh start
bin/hashi-remote-ctl.sh status
```

or on Windows:

```powershell
.\bin\hashi_remote_ctl.ps1 install
.\bin\hashi_remote_ctl.ps1 start
.\bin\hashi_remote_ctl.ps1 status
```

### Rescue A Down Peer

```bash
python tools/remote_rescue.py status HASHI1
python tools/remote_rescue.py start HASHI1 --reason "HASHI core unreachable"
python tools/remote_rescue.py status HASHI1
```

Then resume normal workflows after Workbench health is back.

## Failure Modes To Handle

- Remote is down: cannot rescue; use OS/physical access.
- Remote is alive but L3 disabled: can diagnose, cannot start core.
- Workbench health is down but PID alive: report `state=starting_or_stuck`.
- PID file exists but process is not alive: report `state=stale_pid`, ignore the
  stale PID, and allow start.
- PID file missing and Workbench health is down: report `state=offline`.
- Launcher missing: return clear 500 with supported launcher paths.
- Port collision: start log should show launcher failure.
- Older peer returns 404: mark rescue unsupported, not failed.
- Auth failure: distinguish invalid token from unsupported endpoint.
- Windows doc/process locks: do not treat unrelated open files as Remote failure.

## Security Notes

- L3 rescue is powerful enough to start local processes. Do not enable it on
  untrusted networks.
- L4 OS reboot/shutdown remains prohibited.
- Avoid broad terminal rescue instructions; use fixed endpoints.
- Audit every rescue start request with requester, reason, command, PID, and log
  path.
- Prefer Tailscale or paired-token auth for cross-site rescue.

## Acceptance Criteria

The upgrade is complete when:

- Remote can be installed and started without HASHI core.
- Remote survives HASHI core crash, `/reboot`, and terminal closure.
- A healthy peer can detect that another peer's HASHI core is down.
- A healthy peer can start another peer's HASHI core when L3 is enabled.
- L2/default peers reject remote start.
- Old Hashi Remote peers still work for `/hchat` and are clearly marked as
  rescue-unsupported.
- `tools/hchat_send.py` and `tools/remote_file_transfer.py` remain compatible.
- `/remote on` still works as a development convenience.
- Docs clearly separate "child Remote" from "rescue-grade side program".

## Relationship To Existing Plans

This plan complements:

- `HASHI_REMOTE_P2P_UPGRADE_PLAN.md` for protocol-owned cross-instance messaging.
- `HASHI_REMOTE_PROTOCOL_SPEC.md` for structured message semantics.
- `HASHI_REMOTE_RESCUE_PROTOCOL.md` for the concrete start/status rescue
  endpoint contract.

The side program plan owns process supervision and rescue lifecycle. The P2P
plan owns message semantics.
