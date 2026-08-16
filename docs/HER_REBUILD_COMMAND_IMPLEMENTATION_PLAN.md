# HER `/rebuild` Command Implementation Plan

Status: implemented, live-verified, and consolidated for
`v4.0.0-alpha.2` release preparation

The original isolated implementation remains preserved on
`feature/her-rebuild-command`. The consolidated HASHI line contains
`96aa4fe1` (controller foundation), `6c7fd961` (integrated source, command,
resolver, adoption, and rollback), `2175e0bb` (hot-adoption stability), and
`ccf3f669` (source-scoped build identity), based on the final certified `.22`
HASHI work and its reviewed HER source provenance.

The implementation checkpoint and takeover evidence are recorded in
`HER_REBUILD_PARALLEL_DEVELOPMENT_CHECKPOINT.md`.

Owner: HASHI

Scope: HASHI-integrated HER Rust source, local development builds, safe binary
activation, Agent hot restart, rollback, audit and operator reporting

Related contracts:

- [HER_BACKEND_CONTRACT.md](HER_BACKEND_CONTRACT.md)
- [HER_TOOL_GATEWAY_TELEMETRY_PLAN.md](HER_TOOL_GATEWAY_TELEMETRY_PLAN.md)
- [HER_CODE_MODULE_PLAN.md](HER_CODE_MODULE_PLAN.md)
- [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)
- [HER_CRON_CONVERSATION_CONTINUITY_FIX_PLAN.md](HER_CRON_CONVERSATION_CONTINUITY_FIX_PLAN.md)

## 1. Executive decision

Keep the existing HER execution engine in Rust. Add a HASHI-owned `/rebuild`
command that detects HER Rust source changes, compiles them incrementally,
verifies the candidate, activates it atomically, adopts it through the existing
Agent hot-restart path, and reports a correlated success or failure result.

The two commands have separate, stable meanings:

```text
/reboot
  reload HASHI Python modules/configuration and restart selected Agent runtimes
  using the already-selected HER executable

/rebuild
  build the current host's HER development executable from integrated Rust
  source, verify and atomically select it, then perform the required targeted
  Agent reboot and post-adoption health check
```

`/rebuild` is a development-adoption command, not a release command. It must
never update the certified HER package manifest, certification baseline, release
version, or production binary in place. A successful development build is not a
production-certified HER release.

## 2. Problem to solve

HASHI Python changes can be adopted by `/reboot` because the live kernel
preflights and reloads Python modules before reconstructing target Agent
runtimes. HER Rust changes cannot be adopted from source directly: Rust is
compiled ahead of time and the current adapter resolves an existing executable.

The current development path therefore conflates several different operations:

```text
edit Rust
-> invoke Cargo manually
-> find/copy the resulting executable
-> change or replace package metadata
-> restart the Agent
-> infer whether the new executable was really loaded
```

That path is slow, easy to perform inconsistently, and too close to the
production certification path. It also makes it possible to produce internally
consistent manifest metadata around the wrong source lineage, as happened with
the rejected `.21` artifact.

The intended path is:

```text
edit integrated HER Rust source
-> /rebuild
-> incremental Cargo build in an isolated development target directory
-> deterministic offline verification
-> immutable versioned candidate
-> atomic development selection
-> targeted /reboot adoption
-> explicit post-adoption result
```

## 3. Goals

1. Make one authorized `/rebuild` invocation sufficient to compile and adopt
   current HER Rust changes on the running host.
2. Preserve the existing Rust implementation and process boundary.
3. Make development builds incremental and materially faster than clean release
   builds.
4. Keep the previously active HER usable until a candidate has passed all
   mandatory quick checks.
5. Never expose a partially written or failed candidate as the active runtime.
6. Avoid interrupting an active HER request merely to adopt a development build.
7. Return a concise success, failure, pending-activation, or rollback result with
   a stable job ID and useful reason.
8. Preserve full local logs and provenance without leaking secrets through chat.
9. Keep development adoption strictly separate from production release
   certification.
10. Reuse HASHI's existing `/reboot` lifecycle rather than create a second Agent
    restart implementation.
11. Work for both flexible and legacy/fixed Agent runtimes through one shared
    command handler and one kernel-owned rebuild manager.
12. Allow later `/reboot` calls to adopt the last successful development
    candidate without rebuilding it again.

## 4. Non-goals

- Do not rewrite HER in Python.
- Do not make Cargo or Rust a dependency of normal customer/runtime installs.
- Do not compile Rust during ordinary HASHI startup or ordinary `/reboot`.
- Do not build every supported platform from one host.
- Do not cross-compile the Windows MSVC release from a Linux/WSL `/rebuild`.
- Do not run paid/live provider calls as part of the mandatory rebuild gate.
- Do not update `hashi_assets/her/manifest.json` or
  `hashi_assets/her/certification_baseline.json`.
- Do not write a release version such as `.22` merely because a local development
  build succeeded.
- Do not overwrite the certified packaged executable.
- Do not clear or silently replace the Agent's persistent HER session.
- Do not add `/rebuild force` in the first implementation.
- Do not kill active HER runs to make activation faster.
- Do not treat a quick rebuild smoke suite as full Rust or release
  certification.
- Do not create a second generic background-job framework when a small
  rebuild-specific durable state record is sufficient.

## 5. Authoritative terminology

### 5.1 Build

Compile the integrated HER Rust source into a candidate executable. A build can
succeed while activation is still waiting for an idle Agent.

### 5.2 Verify

