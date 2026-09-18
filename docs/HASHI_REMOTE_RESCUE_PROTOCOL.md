# HASHI WatchTower And Remote Rescue Protocol

For the full side-program rollout plan, see
`HASHI_REMOTE_SIDE_PROGRAM_UPGRADE_PLAN.md`. This document defines the concrete
start/status/restart rescue endpoint contract.

Hashi Remote must be able to survive HASHI core failures. It should be treated
as a small sidecar service, not as a child process whose lifecycle depends on a
running Telegram agent.

## WatchTower Role

HASHI WatchTower is an optional external rescue controller for a protected
HASHI instance. It runs outside the HASHI process and exposes a narrow LAN
rescue API for status, logs, start, and hard restart.

WatchTower enables two operational capabilities:

- **LAN rescue**: another trusted HASHI instance or operator tool can inspect
  and start a down HASHI instance without relying on that instance's Telegram
  bot or Backend API.
- **External cold restart**: an operator can ask WatchTower to stop a HASHI
  process, restart it with the configured launcher, and verify that Backend API
  health returns.

The normal `/restart` command does not depend on WatchTower. It selects the
calling instance's own supervised HASHI Remote, which survives the Core process,
and that Remote owns stop/start/verify. The Remote must advertise
`rescue_restart`, which requires `L3_RESTART`. An explicitly named trusted peer
is routed to that peer's Remote only after the bilateral handshake and live
capability checks pass. There is no automatic WatchTower fallback.

## Problem

`/remote on` starts `python -m remote` from inside a running HASHI agent runtime.
That is useful for normal operation, but it is not enough for rescue:

- if HASHI core crashes or the launcher terminal closes, the child Remote
  process can also disappear depending on the platform/session;
- `/hchat` delivery depends on the local Backend API, so it cannot deliver to
  agents while HASHI core is down;
- generic `/terminal/exec` is too broad for a clean remote-start protocol.

## Target Shape

Run Hashi Remote under an OS-level supervisor:

- Linux/WSL: `systemd --user`, `tmux`, `supervisord`, or another user service.
- Windows: Task Scheduler, NSSM, or a small persistent PowerShell service.
- Development fallback: manually run `python -m remote --no-tls --hashi-root <repo>`.

HASHI core may still start/stop Remote for convenience, but production rescue
should not rely on `/remote on`.

## v1 Topology

v1 implements **Topology A only**:

- Remote: Windows native
- HASHI core: Windows native
- launcher: `bin/bridge_ctl.ps1 -Action start -Resume`
- fallback: `bin/bridge-u.bat --resume-last --no-pause`
- PID checks: Windows PID semantics
- Backend API health: HASHI Windows Backend API port

**Topology C** (Windows Remote -> WSL2 HASHI core) stays out of scope for v1.
Treat it as a future advanced mode after separate launcher, PID, and log
contracts exist for cross-boundary operation.

## Rescue Endpoints

Hashi Remote exposes a fixed control protocol:

```text
GET  /control/hashi/status
GET  /control/hashi/logs?name=start|restart|audit|supervisor&tail=120
POST /control/hashi/start
POST /control/hashi/restart
POST /control/hashi/reboot
GET  /control/hashi/restarts/{restart_id}
```

`/control/hashi/status` reports whether local HASHI core is reachable through
the Backend API health endpoint and whether this instance's
`state/instance/process.pid` process appears
alive.

The status response should distinguish:

- `state=running`: Backend API health is reachable.
- `state=starting_or_stuck`: PID is alive but Backend API health is not reachable.
- `state=stale_pid`: PID file exists but the process is gone.
- `state=offline`: no live PID and no Backend API health.

`/control/hashi/start` starts HASHI through a fixed launcher command:

- Windows native: `bin/bridge_ctl.ps1 -Action start -Resume`, falling back to
  `bin/bridge-u.bat --resume-last --no-pause`.
- Linux/WSL/macOS: `bin/bridge-u.sh --resume-last`.

The endpoint writes stdout/stderr to:

```text
logs/remote_rescue_hashi_start.log
```

Each start attempt also appends a structured audit record to:

```text
logs/remote_rescue_audit.jsonl
```

`/control/hashi/logs` returns bounded tails from fixed log names only. It does
not accept arbitrary paths.

`tail` contract for v1:

