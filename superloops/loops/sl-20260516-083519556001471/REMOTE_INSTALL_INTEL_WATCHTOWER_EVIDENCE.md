# Intel WatchTower Remote Install Evidence

Loop id: `sl-20260516-083519556001471`

## Current Status

Package is staged on Intel. `agent1@INTEL` has been instructed to run
staging-only checks; reply pending.

## Target

```text
target_instance: INTEL
remote_worker: agent1@INTEL
package_source: C:\Users\thene\projects\WatchTower on HASHI9/A9, or equivalent standalone WatchTower repo payload
staging_root: C:\Users\Print\HASHI\tmp\watchtower_install_staging
install_root: C:\Users\Print\projects\WatchTower
controlled_hashi_root: C:\Users\Print\projects\HASHI
service_name: HashiWatchtower
instance_id: WATCHTOWER_INTEL
proposed_port: 43766
```

## Safety Boundaries

- HASHI1 must not be stopped.
- Intel HASHI core must not be modified except being the controlled target.
- WatchTower must remain a standalone repo outside Intel HASHI.
- No secrets, runtime DBs, logs, `.git`, `.venv`, or local state in package.
- No destructive rescue stop until non-destructive smoke is green.

## Preflight Evidence

### HASHI1 Route Checks

```text
hchat route check:
  ok: true
  target: agent1@INTEL
  route_type: remote_protocol
  host: 192.168.0.6
  remote_port: 8766

protocol status:
  url: http://192.168.0.6:8766/protocol/status
  ok: true
  protocol_version: 2.0
  display_handle: @intel
  protocol_auth_mode: shared-token
  shared_token_configured: true
  rescue_start_enabled: true
  rescue_start_requirement: L3_RESTART
  capabilities:
    - handshake_v2
    - agent_directory_v1
    - protocol_message_v1
    - agent_reply_v1
    - rescue_control
    - rescue_start
    - file_transfer_hmac_v1
    - message_attachments_v1

health:
  url: http://192.168.0.6:8766/health
  ok: true
  instance_id: intel
  display_name: INTEL
  workbench_port: 18802
```

### Current INTEL HASHI Status

```text
remote_rescue status:
  base_url: http://192.168.0.6:8766
  ok: true
  state: running
  hashi_running: true
  pid: 12920
  workbench_url: http://127.0.0.1:18802/api/health
  workbench agents: agent2, agent1, lily
```

### Open Preflight Issue

```text
remote_file_transfer stat INTEL:C:\Users\Print\HASHI
=> ERROR: File transfer authentication failed
```

Resolved by loading the HASHI1 shared token into the transfer CLI environment
without logging the token value.

### agent1 Command

Sent non-destructive preflight to `agent1@INTEL`.

```text
delivery: cached API 192.168.0.6:18802
protocol transport: timed out
command_id: sl-20260516-083519556001471-preflight-001
```

## Package Manifest

```text
artifact: superloops/loops/sl-20260516-083519556001471/artifacts/watchtower_standalone_20260516.tar.gz
size: 86711 bytes
sha256: e5e04c41a75f01cf5c4bc993a57e7b7f78cf9fb97a3ca2729c7c047552414e5c
archive_entries: 62
source_repo: C:\Users\thene\projects\WatchTower
source_git_status: clean
```

Excluded from package:

```text
.git/
.venv/
.pytest_cache/
logs/
state/
instances.json
secrets.json
remote_runtime_claim.json
remote_live_endpoints.json
*/__pycache__/
*.pyc
*.sqlite*
```

Privacy scan of the archive matched only sample files:

```text
./.gitignore
./instances.json.sample
./secrets.json.sample
```

## Transfer Evidence

```text
remote staging:
  C:\Users\Print\HASHI\tmp\watchtower_install_staging

pushed:
  watchtower_standalone_20260516.tar.gz
    bytes: 86711
    sha256: e5e04c41a75f01cf5c4bc993a57e7b7f78cf9fb97a3ca2729c7c047552414e5c

  watchtower_standalone_20260516.sha256
    bytes: 159
    sha256: 7ddae08aaed9f7f9a4f4b7127ea69414bf2efe8111a7b3fe5c8179ff4de91b0b

remote stat:
  tar.gz exists: true
  tar.gz size: 86711
  tar.gz sha256: e5e04c41a75f01cf5c4bc993a57e7b7f78cf9fb97a3ca2729c7c047552414e5c
  sha256 file exists: true
```

## agent1 Staging Report

Pending. Sent command:

```text
command_id: sl-20260516-083519556001471-staging-001
delivery: cached API 192.168.0.6:18802
protocol transport: timed out
mode: staging check only, do not install
```

## Install Evidence

Pending.

## Broadcast Evidence

Pending.

## Rescue Smoke Evidence

Pending.

## Open Risks

- Intel username/path may not be `C:\Users\Print`.
- Intel port `43766` may be occupied.
- Service install may require elevation.
- Current Intel HASHI root must be confirmed by `agent1@INTEL`.
