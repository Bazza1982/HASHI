# Core–Functions Runtime Decoupling: Implementation Plan

Status: engineering instructions, NOT completed implementation or live adoption.
Start from the architecture decision in docs/CORE_RUNTIME_DECOUPLING_ARCHITECTURE_20261105.md and current main. Keep PR #137 unchanged until its dependency evidence can be selectively reused in the new Functions lock.

## A. Snapshot and ownership inventory

- Obtain a source and runtime baseline from the current working instance without restarting it: source SHA, product/Core API/Function API/Worker protocol, Core and child PIDs, Python executable and sys.prefix in every process, installed dependencies, current lock and function-generation IDs, health, existing sidecars and ignored test records. Never commit host-specific secrets, paths, real task payloads or credentials.
- Enumerate direct and transitive imports of the nine protected CORE_SOURCE_PATHS and main.py. Prove Core uses only standard library via actual isolated-interpreter import/boot test; document any violation rather than assuming no dependencies.
- For all third-party packages, map owner (Core-host / common Functions / existing specialised sidecar), executable importers, native ABI requirements, supported OS and required behavioural tests. Begin with all existing Function dependencies; move optional packages out only when observed imports and tests support that change.
- Run the current baseline regression matrix before edits. Preserve and classify pre-existing failures. Never weaken a previously passing test.

## B. Implement separate strict runtime identities

Modify protected orchestrator/runtime_contract.py only in the user-authorized Core-major development branch, subject to independent review.

- Represent four distinguishable identities: Core code/protocol, Core-host interpreter/ABI/stdlib/runtime policy, Functions interpreter/site-packages/exact package lock and hashes, and Function source-generation artifact digest.
- A Core-host profile with ZERO third-party dependencies is valid and intentionally verified. The Functions profile must NEVER be allowed to omit validation of its exact lock.
- Keep exact identity comparison within each role, but replace the across-role comparison with an explicit protocol/API/schema/capability and trusted-role compatibility check. Across role boundaries do NOT demand identical sys.executable, sys.prefix, entire dependency digest or runtime-policy digest.
- Bind the candidate child to a trusted FR runtime manifest provided by release/installation owner, not user input, Agent config or prompt. Verify actual executable, local package versions, hashes, OS/ABI and chosen source generation before READY. A missing/fake/stale runtime receipt stops the candidate without touching active routes.
- Preserve existing JSON IPC envelope, frame-size and replay semantics. Avoid changing Worker protocol version if its wire contract is unchanged.
- Prevent accidental Function module or dependency imports into the Core process. Audit source and actual loaded modules.

Source owners: orchestrator/runtime_contract.py; main.py; orchestrator/kernel_artifact.py only when needed; orchestrator/function_worker_bootstrap.py; pyproject.toml role policy and runtime-entry.json. Test owners: test_runtime_contract.py, test_minimal_core.py, test_kernel_process.py, test_check_protected_core_changes.py.

## C. Build cross-interpreter launch adapter BEFORE selecting a new Functions env

The current coupled entrypoints are:
- orchestrator/kernel_process.py: ProcessClient.prepare currently uses multiprocessing spawn from Core.
- orchestrator/kernel_process.py: qualify uses sys.executable.
- orchestrator/function_worker_supervisor.py: prepare_worker uses multiprocessing spawn from its current interpreter.
- orchestrator/function_generation.py: run_candidate_probe uses sys.executable.
- orchestrator/function_worker_bootstrap.py contains the current protected child import boundary.

Implement ONE trusted launch adapter and use it for shared Functions, per-Agent Workers, candidate probes and recovery workers. Do not only change one launcher.

Recommended implementation: start a known absolute, hash-qualified FR Python by subprocess.Popen, use explicitly inherited private pipe/handle and retain the existing JsonConnectionPeer protocol. Do not globally call multiprocessing.set_executable while unrelated Workers can start; it controls a process-global spawn executable.

Windows: close_fds=True; STARTUPINFOEX handle_list includes only the intended IPC handle, use a short process-start lock while any handle is inheritable, keep existing Job/descendant ownership and cleanup. POSIX: close_fds=True and pass_fds only for the owned channel, retain group cleanup. Reconstruct the existing Connection or a validated equivalent, not pickle or unrestricted localhost.

Child fixed bootstrap: run the exact protected script from its pinned path, under its verified FR executable, use a private inherited channel with a one-time nonce, observed PID, protocol version, FR fingerprint and sealed code-generation digest. Parent confirms OS PID, expected child, original executable, nonce and digest before READY. No credential or prompt body in argv/environment. A denied or dead candidate can never become an active route.

Provide a process wrapper preserving the existing is_alive/pid/exitcode/join/terminate/kill contracts, cancellation, request timeouts, failure receipts and Windows Job cleanup. The child must keep the immutable module finder and source import guard. Confirm -I invocation does not accidentally prevent legitimate protected entrypoint imports; never repair this with untrusted PYTHONPATH.

Prove on Windows and Linux with same CPython 3.12.13 base but two distinct virtual environments: READY, message round trip, cancellation, multi-worker simultaneous start, crash, forced stop, no orphaned children/handles, no unauthorized client, exact package/identity rejection. No live system modifications during these tests.