- default `120`
- maximum `1000`
- values above `1000` are truncated to `1000`
- non-positive or invalid values return `400`
- responses include `requested_tail`, `effective_tail`, and `tail_truncated`

The audit record includes requester, reason, launcher command, PID, log path,
outcome, status state, and error text when available.

`/control/hashi/restart` performs a supervised hard restart:

1. create a restart record under `state/restarts/`
2. stop the controlled HASHI process with the fixed launcher/control script
3. wait for the old process to stop
4. start HASHI again with the fixed launcher
5. verify Backend API health
6. verify the old PID exited, the new PID differs and is alive, Backend API
   health is ready, instance identity matches, and runtime version plus Function
   generation match the pre-restart expectation
7. update the restart record to `completed` or a failed phase
8. append a structured audit event to `logs/remote_rescue_audit.jsonl`

Terminal verification must not monopolize Remote's request loop. Blocking
Workbench health probes run outside that loop so the newly started Core can
query Remote `/health` while the original restart request is still waiting.
This liveness rule does not relax any PID, identity, runtime, generation, or
Backend health evidence required for a completed receipt.

On Linux, Remote's systemd user unit manages only the Remote main process.
HASHI started through the rescue endpoint is a separately controlled runtime
and must survive a Remote supervisor reload; stopping or restarting HASHI still
uses the explicit HASHI control endpoint and its terminal receipt.

`/control/hashi/restarts/{restart_id}` returns the durable restart record for a
single restart id. Restart ids are validated before file lookup to avoid path
traversal.

`/control/hashi/reboot` accepts one validated Agent name and `mode=min`. It
first requests the ordinary per-Agent hot reboot through Workbench's
token-protected `/api/admin/command` endpoint. If Workbench is unreachable and
the caller permits fallback, Remote launches the fixed hard-restart path and
records that escalation in the rescue audit. A rejected hot reboot is returned
as a failure and is not silently upgraded to a hard restart.

`reason` contract for v1:

- stored as a single sanitized line
- truncated to at most `500` characters
- truncation is recorded in audit metadata when applicable

Restart requests bind the operation to the controlled instance and identify the
requesting Agent and supported command source:

```json
{
  "reason": "telegram /restart hard restart",
  "target_instance": "HASHI3",
  "request_source": "telegram",
  "requester_agent": "hashiko"
}
```

No separate human HMAC proof, nonce, replay database, or second confirmation is
required. Authorization remains the existing authenticated Remote control
protocol plus `L3_RESTART`. The target Remote rejects a request before launch
unless `target_instance` matches the instance controlled by that Remote. The
same credential cannot authorize arbitrary shell execution, service-definition
changes, or secret reads through this endpoint.

After the controlled HASHI process comes back, Remote makes a best-effort call
when `notify_agent` was supplied:

```text
POST /api/admin/notify
```

on the controlled Backend API with an admin-authenticated payload such as:

```json
{"agent":"hashiko","text":"HASHI restart completed."}
```

This endpoint sends a bounded operator notification through the target agent's
primary chat. It is not a general command execution endpoint. Notification
success or failure is stored in the restart record but never changes the
already verified restart result.

## Capability Advertisement

Upgraded Remotes advertise rescue support through protocol capabilities:

- `rescue_control`: status endpoint exists.
- `rescue_start`: start endpoint exists and this Remote is configured with
  `L3_RESTART`.
- `rescue_restart`: restart endpoint exists and this Remote is configured with
  `L3_RESTART`.
- `rescue_reboot`: supervised per-Agent hot reboot exists and this Remote is
  configured with `L3_RESTART`.

Older Remotes will not advertise these capabilities and may return `404` for
the rescue endpoints. Client tools must treat that as "unsupported", not as a
peer outage.

## Safety Gate

Remote start is a restart-class operation. It is blocked unless Hashi Remote is
started with:

```text
--max-terminal-level L3_RESTART
```

or the equivalent `remote/config.yaml` setting:

```yaml
security:
  max_terminal_level: "L3_RESTART"
```

Default `L2_WRITE` Remote instances can push files and perform normal hchat
relay, but cannot start or restart HASHI core.

Do not expose an L3 Remote instance directly to the public internet. Use
Tailscale or a trusted LAN, and disable LAN auto-auth before any wider network
exposure.

## Operational Flow

From a healthy peer:

