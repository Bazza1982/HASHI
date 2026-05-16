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

## MSI Staging Transfer

```text
archive_path: C:\Users\Public\hashi_watchtower_msi_staging\watchtower_standalone_msi_20260516.tar.gz
archive_size: 86649
archive_sha256: 95ae798c92d1039b5815c619a5adeeeaf2889686db08c49aa776346827b2f460
remote_stat: verified

sha_path: C:\Users\Public\hashi_watchtower_msi_staging\watchtower_standalone_msi_20260516.sha256
sha_file_size: 163
sha_file_sha256: eb79e7002fd7cb1e000c01cfabe6ea2e336779c2d7153f4994cb16fe8dff9d36
remote_stat: verified
```

## Active Follow-Up

The preflight/staging wait deadline was reached without a visible reply in the
controller session. The loop did not idle. Controller-side probes were run:

```text
agent3@MSI route check: ok via 192.168.0.41:8767
MSI capabilities: rescue_control, rescue_start, file_transfer_hmac_v1
staged archive stat: exists, size 86649, sha256 matched
staged sha file stat: exists, size 163, sha256 matched
```

Follow-up was sent to `agent3@MSI` asking for both preflight and staging report
status. No install, stop, restart, or shutdown was authorized.

## Escalation To Same-Machine Reviewer

`agent3@MSI` did not visibly reply after the follow-up and a short one-line
status ping. The loop escalated to `agent4@MSI` for read-only, staging-only
inspection.

```text
command_id: sl-20260516-110606318920490-agent4-inspect-001
target: agent4@MSI
mode: read-only / staging-only
install_authorized: false
service_change_authorized: false
shutdown_authorized: false
```
