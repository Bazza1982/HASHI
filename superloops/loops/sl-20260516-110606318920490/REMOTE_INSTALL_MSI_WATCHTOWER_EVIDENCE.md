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

## Staging V2 Request

Staging check v2 was sent to `agent3@MSI` using the active HASHI-root staging
path:

```text
command_id: sl-20260516-110606318920490-staging-002
archive: C:\Projects\HASHI\tmp\watchtower_install_staging\watchtower_standalone_msi_20260516.tar.gz
mode: staging check only
install_authorized: false
service_change_authorized: false
shutdown_authorized: false
```

Staging v2 follow-up:

```text
agent3@MSI route check: ok
active staging archive stat: exists, size 86649, sha256 matched
follow-up sent: yes
install_authorized: false
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

`agent4@MSI` replied `received`. Zelda sent a follow-up requesting the actual
staging verdict: `staging_green`, `running`, or `blocked`.

## MSI Agent Responsiveness Blocker

At the reviewer deadline, controller-side probes still showed MSI Remote and
the staged files healthy:

```text
agent4@MSI route check: ok via 192.168.0.41:8767
MSI /protocol/agents: agent3 and agent4 active/fresh
staged archive stat: exists, size 86649, sha256 matched
```

A direct Workbench exchange attempt to `192.168.0.41:8779` timed out. Another
follow-up was sent to `agent4@MSI`.

Current blocker:

```text
MSI Remote API is online and staged files are verified, but MSI agents have not
returned preflight/staging execution reports yet.
```

The install is intentionally not authorized until a remote agent confirms the
staging checks from inside MSI.

## Agent3 Preflight Received

`agent3@MSI` returned the preflight report for
`sl-20260516-110606318920490-preflight-001`.

```text
hostname: MSI
pwd: C:\Projects\HASHI\workspaces\agent3
active_hashi_root: C:\Projects\HASHI
hashi_tmp: C:\Projects\HASHI\tmp, writable
proposed_staging: C:\Projects\HASHI\tmp\watchtower_install_staging
proposed_install_root: C:\Projects\WatchTower
HashiWatchtower service: not found
scheduled WatchTower tasks: none found
port_43766: clear
go_no_go: preflight_only_go
blockers: none detected
```

Python/admin exact raw values were not fully included in the relayed structured
report, so the staging check must still report precise `py -0p`, `py -3.12`,
and admin status before install.

## Active MSI Staging Transfer

The artifact was pushed again into the active HASHI-root staging path selected
from preflight:

```text
archive_path: C:\Projects\HASHI\tmp\watchtower_install_staging\watchtower_standalone_msi_20260516.tar.gz
archive_size: 86649
archive_sha256: 95ae798c92d1039b5815c619a5adeeeaf2889686db08c49aa776346827b2f460
remote_stat: verified

sha_path: C:\Projects\HASHI\tmp\watchtower_install_staging\watchtower_standalone_msi_20260516.sha256
sha_file_size: 163
sha_file_sha256: eb79e7002fd7cb1e000c01cfabe6ea2e336779c2d7153f4994cb16fe8dff9d36
remote_stat: verified
```

## Coordinator Assist Request

Because `agent3@MSI` and `agent4@MSI` did not visibly return reports, a
read-only/staging-only assist request was sent to `hashiko@MSI`.

```text
command_id: sl-20260516-110606318920490-hashiko-assist-001
target: hashiko@MSI
mode: read-only / staging-only coordination
install_authorized: false
shutdown_authorized: false
```

## Hashiko Staging Green Report

`hashiko@MSI` returned a staging assist report for
`sl-20260516-110606318920490-hashiko-assist-001`.

```text
hostname: DESKTOP-VN0AMD7
whoami: desktop-vn0amd7\barry li (uon)
pwd: C:\Projects\HASHI
active_hashi_root: C:\Projects\HASHI
existing HashiWatchtower service: not found
port_43766: not listening
python default: Python 3.14.2
python paths: Python314, Python311, Python312
artifact sha256: matched
compileall: ok
staging venv + requirements: ok
python -m remote --help: ok
go_no_go: staging_green
```

Install decisions:

```text
final WatchTowerRoot: C:\Projects\WatchTower
controlled HASHI root: C:\Projects\HASHI
service name: HashiWatchtower
instance_id: MSI-WT
display_name: MSI-WT
port: 43766
interpreter: py -3.12 preferred for reproducibility
```

## Install Go

Explicit WatchTower-only install was authorized and sent to `hashiko@MSI`.

```text
command_id: sl-20260516-110606318920490-install-001
target: hashiko@MSI
final WatchTowerRoot: C:\Projects\WatchTower
controlled HASHI root: C:\Projects\HASHI
service name: HashiWatchtower
instance_id: MSI-WT
display_name: MSI-WT
port: 43766
interpreter: py -3.12
MSI HASHI shutdown authorized: false
MSI HASHI stop/restart authorized: false
```

## Install Deadline Follow-Up

At the install wait deadline, HASHI1 controller probes showed:

```text
MSI core Remote health: ok at http://192.168.0.41:8767/health
MSI-WT health: timeout at http://192.168.0.41:43766/health
```

Follow-up sent to `hashiko@MSI` asked for the exact install blocker:

```text
current step
last command
stdout/stderr summary
C:\Projects\WatchTower exists?
.venv exists?
secrets.json exists?
HashiWatchtower service exists?
nssm.exe exists?
```

## Agent3 Install State Inspection Request

`agent3@MSI` returned a staging execution summary after install had already been
requested. Zelda asked `agent3@MSI` to perform read-only install state
inspection:

```text
command_id: sl-20260516-110606318920490-install-inspect-001
mode: read-only
checks: C:\Projects\WatchTower, .venv, secrets.json, instances.json,
        bin\nssm.exe, HashiWatchtower service, port 43766, logs
service changes authorized: false
shutdown authorized: false
```

## Agent4 Staging Green And Install Inspection

`agent4@MSI` replied `staging_green`. Zelda recorded this as a second
independent staging pass and redirected `agent4@MSI` to read-only install state
inspection.

```text
command_id: sl-20260516-110606318920490-install-inspect-002
mode: read-only
checks: C:\Projects\WatchTower, .venv, secrets.json, instances.json,
        bin\nssm.exe, HashiWatchtower service, port 43766, logs
install/service changes authorized: false
shutdown authorized: false
```