Run deterministic, offline checks against the candidate executable and its
source/build metadata. Verification is deliberately smaller than release
certification.

### 5.3 Activate

Atomically change the development selection record from the previous candidate
to the newly verified immutable candidate. Activation does not mutate the
certified package manifest.

### 5.4 Adopt

Reconstruct the target Agent through the existing hot-restart path so its new
HER adapter resolves and validates the selected development candidate.

### 5.5 Rebuild success

`/rebuild` reports success only after build, verification, activation, targeted
restart and post-adoption health checks all pass.

### 5.6 Release certification

The separate clean-source, full-test, Clippy, reproducibility, cross-platform,
manifest, digest and live-canary process required to promote a production HER
package. `/rebuild` never claims this state.

## 6. Source integration prerequisite

The authoritative HER source must become a first-class HASHI source component
before `/rebuild` is enabled. An external working copy whose branch and commit
can drift independently is not a sufficient runtime build source.

Use this repository shape:

```text
native/her/
  LICENSE
  UPSTREAM_SOURCE.json
  rust/
    Cargo.toml
    Cargo.lock
    crates/
```

Import the reviewed MIT source as a Git subtree or equivalent vendored source
snapshot, not a Git submodule required at runtime. Preserve:

- original copyright and MIT license;
- upstream repository identity;
- upstream base commit;
- HASHI source-line commit and import date;
- any downstream patch-series/provenance record;
- the exact source used to reproduce the current certified package before
  applying new changes.

The production package may continue to ship only the executable, license and
certification metadata. The integrated source is a development/release source
component; normal installed runtime startup must not require it.

### 6.1 Source root discovery

`/rebuild` resolves only the canonical source root under the running HASHI code
root. It must not search arbitrary external directories, environment-provided
repositories, PATH, the current Agent workzone, or user prompt content.

If the integrated source is absent, `/rebuild` returns an unavailable result and
leaves the current HER untouched.

### 6.2 Supported host

The first implementation builds only the current host platform:

```text
Linux/WSL x86-64 -> x86_64-unknown-linux-gnu development candidate
Windows x86-64   -> x86_64-pc-windows-msvc development candidate
```

Unsupported hosts fail before Cargo starts. Platform support for `/rebuild`
does not imply that the corresponding production release target is certified.

## 7. Command contract

### 7.1 `/rebuild`

Starts or joins the HER development rebuild for the current source fingerprint.
The first release targets the Agent from which the command was authorized.

Expected immediate response:

```text
HER rebuild started
Job: rebuild-...
Target: this Agent / current host
Current HER: ...
Stage: source preflight
```

The command returns immediately after the job is durably accepted; it must not
hold a Telegram handler open for the entire Cargo build. A later correlated
message reports success, failure, deferred activation or rollback.

If another job is already building the same source fingerprint, `/rebuild`
joins that job instead of launching a duplicate Cargo process. If a different
fingerprint is already building, the command reports the existing job and asks
the operator to wait; v1 does not maintain an unbounded rebuild queue.

### 7.2 `/rebuild status`

Returns the current or most recent rebuild record:

```text
job_id
state and stage
source commit / dirty flag / source fingerprint
host target and Rust toolchain identity
build profile
elapsed time
candidate binary SHA-256, when available
active development candidate
target Agent adoption state
previous candidate retained for rollback
last bounded error, if any
full local log reference
```

The status response must explicitly distinguish:

- certified packaged HER;
- development candidate built but not active;
- active development HER;
- rolled-back development HER.

Every status surface is strictly read-only. In particular, the offline
`scripts/her_rebuild_dev.py --status` path reads `HERRebuildJobStore` directly;
it must not construct a coordinator, run startup recovery, acquire manager or
build ownership, create an absent state directory, or rewrite a job record.

### 7.3 `/rebuild help`

May be an alias of the no-argument help card during implementation, but it must
not add mutation options beyond `/rebuild` and `/rebuild status` in v1.

### 7.4 Authorization and surfaces

- Only the same authorized human owner accepted by lifecycle commands may run
  `/rebuild`.
- The command is intercepted by HASHI and is never sent to a model or exposed as
  a HER tool.
- It is unavailable through untrusted API prompts, cron content, Habit content,
  MCP tools, or assistant-generated slash-command text.
- It must appear in slash-command audit with actor, channel, target, job ID,
  result and side effects.
- Normal packaged installations without integrated source/toolchain report the
  command as unavailable; they do not attempt downloads or bootstrap Rust.

## 8. Rebuild state machine

Use one explicit state machine. A job never moves backwards except through a
recorded rollback transition.

```text
accepted
  -> source_preflight
  -> waiting_for_build_lock
  -> building
  -> verifying
  -> candidate_ready
  -> waiting_for_agent_idle
  -> activating
  -> reboot_requested
  -> adopting
  -> postcheck
  -> succeeded
```

Failure/alternative terminal paths:

```text
source_preflight -> failed
building         -> failed
verifying        -> failed
waiting_for_agent_idle -> activation_deferred
activating       -> failed
reboot_requested/adopting/postcheck -> rolling_back
rolling_back     -> rolled_back
rolling_back     -> rollback_failed
```

Definitions:

- `failed`: the selected HER never changed; the previous runtime remains
  authoritative.
- `activation_deferred`: the candidate is verified and retained, but the target
  Agent did not become safely idle within the activation window. It is not a
  successful rebuild adoption.
- `rolled_back`: the candidate was briefly selected, adoption failed, the prior
  selection was restored, and the prior HER passed its restart/postcheck.
