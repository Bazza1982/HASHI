# HASHI3 Shared Release Preflight — 2026-09-08

Status: **prepared, not adopted**

Scope: PAO delivery coordination, Frontend Connector behavior, Functions, and
Windows platform integration. This record does not authorize or claim a merge,
restart, migration, Remote task update, browser action, or live message send.

## Exact source boundary

The release-preparation worktree was created from:

- runtime source base: `bef485fe521c88edd6f50236f70761b729a92c4d`;
- reviewed HChat merge: `6945bf14`;
- reviewed HChat fix within that merge: `a37480ea`;
- preparation branch: `release-h3-preflight-20260908`.

The HASHI2 `main` branch advanced to `4b5497cb10a3b3b8f44f179c6acedad805329fde`
after this worktree was created. That commit changes Superloop controller
follow-through. It is not present in this candidate. The release coordinator
must decide whether to retain the requested `bef485fe` boundary or rebuild and
requalify from the newer HASHI2 tip. No implicit fast-forward is safe.

The inspected HASHI3 source root remained read-only at committed HEAD
`9ee2f4185a3c1acaf2df4130bee9f84a2aa89962`. It was eight commits ahead and
seven commits behind its configured `origin/main`, with 13 modified tracked
files and three untracked files. No HASHI3 file, process, task, configuration, or
runtime state was changed during this preflight.

## Repository and runtime comparison

The committed-tree comparison between `bef485fe` and HASHI3
`9ee2f418` was:

| Class | Count |
| --- | ---: |
| Candidate tracked paths | 1,423 |
| HASHI3 tracked paths | 1,395 |
| Identical paths | 1,285 |
| Changed paths | 106 |
| Candidate-only paths | 32 |
| HASHI3-only paths | 4 |

This is a shared release, not a narrow HChat cherry-pick.

### Protected Core

The candidate's authoritative `CORE_SOURCE_PATHS` contains nine paths. Against
the committed HASHI3 tree, two are identical, four are changed, and three are
new:

| Candidate Core path | Comparison with HASHI3 committed HEAD |
| --- | --- |
| `__main__.py` | changed |
| `main.py` | changed |
| `orchestrator/runtime_contract.py` | changed |
| `orchestrator/instance_lock.py` | identical |
| `orchestrator/kernel_artifact.py` | candidate-only |
| `orchestrator/kernel_import_guard.py` | candidate-only |
| `orchestrator/kernel_process.py` | candidate-only |
| `orchestrator/function_worker_bootstrap.py` | changed |
| `orchestrator/function_worker_protocol.py` | identical |

Runtime identity differs materially:

| Field | Candidate | Active HASHI3 |
| --- | --- | --- |
| Core API | 3 | 2 |
| Function API | 3 | 2 |
| protected Core digest | `sha256:4d4c776d205a76c42d66439ca4655c646d46c0d6823ec4864b048e2215162acb` | `sha256:dc53fd4048d919e8c55bf282858441a0c062ce2182d2b4e50685ff0f9eaf9a06` |
| active/candidate generation | `sha256:7abae7990889635f6a9c059eecea1c2bab5cd0acea7401f4b0c8c4ab46e8ef79` | `sha256:225ba333…` |
| Function modules | 351 | 278 |

The candidate also replaces HASHI3's older broad 64-path protection manifest
with the nine-path Minimal Core boundary. The manifest itself passed the
candidate's protection check. Because the Core digest and both API versions
change, `/reboot` and `python main.py --replace-functions` must reject this
transition. Adoption requires a separately authorized, coordinated cold Core
migration.

### Python and dependencies

Both sides use CPython 3.12.13. The candidate qualified on native Windows as
`.cp312-win_amd64.pyd`.

| Input | Comparison |
| --- | --- |
| `constraints/standard-py312.lock` | identical, `58e908ed…` |
| `requirements.txt` | identical, `049dcff5…` |
| `requirements-dev.txt` | identical, `a0fe1d97…` |
| `package.json` | identical, `52fd1ed9…` |
| `pyproject.toml` | changed: candidate `379250eb…`, HASHI3 `55cc0580…` |
| `runtime-entry.json` | candidate-only, `872d227…` |
| effective dependency digest | identical, `sha256:b701bf8081dfceadd91203d8cea859d5f38addc5b072b389088b31c016dd4974` |

The lock equality avoids a package-set change, but the runtime policy and API
changes still require cold adoption.

## Native Windows qualification

A one-time native Windows qualification used HASHI3's existing approved
CPython 3.12.13 environment without starting a model, service, listener, or
Agent. It produced:

