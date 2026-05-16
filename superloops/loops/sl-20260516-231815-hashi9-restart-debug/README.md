# HASHI9 WatchTower hard restart debug superloop

- Loop id: `sl-20260516-231815-hashi9-restart-debug`
- Template: `superloops/templates/auto_debug`
- Created: `2026-05-17T09:18:15+10:00`
- Orchestrator: `zelda@HASHI1`
- Subject agent: `hashiko@HASHI9`
- Subject repos:
  - HASHI9: `C:\Users\thene\projects\HASHI`
  - WatchTower: `C:\Users\thene\projects\WatchTower`

## Reported Issue

The live Telegram `/restart` hard restart test with `hashiko@HASHI9` appears
to have failed. Evidence reported by the operator:

- HASHI9 `main.py` process kept the original start time: `2026-05-17 07:46:21`.
- HASHI9 remote kept original start times: `2026-05-17 07:39:47 / 07:39:57`.
- WatchTower did not record hard restart execution:
  - no new file under `state/restarts/`
  - no new entry in `logs/remote_rescue_audit.jsonl`
  - no stop/start/verify/restarted record in `watchtower-svc-stdout.log`
- `hashiko` processed new request `req-0004` at `09:13:39`, which implies the
  bot stayed online instead of being stopped and relaunched.
- The new log directory
  `C:\Users\thene\hashi9-home\logs\hashiko\2026-05-17_090252\...` likely came
  from an earlier local reload/restart, not from the failed `/restart` attempt.

## Expected Behavior

`/restart` in HASHI9 should:

1. show a Telegram confirmation flow,
2. fail closed if WatchTower is unreachable, unauthenticated, or unsupported,
3. dispatch a restart request to WatchTower after confirmation,
4. cause WatchTower to stop HASHI9, start it again, and verify health,
5. leave durable WatchTower restart evidence under `state/restarts/` and audit
   logs.

## Initial Hypotheses

- `/restart` is not registered in the running HASHI9 bot.
- `/restart` is registered but callback confirmation did not reach the handler.
- Handler ran but did not dispatch `remote_rescue.rescue_restart()`.
- Dispatch targeted the wrong WatchTower instance/address.
- WatchTower service is running old code without `/control/hashi/restart`.
- Auth/shared-token route failed before WatchTower recorded a restart.
- HASHI9 process is running older code than commit `51e6e65`.

## Safety Boundary

- Do not blindly trigger another hard restart.
- Prefer read-only probes first: command registration, process start path,
  deployed commit/version, WatchTower health/capabilities/logs.
- Any second live restart attempt must have a bounded smoke plan and evidence
  capture target before execution.

## Exit Condition

Root cause is identified and fixed or explicitly deferred, then a controlled
live smoke proves that `/restart` reaches WatchTower and produces durable
WatchTower evidence, or the loop records a clear blocker explaining why live
restart cannot be safely run.

## Final Result

- Status: completed
- Final restart id: `restart-fc7fbc153e484337b789a4386be9f075`
- Final phase: `completed`
- Final health:
  - HASHI9 running: yes
  - hashiko online: yes
  - API gateway enabled: yes
  - API gateway default model: `claude-haiku-4-5`

## Root Cause

The initially reported Telegram test was run against an old HASHI9 process that
had not loaded the new `/restart` command. After WatchTower was exercised
directly, a second issue appeared: Windows service-context startup through
`bridge_ctl.ps1` / `bridge-u.bat` did not reliably restore a healthy HASHI9
runtime. A direct Python launcher worked, but initially used the repo root as
`--bridge-home`, so API gateway state was not restored.

## Fix

WatchTower now uses a fixed direct HASHI launcher on Windows:

```text
C:\Users\thene\projects\HASHI\.venv\Scripts\python.exe
C:\Users\thene\projects\HASHI\main.py
--bridge-home C:\Users\thene\hashi9-home
```

The launcher environment also adds user tool paths such as
`C:\Users\thene\AppData\Roaming\npm` so CLI backends are visible from the
WatchTower service context.

## Verification

- `PYTHONPATH=. pytest -q tests/test_remote_rescue_control.py`
  - Result: `23 passed in 8.89s`
- Controlled live smoke:
  - `restart-fc7fbc153e484337b789a4386be9f075`
  - WatchTower state: `completed`
  - WatchTower audit outcome: `restarted`
  - Workbench health: `ok`
  - Agents: `hashiko`
  - API gateway: `enabled`