```bash
curl http://<host>:<remote-port>/control/hashi/status
curl -X POST http://<host>:<remote-port>/control/hashi/start \
  -H 'Content-Type: application/json' \
  -d '{"reason":"remote rescue"}'
curl -X POST http://<host>:<remote-port>/control/hashi/restart \
  -H 'Content-Type: application/json' \
  -d '{"reason":"operator hard restart"}'
curl -X POST http://<host>:<remote-port>/control/hashi/reboot \
  -H 'Content-Type: application/json' \
  -d '{"agent":"agent1","mode":"min","reason":"supervised hot reboot"}'
```

Then poll `/control/hashi/status` until `hashi_running` is true. After HASHI is
back, normal `/hchat`, Backend API, Telegram, and `/reboot` workflows can
resume.

The Telegram restart menu keeps its existing single dangerous-operation
confirmation. A directly authorized `/restart` command, including the local
admin command path used by an Agent, does not add a second confirmation layer.

Other supported frontends use their existing command authorization. They do not
create a separate proof or a second remote confirmation.

## Supervisor Control Scripts

Phase 2 adds optional OS supervisor helpers:

```text
bin/hashi-remote-ctl.sh
bin/hashi_remote_ctl.ps1
packaging/systemd/hashi-remote.service
packaging/windows/hashi-remote-task.xml
```

Linux/WSL:

```bash
bin/hashi-remote-ctl.sh install
bin/hashi-remote-ctl.sh start
bin/hashi-remote-ctl.sh status
bin/hashi-remote-ctl.sh logs
```

Windows PowerShell:

```powershell
.\bin\hashi_remote_ctl.ps1 install
.\bin\hashi_remote_ctl.ps1 start
.\bin\hashi_remote_ctl.ps1 status
.\bin\hashi_remote_ctl.ps1 logs
```

The supervisor starts Remote with `--supervised`, so `/protocol/status` can
report `remote_supervisor.mode=supervised`. Legacy `/remote on` still works and
should report `remote_supervisor.mode=child`.

Supervisor health checks use the explicit command port first, then the
instance-owned `remote_port` from `instances.json` or `agents.json`, and only
then the YAML compatibility default. A healthy endpoint for another instance
is rejected; it must never satisfy `start`, `restart`, or `doctor`.

For an existing same-principal installation, `start` and `restart` verify that
the credential file is readable and preserve its restrictive ACL. ACL mutation
is reserved for initial provisioning or an intentional principal change.

Set `HASHI_REMOTE_MAX_TERMINAL_LEVEL=L3_RESTART` only on trusted LAN/Tailscale
machines where remote HASHI rescue is intentionally enabled. Default supervised
Remote remains `L2_WRITE`.

## Operator Notes

- Treat `/control/hashi/start` as a fixed rescue lever, not a generic remote
  shell.
- Treat `/control/hashi/restart` as a destructive cold-restart operation. It
  must be scoped to the intended HASHI instance and audited.
- Prefer `/control/hashi/reboot` for a healthy Core. It is Agent-scoped,
  authenticated at both Remote and Workbench, and falls back to a cold restart
  only when Workbench is unreachable and fallback was explicitly allowed.
- Prefer `bridge_ctl.ps1` on HASHI9 Windows because it follows the native
  bridge lifecycle more reliably than ad-hoc process creation.
- Use `/control/hashi/logs?name=audit` first when checking who initiated a
  rescue and why; use `name=start` for launcher output and `name=supervisor`
  for the always-on Remote wrapper.

## Client Helper

Phase 3 adds:

```text
tools/remote_rescue.py
```

Commands:

```bash
python tools/remote_rescue.py capabilities HASHI1
python tools/remote_rescue.py status HASHI1
python tools/remote_rescue.py logs HASHI1 --name start --tail 120
python tools/remote_rescue.py start HASHI1 --reason "core down"
python tools/remote_rescue.py restart WATCHTOWER --target-instance HASHI3 --reason "operator hard restart"
python tools/remote_rescue.py restart-status WATCHTOWER <restart-id>
python tools/remote_rescue.py status HASHI1 --json
```

The helper resolves peers from `instances.json`, tries HTTPS then HTTP, probes
live endpoints, and treats `404` from rescue endpoints as "unsupported" rather
than as a peer outage. Use `--token` or `HASHI_REMOTE_TOKEN` when the target
Remote is not in LAN auto-auth mode.

## Remaining Work

- Add end-to-end acceptance on a real Windows peer and a real WSL/Linux peer.
