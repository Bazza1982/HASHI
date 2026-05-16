# MSI WatchTower Install Runbook

## Phase 1: Preflight

Ask `agent3@MSI` to report:

```text
hostname
whoami
pwd
HASHI root candidate paths
existing WatchTower paths
existing HashiWatchtower service status
Python launcher availability
PowerShell version
free TCP port 43766
write access to staging/install roots
admin rights
```

No uninstall, stop, restart, or final install is allowed during preflight.

## Phase 2: Package

Build package from the standalone WatchTower repo, excluding:

```text
.git/
.venv/
.pytest_cache/
logs/
state/
instances.json
remote_runtime_claim.json
remote_live_endpoints.json
secrets*
*.sqlite
*.sqlite-shm
*.sqlite-wal
__pycache__/
*.pyc
```

Record SHA256 and byte size.

## Phase 3: Transfer And Staging Check

Transfer to remote staging first. `agent3@MSI` runs extraction, hash, compile,
venv/dependency, and `python -m remote --help` checks before any service write.

## Phase 4: Apply With Rollback

If an existing MSI WatchTower exists, record state and backup before changing
it. Install standalone WatchTower to the final root, create venv, install
requirements, stage `nssm.exe` if needed, and install/update `HashiWatchtower`
with:

```text
instance_id: MSI-WT
display_name: MSI-WT
port: 43766
controlled HASHI root: MSI active HASHI root from preflight
```

## Phase 5: Live Gates

MSI local:

```text
service Running, Automatic
GET /health returns MSI-WT
GET /protocol/status reports shared_token_configured true
```

HASHI1:

```text
remote list / health peers sees MSI-WT online via LAN
MSI-WT is distinct from local WATCHTOWER and INTEL-WT
```

Rescue smoke:

```text
GET /control/hashi/status
GET /control/hashi/logs
POST /control/hashi/start while already running
```

Never shut down MSI HASHI during this smoke.

## Orchestration Correction: Short Tick Waits

For the rest of this MSI-WT loop, `agent3@MSI` remains the sole execution
worker. Other MSI agents are evidence-only reviewers unless the operator
explicitly reassigns execution ownership.

The orchestrator must not use long blocking waits. Use 30-60 second ticks:

```text
1. check agent3 route/message status
2. probe MSI-WT health/protocol once
3. stat focused service artifacts if useful
4. record the result
5. send one narrow follow-up only if the tick changes the decision state
```

If no useful reply arrives, ask `agent3@MSI` for exactly one of:

```text
service_green
blocked with failing command/exit/stdout/stderr
not_started with reason
```
