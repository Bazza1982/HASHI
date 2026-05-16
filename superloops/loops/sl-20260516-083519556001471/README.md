# Intel Standalone WatchTower Remote Install Superloop

Loop id: `sl-20260516-083519556001471`

## Goal

Install the standalone WatchTower repo on `INTEL`, operated as its own repo,
service, identity, and broadcast peer. The loop closes only when HASHI1 can see
the Intel WatchTower online and can run non-destructive rescue smoke checks
against it.

## Roles

- Orchestrator: `Zelda@HASHI1`
- Remote worker: `agent1@INTEL`
- Reviewer: optional, only if another Intel-side agent is reachable or if the
  install hits a blocker that needs independent review

## Exit Condition

- Standalone WatchTower repo exists on Intel outside the HASHI core repo.
- WatchTower service/autostart is installed and stable on Intel.
- WatchTower broadcasts as an Intel-specific peer, not as HASHI9 and not as
  the existing A9 WatchTower.
- HASHI1 can see the Intel WatchTower via remote list/health.
- Capabilities include `rescue_control` and `rescue_start`.
- Non-destructive rescue smoke passes from HASHI1 route:
  - `/health`
  - `/control/hashi/status`
  - `/control/hashi/logs`
  - `/control/hashi/start` while Intel HASHI is already running
- Evidence is recorded and no blockers remain.

## Safety Boundaries

- Do not stop HASHI1.
- Do not install WatchTower inside Intel HASHI core.
- Do not copy secrets, memory DBs, logs, runtime state, `.venv`, or `.git` as
  package payload.
- Do not run destructive rescue until the non-destructive smoke gate is green.
- Stop/remove only an existing Intel WatchTower install after rollback path is
  documented.

