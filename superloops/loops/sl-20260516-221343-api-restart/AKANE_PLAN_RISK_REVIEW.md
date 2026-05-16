# Akane Plan-Risk Review Summary

Source: Akane transcript at `2026-05-16T22:16:18Z`.

## Blockers

### B1: `/restart` Depends On WatchTower Restart Endpoint

WatchTower does not currently expose restart supervision. Hashi `/restart` must not silently degrade into self-restart.

Orchestrator decision:

- If WatchTower is unreachable, unauthenticated, or does not support restart supervision, Hashi `/restart` must refuse execution with a clear error.
- No direct Hashi self-kill/self-restart fallback is allowed.

### B2: WatchTower Restart Endpoint Must Be Authenticated

Restart supervision is process-control behavior and must not be exposed to unauthenticated LAN callers.

Orchestrator decision:

- Use the existing rescue-control authentication path or equivalent shared-token/HMAC path.
- Tests must cover unauthenticated restart rejection.

## Non-Blockers / Follow-Ups To Check In Diff Review

- Button callbacks must be registered. Modular `RuntimeCallback` registration through `orchestrator/command_registry.py` is acceptable and preferred; static `CALLBACK_BINDINGS` changes are not required if modular callbacks work.
- `/api` semantics must be documented as controlling the OpenAI-compatible API Gateway, not agent backend selection.
- WatchTower restart state schema should be explicit and persisted.
- Restart supervision logic should be factored outside the large `remote/api/server.py` where practical.
- Restart must be scoped to controlled Hashi process only, not OS reboot/shutdown.

## Reviewer Follow-Up

Akane should review the actual diff and evidence after Lulu's implementation, with special attention to:

- Restart auth.
- WatchTower-unreachable behavior.
- Button callback registration.
- API default model fallback.
- Focused tests.
