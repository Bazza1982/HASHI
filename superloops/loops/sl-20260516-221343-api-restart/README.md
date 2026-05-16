# Hashi API switch and WatchTower-backed hard restart

- Loop id: `sl-20260516-221343-api-restart`
- Template: `superloops/templates/auto_vibe_coding`
- Controller: `zelda@HASHI1`
- Worker: `lulu@HASHI1`
- Reviewer: `akane@HASHI1`
- Created: `2026-05-16T22:13:43.269025+00:00`

## User Request

Implement Hashi /api runtime command with Telegram buttons/default API model, and /restart hard restart command backed by WatchTower restart supervision upgrade. Lulu is worker, Akane is reviewer, Zelda orchestrates using auto_vibe_coding.

## Exit Condition

Hashi exposes /api on/off/model with buttons, status includes API address and default model; WatchTower exposes restart supervision endpoint/state/audit; Hashi /restart confirms via buttons, notifies WatchTower, and has focused tests plus recorded verification evidence.

## Scope

In scope:
- Hashi `/api` Telegram command: on/off/model/status/buttons.
- API gateway runtime control and default model behavior.
- Hashi `/restart` Telegram command with confirmation buttons.
- WatchTower restart supervision endpoint, job state, audit log, and start/verify flow in `C:\Users\thene\projects\WatchTower`.
- Focused tests and recorded verification evidence.

Out of scope:
- Reverting unrelated dirty files.
- Changing unrelated agent runtime behavior except where command registration requires it.
- Machine reboot/shutdown; only Hashi process restart after explicit operator command.
- Logging secrets or auth tokens.

## Dirty Worktree Baseline

Captured separately in the initial event and current `git status`. Existing dirty files are treated as user/parallel work unless this loop explicitly changes them.

## Orchestration Notes

- Preserve Lulu as worker by default.
- Akane reviews; she does not take over write scope.
- No long blocking waits; use short state ticks and recorded evidence.
- Do not close without exit evidence.
