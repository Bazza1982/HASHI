# MSI WatchTower Remote Install Evidence

Loop id: `sl-20260516-110606318920490`

## Goal

Install standalone WatchTower on MSI as `MSI-WT`, verify it is visible from
HASHI1 over LAN, and run authenticated non-destructive rescue smoke without
shutting down MSI HASHI.

## Initial Route Preflight

```text
target: agent3@MSI
hchat dry-run: ok
route: remote_protocol
host: 192.168.0.41
remote_port: 8767
remote capabilities: rescue_control, rescue_start, file_transfer_hmac_v1
```

## Exit Condition

```text
MSI-WT service running and automatic
MSI-WT health/protocol endpoints healthy
HASHI1 sees MSI-WT over LAN
MSI-WT distinguishable from WATCHTOWER and INTEL-WT
authenticated non-destructive rescue smoke passes
MSI HASHI core is not shut down
```

## Package Manifest

```text
artifact: superloops/loops/sl-20260516-110606318920490/artifacts/watchtower_standalone_msi_20260516.tar.gz
size: 86649 bytes
sha256: 95ae798c92d1039b5815c619a5adeeeaf2889686db08c49aa776346827b2f460
archive_entries: 61
```

Privacy/runtime excludes:

```text
.git, .venv, .pytest_cache, logs, state, instances.json,
remote_runtime_claim.json, remote_live_endpoints.json, secrets.json,
secrets.*, __pycache__, *.pyc, *.sqlite*
```
