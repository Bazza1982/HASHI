# Hashi Remote Rescue Protocol

For the full side-program rollout plan, see
`HASHI_REMOTE_SIDE_PROGRAM_UPGRADE_PLAN.md`. This document defines the concrete
start/status rescue endpoint contract.

Hashi Remote must be able to survive HASHI core failures. It should be treated
as a small sidecar service, not as a child process whose lifecycle depends on a
running Telegram agent.

## Problem

`/remote on` starts `python -m remote` from inside a running HASHI agent runtime.
That is useful for normal operation, but it is not enough for rescue:

- if HASHI core crashes or the launcher terminal closes, the child Remote
  process can also disappear depending on the platform/session;
- `/hchat` delivery depends on the local Workbench API, so it cannot deliver to
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
- Workbench health: HASHI Windows Workbench port

**Topology C** (Windows Remote -> WSL2 HASHI core) stays out of scope for v1.
Treat it as a future advanced mode after separate launcher, PID, and log
contracts exist for cross-boundary operation.

## Rescue Endpoints

Hashi Remote exposes a fixed control protocol:

```text
GET  /control/hashi/status
GET  /control/hashi/logs?name=start|audit|supervisor&tail=120
POST /control/hashi/start
```

`/control/hashi/status` reports whether local HASHI core is reachable through
the Workbench health endpoint and whether the `.bridge_u_f.pid` process appears
alive.

The status response should distinguish:

- `state=running`: Workbench health is reachable.
- `state=starting_or_stuck`: PID is alive but Workbench health is not reachable.
- `state=stale_pid`: PID file exists but the process is gone.
- `state=offline`: no live PID and no Workbench health.

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

`reason` contract for v1:

- stored as a single sanitized line
- truncated to at most `500` characters
- truncation is recorded in audit metadata when applicable

## Capability Advertisement

Upgraded Remotes advertise rescue support through protocol capabilities:

- `rescue_control`: status endpoint exists.
- `rescue_start`: start endpoint exists and this Remote is configured with
  `L3_RESTART`.

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
relay, but cannot start HASHI core.

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
```

Then poll `/control/hashi/status` until `hashi_running` is true. After HASHI is
back, normal `/hchat`, Workbench API, Telegram, and `/reboot` workflows can
resume.

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

Set `HASHI_REMOTE_MAX_TERMINAL_LEVEL=L3_RESTART` only on trusted LAN/Tailscale
machines where remote HASHI rescue is intentionally enabled. Default supervised
Remote remains `L2_WRITE`.

## Operator Notes

- Treat `/control/hashi/start` as a fixed rescue lever, not a generic remote
  shell.
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
python tools/remote_rescue.py status HASHI1 --json
```

The helper resolves peers from `instances.json`, tries HTTPS then HTTP, probes
live endpoints, and treats `404` from rescue endpoints as "unsupported" rather
than as a peer outage. Use `--token` or `HASHI_REMOTE_TOKEN` when the target
Remote is not in LAN auto-auth mode.

## Remaining Work

- Add end-to-end acceptance on a real Windows peer and a real WSL/Linux peer.
