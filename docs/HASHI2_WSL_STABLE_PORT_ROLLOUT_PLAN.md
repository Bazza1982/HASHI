# HASHI2 WSL Stable Port Rollout And Test Plan

Status: rollout completed on HASHI2; retained as runbook for repeats
Target: HASHI2 running in WSL
Source checkpoint: `8bf8c5d Prefer full HASHI banner in Windows Terminal`

## Current Result: 2026-05-18

HASHI2 completed the WSL stable Remote port rollout and was then pushed to
GitHub `main`. HASHI1 was reset to the same tracked commit afterward.

Verified outcome:

- HASHI2 kept `hashi_remote` on the configured legacy migration port `8767`.
- `runtime_port_assignments.json` persisted `hashi_remote -> 8767` with
  `source=configured`.
- HASHI2 Remote `/health`, Workbench `18802`, API Gateway `18803`, and local
  hchat smoke passed during rollout.
- HASHI1 and HASHI2 both ended on commit `8bf8c5d`.
- `croniter` was installed through the full launcher requirements path, so the
  startup warning about missing croniter should not appear on updated installs.
- Windows Terminal WSL profiles were configured locally to use `Noto Sans SC`;
  HASHI2's desktop launcher now starts WSL through Windows Terminal with
  `BRIDGE_BANNER_GLYPH_PROFILE=full`.

Important display lesson:

- UTF-8 locale alone is not enough for the full HASHI banner. The terminal font
  must also support CJK/Japanese glyphs.
- Windows Terminal with a CJK-capable font can use the full banner.
- Classic console fallback should use the Latin-safe glyph profile to avoid
  square replacement glyphs.

Important port-status lesson:

- `python -m remote.main --check-port-assignment --hashi-root .` may report
  `available=false` while HASHI2 Remote is already running and legitimately
  holding the assigned port. In that case `assigned=true`, the expected port,
  and a healthy Remote `/health` response are the authoritative evidence.

## Objective

Validate the stable Remote port allocator on HASHI2 WSL before broader rollout.
HASHI2 is the right first target because it is WSL like HASHI1, is expected to
exercise same-host multi-instance routing, and can be restored if anything goes
wrong.

Success means:

- HASHI2 keeps its configured Remote port when free;
- the new allocator state is persisted and ignored by git;
- Workbench, API Gateway, Hashi Remote, HChat, peer discovery, file transfer,
  reboot, and WSL path/platform behavior still work;
- HASHI1 remains healthy while HASHI2 is tested;
- rollback is documented and quick.

## Non-Goals

- Do not test destructive WatchTower start/restart actions against HASHI2 unless
  explicitly authorized at test time.
- Do not roll out to HASHI9/Windows, INTEL, or MSI in this phase.
- Do not reset HASHI2 ports unless a rollback or explicit allocator reset test
  is being performed.
- Do not touch unrelated dirty files in HASHI1:
  - `orchestrator/runtime_superloop.py`
  - `tests/test_superloop_commands.py`

## Preflight Facts To Capture

Run from HASHI2 WSL repo before pulling:

```bash
pwd
git branch --show-current
git rev-parse --short HEAD
git status --short
python - <<'PY'
import json, pathlib
cfg = json.loads(pathlib.Path("agents.json").read_text(encoding="utf-8-sig"))
print(json.dumps(cfg.get("global", {}), indent=2, sort_keys=True))
PY
test -f instances.json && python - <<'PY'
import json, pathlib
data=json.loads(pathlib.Path("instances.json").read_text())
print(json.dumps(data.get("instances", {}).get("hashi2", {}), indent=2, sort_keys=True))
PY
```

Record:

- repo path;
- current commit;
- dirty files;
- `global.instance_id`;
- `global.workbench_port`;
- `global.api_gateway_port`;
- configured `remote_port`;
- whether `runtime_port_assignments.json` already exists;
- current Remote PID/listener/health if running.

## Backup And Restore

Before pulling:

```bash
mkdir -p .rollback/hashi2-stable-port-$(date +%Y%m%d-%H%M%S)
cp -a agents.json instances.json remote/config.yaml .rollback/hashi2-stable-port-*/ 2>/dev/null || true
cp -a runtime_port_assignments.json .rollback/hashi2-stable-port-*/ 2>/dev/null || true
git rev-parse HEAD > .rollback/hashi2-stable-port-*/HEAD.txt
```

Rollback if startup or Remote breaks:

```bash
# Stop only HASHI2 Remote/core processes, not HASHI1.
bin/hashi-remote-ctl.sh stop || true

# Restore previous code.
git checkout <previous_HASHI2_commit>

# Restore local state/config from the backup directory created above.
cp -a .rollback/<backup>/agents.json . 2>/dev/null || true
cp -a .rollback/<backup>/instances.json . 2>/dev/null || true
cp -a .rollback/<backup>/remote/config.yaml remote/config.yaml 2>/dev/null || true
cp -a .rollback/<backup>/runtime_port_assignments.json . 2>/dev/null || true

# Restart HASHI2 Remote/core using the previous runbook.
```

