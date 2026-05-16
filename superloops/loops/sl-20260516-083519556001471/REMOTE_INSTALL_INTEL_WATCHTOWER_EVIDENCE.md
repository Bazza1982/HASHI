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
controlled_hashi_root: C:\Users\Print\HASHI
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

### agent1 Preflight Reply

```text
report_type: watchtower_intel_preflight
target_instance: INTEL
agent: agent1
command_id: sl-20260516-083519556001471-preflight-001

hostname: INTEL
whoami: intel\print
pwd: C:\Users\Print\HASHI\workspaces\agent1

paths:
  C:\Users\Print\projects\HASHI: false
  C:\Users\Print\HASHI: true
  C:\Users\Print\.hashi-remote: true
  C:\Users\Print\projects\WatchTower: false
  C:\Users\Print\HASHI\tmp: true
  C:\Users\Print\HASHI\tmp\watchtower_install_staging: true
  C:\Users\Print\projects: false

existing service: HashiWatchtower not present
existing scheduled task: no *WatchTower* tasks
port 43766: no local listener; only TIME_WAIT to 192.168.0.211:43766
python:
  py -0p: 3.14, 3.12
  python: 3.14.2
  py -3.11: missing
PowerShell: 5.1.26100.8457
admin_rights: true
go_no_go: preflight_only_go
```

Decision:

```text
Use C:\Users\Print\HASHI as the controlled HASHI root.
Create C:\Users\Print\projects\WatchTower as the standalone WatchTower root.
Use Python 3.12 because WatchTower requires >=3.10, not specifically 3.11.
Supersede staging-001 because it used py -3.11.
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

### staging-002 Reply

```text
report_type: watchtower_intel_staging_check
command_id: sl-20260516-083519556001471-staging-002

artifact_sha256:
  expected: e5e04c41a75f01cf5c4bc993a57e7b7f78cf9fb97a3ca2729c7c047552414e5c
  actual:   e5e04c41a75f01cf5c4bc993a57e7b7f78cf9fb97a3ca2729c7c047552414e5c
  result: match

extract_path: C:\Users\Print\HASHI\tmp\watchtower_install_staging\extract
py_compile: passed with py -3.12
python: 3.12.10
pip: 25.0.1
remote_help: blocked by missing uvicorn in bare extract
install_script_present: true
agents_json:
  instance_id: WATCHTOWER
  remote_port: 43766
  agents: []
go_no_go: blocked
```

Decision:

```text
This is green enough for install preparation. The bare extract is not expected
to run without dependencies. Install phase must create .venv with py -3.12,
install requirements.txt, then rerun python -m remote --help from the venv
before service install.
```

Earlier sent command:

```text
command_id: sl-20260516-083519556001471-staging-001
delivery: cached API 192.168.0.6:18802
protocol transport: timed out
mode: staging check only, do not install
```

Superseded by corrected Python 3.12 command:

```text
command_id: sl-20260516-083519556001471-staging-002
delivery: cached API 192.168.0.6:18802
protocol transport: timed out
mode: staging check only, do not install
reason: Intel has Python 3.12 and 3.14, no Python 3.11
```

### agent1 Staging-001 Reply

Received after `staging-002` had already been issued.

```text
report_type: watchtower_intel_staging_check
command_id: sl-20260516-083519556001471-staging-001
artifact_sha256:
  expected: e5e04c41a75f01cf5c4bc993a57e7b7f78cf9fb97a3ca2729c7c047552414e5c
  actual:   e5e04c41a75f01cf5c4bc993a57e7b7f78cf9fb97a3ca2729c7c047552414e5c
  result: match
extract_path: C:\Users\Print\HASHI\tmp\watchtower_install_staging\extract
py_compile: blocked, py -3.11 missing
remote_help: blocked, py -3.11 missing
install_script_present: true
agents_json:
  instance_id: WATCHTOWER
  remote_port: 43766
  agents: []
existing_watchtower_service_status: not present
go_no_go: blocked
```

Decision:

```text
This is not a package failure. It is the expected failure mode from the
superseded staging-001 command that used py -3.11. Current valid check remains
staging-002, which uses py -3.12.
```

## Install Evidence

Install Go sent to `agent1@INTEL`.

```text
command_id: sl-20260516-083519556001471-install-001
delivery: cached API 192.168.0.6:18802
protocol transport: timed out

