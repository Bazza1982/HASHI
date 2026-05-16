# Orchestration Plan

Loop: `sl-20260516-221343-api-restart`

## Current Baseline

- Hashi Workbench health is reachable at `http://10.255.255.254:18800/api/health`.
- Health reports `api_gateway_enabled: true` and `api_gateway_port: 18801`.
- Current API gateway code lives in `orchestrator/api_gateway.py`.
- Runtime service lifecycle lives in `orchestrator/service_manager.py`.
- Telegram command binding supports modular commands through `orchestrator/command_registry.py` and `orchestrator/commands/`.
- Existing Telegram model/backend buttons are in `orchestrator/flexible_agent_runtime.py`, but new `/api` should preferably use modular command callbacks to keep the core slim.
- WatchTower repo is `C:\Users\thene\projects\WatchTower`.
- WatchTower default remote API port is `43766`.
- Current Hashi `instances.json` points WatchTower at `http://192.168.0.211:43766`.
- WatchTower already exposes `GET /control/hashi/status` and `POST /control/hashi/start`.
- WatchTower does not yet expose restart supervision.

## Implementation Split

### Lulu Worker Scope

Lulu owns implementation. She may edit Hashi and WatchTower files needed for:

- `/api` Telegram command and callback buttons.
- API gateway runtime on/off/status/default-model behavior.
- API gateway default model fallback when `model` is omitted.
- `/restart` Telegram command and confirmation buttons.
- WatchTower restart supervision endpoint, restart state, audit log, and controlled stop/start/verify flow.
- Focused tests.

Lulu must not revert unrelated dirty files or take broad refactors outside this scope.

### Akane Reviewer Scope

Akane independently reviews:

- Plan risks before implementation.
- Actual diffs after implementation.
- Tests and live/smoke evidence.
- Blockers, non-blockers, and residual risks.

Akane should not edit implementation files unless Zelda explicitly reassigns ownership.

### Zelda Orchestrator Scope

Zelda owns:

- Scope boundaries and dirty-worktree protection.
- Dispatch and state updates.
- Controller-side baseline and evidence.
- Integration inspection.
- Focused checks and final commit decision.
- User-facing status.

## Proposed Hashi Design

Keep Hashi core slim by adding modular command files:

- `orchestrator/commands/api.py`
- `orchestrator/commands/restart.py`

Use `RuntimeCommand` and `RuntimeCallback` so static command binding does not grow unnecessarily.

### `/api`

Required forms:

- `/api`
- `/api on`
- `/api off`
- `/api model`
- `/api model <model_id>`

Every `/api` response must show:

- API status.
- API base address.
- `GET /v1/models`.
- `POST /v1/chat/completions`.
- Default API model.
- Buttons for on/off, model, refresh.

Persistent runtime state should be small, likely:

`state/api_gateway_config.json`

Required fields:

- `enabled`
- `default_model`
- `updated_at`
- `updated_by`

The API gateway should use request `model` when supplied and default model when omitted.

### `/restart`

Required forms:

- `/restart`
- `/restart status`
- `/restart confirm`

Telegram flow:

1. Probe WatchTower route and status.
2. Show instance id, current pid if available, WatchTower address, controlled workbench port, and risk warning.
3. Require explicit confirmation button.
4. On confirmation, call WatchTower restart endpoint.
5. Report accepted `restart_id`.

Do not kill or restart the Hashi process directly inside Hashi.

## Proposed WatchTower Design

Add fixed restart supervision API, not generic shell execution:

- `POST /control/hashi/restart`
- `GET /control/hashi/restart/{restart_id}`
- `GET /control/hashi/restarts/latest`

Restart state directory:

`state/restarts/`

Audit log:

`logs/watchtower_restart_audit.jsonl`

Phases:

- `accepted`
- `draining`
- `stopping`
- `killed`
- `starting`
- `verifying`
- `healthy`
- `failed`

Stop/start policy:

1. Read controlled Hashi PID from `.bridge_u_f.pid`.
2. Allow a grace window.
3. Terminate if still alive.
4. Kill only if terminate fails.
5. Start via existing fixed launcher.
6. Verify controlled Workbench health.

## Required Checks

Hashi focused checks should include:

- Command registry tests for new modular commands/callbacks.
- API gateway tests for default model fallback and health metadata.
- Service manager tests for runtime on/off where practical.
- Restart command tests with mocked WatchTower HTTP calls.

WatchTower focused checks should include:

- Restart payload validation.
- Restart job state persistence.
- Audit log write.
- Stop/start/verify path with process and HTTP probes mocked.

Live verification is required before final close if `/api` or `/restart` affects the running service. Hard restart live verification must not be triggered automatically without explicit operator confirmation.