- `rollback_failed`: neither the candidate nor automatic recovery could produce
  a healthy target Agent. This is a critical operator-visible result.

Persist every state transition before performing the next irreversible action.

## 9. Kernel ownership and lifecycle coordination

`/rebuild` cannot be owned by the HER adapter that it will replace. It must be
owned above Agent runtimes by the long-running HASHI kernel.

Add a kernel-level `HERRebuildManager` with responsibility for:

- accepting/deduplicating jobs;
- source/toolchain preflight;
- Cargo subprocess supervision;
- candidate verification and metadata;
- active-run quiescence checks;
- atomic selection and rollback;
- requesting the existing targeted hot restart;
- post-adoption validation;
- durable state and final notification.

Exactly one live `HERRebuildManager` may own a rebuild state root. The manager
holds a process-lifetime OS file lock from construction until shutdown. A
second process is rejected before it can inspect active jobs or invoke startup
recovery. When the owning process actually exits, the operating system releases
the lock; only the next successful owner may classify pre-activation jobs as
interrupted and reconcile any interrupted adoption state. Lock-file PID and
manager-ID fields are diagnostic metadata—the OS lock, not PID reuse or a
heartbeat timeout, is authoritative.

The slash-command handler should be shared through the hot-reloadable command
registry so flexible and fixed runtimes do not gain separate implementations.
It only authenticates, captures the reply target and submits/queries the
kernel-owned job.

### 9.1 Main-loop request model

Do not overload `_restart_request` with build state. Add a small lifecycle
maintenance request/queue whose accepted actions include `her_rebuild`. The
kernel may run the Cargo build while Agents and services remain online. Only
after a candidate is verified and the target is idle does the manager invoke
the existing `RebootManager.hot_restart()` path.

The existing Python module preflight still runs during adoption. A Rust build
must not bypass Python hot-reload safety.

### 9.2 Durable completion notification

The command handler sends only the accepted acknowledgement. The final result
must be deliverable after the target Agent has been reconstructed.

Persist with the job:

- originating Agent;
- authorized actor ID;
- originating chat/channel;
- accepted message/request identity;
- whether the accepted acknowledgement was delivered;
- whether the terminal result was delivered;
- terminal result event ID.

After adoption or rollback, use the newly running Agent's normal HASHI delivery
surface to send the result. The result event ID is idempotent so manager/module
reload or a retry cannot deliver the same terminal notice twice.

## 10. Build lock and concurrency

HER coordination, source and Cargo caches are process-wide resources. Protect
them with all three layers:

1. one process-lifetime manager ownership lock per rebuild state root;
2. an in-process `asyncio.Lock`; and
3. an OS-visible Cargo build lock scoped to the HASHI instance/source root.

Rules:

- Only one Cargo HER build runs per source root and host target.
- A second live manager cannot recover, fail, join or mutate the first
  manager's jobs.
- Status readers never acquire ownership and never mutate durable state.
- Two requests for the same source fingerprint share one build job and may each
  receive their own authorized adoption result.
- A request for a different fingerprint does not silently join an older build.
- A stale lock may be recovered only after validating that its recorded PID is
  absent and no matching Cargo process remains.
- Kernel restart during build marks the job interrupted; it does not assume the
  orphaned candidate is valid.
- V1 does not auto-adopt a candidate produced by an interrupted or uncorrelated
  Cargo process.

## 11. Source and build fingerprint

Every candidate needs a deterministic development identity that does not
pretend a dirty tree is its Git HEAD.

The fingerprint includes:

- normalized relative path and contents of relevant Rust source;
- `Cargo.toml`, workspace manifests and `Cargo.lock`;
- `build.rs` and embedded protocol/resource inputs;
- source Git HEAD, if present;
- dirty flag and a hash covering modified/untracked relevant files;
- Cargo and rustc versions;
- Rust target triple;
- selected build profile and features.

Exclude:

- `.git` internals;
- Cargo `target` output;
- logs and candidate directories;
- editor/temporary files;
- Agent workspaces and credentials.

Candidate identity format may be human-readable, for example:

```text
<short-head>-dirty-<source-hash>-<target>-<profile>
```

The full SHA-256 remains authoritative. If the same complete fingerprint has an
already verified immutable candidate, `/rebuild` may skip Cargo and proceed to
the idle/adoption stage after revalidating the candidate digest.

## 12. Build environment and profile

### 12.1 Environment isolation

Construct a minimal Cargo environment. Do not pass the Agent's provider keys,
Telegram token, browser secrets, Gateway context, prompt content or workzone
environment into the build subprocess.

Allow only the platform/toolchain variables required to locate Cargo, rustc,
linkers and the configured Cargo cache. Record toolchain versions but never dump
the complete environment.

### 12.2 Incremental development profile

Add a dedicated Cargo profile such as `hashi-dev` with:

- incremental compilation enabled;
- modest optimization suitable for real canary execution;
- sufficient debug symbols for useful failures;
- the same functional features and protocol surface as the production package;
- a separate target directory from certified/release builds.

The exact optimization settings should be chosen after measuring warm build
time and short-task runtime. Do not use the default unoptimized debug profile if
it materially changes timeout-sensitive behavior, and do not require a clean
fully optimized release build for every development edit.

Build command shape:

```text
cargo build --locked --profile hashi-dev -p rusty-claude-cli
```

Cargo output goes to an ignored development target/cache directory. After the
build succeeds, copy the resulting executable into a new immutable staging
candidate; never execute directly from Cargo's mutable output path.