install_root: C:\Users\Print\projects\WatchTower
controlled_hashi_root: C:\Users\Print\HASHI
instance_id: WATCHTOWER_INTEL
display_name: HASHI WatchTower Intel
service_name: HashiWatchtower
port: 43766
python: py -3.12
```

The first local send attempt failed before delivery because Bash expanded
PowerShell `$variables`. The command was resent safely via subprocess argv.

Partial progress from `agent1@INTEL`:

```text
venv: created
dependencies: installed
python -m remote --help: works from final WatchTower root
next: explicit service install, then service state + HTTP endpoint validation
```

### Missed Follow-Up Correction

At 19:35 local time, the orchestrator reviewed the loop and found a workflow
failure: after `agent1@INTEL` reported install progress, no follow-up deadline
was scheduled.

Controller-side probes:

```text
http://192.168.0.6:43766/health
  result: timeout after 5 seconds

python3 tools/remote_rescue.py --json status INTEL
  ok: true
  state: running
  hashi_running: true
  workbench_port: 18802
  agents: agent2, agent1, lily

hchat route check to agent1@INTEL
  ok: true
  route_type: cached_workbench
  host: 192.168.0.6
  port: 18802
```

Follow-up sent:

```text
command_id: sl-20260516-083519556001471-install-followup-001
delivery: cached API 192.168.0.6:18802
purpose: report current service/root/health/log status immediately
```

Independent inspection requested from `agent2@INTEL`:

```text
command_id: sl-20260516-083519556001471-agent2-inspect-001
delivery: remote API 192.168.0.6:18802
mode: read-only inspection
reason: agent1 install report overdue and WatchTower health probe timed out
```

### agent1 Follow-Up Reply

```text
report_type: watchtower_intel_install_followup
command_id: sl-20260516-083519556001471-install-followup-001
install_001_status: blocked
service: HashiWatchtower does not exist
port_43766: no listener
final_root: C:\Users\Print\projects\WatchTower exists
venv_python: exists
bin\nssm.exe: missing
health: unable to connect
logs: no service/runtime logs because service was not created
blocker: NSSM missing; install script failed before service creation
```

### NSSM Fix

```text
source: https://nssm.cc/release/nssm-2.24.zip
selected: win64/nssm.exe
target: C:\Users\Print\projects\WatchTower\bin\nssm.exe
size: 331264
sha256: f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97
remote_stat: verified
```

Service retry sent:

```text
command_id: sl-20260516-083519556001471-service-retry-001
delivery: cached API 192.168.0.6:18802
action: rerun service-install portion only
```

### HASHI1 Health Probe After NSSM Fix

```text
url: http://192.168.0.6:43766/health
ok: true
instance_id: WATCHTOWER_INTEL
display_name: HASHI WatchTower Intel
remote_port: 43766
capabilities:
  - handshake_v2
  - agent_directory_v1
  - protocol_message_v1
  - agent_reply_v1
  - rescue_control
  - rescue_start
  - file_transfer_hmac_v1
  - message_attachments_v1
shared_token_configured: false
```

Rescue endpoint smoke before token fix:

```text
/control/hashi/status: 401
/control/hashi/logs: 401
/control/hashi/start: 401
```

### Token Fix

Standalone WatchTower package intentionally excluded real `secrets.json`. After
health confirmed the service was running, HASHI1 pushed a minimal
`secrets.json` with only `hashi_remote_shared_token` to:

```text
C:\Users\Print\projects\WatchTower\secrets.json
```

The token value was not logged or committed. Restart requested:

```text
command_id: sl-20260516-083519556001471-token-restart-001
delivery: cached API 192.168.0.6:18802
action: Restart-Service HashiWatchtower; then report health/status/logs
```

## Broadcast Evidence

Pending.

## Rescue Smoke Evidence

Pending.

## Open Risks

- Intel username/path may not be `C:\Users\Print`.
- Intel port `43766` may be occupied.
- Service install may require elevation.
- Current Intel HASHI root must be confirmed by `agent1@INTEL`.
