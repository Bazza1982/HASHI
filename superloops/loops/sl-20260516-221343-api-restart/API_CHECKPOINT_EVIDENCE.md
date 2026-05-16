# API Checkpoint Evidence

Checkpoint: `/api` runtime API gateway control.

## Ownership Adjustment

Lulu did not produce a task-scope diff after several short ticks and a reduced `/api` first-checkpoint request. Zelda took over checkpoint 1 intentionally to keep the loop moving. Lulu was notified to stop `/api` edits and continue with checkpoint 2 (`/restart` + WatchTower) after `/api` is green.

## Files Changed

- `orchestrator/api_gateway_config.py`
- `orchestrator/api_gateway.py`
- `orchestrator/service_manager.py`
- `orchestrator/commands/api.py`
- `tests/test_api_gateway_command.py`

## Behavior Changed

- Added persisted API gateway runtime config at `state/api_gateway_config.json`.
- Added default API model configuration with default `gpt-5.4`.
- API gateway `/v1/chat/completions` now falls back to the configured default model when request `model` is omitted.
- API gateway `/health` now reports bind host, port, enabled state, and default model.
- ServiceManager can now start/stop API Gateway at runtime.
- Cold startup now honors persisted `/api on` state through `ServiceManager`, keeping `main.py` unchanged.
- Added modular Telegram `/api` command through `orchestrator.command_registry`.
- `/api` supports:
  - `/api`
  - `/api on`
  - `/api off`
  - `/api model`
  - `/api model <model_id>`
- `/api` status always shows:
  - current status
  - configured switch
  - API address
  - `/v1/models`
  - `/v1/chat/completions`
  - default API model
- `/api` uses Telegram inline buttons for on/off/default model/refresh.

## Checks Run

```text
pytest -q tests/test_api_gateway_command.py tests/test_command_registry.py tests/test_runtime_command_binding.py
```

Result:

```text
14 passed, 2 warnings in 0.39s
```

Warnings were pre-existing library warnings from `pytz`.

## Reviewer Fixes Applied

Akane found no blocker. Zelda addressed the highest-value non-blockers before checkpoint close:

- Added warning logging when `api_gateway_config.json` cannot be parsed.
- Switched config save to temp-file + replace.
- Answered unauthorized callback queries so Telegram buttons do not spin forever.
- Revalidated crafted `api:model:*` callback payloads.
- Clarified status text: API callers can override request-level `model`.
- Moved cold-start persisted state handling into `ServiceManager`, avoiding feature-specific logic in `main.py`.

## Residual Risks

- `/api` live Telegram button behavior has not yet been manually clicked in Telegram.
- Runtime `/api off` stops the API gateway; retesting through API endpoints after turning it off requires turning it back on from Telegram.
- `/restart` and WatchTower restart supervision are not part of this checkpoint.