- runtime policy digest `sha256:d7f608ca…`;
- Core API 3 and Function API 3;
- 351 isolated Function modules;
- generation `sha256:7abae7990889635f6a9c059eecea1c2bab5cd0acea7401f4b0c8c4ab46e8ef79`.

The temporary generation directory was removed after inspection.

The candidate Remote PowerShell files are content-identical to the corresponding
HASHI3 local working files after line-ending normalization. Direct execution
from a WSL UNC path is blocked by Windows `RemoteSigned`; this is a source-zone
policy effect, not a parser or product failure. The same three Remote tests
passed against the native NTFS copy, and PowerShell AST parsing reported zero
errors for both scripts. Final adoption must use a native NTFS checkout.

## HASHI3 local work that must be reconciled

The HASHI3 working root contained these modified tracked files:

- `bin/hashi_remote_ctl.ps1`;
- `bin/hashi_remote_task_runner.ps1`;
- `docs/FIXED_FLEX_WORKING_MODES.md`;
- `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`;
- `orchestrator/flexible_agent_runtime.py`;
- `orchestrator/flexible_backend_manager.py`;
- `orchestrator/runtime_model_selection.py`;
- `tests/test_api_gateway_process.py`;
- `tests/test_flexible_backend_state.py`;
- `tests/test_hashi_api.py`;
- `tests/test_pcm_upgrade_contract.py`;
- `tests/test_wrapper_commands.py`;
- `tools/run_api_gateway_sidecar.py`.

It also contained these untracked files:

- `orchestrator/runtime_effort_options.py`;
- `tests/test_backend_selection_transaction.py`;
- `tests/test_remote_windows_supervisor.py`.

The candidate matches the HASHI3 working copy for both Remote PowerShell files
after line-ending normalization, and exactly matches
`orchestrator/runtime_effort_options.py`,
`tests/test_flexible_backend_state.py`, `tests/test_hashi_api.py`,
`tests/test_pcm_upgrade_contract.py`, and
`tests/test_remote_windows_supervisor.py`. Other dirty files require semantic
reconciliation. The candidate intentionally lacks the old
`tests/test_api_gateway_process.py` under the Minimal Core architecture.

Do not switch or clean the HASHI3 root until all 16 paths are captured in a
local preservation commit or equivalent immutable bundle and reviewed. In
particular, an untracked path that becomes tracked in the candidate must be
moved or committed before checkout.

## Configuration and state preservation

At inspection time the HASHI3 root contained these ignored local files:

- `.bridge_u_last_agents.txt`;
- `.runtime_port_assignments.lock`;
- `agents.json`;
- `contacts.json`;
- `runtime_port_assignments.json`;
- `scheduler_state.json`;
- `secrets.json`;
- `tasks.json`.

`instances.json` was absent. The durable directories `state`, `logs`,
`workspaces`, and `superloops` were present. Remote also uses the
per-user `.hashi-remote` directory. These are instance data and must remain
outside the release commit.

Before cutover, record SHA-256 values for every present non-secret configuration
file, record the presence and access controls of `secrets.json` without
publishing its digest, and capture the exact scheduled-task definitions.
After startup, compare the same files byte-for-byte. A missing
`instances.json` must remain an explicit pre-state; do not silently copy one
from another instance.

The active HASHI3 configuration assigns Remote port 8769 through
`agents.json` and `runtime_port_assignments.json`; the tracked
`remote/config.yaml` default is 8767. Any Remote registration or verification
must resolve the effective value as 8769 and must not reset the allocator.

At inspection, these Windows tasks were running and pointed at the existing
HASHI3 root:

- `HashiRemote-hashi3`;
- `HASHI-HASHI3-DeviceControl-Browser`;
- `HASHI-HASHI3-DeviceControl-Computer`.

Their definitions and enabled/running states must be restored exactly. Generic
Python process scans or kills are prohibited.

## Exact adoption plan

The following is an operator plan. None of these adoption steps ran during this
preflight.

### A. Freeze and stage before an outage

1. Resolve whether `4b5497cb` belongs in the release. If included, rebuild this
   branch from that exact ref and repeat all qualification.
2. Capture HASHI3's current committed HEAD, branch/upstream relation, binary
   tracked diff, untracked product files, and ignored configuration inventory
   into a local rollback area. Create and verify a preservation ref for the 16
   dirty product paths.
3. Import the exact reviewed release commit into HASHI3's Git object database
   using a reviewed local bundle or explicit fetch ref. Do not merge it into
   `main`.
4. Create a separate native NTFS checkout of that exact commit for Windows
   qualification. Build its `.venv` from the approved Python and lock before
   the outage. Re-run the runtime check, isolated Function qualification,
   focused Windows tests, offline product suite, and protected-Core gate.
