# HASHI — Troubleshooting

> Single source of truth for operational troubleshooting.

---

## Quick Checklist (copy/paste)

### Core processes
- [ ] Bridge / Orchestrator is running
- [ ] Correct `bridge_home` is being used
- [ ] `agents.json` exists in `bridge_home`
- [ ] `secrets.json` exists in `bridge_home` (tokens/keys present)

### Ports
- [ ] Bridge Workbench API port is listening (default: `workbench_port`, e.g. 18819)
- [ ] Hashi Remote peer port is listening (`remote_port`, e.g. 8766/8767/8768)
- [ ] Workbench Node API is listening (`:3001`)
- [ ] Workbench UI is reachable (`:5173`)
- [ ] API Gateway (optional) is listening on `api_gateway_port`; default is `workbench_port + 1`
- [ ] Multi-instance ports do not collide: HASHI1 `18800/18801`, HASHI2 `18802/18803`, HASHI9 `18819/18820`
- [ ] Same-host HASHI instances do not share the same `remote_port`

### HASHI API / API Gateway ownership
- [ ] `curl http://<api_host>:<workbench_port>/api/health` returns the expected `instance_id`
- [ ] `api_gateway_port` in `/api/health` matches the instance's expected gateway port
- [ ] If `api_gateway_enabled` is true, `curl http://<api_host>:<api_gateway_port>/health` returns `{"status":"ok", ...}`
- [ ] No other HASHI instance is listening on the same API Gateway port
- [ ] In WSL, if `127.0.0.1` hangs but `10.255.255.254` works, use the `10.255.255.254` address reported by `/api/health` or startup logs

### Workbench UI "no reaction" symptom
- [ ] `http://127.0.0.1:3001/api/config` returns JSON
- [ ] `bridgeUApi` in that JSON points to the correct `workbench_port`

### /new and /fresh semantics
- [ ] CLI backends use `/new` for a fresh CLI session reset
- [ ] Non-CLI backends use `/fresh` for a clean API context
- [ ] `/fresh` clears recent turns but preserves saved memories
- [ ] `/fresh` disables saved-memory auto-injection until `/memory saved on` or `/memory on`

### /reboot semantics
- [ ] `/reboot min` restarts the requester and rebuilds hot managers
- [ ] `/reboot max` restarts all running agents and rebuilds hot managers
- [ ] Workbench API stays healthy after reboot
- [ ] Scheduler is recreated and started after reboot
- [ ] No post-reboot `ERROR`, `CRITICAL`, `Traceback`, `failed`, or unexpected `LOCAL MODE` entries appear in `logs/bridge.log`

### Hashi Remote peer visibility
- [ ] `/remote status` shows `Lifecycle: enabled` and the expected Remote port
- [ ] `/remote status` does not report same-host Remote port conflicts
- [ ] `/remote list` shows the peer as `online`, not only `pending` or `offline`
- [ ] Both peers use the same `hashi_remote_shared_token` or `HASHI_REMOTE_SHARED_TOKEN`
- [ ] Missing-token peers are expected to show as discovery-only or untrusted during rolling upgrades
- [ ] On Windows, run `.\bin\hashi_remote_ctl.ps1 doctor` and check listening/firewall/WSL output
- [ ] On Linux/WSL, run `bin/hashi-remote-ctl.sh status` and inspect `logs/hashi-remote-supervisor.log`

### Hashi Remote rescue
- [ ] `python tools/remote_rescue.py capabilities <INSTANCE>` shows `rescue_control: yes`
- [ ] `python tools/remote_rescue.py status <INSTANCE>` reports `running`, `starting_or_stuck`, `stale_pid`, or `offline`
- [ ] `rescue_start` is expected to be `no` unless `security.max_terminal_level` is `L3_RESTART`
- [ ] `python tools/remote_rescue.py logs <INSTANCE> --name start` returns a bounded log tail

### Anatta mode
- [ ] `/anatta status` reports the current workspace mode
- [ ] `/anatta shadow` records observation config without prompt injection
- [ ] `/anatta on` enables pre-turn live self-assembly and post-turn observation
- [ ] `/anatta off` disables Anatta while preserving the workspace config

---

## Fixed / Outstanding Tracking

> Keep this list maintained. Each item must include the **branch** and **commit**.

### Fixed
- [x] Windows onboarding launches bridge using `bridge-u.bat` (not `/usr/bin/bash`).
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17
- [x] Onboarding writes `agents.json/secrets.json` to the correct `bridge_home`.
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17
- [x] Workbench control script resolves repo root correctly (Windows).
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17
- [x] Vite dev server binds to `127.0.0.1` so `127.0.0.1:5173` works.
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17
- [x] `/new` is **bare** for CLI session reset; non-CLI clean context uses `/fresh`.
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17

### Outstanding
- [ ] (none)

---

## Where to look for logs

- Bridge logs: `logs/`
- Main bridge lifecycle log: `logs/bridge.log`
- Remote supervisor log: `logs/hashi-remote-supervisor.log`
- Remote rescue start log: `logs/remote_rescue_hashi_start.log`
- Remote rescue audit log: `logs/remote_rescue_audit.jsonl`
- Browser/native host log: `logs/browser_native_host.log`
- Workbench control logs (Windows): `state/workbench/logs/`

---

## Hashi Remote Rollback

Use rollback when a Remote rollout causes peer visibility or routing problems:

1. Run `/remote off` from a local HASHI agent to write the persistent disabled state.
2. Stop OS supervision:
   - Linux/WSL: `bin/hashi-remote-ctl.sh stop`
   - Windows: `.\bin\hashi_remote_ctl.ps1 stop`
3. Set `remote/config.yaml`:

```yaml
lifecycle:
  remote_enabled: false
```

4. Restart HASHI core. Legacy local operation does not require Remote.
5. To re-enable, remove the config override or set `remote_enabled: true`, then run `/remote on` or start the supervisor.
