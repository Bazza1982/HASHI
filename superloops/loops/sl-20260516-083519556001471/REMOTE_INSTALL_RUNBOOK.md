# Intel WatchTower Install Runbook

## Phase 1: Preflight

Ask `agent1@INTEL` to report:

```text
hostname
whoami
pwd
HASHI root candidate paths
existing WatchTower paths
existing HashiWatchtower service status
python launcher availability
PowerShell version
free TCP ports around 43766
write access to staging/install root
```

The worker must not uninstall or stop anything during preflight.

## Phase 2: Package

Build package from the standalone WatchTower repo, excluding:

```text
.git/
.venv/
logs/
state/
instances.json
remote_runtime_claim.json
remote_live_endpoints.json
secrets*
*.sqlite
*.sqlite-shm
*.sqlite-wal
```

Record SHA256 and byte size for each artifact.

## Phase 3: Transfer And Staging Check

Transfer to staging first. `agent1@INTEL` runs:

```text
Get-FileHash <package>
tar/list or extraction preview
python -m py_compile on key Python files
python -m remote --help
installer syntax/dry-run check where available
```

No final install before the staging check is green.

## Phase 4: Apply With Rollback

If an existing Intel WatchTower exists:

```text
query service
stop service only after approval
backup existing install root
record rollback path
```

Then install standalone WatchTower to the final root, create venv, install
requirements, and install service/autostart.

## Phase 5: Live Gates

Intel local:

```text
service Running
GET /health returns WATCHTOWER_INTEL
capabilities include rescue_control and rescue_start
root is WatchTower install root
controlled HASHI root is Intel HASHI root
```

HASHI1 remote:

```text
remote list sees Intel WatchTower online
route points to Intel host
not aliased to A9 WatchTower or HASHI9
```

Rescue smoke:

```text
GET /control/hashi/status
GET /control/hashi/logs
POST /control/hashi/start while already running
```