5. Record current health, exact Core/shared/Worker PIDs, API ports, active
   generation IDs, task definitions, effective Remote port, and configuration
   hashes.
6. Confirm the target HASHI3 worktree can switch cleanly after the preservation
   ref is made. Do not use `git clean`.

A separate checkout is appropriate for staging, but final execution should
retain the existing HASHI3 root path. Remote currently treats its
`--hashi-root` as both code and instance-state root, and the Windows tasks are
registered to that path. Running Core from one root and Remote from another
would create a split release and duplicate state.

### B. Coordinated cold cutover after explicit authorization

1. Quiesce intake and wait for current requests to finish. Record final audit
   cursors.
2. Stop and disable the three exact HASHI3 scheduled tasks named above. Confirm
   their PIDs have exited without touching another HASHI instance.
3. From the old HASHI3 root, use the instance-aware controller with the current
   HASHI3 bridge home to stop the exact Core tree. Confirm the instance lock is
   released and ports 18804, 18805, and 8769 are no longer owned by those old
   PIDs.
4. Switch the stopped HASHI3 working tree to the exact imported release commit.
   Verify `git rev-parse HEAD`, the protected Core digest, runtime policy,
   dependency digest, and the unchanged ignored configuration hashes.
5. Start HASHI3 from the same root and bridge home using the approved native
   Python and the saved Agent selection. Start the API Gateway only if it was in
   the recorded pre-state.
6. Re-register/enable `HashiRemote-hashi3` from the new scripts at the same
   root. Verify instance `HASHI3`, effective port 8769, task action, working
   directory, process claim, and health before restoring peer traffic.
7. Restore the two DeviceControl tasks to their recorded enabled/running states,
   then verify their task actions still reference the same HASHI3 root.
8. Require health to report Core API 3, Function API 3, the expected Core digest,
   the exact source/generation ref, all intended Workers active, API ports
   correct, and Telegram ingress connected.
9. Run the authorized HChat canary below. Source adoption and terminal delivery
   are separate gates.

### C. Rollback

1. Stop/disable only the exact HASHI3 scheduled tasks and candidate PIDs.
2. Stop the candidate Core with the instance-aware controller and confirm the
   HASHI3 lock and ports are released.
3. Switch the stopped root to the verified preservation ref. Do not reset or
   delete conversations, audit logs, queues, workspaces, or Remote peer state.
4. Restore only configuration files proven to have been changed by startup,
   using the pre-cutover byte snapshot. Keep newly received user data.
5. Validate the old runtime fingerprint and dependencies, start the old Core
   with the same bridge home and Agent selection, then restore the exact task
   definitions and states.
6. Verify the old health/generation identities and record the rollback receipt.
   If a candidate state migration is not backward compatible, stop and escalate
   rather than rewinding the whole state directory.

## HChat evidence boundary

Evidence was read from existing protocol state, the conversation database,
canonical audit events, and existing Telegram/bridge logs. No message was sent
for this preflight.

| Case | Existing evidence | Correct classification |
| --- | --- | --- |
| Current assignment `msg-1347c029ab3244d0` | The assignment file was 899 UTF-8 bytes with SHA-256 `d5a4c819ce6f423d70c19f63477895b7d8fe69eed780f5958ec1b3ac12c7b305`; after removing the typed system wrapper and protocol footer, the HASHI3 inbound prompt matched byte-for-byte. Sender and receiver state was `delivered_to_local_queue`. | Body integrity and local queue admission only; no terminal-send claim. |
| Prior reply `msg-9495c23621bc403a` | Reply body length 1,375 characters, SHA-256 `bcdd4969d521a8a68b4cd0ccde0b008929d3ff6a4df88868eb0f9182388838ec`; canonical event recorded `stage=completed`, `delivery_mode=final_delivery`, `purpose=response`, `disposition=sent`, one chunk; Telegram log recorded the same request at 11:05:47 with one chunk and text length 1,375. | Terminal Telegram send evidenced. The protocol `reply_sent` state alone would not prove it. |
| HASHI2 receipt for the prior reply | State `reply_delivered_locally`, `deliver_to_telegram=false`; queue/processing/backend completion existed without a matching delivery event or Telegram send line. | Local protocol receipt only. |
| Historical failures | Existing records `msg-54bfca…` and `msg-2e394c…` ended in `failed` with HTTP 404 and 401 respectively, with no contradictory terminal success evidence. | Failed. These records demonstrate the running system's failure boundary, not candidate adoption. |