If the only problem is a bad allocator assignment and code is otherwise good:

```bash
python -m remote.main --reset-port-assignment --hashi-root .
```

Only run the reset after confirming peers can rediscover the intended port.

## Rollout Sequence

### Phase 0: HASHI1 Control Baseline

Purpose: prove the controller side is healthy before touching HASHI2.

From HASHI1:

```bash
git rev-parse --short HEAD
python scripts/check_protected_core_changes.py --cached
python -m pytest tests/test_stable_port_allocator.py tests/test_check_protected_core_changes.py tests/test_remote_port_selection.py -q
curl -sS http://127.0.0.1:<HASHI1_REMOTE_PORT>/health
curl -sS http://127.0.0.1:<HASHI1_REMOTE_PORT>/peers
```

Expected:

- commit is `8bf8c5d` or later;
- focused tests pass;
- HASHI1 Remote remains responsive.

### Phase 1: HASHI2 Pull Without Restart

Purpose: update files without changing running processes yet.

From HASHI2:

```bash
git fetch origin
git pull --ff-only origin main
git rev-parse --short HEAD
git status --short
```

Expected:

- HEAD includes `8bf8c5d`;
- no unexpected tracked local modifications;
- any existing local config/state remains intact.

### Phase 2: Static And Allocator Checks

Purpose: validate WSL allocator behavior before Remote restart.

```bash
python -m py_compile orchestrator/stable_port_allocator.py remote/main.py scripts/check_protected_core_changes.py
python scripts/check_protected_core_changes.py
python scripts/check_protected_core_changes.py --cached
python -m pytest tests/test_stable_port_allocator.py tests/test_check_protected_core_changes.py tests/test_remote_port_selection.py -q
cat /proc/sys/net/ipv4/ip_local_port_range
python -m remote.main --check-port-assignment --hashi-root .
```

Expected:

- tests pass;
- Linux ephemeral range is readable;
- check command prints JSON;
- if no assignment exists, `assigned` is false;
- if assignment exists and Remote is stopped, `available` is true;
- if assignment exists and the current Remote process is already holding the
  port, `available=false` is expected and must be interpreted with Remote
  `/health` plus runtime claim evidence.

### Phase 3: Controlled HASHI2 Remote Restart

Purpose: prove allocator integrates with actual Remote startup.

```bash
bin/hashi-remote-ctl.sh status || true
bin/hashi-remote-ctl.sh stop || true
bin/hashi-remote-ctl.sh start
sleep 2
bin/hashi-remote-ctl.sh status
python -m remote.main --check-port-assignment --hashi-root .
```

Then:

```bash
curl -sS http://127.0.0.1:<HASHI2_REMOTE_PORT>/health
curl -sS http://127.0.0.1:<HASHI2_REMOTE_PORT>/peers
curl -sS http://127.0.0.1:<HASHI2_REMOTE_PORT>/protocol/status
```

Expected:

- Remote starts;
- `runtime_port_assignments.json` is created if needed;
- persisted port equals HASHI2 configured Remote port when that port was free;
- no unexpected random port switch;
- `git status --short` does not show `runtime_port_assignments.json` or
  `.runtime_port_assignments.lock`;
- `/health`, `/peers`, and `/protocol/status` respond.

### Phase 4: HASHI1 Observes HASHI2

Purpose: prove cross-instance visibility from the controller side.

From HASHI1:

```bash
curl -sS http://127.0.0.1:<HASHI1_REMOTE_PORT>/peers
python tools/hchat_send.py --to <agent>@HASHI2 --from zelda --check
```

Expected:

- HASHI2 appears online or becomes online after normal discovery/handshake
  interval;
- route uses HASHI2's actual configured/persisted Remote port;
- hchat route check succeeds or fails with a specific actionable reason.

### Phase 5: HASHI2 Full WSL Function Matrix

Run every row and record pass/fail/evidence.