### 12.3 Build supervision

- Use an argument array, never a shell command string.
- Capture stdout and stderr to bounded chat summaries and an untruncated local
  log subject to an overall size ceiling.
- Stream only meaningful stage changes to chat; do not forward every compiler
  line.
- Apply a generous but finite build timeout.
- Terminate the Cargo process group on HASHI shutdown or explicit operator
  cancellation outside v1's chat surface.
- Preserve the first actionable compiler diagnostics and terminal exit code.

## 13. Mandatory quick verification

`/rebuild` runs deterministic checks that are fast enough for normal
development adoption. The first implementation must include:

1. source fingerprint and build metadata were written successfully;
2. candidate is a regular executable for the current target;
3. candidate SHA-256 is computed after the immutable copy;
4. `version --output-format json` succeeds and is parseable;
5. reported target matches the running platform;
6. expected HER/HASHI runtime identity is present;
7. `doctor --output-format json` completes in an isolated disposable config;
8. `prompt --help` confirms stdin prompt capability;
9. `stream-json` is advertised and a deterministic mock/offline contract emits
   valid ordered JSONL;
10. a disposable fresh session exposes a session ID;
11. a disposable resume turn preserves that session identity;
12. mandatory HASHI Tool Gateway configuration can be parsed without using
    live credentials;
13. no mandatory check wrote into an actual Agent workspace or session.

Add a small named Rust/HASHI rebuild smoke suite rather than selecting tests by
line number or fragile substring. The smoke suite must exercise output format,
session/resume and startup contracts, not planning quality or live provider
behavior.

The following remain release-only gates:

- full Rust workspace tests;
- all-target Clippy with the certification baseline;
- clean-source/reproducibility proof;
- Linux and Windows production artifacts;
- complete HASHI test suite;
- paid/live provider matrix;
- full Tool Gateway/media/planning/cancellation canary;
- production manifest and certification promotion.

## 14. Candidate storage and atomic activation

Store candidates outside the tracked source tree under the instance's ignored
build/state area. Each verified candidate directory is immutable and contains:

```text
candidate executable
candidate.json
quick-verification.json
build.log
```

`candidate.json` records at least:

```text
schema version
job ID
source fingerprint
Git HEAD and dirty state
host target
Cargo/rustc versions
build profile/features
build start/end/duration
binary SHA-256 and size
quick-verification result
development/non-certified marker
```

Maintain a small atomic development selection record:

```text
active candidate ID/path/digest
previous candidate ID/path/digest
selection timestamp
selecting job ID
target Agent adoption status
```

Write the new selection to a temporary file on the same filesystem, flush it,
and atomically replace the old selection. Do not mutate the candidate executable
after its digest is recorded.

The HER adapter gains an explicit development-source resolution mode. In that
mode it:

- reads only the canonical selection record;
- validates candidate location, platform, executable permission and SHA-256;
- refuses a missing, partial or mismatched candidate;
- reports binary source as `development-source-build`;
- retains the resolved immutable versioned path for its lifecycle.

It must never fall back silently from a requested development candidate to an
uncertified PATH binary. The configured certified package remains the rollback
target.

## 15. Active-run quiescence

Building in the separate Cargo target directory is safe while the old HER is
running. Activation/reboot is not.

After verification, query the target Agent and HER adapter for:

- foreground generation state;
- queued direct requests;
- detached/background HER subprocesses;
- Dream/Meditation/helper HER subprocesses;
- an Agent stop/restart already in progress.

The target is safe to adopt only when all relevant HER processes are terminal
and no earlier direct turn is awaiting delivery. Do not infer idleness merely
from the absence of Telegram typing.

If the target does not become idle within the bounded activation window:

- leave the verified candidate stored but inactive;
- leave the old HER and Agent running;
- mark the job `activation_deferred`;
- report the count/category of active work without exposing private prompt
  content;
- allow a later `/rebuild` of the identical fingerprint to reuse and adopt the
  verified candidate when idle.

V1 has no force switch. Existing `/stop` remains the operator's explicit way to
end work before invoking `/rebuild` again.

## 16. Adoption through existing `/reboot`

Once idle:

1. persist the previous selection and rollback metadata;
2. atomically select the candidate;
3. persist `reboot_requested`;
4. invoke the existing hot restart with target mode equivalent to `/reboot min`;
5. let Python source preflight/module reload and manager reconstruction run as
   usual;
6. initialize the reconstructed target Agent;
7. require its HER adapter to resolve the expected candidate ID/digest;
8. run post-adoption health checks;
9. persist terminal state before sending the final notification.

Do not clear the HER session ID. The candidate must adopt the same persistent
session contract unless an explicit separately approved migration exists.

Other running Agents retain their already resolved immutable executable and are
not interrupted. Future Agent initialization or a later operator-controlled
`/reboot` may adopt the selected development candidate. V1 does not restart all
HER Agents automatically.

## 17. Post-adoption checks

The rebuild succeeds only if the reconstructed target Agent proves:

- target Agent is online or its accepted local-mode equivalent;
- selected backend remains HER where it was HER before rebuilding;
- HER initialization succeeded;
- resolved binary source is `development-source-build`;
- resolved candidate ID and binary SHA match the rebuild job;
- `version` and stream-json capability checks pass in the new adapter;
- HASHI Tool Gateway initialization remains healthy;
- persistent session checkpoint was not cleared or silently replaced;
- no immediate crash or initialization error appears during a short settle
  window.

