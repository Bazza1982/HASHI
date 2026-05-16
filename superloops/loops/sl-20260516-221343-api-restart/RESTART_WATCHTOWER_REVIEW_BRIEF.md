# Checkpoint 2 Review Brief: /restart + WatchTower

## Current Situation

- `/api` checkpoint is committed in `/home/lily/projects/hashi` as `0418270`.
- Lulu candidate implementation for checkpoint 2 was detected in:
  - Hashi live/Windows candidate repo: `/mnt/c/Users/thene/projects/HASHI`
  - WatchTower repo: `/mnt/c/Users/thene/projects/WatchTower`
- This checkpoint is not accepted or merged by the orchestrator yet.

## Candidate Changed Files

Hashi candidate:

- `orchestrator/api_gateway.py`
- `orchestrator/service_manager.py`
- `orchestrator/workbench_api.py`
- `orchestrator/commands/api_restart.py`
- `tools/remote_rescue.py`
- `tests/test_api_gateway_runtime.py`
- `tests/test_api_restart_commands.py`
- `tests/test_remote_rescue_tool.py`

WatchTower candidate:

- `remote/api/server.py`
- `tests/test_remote_rescue_control.py`

## Positive Evidence

- WatchTower adds `POST /control/hashi/restart`.
- Endpoint requires `_authenticate_rescue_control(... body_bytes=...)`.
- Endpoint requires `AuthLevel.L3_RESTART`.
- Restart flow has stop, start, verify phases.
- Restart writes audit records to `remote_rescue_audit.jsonl`.
- `tools/remote_rescue.py` adds `restart WATCHTOWER`.

## Orchestrator Risk Findings For Reviewer

1. Unauthorized Telegram callbacks in `api_callback` and `restart_callback` returned without `query.answer()`, which can leave Telegram clients spinning. Fixed by Zelda in candidate repo.
2. `/restart` UI allowed arming even after WatchTower status errors. Fixed by Zelda to fail closed and only show Refresh when WatchTower is unavailable.
3. `hardrestart:confirm` had no visible re-entry guard. Fixed by Zelda with runtime `_watchtower_restart_inflight` guard and confirm-time WatchTower re-check.
4. WatchTower originally only used synchronous endpoint supervision. Fixed by Zelda: restart now creates a `restart_id`, writes a durable state record under `state/restarts`, updates phase transitions, and exposes `GET /control/hashi/restarts/{restart_id}`.
5. `_hashi_stop_command()` currently supports Windows `bridge_ctl.ps1 -Action stop` only. This may be acceptable for current WatchTower-on-Windows target, but should be explicit.
6. Hashi candidate combines `/api` and `/restart` into `orchestrator/commands/api_restart.py`; WSL checkpoint already has a cleaner modular `/api` command in `orchestrator/commands/api.py`.

## Latest Test Evidence

- `/mnt/c/Users/thene/projects/HASHI`: `pytest -q tests/test_api_gateway_runtime.py tests/test_api_restart_commands.py tests/test_remote_rescue_tool.py tests/test_command_registry.py tests/test_runtime_command_binding.py` => 29 passed, 2 pytz warnings.
- `/mnt/c/Users/thene/projects/WatchTower`: `PYTHONPATH=. pytest -q tests/test_remote_rescue_control.py` => 20 passed.

## Requested Reviewer Verdict

Classify findings as:

- BLOCKER
- NON-BLOCKER
- ACCEPTABLE TRADEOFF

Please also state:

- Whether Windows HASHI is the correct target repo for `/restart`.
- Whether WatchTower needs durable restart state before acceptance.
- Minimum fixes required before checkpoint 2 commit.