| Area | Command / Probe | Expected |
|---|---|---|
| Instance identity | `curl http://127.0.0.1:<WORKBENCH_PORT>/api/health` | reports HASHI2 instance id and expected ports |
| Workbench API | `curl http://127.0.0.1:<WORKBENCH_PORT>/api/health` | healthy, online agents visible |
| API Gateway | `curl http://127.0.0.1:<API_GATEWAY_PORT>/v1/models` or `/api` status if enabled | gateway state matches HASHI2 config |
| Remote health | `curl http://127.0.0.1:<REMOTE_PORT>/health` | HTTP 200 / healthy JSON |
| Remote peers | `curl http://127.0.0.1:<REMOTE_PORT>/peers` | peers listed, no duplicate HASHI2 identity |
| Protocol status | `curl http://127.0.0.1:<REMOTE_PORT>/protocol/status` | route diagnostics clean or actionable |
| Stable port status | `python -m remote.main --check-port-assignment --hashi-root .` | assigned port matches config; `available=false` is acceptable when held by the live Remote process |
| Git hygiene | `git status --short` | allocator state ignored |
| HChat inbound | HASHI1 sends `python tools/hchat_send.py --to <agent>@HASHI2 --from zelda --check` | delivered or actionable error |
| HChat outbound | HASHI2 sends check to `zelda@HASHI1` | delivered or actionable error |
| Protocol message | `python tools/protocol_send.py --to <agent>@HASHI1 --from <agent> --text smoke ...` | message accepted when token configured |
| File transfer stat | `python tools/remote_file_transfer.py ... stat HASHI1:<path>` | capability/auth works or clear missing-token error |
| File transfer push | push small temp file to HASHI1 incoming smoke path | file appears, no path escape |
| Remote rescue status | `python tools/remote_rescue.py status HASHI1` and HASHI2 target where configured | status works; no destructive start |
| Remote on/off | `/remote status`, `/remote off`, `/remote on` or lifecycle script equivalent | Remote stops/starts and keeps same persisted port |
| Reboot min | `/reboot min` from HASHI2 agent | agent returns; Workbench/Remote still healthy |
| Reboot max | optional only if safe | all agents return; Remote still healthy |
| Scheduler | inspect scheduler heartbeat/log or known scheduled task list | no post-reboot scheduler errors |
| WSL pathing | commands run from WSL path, no Windows path leakage in config | paths are WSL-native |
| Local state | `runtime_port_assignments.json` content | local only, not committed |
| Logs | inspect `logs/hashi_remote*.log`, `logs/bridge.log` | no tracebacks from allocator/startup |
| Banner/font | start through Windows Terminal or run a TTY banner smoke | full CJK banner renders when WT profile uses a CJK font; classic console uses latin-safe profile |

### Phase 6: Occupied-Port Failure Smoke

Only run if safe and HASHI2 can be restored.

Purpose: prove persisted occupied port fails clearly.

Method:

1. Record current assignment.
2. Stop HASHI2 Remote.
3. Temporarily bind the persisted Remote port with a simple local listener.
4. Start HASHI2 Remote.
5. Confirm startup fails with an actionable `Persisted hashi_remote port ... is unavailable` error.
6. Stop the temporary listener.
7. Start HASHI2 Remote again and confirm it resumes on the same persisted port.

Expected:

- no silent random reassignment;
- no stale state corruption;
- normal restart after releasing the port.

### Phase 7: Closeout From HASHI1

From HASHI1:

```bash
curl -sS http://127.0.0.1:<HASHI1_REMOTE_PORT>/peers
python tools/hchat_send.py --to <agent>@HASHI2 --from zelda --check
git status --short
```

Close only if:

- HASHI2 WSL function matrix passed or any skipped rows are justified;
- HASHI1 still sees HASHI2 correctly;
- no allocator state is tracked by git;
- no Remote startup regression is present;
- rollback was not needed, or rollback was tested and recorded.

## Failure Classification

| Failure | Classification | Action |
|---|---|---|
| Pull conflict | rollout_blocker | stop; do not restart HASHI2; resolve dirty state |
| `--check-port-assignment` fails | allocator_blocker | do not restart Remote; inspect exception |
| Remote start fails on persisted occupied port | expected_if_port_occupied | release port or reset only with approval |
| Remote silently changes persisted port | release_blocker | rollback and fix allocator |
| HASHI1 cannot see HASHI2 after restart | routing_blocker | inspect `/peers`, `/protocol/status`, live endpoints |
| HChat fails but Remote peers healthy | hchat_transport_blocker | inspect `tools/hchat_send.py --check` output and Workbench health |
| File transfer auth fails | config_or_token_blocker | verify shared token/capabilities |
| `runtime_port_assignments.json` appears in git | git_hygiene_blocker | fix `.gitignore` before close |

## Evidence Template

Record results in a HASHI2 rollout evidence file:

```text
HASHI2 Stable Port Rollout Evidence

Date:
HASHI2 repo:
Before commit:
After commit:
Configured Remote port:
Persisted Remote port:
Workbench port:
API Gateway port:

Phase 0 HASHI1 baseline:
Phase 1 pull:
Phase 2 static/allocator:
Phase 3 Remote restart:
Phase 4 HASHI1 observes HASHI2:
Phase 5 WSL function matrix:
Phase 6 occupied-port smoke:
Phase 7 closeout:

Failures:
Rollback used:
Final verdict:
```

## Go / No-Go

Go to HASHI1/HASHI9 broader rollout only if:

- HASHI2 preserves its configured Remote port;
- HASHI2 passes Workbench/API/Remote/HChat/file-transfer/reboot checks;
- HASHI1 and HASHI2 agree on peer liveness;
- no git-tracked local allocator state appears;
- occupied persisted port behavior is either tested or explicitly deferred with
  reason;
- no unresolved WSL-specific blocker remains.