If the Agent was not using HER when `/rebuild` was invoked, the candidate may be
built and selected, but the postcheck must use an explicit ephemeral HER probe;
it must not silently switch the Agent's selected backend.

## 18. Automatic rollback

If activation succeeds but restart/adoption/postcheck fails:

```text
persist rolling_back
-> atomically restore previous development selection or certified package target
-> hot restart the same target Agent again
-> verify previous HER health
-> persist rolled_back or rollback_failed
-> send one terminal report
```

Rollback must not:

- delete the failed candidate or its logs;
- change the certified manifest;
- erase the user's HER session;
- claim the rebuild succeeded;
- repeat indefinitely.

Allow one automatic rollback attempt. If rollback also fails, report a critical
`rollback_failed` result with both candidate and rollback failure stages. Leave
the complete evidence locally for manual recovery.

## 19. Result and progress reporting

### 19.1 Progress

Send only material stage transitions, for example:

```text
accepted
building
verifying
waiting for Agent idle
restarting Agent
checking adoption
```

Do not stream compiler noise or repetitive polling updates. `/rebuild status`
is the detailed progress surface.

### 19.2 Success

The terminal success message contains:

```text
result: succeeded
job ID
source fingerprint and dirty marker
target/profile
binary SHA prefix
build and total durations
quick verification summary
reloaded Agent
active HER identity/source
rollback candidate retained
explicit “development build; not production certified” label
```

### 19.3 Pre-activation failure

Contain:

```text
result: failed
job ID
failed stage
bounded actionable reason
compiler/test exit code when relevant
current HER unchanged
local log reference
```

### 19.4 Deferred activation

Contain:

```text
result: activation deferred
job ID and candidate ID
build/verification succeeded
reason target was not safely idle
current HER unchanged
instruction to check /rebuild status or retry when idle
```

### 19.5 Rollback

Contain:

```text
result: candidate rejected and rolled back
candidate identity
failed adoption stage/reason
restored HER identity
restored Agent health result
local evidence reference
```

All user-visible text added during implementation must follow the established
HASHI command UI style and be rendered safely for Telegram HTML. Do not include
unescaped compiler output, absolute credential paths, environment dumps or raw
provider/config values.

## 20. Failure taxonomy

Use stable machine-readable `failure_kind` values:

```text
source_missing
source_invalid
unsupported_platform
toolchain_missing
toolchain_mismatch
build_lock_busy
stale_lock_unrecoverable
fingerprint_failed
cargo_timeout
cargo_failed
candidate_missing
candidate_digest_mismatch
candidate_identity_mismatch
quick_test_failed
version_probe_failed
stream_json_probe_failed
session_probe_failed
gateway_probe_failed
activation_deferred
selection_write_failed
reboot_rejected
agent_restart_failed
adapter_initialization_failed
postcheck_identity_mismatch
postcheck_health_failed
rollback_selection_failed
rollback_restart_failed
notification_failed
internal_error
```

The chat reason is concise; the durable record stores stage, exception type,
exit code and bounded diagnostic metadata. Secrets are redacted before either
surface.

## 21. Durable audit and retention

Persist rebuild records outside Agent workspaces so a target Agent restart
cannot lose them. The record schema includes:

- job/request/actor/chat correlation;
- every state transition and timestamp;
- source/build/candidate identities;
- command argv without secrets;
- toolchain and target;
- build and verification outcomes;
- selection before/after;
- restart and postcheck outcomes;
- rollback outcome;
- notification acceptance/delivery state.

Retain:

- the current active development candidate;
- the immediate rollback candidate;
- failed candidate metadata/logs for a bounded diagnostic window;
- recent terminal job summaries.

Garbage collection must never delete the active candidate, rollback candidate,
candidate referenced by a nonterminal job, or certified packaged HER. Candidate
cleanup is a later bounded maintenance action, not part of the critical
activation transaction.

## 22. Proposed code changes

Exact names may be adjusted during implementation, but responsibilities must
remain separated.

### 22.1 Source and build assets

```text
native/her/                                  integrated HER source
native/her/UPSTREAM_SOURCE.json              source/provenance record
scripts/her_rebuild.py                       offline/manual entry using the same controller
```

### 22.2 Kernel orchestration

```text
orchestrator/her_rebuild.py                  fingerprint, build, verify, candidate metadata
orchestrator/her_rebuild_manager.py          job state, lock, activation, rollback, notification
orchestrator/manager_registry.py              manager construction/reconstruction contract
main.py                                      maintenance request handling
orchestrator/reboot_manager.py                correlated post-restart result hook
```

The build/fingerprint/verification functions should be independently testable
without a Telegram runtime or live Agent.

### 22.3 Command surface

```text
orchestrator/commands/rebuild.py              one shared authorized handler
orchestrator/command_specs.py                 canonical built-in metadata if menu-visible
orchestrator/slash_command_audit.py           sensitive lifecycle side-effect audit as needed
```

Do not add separate `cmd_rebuild` copies to flexible and legacy runtimes.

### 22.4 Adapter resolution

```text
adapters/her.py                               explicit development candidate resolution,
                                              identity reporting and postcheck fields
```

Keep `require-packaged` production semantics unchanged. Development resolution
must be explicit, fail-closed and visibly non-certified.

### 22.5 Tests

```text
tests/test_her_rebuild.py
tests/test_her_rebuild_manager.py
tests/test_rebuild_command.py
tests/test_reboot_manager.py
tests/test_her_adapter.py
tests/test_command_registry.py
```

### 22.6 Documentation on completion

Update:

