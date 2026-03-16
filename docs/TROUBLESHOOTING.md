# HASHI — Troubleshooting

> Single source of truth for operational troubleshooting.

---

## Quick Checklist (copy/paste)

### Core processes
- [ ] Bridge / Orchestrator is running
- [ ] Correct `bridge_home` is being used
- [ ] `agents.json` exists in `bridge_home`
- [ ] `secrets.json` exists in `bridge_home` (tokens/keys present)

### Ports
- [ ] Bridge Workbench API port is listening (default: `workbench_port`, e.g. 18819)
- [ ] Workbench Node API is listening (`:3001`)
- [ ] Workbench UI is reachable (`:5173`)
- [ ] API Gateway (optional) is listening (`:18801`)

### Workbench UI "no reaction" symptom
- [ ] `http://127.0.0.1:3001/api/config` returns JSON
- [ ] `bridgeUApi` in that JSON points to the correct `workbench_port`

### /new semantics
- [ ] `/new` starts a **bare** session (no FYI injection)
- [ ] Agent follows only its `agent.md` (workspace)

---

## Fixed / Outstanding Tracking

> Keep this list maintained. Each item must include the **branch** and **commit**.

### Fixed
- [x] Windows onboarding launches bridge using `bridge-u.bat` (not `/usr/bin/bash`).
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17
- [x] Onboarding writes `agents.json/secrets.json` to the correct `bridge_home`.
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17
- [x] Workbench control script resolves repo root correctly (Windows).
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17
- [x] Vite dev server binds to `127.0.0.1` so `127.0.0.1:5173` works.
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17
- [x] `/new` is **bare** (no Bridge FYI injection; follows agent.md only).
  - Branch: `v1.1-debugging`
  - Commit: (fill)
  - Date: 2026-03-17

### Outstanding
- [ ] (none)

---

## Where to look for logs

- Bridge logs: `logs/`
- Workbench control logs (Windows): `state/workbench/logs/`