A peer API acceptance, `delivered_to_local_queue`, protocol `reply_sent`, or
request completion must never be labelled Telegram `sent`. Terminal send
requires the Frontend Connector's canonical completed/sent delivery event and a
matching Telegram send record for the same request and payload.

## Minimal authorized live HChat canary

Run this only after separate authorization and successful cold adoption.

1. Record protocol, canonical-audit, bridge-log, and Telegram-log cursors.
2. Send one HChat request with a unique nonce and an exact body containing
   Markdown, CJK text, and Unicode punctuation.
3. At the sender, require the immediate result to say `queued`; retain the
   message ID, correlation ID, and request ID.
4. At HASHI3, require exactly one request for that identity and verify the
   unwrapped inbound body byte length and SHA-256 against the source.
5. Complete one normal reply. Do not call it sent until a canonical
   `delivery_event` records `stage=completed`,
   `delivery_mode=final_delivery`, `purpose=response`, and
   `disposition=sent`, and the Telegram log matches the same request,
   chunk count, and body length/hash.
6. Classify the HASHI2 terminal reply receipt separately as local or Telegram
   according to its own connector evidence.
7. Replay the same message ID once. Require no second queue entry, Agent request,
   delivery event, or Telegram message.
8. Send one request to a unique nonexistent Agent target. Require the command to
   return false/failed, preserve the complete original payload in the failure
   report, and produce no HASHI3 request or delivery event.
9. Do not induce a real Connector outage. Keep the contradictory
   `ok=true/state=reply_failed` boundary in deterministic offline tests.

## Validation record

The first Windows and WSL runs intentionally exposed the candidate's unresolved
test contracts.

Initial Windows execution:

- 415 passed and five failed in the first 420-test selection;
- two failures asserted POSIX `0600` mode bits on Windows;
- three failures were unsigned UNC PowerShell execution blocked by
  `RemoteSigned`;
- a later model/API selection exposed 17 stale handoff visibility expectations,
  one Windows symlink privilege failure, and one case-insensitive filename
  assumption.

Initial WSL offline product suite:

- 3,829 passed, 161 deselected, five failed, and one third-party AnyIO
  deprecation warning;
- three failures were stale Frontend visibility/FYI contracts;
- two high-volume HER concurrency tests reached their fixed one-second wait
  before planning and audit setup admitted both sub-agents.

The corrected tests and reference now:

- expect accepted backend, browser/API, and retry-handoff turns to remain
  Telegram-visible, matching the accepted activity-visibility decision;
- skip only the symlink subcase when Windows returns WinError 1314;
- verify byte preservation instead of relying on a case-sensitive filename
  absence;
- mark POSIX mode-bit contracts as non-Windows;
- allow five seconds for the two high-volume concurrency boundaries without
  changing the HER implementation;
- keep `AGENT_FYI.md` below its 12,000-character runtime budget by removing
  duplicated detail while retaining current authority, HChat, adoption, and
  routing rules.

Red/green evidence:

- stale visibility, FYI budget, and one-second concurrency selection: five
  failed before correction;
- the same seven focused checks, including all `test_agent_fyi.py` nodes:
  seven passed after correction;
- concurrency-only diagnostic with a five-second boundary: two passed in
  2.88 seconds, confirming ordered waves and cancellation complete;
- WSL selection/PCM suite: 119 passed;
- WSL POSIX mode-bit contract: two passed;
- Windows model/API selection: 169 passed, one Windows privilege skip;
- final Windows touched-file selection: 125 passed and three expected platform
  skips;
- native NTFS Remote PowerShell tests: three passed;
- PowerShell AST parse: zero errors for both scripts;
- final curated Core gate: 606 passed in 72.30 seconds;
- final explicit offline product suite: 3,834 passed, 161 deselected, one
  third-party AnyIO deprecation warning, in 484.58 seconds.
- protected Core manifest and the diff against `bef485fe` both passed;
- `git diff --check` passed.

The final Core gate, protected-Core checks, and diff checks are recorded in the
preparation commit report. No live, provider, browser, desktop, restart, or
migration test was run.

## Open release gates

1. Decide whether to include HASHI2 `4b5497cb`; rebuild and requalify if yes.
2. Reconcile and preserve HASHI3's 16 dirty product paths.
3. Repeat native NTFS qualification against the exact final preparation commit,
   rather than the WSL UNC source zone.
4. Obtain explicit authorization for cold adoption and the live HChat canary.
5. After adoption, collect independent Core/shared/Worker identity,
   configuration preservation, Remote/task, API health, body integrity,
   failure, deduplication, and terminal Telegram delivery evidence.
