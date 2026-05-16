# MSI WatchTower Remote Install Superloop

Loop id: `sl-20260516-110606318920490`

Goal: install the standalone WatchTower package on MSI, name it `MSI-WT`,
verify it is stable and visible from HASHI1 over LAN, and run an authenticated
non-destructive rescue smoke against MSI HASHI.

Exit condition:

- `MSI-WT` service is running and automatic on MSI.
- `MSI-WT` health/protocol endpoints report shared-token mode.
- HASHI1 sees `MSI-WT` clearly and distinctly from local `WATCHTOWER` and
  `INTEL-WT`.
- Authenticated rescue smoke passes without shutting down MSI HASHI:
  `status`, `logs`, and `start` while already running.
- All waits have explicit deadlines, controller-side probes, and escalation.

