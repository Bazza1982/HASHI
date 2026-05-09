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

## Rescue Endpoints

Hashi Remote exposes a fixed control protocol:

```text
GET  /control/hashi/status
POST /control/hashi/start
```

`/control/hashi/status` reports whether local HASHI core is reachable through
the Workbench health endpoint and whether the `.bridge_u_f.pid` process appears
alive.

`/control/hashi/start` starts HASHI through a fixed launcher command:

- Windows native: `bin/bridge_ctl.ps1 -Action start -Resume`, falling back to
  `bin/bridge-u.bat --resume-last --no-pause`.
- Linux/WSL/macOS: `bin/bridge-u.sh --resume-last`.

The endpoint writes stdout/stderr to:

```text
logs/remote_rescue_hashi_start.log
```

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

## Remaining Work

- Add `bin/hashi-remote-ctl.*` scripts for install/start/stop/status under the
  OS supervisor.
- Add an authenticated client helper, for example
  `tools/remote_rescue.py status|start HASHI1`.
- Add service templates for WSL/Linux `systemd --user` and Windows Task
  Scheduler.