- `HER_BACKEND_CONTRACT.md` with development resolution and adoption semantics;
- `packaging/her/README.md` with the development/release boundary;
- `RELEASE_CHECKLIST.md` to state that `/rebuild` output is never promotion
  evidence by itself;
- operator help with command states and recovery instructions.

## 23. Test plan

### 23.1 Fingerprint unit tests

- identical source/toolchain/profile produces the same fingerprint;
- relevant tracked modification changes it;
- relevant untracked Rust file changes it;
- `target`, logs and temporary files do not change it;
- Cargo manifest/lock change changes it;
- host target/profile/toolchain change changes it;
- dirty build is never labelled only with clean Git HEAD.

### 23.2 Source/toolchain preflight

- canonical source present and valid;
- source missing;
- wrong workspace shape;
- license/provenance missing;
- Cargo absent;
- rustc absent;
- unsupported platform;
- `Cargo.lock` would change under `--locked`;
- build environment excludes configured secrets.

### 23.3 Build controller

- successful fake/incremental Cargo build;
- non-zero Cargo exit;
- compiler diagnostic extraction;
- timeout kills process tree;
- HASHI shutdown interrupts build safely;
- output-size bounding does not remove full local log;
- mutable Cargo output is copied to a new immutable candidate;
- binary digest mismatch fails before activation;
- cached verified identical fingerprint avoids redundant Cargo invocation.

### 23.4 Locking and deduplication

- same-fingerprint requests join one build;
- different-fingerprint request reports existing job;
- second process cannot acquire OS build lock;
- live PID lock is not stolen;
- stale lock is recovered only after process validation;
- restart marks interrupted nonterminal build safely.
- concurrent offline `--status` preserves the byte-identical active job record;
- status against an absent state root creates no files or directories;
- a second live manager is rejected before recovery, while a replacement after
  owner exit still performs the intended interrupted-job recovery.

### 23.5 Quick verification

- valid candidate passes all mandatory offline checks;
- malformed `version` JSON fails;
- wrong target fails;
- stream-json capability missing fails;
- invalid/truncated JSONL fails;
- fresh session without ID fails;
- resume changes identity fails;
- Gateway probe failure fails;
- probes use disposable state and do not touch a real Agent workspace.

### 23.6 Atomic selection

- candidate selection is atomic;
- crash before replace leaves previous selection;
- crash after replace is recoverable from durable job state;
- adapter rejects digest mismatch;
- adapter rejects candidate outside approved root;
- adapter does not fall back to PATH;
- certified package files and manifests remain byte-identical.

### 23.7 Active-run handling

- foreground HER run defers activation;
- queued direct turn defers activation;
- detached HER subprocess defers activation;
- Meditation/helper HER subprocess defers activation;
- build proceeds while old HER is active without changing it;
- idle transition permits exactly one activation;
- activation-window expiry preserves old HER and candidate.

### 23.8 Reboot/adoption

- successful candidate triggers the existing targeted restart path;
- Python module preflight still runs;
- only the requesting Agent is restarted in v1;
- unrelated Agents remain online and keep their resolved binary;
- reconstructed adapter reports expected candidate/digest;
- persistent HER session ID remains available;
- non-HER selected backend is not silently changed;
- terminal success is recorded before notification.

### 23.9 Rollback injection

- Agent restart failure restores previous selection;
- adapter initialization failure restores previous selection;
- candidate identity mismatch restores previous selection;
- postcheck failure restores previous selection;
- restored Agent health produces `rolled_back`;
- rollback failure produces one critical `rollback_failed` result;
- no recursive rollback/restart loop occurs.

### 23.10 Command and notification

- unauthorized user is ignored/denied consistently with lifecycle commands;
- model text cannot trigger rebuild;
- `/rebuild` accepts once and returns a job ID quickly;
- `/rebuild status` works before, during and after target restart;
- repeated status calls do not mutate the job;
- final notification survives target Agent reconstruction;
- duplicate manager recovery does not resend accepted terminal event ID;
- HTML escaping and bounded compiler diagnostics are safe;
- slash-command audit records side effects without secrets.

### 23.11 Real canary

On a disposable or designated development Agent:

1. record current certified/development HER identity and session ID;
2. make a harmless observable Rust version/test fixture change;
3. invoke `/rebuild`;
4. verify Cargo uses incremental artifacts;
5. verify the old Agent remains responsive during build;
6. verify candidate smoke passes;
7. verify targeted Agent restart and candidate adoption;
8. resume the same HER session with an incremental turn;
9. run one Tool Gateway read-only tool;
10. inject a bad Rust edit and prove the active HER remains unchanged;
11. inject an adoption failure and prove automatic rollback;
12. restore the source and rerun the focused/full relevant suites.

## 24. Implementation phases

### Phase 0 — Protect and baseline

1. Protect current HASHI and HER work in intentional commits.
2. Record current packaged binary identity, source lineage and quick startup
   timing.
3. Record warm and clean Cargo build timings in the certified source checkout.
4. Add failing tests for development source absence and unsafe in-place
   activation before production changes.

Exit: reproducible baseline and regression fixtures exist.

### Phase 1 — Integrate source

1. Recover and verify the exact certified HER source line.
2. Import it under `native/her` with license and provenance.
3. Ensure normal HASHI startup/package import does not invoke Cargo.
4. Add source-root and fingerprint tests.

Exit: one canonical in-repository development source root exists.

### Phase 2 — Pure rebuild controller