## D. Split environments using OLD known-good dependency versions first

Create immutable independently materialized runtimes, outside active source and production site-packages:
- Core-host runtime: CPython + stdlib/bootstrap, no default hashi-bridge app dependencies.
- Functions runtime: separate venv, the SAME currently approved application package versions as baseline, exact locked hash-verified artifacts.
- Optional sidecars and L2 Safety/Runner/LPAC remain separate and unchanged.

Add canonical role-aware manifests and build commands for CR and FR. Never install packages into live Core or active FR; always build a fresh candidate directory. No network fetch after qualification; no ambient system/user site-packages, editable-source fallback, or missing dependency silently obtained from Core.

Run the full Function parity suites WITHOUT adopting new aiohttp/Pillow etc yet. This first proves that splitting process environments did not break existing functionality. A failure here is an architecture/launcher issue, not a vendor package upgrade issue. Measure worker startup, FR probe, memory, steady-state latency against baseline and investigate material regressions.

## E. Adopt PR #137's tested security dependencies in FR only

On the successfully split architecture, create a NEW sealed FR generation and copy the verified version selection/probes selectively:
- aiohttp 3.14.4; certifi 2026.10.19; multidict 6.9.1; Pillow 13.0.0; yarl 1.25.1.
- Confirm import ownership and compatibility; if Pillow only belongs in media/sidecar for some targets, do not add it to CR.
- Regenerate the Portable derived requirements hash lock from new canonical role inputs rather than copying PR #137's old unified Core lock. Verify all platform-required wheels and hashes.
- Run the exact package security red/green checks and full Function tests. Confirm Core PID, Core interpreter, Core runtime fingerprint and protected source stay IDENTICAL while FR identity changes.
- Demonstrate same-Core second FR update and rollback without a Core major bump, without a Core restart and without modifying the running Core venv.

Do not cherry-pick PR #137's pyproject version bump, full unified lock or Core governance record into this new implementation unchanged; those belong to the old coupling. Coordinate its retirement/supersession to avoid double Core 5 release identity.

## F. Release, governance and packaging

Update exactly the existing authorities: runtime_contract, function-generation qualification, version/runtime metadata, source build, Windows Portable installer, current package metadata projections, role-specific locks and acceptance docs. Remove competing configuration owners rather than adding them.

Protect a new Core-major code change through one legitimate authorized Core migration, an independent same-tree review, digest and core-change-approved label before merging. Distinguish its protected source changes from later CR-only Python security patches and later FR-only dependency updates. Preserve API/protocol numbers when compatible.

A complete distributable release must identify immutable CR, FR, FG and sidecar manifests, role-specific wheel hashes, Windows/OS qualification and the prior known-good release reference. Installing/merging the source must not auto-switch active services, run pip in place or silently modify current state.

## G. Operator-approved, reversible adoption

Test the new complete release on a separate non-production instance with isolated state, using synthetic business input first. Then obtain separate explicit approval for real canary or a cold Core cutover.

First core-major cutover: quiesce intake, checkpoint pending operations and durable message offsets; record active leases/unknown side effects; preserve previous complete release and secrets/ACL state; start new Core/CR and separate FR; verify identical persisted Sessions and unique command/tool identities, approval/fence, Telegram/Workbench/Phone health, Scheduler and audit before committing routes.

If preparation fails, discard candidate and keep old release live. If the cutover fails before accepting new effects, restore the old COMPLETE CR/FR/code pair, never a mixed environment. If operations may have run, reconcile by operation_id and receipt rather than replay.

Every LATER FR-only update should be a bounded Function/Worker handoff leaving Core PID unchanged; keep old FR until all descendant processes are retired and cleanup confirmed. Only afterwards may an explicit garbage-collection step delete old environments.

## Technical source map

| Owner | Files to inspect/modify | Failure boundary |
| --- | --- | --- |
| Core policy | orchestrator/runtime_contract.py, main.py | Code/runtime identities and strict compatibility |
| Core spawn | orchestrator/kernel_process.py, orchestrator/function_worker_bootstrap.py | Exact independent FR executable, owned IPC and job cleanup |
| Sealed code | orchestrator/kernel_artifact.py | No unverified source imports or path escape |
| Function supervisor | orchestrator/function_worker_supervisor.py | Per-Agent Worker READY, stop, recover, route/cutover |
| Function staging | orchestrator/function_generation.py | Candidate probe in new FR, exact dependency receipts |
| Runtime metadata | orchestrator/runtime_fingerprint_cache.py, orchestrator/runtime_release.py | Correct distinct CR/FR/FG observed status |
| Packaging | pyproject.toml, runtime-entry.json, constraints/, packaging/portable_windows/, launchers | Reproducible role-specific builds and no in-place install |
| Direct consumers | HERV3, PAO, Telegram, Workbench/FC, Phone, Scheduler, Agent Move, Fingerprint, Windows L1/L2 | Behavioural parity, persistence and no unauthorized tool replay |

Acceptance and negative tests are in docs/CORE_RUNTIME_DECOUPLING_ACCEPTANCE_20261105.md. No implementation stage is accepted only because its code compiles or GitHub turns green.
