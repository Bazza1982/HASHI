# Core–Functions Runtime Decoupling: Acceptance / Rollback Matrix

Status: mandatory engineering acceptance plan. No gates in this document are currently represented as passed merely by writing this file.

## Acceptance policy

Priority: do not break a currently working function. Backward compatibility for arbitrary older distributed versions is NOT a requirement; operational continuity of the one known-good installed instance, durable identities and explicit rollback IS required.

Each phase must produce: exact commit, CR_ID/FR_ID/FG_ID, OS and Python version, executable/prefix, artifact hashes, deterministic and native test command+JUnit receipt, baseline comparison, test skips with reasons, negative tests, and observed physical side effects. Keep real data, keys and host-specific paths out of Git.

No change to the running Core/Functions/Worker, registry packages, OS services, installed Python, user credentials, production instance configuration or active Session is allowed during source-only work. Failures do not authorize a bypass or automatic deployment.

## Gate 0 — Reference baseline and isolation inventory

Required pass:
- Identify nine protected Core files and prove their import closure is stdlib-only from a clean process.
- Record current Core/Shared/Agent interpreter executable and environment prefix, full exact dependency baseline and working Function code generation, source SHA, version/API/protocol and health.
- Capture baseline functional checks and pre-existing failures; preserve original reports. Do not make a new pass by deselecting a previous passing test.
- Inventory all consumers of aiohttp, certifi, multidict, yarl, Pillow and optional native extensions. Distinguish imports in Core host from packages merely installed in its current shared environment.
- Enumerate current install/Portable/npm/Helm/Windows launcher owners and L1/L2 independent sealed runtimes.

## Gate 1 — Strict role and manifest contracts (synthetic)

Positive:
- CR with no third-party installed packages starts minimal protected Core successfully.
- FR with its own approved Python prefix and EXACT standard package lock passes admission even though executable/prefix/digest differ from CR.
- IPC contract, API/worker schema, generation digest and owner identity remain strict; the original candidate manifest/import guard still rejects untracked modules.
- A new FR identity does not change Core source digest, product Core major, Core runtime identity or Core PID.

Negative (each must reject BEFORE READY without accepting effects):
- Missing/changed runtime manifest; missing or wrong executable; mismatched file hash or lock.
- Wrong ABI/architecture, unsupported Python, missing package, changed exact required package version, unexpected user/system site-package bleed.
- Wrong source generation digest; incorrect wire schema/API/feature; stale parent nonce or reply from wrong PID; untrusted path/Agent-supplied executable override.
- Attempt to import third-party function packages directly into minimal CR; attempt to import mutable or unlisted project modules.
- Simulated dependency import error cannot fall back to a Core-installed package or install dependencies automatically.

## Gate 2 — Cross-interpreter process / IPC / lifecycle

All on real supported native Windows and on Linux if support is claimed:
- Separate Core/FR venv paths; distinct sys.executable/sys.prefix observed inside both.
- Core launches Shared Functions and multiple Agent Workers in verified FR, seals code artifacts and handles command round-trip and unsolicited event with original JSON frame/version bound.
- Parallel launch of multiple agents and candidate probe, clean bootstrap error, child startup timeout, quiesce, prepare, commit, abort, restart, kill during activation, parent crash and child crash.
- Windows: no leaked HANDLEs/inheritable credentials, Job object descendants terminated or preserved only when owned by confirmed recovery, no private named pipe exposed to unrelated user, no hidden extra child. POSIX: pass_fds and close_fds are constrained; correct process-group and zombie cleanup.
- Verify current parent-child cancellation, request IDs, at-most-once physical tool dispatch, immutable external protocol payloads, no pickled Python objects and no loss of health/timeout metadata.
- Interactive TUI terminal, logs and stdout/stderr routing work as previously on Windows. Force interruption in the middle of a candidate launch must leave old route usable.

## Gate 3 — Business functionality parity using ORIGINAL approved dependencies

This is the mandatory mid-migration hold point. Do not introduce #137 dependency upgrades until every previously working path below passes under the split CR+FR model.

| Capability | Required tests / acceptance |
| --- | --- |
| Core lifecycle | instance lock, boot/cold startup, /version identity, independent worker READY, /stop, /reboot min, per-Agent restart, /reboot max and failed candidate abort |
| Persistent Conversations | /new, current primary Session, history restoration, PCM injection and compaction, cross-Session controls, durable message status and idempotent delivery |
| Model providers | HERV3 / selected API/CLI engines, Ollama local, tool loops, result paging, provider fallback, checkpoint resume, recorded usage; no model/tool replay |
| Tools and Workzones | file read/write/search, patch, HTTP tool, Script/Process where qualified, session-scoped Workzones; correct denied/unavailable paths |
| L0/L1/L2 | L0 unaffected, L1 approve-once/deny/duration/task, exact pending operation resumed once, token expiry and revocation; L2 remains bounded by native Windows WAC evidence; never weaken Safety/Runner/LPAC |
| Frontends | Telegram inbound/outbound and offsets, Workbench agent list/config/chat/history/cached Session, TUI and supported Frontend Connector APIs |
| Phone, speech and media | /phone and /call, audio/message presentations, STT/TTS, speech streaming/cleanup, existing media and asset ownership; preserve original release baseline |
| Scheduler and tasks | cron executions, pause/cancel, handoff and wake-up, preserved schedules, task receipts and timeout handling |
| Cross-agent operations | Agent Move and copy/return receipts, Workbench Agent management, Fingerprint audit, HChat, Compute inference-only call path |
| Security and release | private paths, credentials not exposed, canonical audit, release/portable manifests, version reporting, process provenance, role-specific dependency and wheel lock checks |

