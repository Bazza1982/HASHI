# Final Product Review Package: HASHI Watchtower v1

Generated: 2026-05-15T12:08:23.020438+00:00
Superloop: `sl-20260515-103210320305-ef51`
Commit: `a56d3ff Harden remote rescue control contracts`
Target repo/workzone: `/mnt/c/Users/thene/projects/HASHI`
Target runtime: HASHI9 Windows native
Worker: `lulu`
Orchestrator verification: `zelda`

## Implementation Summary

Watchtower v1 rescue control contract was hardened and documented for HASHI9 Windows-native Topology A.

Changed files in scoped commit:

- `remote/api/server.py`
- `tests/test_remote_rescue_control.py`
- `tests/test_remote_rescue_tool.py`
- `docs/HASHI_REMOTE_RESCUE_PROTOCOL.md`

Unrelated dirty files intentionally not included in commit:

- `bin/bridge-u.bat`
- `orchestrator/banner.py`

## Implemented Review Constraints

- Topology A documented as v1: Windows-native Remote + Windows-native HASHI core.
- Topology C documented as future/advanced, not v1.
- `GET /control/hashi/logs` included in v1 scope.
- logs `tail` default 120, max 1000, non-positive/invalid values return 400.
- logs response includes `requested_tail`, `effective_tail`, `tail_truncated`.
- `POST /control/hashi/start` reason is sanitized to one line and truncated to 500 characters.
- audit JSONL includes `reason_truncated` and `reason_original_length`.
- Windows start response includes structured `command`, `log_path`, `launcher_kind`, `platform`.
- `L3_RESTART` gate preserved.
- helper treats unsupported/404 logs endpoint as unsupported.

## Verification Run By Zelda

```text
pytest -q tests/test_remote_rescue_control.py tests/test_remote_rescue_tool.py
22 passed in 15.59s

python3 -m compileall remote/api/server.py tools/remote_rescue.py
passed

git diff --check -- remote/api/server.py tests/test_remote_rescue_control.py tests/test_remote_rescue_tool.py docs/HASHI_REMOTE_RESCUE_PROTOCOL.md
passed
```

## HASHI9 Windows Smoke Evidence Reported By Lulu

- `GET /control/hashi/status` in closed-port scenario returned `200 / state=offline / hashi_running=false`.
- `POST /control/hashi/start` under L2 returned `403 / HASHI start requires max_terminal_level=L3_RESTART`.
- `POST /control/hashi/start` under L3 returned `200 / started=true / launcher_kind=powershell.exe / platform=windows`.
- `GET /control/hashi/logs?name=start&tail=5000` returned `200 / effective_tail=1000 / tail_truncated=true`.
- `GET /control/hashi/logs?name=../../secret` returned `400`.
- helper on 404 rescue endpoint returned unsupported: `exit_code=3 / supported=false`.
- audit JSONL included `requester`, `reason`, `reason_truncated`, `reason_original_length`, `command`, `pid`, `log_path`, `outcome`, `status_state`, `error`, `ts`.
- HASHI9 Windows repo startup command resolved to `powershell.exe ... C:\Users\thene\projects\HASHI\bin\bridge_ctl.ps1 -Action start -Resume`.

## Known Limitations

- v1 covers Topology A only.
- Topology C (Windows Remote -> WSL2 HASHI core) remains future/advanced.
- Lulu reported one attempted Windows `.venv` pytest was interrupted by KeyboardInterrupt; formal test evidence is focused pytest plus Windows smoke script/report.
- HASHI9 worktree contains unrelated dirty files outside scoped commit: `bin/bridge-u.bat`, `orchestrator/banner.py`.

## Requested Final Review From Akane

Please review whether this is acceptable for final product gate:

1. Does commit `a56d3ff` satisfy the approved plan constraints?
2. Are tests and smoke evidence sufficient for v1 Topology A?
3. Are unrelated dirty files correctly excluded from this delivery?
4. Are known limitations acceptable and clearly documented?
5. Are there any blocking final-review comments before Zelda can exit the superloop?