1. Implement fingerprint and toolchain preflight.
2. Add isolated incremental Cargo profile/cache.
3. Implement build supervision, candidate copy, digest and metadata.
4. Implement mandatory offline quick verification.
5. Add the offline script entry using the same code.

Exit: a manual controller can produce a verified immutable candidate without
changing the active runtime.

### Phase 3 — Development resolver and atomic selection

1. Implement selection record and atomic replace.
2. Add explicit adapter development resolution.
3. Add candidate validation and status identity.
4. Prove production packaged resolution is unchanged.

Exit: a test Agent can deliberately initialize from a selected candidate and
reject tampering.

### Phase 4 — Kernel job and `/rebuild`

1. Add kernel-owned durable job manager and locks.
2. Add shared command handler and `/rebuild status`.
3. Add material progress and durable result notification.
4. Add active-run quiescence and deferred activation.

Exit: one command builds and waits safely without restarting or interrupting
active work prematurely.

### Phase 5 — Adoption and rollback

1. Connect verified activation to targeted existing hot restart.
2. Add post-adoption identity/health contract.
3. Add automatic rollback and one recovery restart.
4. Add failure injection tests.

Exit: `/rebuild` is transactional from the operator's perspective.

### Phase 6 — Canary and documentation

1. Run targeted Python/Rust tests.
2. Run the HASHI HER regression suite.
3. Run the real development canary and rollback drill.
4. Update active contracts/help/release documentation.
5. Only then enable the menu-visible command for normal authorized development
   use.

Exit: accepted command behavior is evidenced on the real host without changing
production certification claims.

## 25. Estimated effort

For one experienced engineer with the certified source available:

| Workstream | Estimate |
| --- | ---: |
| Source integration and provenance | 1–2 days |
| Fingerprint/build/verification controller | 1–2 days |
| Candidate resolver, atomic activation and rollback | 1–2 days |
| Kernel job, command, notification and status | 1–2 days |
| Tests, failure injection, canary and documentation | 1–2 days |
| **Total** | **5–10 engineering days** |

The lower bound assumes Cargo/toolchains are already healthy and the exact
certified source is readily recoverable. Cross-platform development support,
toolchain installation automation, UI expansion or release-pipeline changes are
outside this estimate.

## 26. Acceptance criteria

Implementation is complete only when all of the following are true:

1. HER Rust source is integrated and provenance-preserving inside HASHI.
2. `/rebuild` is handled entirely by HASHI lifecycle code, never by the model.
3. A relevant Rust edit causes an incremental build for the current host.
4. An unchanged verified fingerprint does not rebuild unnecessarily.
5. Build and mandatory quick checks happen before active selection changes.
6. A compile/test/probe failure leaves the current HER byte-for-byte and
   selection-for-selection unchanged.
7. Active HER work is not killed by default; unsafe activation is deferred.
8. Candidate activation is atomic and digest-verified.
9. The existing `/reboot` Python preflight/reconstruction path is reused.
10. The reconstructed Agent proves it loaded the expected candidate.
11. The existing HER session is not cleared merely because of rebuild.
12. Adoption failure automatically restores and verifies the previous HER.
13. Rollback is bounded to one attempt and cannot loop.
14. `/rebuild status` remains correct across Agent reconstruction.
15. Terminal notifications are idempotent and contain an actionable reason.
16. Build environment/logs/chat do not disclose credentials.
17. Other running Agents are not interrupted by v1 adoption.
18. Certified package binary, manifest and certification baseline remain
    untouched by development rebuilds.
19. Status clearly labels the active candidate as development/non-certified.
20. Targeted tests, real canary and rollback drill pass.

## 27. Explicit decisions for v1

The following decisions are closed for the first implementation:

1. Keep HER in Rust.
2. Integrate source under HASHI rather than depend on an arbitrary external
   checkout.
3. `/reboot` does not compile Rust.
4. `/rebuild` compiles, verifies, selects and then reuses targeted `/reboot`.
5. `/rebuild` builds only the current host target.
6. `/rebuild` uses an incremental development profile, not a clean release
   build.
7. `/rebuild` never edits production release metadata.
8. No live provider call is mandatory for development activation.
9. No force-kill/force-activate option exists in v1.
10. Only the requesting Agent is restarted automatically.
11. Other Agents adopt later through their own operator-controlled restart.
12. One automatic rollback attempt is mandatory after post-selection failure.
13. Full certification remains a separate release workflow.

## 28. Recommended delivery order relative to current HER repairs

The current conversation-continuity/commentary work and native TaskFrame
planning repairs remain the immediate behavioral fixes. Deliver in this order:

```text
1. protect current parallel HASHI changes
2. recover/integrate the certified HER source line
3. add /rebuild controller and transactional adoption
4. use that development path while implementing the native planning fixes
5. run focused rebuild canaries rapidly
6. once behavior is correct, produce the separate fully certified release
```

This prevents the rebuild feature from being confused with the behavioral fix,
while making every later Rust repair materially faster to compile, adopt and
verify.

## 29. Implementation and verification record

Completed on 2026-08-16:

- rebased the isolated branch onto Zelda's final `.22` HASHI certification;
- imported the exact certified Rust source under `native/her`, retaining the
  MIT licence and explicit upstream/bundle provenance;
- excluded upstream interactive session, sandbox and agent-workflow state that
  is not build source;
- added the dedicated incremental `hashi-dev` Cargo profile;
- registered authorized `/rebuild` and `/rebuild status` through the shared
  dynamic command registry;
- added `scripts/her_rebuild_dev.py` as the build-and-verify-only offline entry
  using the same durable manager/controller path without selection or restart;
