# HASHI Remote Migration and Rollback

This guide covers migration from child-process Remote to the default-on,
shared-token, rescue-capable Remote sidecar.

## Migration Goals

- Remote starts by default unless explicitly disabled.
- Trusted protocol traffic uses shared-token HMAC.
- `/health`, `/peers`, and `/protocol/status` are redacted for unauthenticated
  non-loopback callers.
- Agent directories refresh without restarting Remote.
- Same-host WSL/Windows routes prefer loopback with unique Remote ports.
- Rescue status/logs remain available when HASHI core is down.

## Before You Start

1. Confirm each instance has a unique `remote_port` when it can run on the same
   host as another HASHI instance.
2. Pick one long random shared token for trusted peers.
3. Decide whether this machine should allow remote HASHI start:
   - default: `L2_WRITE`, rescue start disabled;
   - opt-in: `L3_RESTART`, rescue start enabled.

## Shared Token Setup

Preferred for supervisors and shells:

```bash
export HASHI_REMOTE_SHARED_TOKEN="<long random token>"
```

Persistent per-instance option:

```json
{
  "hashi_remote_shared_token": "<long random token>"
}
```

Put the JSON key in each instance's `secrets.json`. All trusted peers must use
the same token. If a token is missing, Remote starts in `discovery-only` mode:
peers can be counted/discovered, but trusted protocol messaging and rescue
control are unavailable.

## Rolling Upgrade Order

1. Upgrade one instance and start Remote.
2. Check local status:

```bash
/remote status
```

3. Confirm token mode is `shared-token`, not `discovery-only`.
4. Upgrade the next peer and repeat.
5. During mixed-version rollout, old peers without HMAC may appear as
   `untrusted`, `auth_required`, or legacy-only. This is expected and should
   not be treated as a network outage.
6. After every peer is upgraded, verify:

```bash
/remote list
python tools/remote_rescue.py capabilities <INSTANCE>
```

## Supervisor Install

Linux / WSL:

```bash
bin/hashi-remote-ctl.sh install
bin/hashi-remote-ctl.sh start
bin/hashi-remote-ctl.sh status
```

Windows:

```powershell
.\bin\hashi_remote_ctl.ps1 install
.\bin\hashi_remote_ctl.ps1 start
.\bin\hashi_remote_ctl.ps1 status
.\bin\hashi_remote_ctl.ps1 doctor
```

Keep `security.max_terminal_level: "L2_WRITE"` unless you intentionally want
remote HASHI start. To enable rescue start:

```yaml
security:
  max_terminal_level: "L3_RESTART"
```

Only use `L3_RESTART` on trusted LAN/Tailscale machines.

## Verification

- `/remote status` shows lifecycle, supervisor mode, token mode, route warnings,
  and rescue start state.
- `/remote list` shows peers as `online` after secure handshake.
- `/protocol/status` includes `route_diagnostics`, `local_agent_directory`, and
  rescue capability flags.
- `python tools/remote_rescue.py status <INSTANCE>` returns the core state.
- `python tools/remote_rescue.py logs <INSTANCE> --name start` returns a bounded
  log tail.

## Troubleshooting

- `discovery-only`: set `HASHI_REMOTE_SHARED_TOKEN` or
  `hashi_remote_shared_token` in `secrets.json`.
- `auth_required`: one side is upgraded and requires HMAC, while the other side
  is missing token support or the token differs.
- same-host port conflict: give each same-host instance a unique `remote_port`.
- stale directory: Remote is alive but HASHI core or Workbench health is down.
- rescue start forbidden: `max_terminal_level` is not `L3_RESTART`.

## Rollback

1. Stop Remote and persist operator disable:

```text
/remote off
```

2. Stop the OS helper:

```bash
bin/hashi-remote-ctl.sh stop
```

or on Windows:

```powershell
.\bin\hashi_remote_ctl.ps1 stop
```

3. Disable default startup:

```yaml
lifecycle:
  remote_enabled: false
```

4. Restart HASHI core. Local single-instance HASHI does not depend on Remote.
5. To re-enable, set `remote_enabled: true` or remove the override, then run
   `/remote on` or start the supervisor.