Use existing test selectors FIRST: tests/test_runtime_contract.py, tests/test_minimal_core.py, tests/test_kernel_process.py, tests/test_function_generation.py, tests/test_function_worker_supervisor.py, tests/test_runtime_handoff.py, tests/test_runtime_release.py, tests/test_version_info.py, tests/test_dependency_profiles.py, tests/test_portable_windows_packaging.py, permission and Fingerprint workflows, Phone/Voice/Workbench selectors, and the curated bare pytest. Expand based on files touched and observed runtime imports. Command names represent initial selectors, not permission to skip broader integration.

Performance: record cold Core startup, candidate probe, Shared READY, Agent READY, RAM/CPU and normal request latency. Do not impose arbitrary throttling or safety delays to get a clean status; investigate meaningful regressions against the real baseline.

## Gate 4 — Independent Function dependency security maintenance

- Build a new immutable FR environment with only #137's reviewed five version changes and the required resolved compatible closure. No CR packages change.
- Verify wheel SHA256, reproducible frozen lock, no unpinned install, runtime source/protocol identity, native ABI and target platform.
- The prior aiohttp secure-cookie and multidict reference-lifetime red/green regressions work against the respective old/new environment; image and certifi coverage use real compatible packages without opening uncontrolled external connections.
- Re-run Gate 3 targeted paths and startup/health. Only the FR_ID and derived Function lock digest should change, not Core PID/CR_ID/CORE_SOURCE_PATHS.
- Prove a SECOND FR-only update/downgrade in an isolated instance while Core stays running; failure must reject the candidate and leave the old FR healthy.
- No existing sidecar or L2 sealed runtime is silently altered or granted new permissions.

## Gate 5 — Packaging, CI and independent Core major review

- Windows Portable installs a separately verified Core runtime and Function runtime; offline install and reset do not reuse ambient Python packages or mutate an active interpreter.
- CI on Windows + Linux validates trusted launcher, role locks, strict handshake, import boundaries, multi-worker handoff, contract compatibility, bad package/path/nonce rejection, failed migration rollback, resource cleanup, and enterprise/npm/Helm version projections.
- New independent protected Core review record matches exact candidate Core digest and migration major, GitHub core-change-approved label and its required CI enforcement exist. Product metadata is consistent; no duplicate 5.0.0a1 release is published.
- GitHub PR/head fully integrated with main; all required checks pass. Any existing broad-suite failure has source/baseline evidence and explicit release adjudication, never false green.
- Hash/dependency/version metadata are observed from actual tested artifacts, not manually fabricated receipt values.

## Gate 6 — Native isolated instance / user-approval release

- Build from exact final source and exact role locks, using a separate test instance with no live-user task ingress, and verify startup, external API/Workbench/Telegram connector health with safely controlled test input.
- Explicitly approve any test that touches real external accounts, model endpoints or mutable state. Real provider use and complete L2/WAC do not follow from synthetic tests.
- Before cutover, preserve a complete verified prior release with CR, FR and Function code, database version, worker bindings, approvals/leases and rollback plan. Verify new runtime can read current state without destructive schema changes.
- First controlled Core major cutover requires separate operator approval: freeze intake and settle or account for active tools; transfer only supported durable state, revalidate owner and idempotency; start new CR+FR; reject partial readiness; only commit routing on healthy acceptance.
- Failure before commit: restore old complete release pair, not a mix; preserve same operation IDs, original receipts and message offsets. Failure after effects: reconcile and seek approval rather than execute again.
- Run a live FR-only patch qualification with old Core PID unchanged after the major cutover (in a test instance first). That successful proof, not the design document, is the final architecture acceptance.
- Old artifact garbage collection is separately authorised after zero owned processes/handles, verified credentials and database safety.

## Stop / HOLD conditions

Any of the following blocks adoption and release promotion, irrespective of aggregate green tests:
- Core imports app third-party libraries in the new minimal host; candidate can borrow packages from old Core; CR and FR runtime identities are conflated.
- A change in FR lock forces Core code-major increment or host restart; a new FR fails without clear typed error and automatic fallback attempts to install packages.
- Missing identity/nonce/manifest validation or a malformed child response accepted as READY.
- Workbench/Telegram/Phone/Scheduler/Worker route regression; lost in-flight state, duplicate tool side effect, leaked handle/process, private credential or audit receipt.
- Permission Safety/Runner/LPAC isolation changed without separately passing its WAC; Core admin/auth boundary expanded.
- An in-place dependency update, forced switchover, rollback that reactivates revoked access, or a claim that a source merge equals deployment.
- CI is green but source, dependency or installed runtime does not match the tested candidate.

## Deliverable reports

Report three separate outcomes:
1. **Source implementation** — branch/PR/HEAD, files, functionality parity and negative tests.
2. **Runtime qualification** — exact CR_ID, FR_ID, FG_ID, real OS tested, immutable artifacts and rollback proof.
3. **Production adoption** — explicit user authority, exact instance, time, actual PID/health/receipts and rollback status; leave as NOT STARTED until authorised.

These outcomes are not interchangeable. Do not claim complete migration until the same Core-host code remains running through an independently adopted FR security update.