- added a kernel-owned manager that survives its own targeted Agent reboot;
- implemented source/toolchain fingerprinting, same-fingerprint join, OS build
  lock, Cargo PID tracking, bounded logs and credential-free environment;
- implemented offline version, target, doctor, stdin and stream-json checks;
- implemented immutable candidate storage and explicit
  `development-source-build` Adapter resolution;
- implemented active-run quiescence, deferred activation, targeted existing
  hot restart, adoption identity/health checks, one automatic rollback and
  cold-start transaction reconciliation;
- retained idempotent terminal notification state and startup retry;
- left the certified `.22` manifest, binaries and certification evidence
  byte-for-byte unchanged.

Recorded host evidence:

```text
First clean development build: 71.734 seconds
Unchanged candidate reuse:       0.398 seconds
Candidate:                       dev-a63e2ab0cf29425c-85e5e5a1b647
Candidate SHA-256:               85e5e5a1b647ca28eb24b2d09de741202e8fa198e69fbc455a983cc0e26be033
Embedded HASHI source commit:    c20cd08da2200b28e25b38c452e797688092af7a
Offline quick verification:      passed
Development resolver canary:     passed
Atomic rollback to packaged .22: passed
Rust CLI tests:                  358 passed
HASHI Python tests:              2237 passed, 3 skipped
```

The first controller canary deliberately targeted a nonexistent Agent and
ended as `activation_deferred`. This proves that a verified candidate is
retained without writing the active selection or interrupting any real Agent.
The resolver drill then selected that immutable candidate, verified its
development identity, atomically restored the prior selection and proved the
Adapter returned to certified packaged `.22`.

Production release promotion remains outside `/rebuild`: a development
candidate never edits `hashi_assets/her/manifest.json`, a release manifest,
certification evidence or certified binary.

### 29.1 Primary-branch live adoption

The completed feature was integrated into `agent/latest-hashi-her` after
Zelda's `.22` work. Commit `2175e0bb` added the first-hot-reload bridge for an
already-running pre-feature kernel: a missing stable rebuild manager is
constructed transactionally during `/reboot`, while an existing manager is
preserved across the targeted restart that it supervises. A live non-HER Agent
is rejected before Cargo starts, with an instruction to switch that Agent to
HER; the offline build-only target remains supported.

The live Sunny canary then passed on the primary branch:

```text
Initial /reboot min:              Sunny offline -> online in about 5 seconds
Job:                              rebuild-20260816-090741-d3151617
Fingerprint:                      0ee10120fd7564e02d22f856b4a868e7ac014b9791dd60eb16f34051c195e7e4
Candidate:                        dev-0ee10120fd7564e0-61c151ebf1ec
Candidate SHA-256:                61c151ebf1eca6b1ad46739f3ad1f78dad616cc45451d53ae324b5c29a6a17d1
Embedded HASHI source commit:     2175e0bb34d9c46b73aeee6157624587efef0ad4
Cargo build time:                 65.171 seconds
End-to-end first adoption:        81 seconds
Quick checks:                     version, doctor, CLI/stdin, stream-json
Final state:                      succeeded / adopted
Terminal notification:            delivered
```

An immediate second `/rebuild` used the same fingerprint and immutable
candidate. It recorded `candidate_reused=true`, launched no Cargo process and
completed verification, targeted restart and adoption in 15 seconds. The
stored and independently recalculated binary digests matched exactly. The
certified `.22` release artefacts remained unchanged; this active selection is
explicitly labelled development and non-certified.

After primary integration and the source-scoped cache correction, the final
HASHI Python suite completed with `2279 passed, 2 skipped, 23 warnings`; the warnings are
the existing python-telegram-bot `retry_after` deprecations.

### 29.2 Source-scoped cache identity correction

Final acceptance review found that fingerprint schema v1 included the whole
HASHI repository `HEAD`. That was safe but overly broad: a documentation-only
commit could create a new fingerprint even though every Cargo input was
unchanged. Fingerprint schema v2 now uses the latest commit affecting
`native/her` plus dirty state restricted to that subtree. File-content hashing,
Cargo lock/manifest inputs, target, profile, features and toolchain identity
remain mandatory inputs.

Rust build provenance uses the same source-scoped revision. Runtime provenance
uses the `native/her` revision when the checkout contains integrated HER, and
falls back to the repository `HEAD` for ordinary standalone Git workspaces.
This keeps `workspace_match` meaningful without recompiling HER after unrelated
HASHI Python or documentation commits.

The source-scoped live canary passed:

```text
Job:                              rebuild-20260816-092112-7e7199c5
Fingerprint:                      7bde32d30a79ffa15e697ea58a2f6f5013bbb507b3f3d67477600c05b8420982
Candidate:                        dev-7bde32d30a79ffa1-92eb5de29ce9
Candidate SHA-256:                92eb5de29ce95d5c3b5754b962deafb3961b6fb53a784495effa556bc7e01722
HER source revision:              ccf3f669b11e049a17186eaba1bbc02d393a683f
Incremental Cargo build:          9.534 seconds
End-to-end adoption:              24 seconds
Final state:                      succeeded / adopted / terminal delivered
Runtime source workspace match:   true
```

After committing only this documentation, job
`rebuild-20260816-092231-aca5cfab` retained the exact same fingerprint and
candidate, recorded `candidate_reused=true`, launched no Cargo build, and
completed the full verify/restart/adopt transaction successfully in 15
seconds. This is the live acceptance proof for the source-scoped cache rule.
