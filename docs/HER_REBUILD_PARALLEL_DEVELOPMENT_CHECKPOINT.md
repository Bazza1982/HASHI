# HER `/rebuild` Parallel Development Checkpoint

Date: 2026-08-16

Branch: `feature/her-rebuild-command`

Base commit: `7b901be6b69dd02a8ee48dfbee33d02610cb94f3`

Status: independent controller foundation implemented; live command and runtime
adoption intentionally not connected

## Why this checkpoint is isolated

The primary HASHI worktree is actively being edited on
`agent/latest-hashi-her`. Its uncommitted changes overlap the HER adapter and
Tool Gateway. This branch was therefore created in a separate Git worktree from
the last committed HASHI HEAD.

No file in the primary worktree was edited, staged, reset or switched while
creating this checkpoint.

## Implemented in this branch

### Pure rebuild controller

`orchestrator/her_rebuild.py` now provides independently testable primitives
for:

- canonical `native/her` source discovery;
- source/license/provenance/Cargo profile preflight;
- supported host target detection;
- deterministic source, toolchain, target, profile and feature fingerprinting;
- source-change rejection before and after Cargo execution;
- minimal allowlisted Cargo environment construction;
- argument-array Cargo invocation with `--locked`;
- incremental isolated target-directory selection;
- build timeout, cancellation and process-group termination;
- bounded local build logs and redacted actionable diagnostics;
- immutable candidate staging with binary digest verification;
- candidate-local build and quick-verification evidence;
- explicit development/non-certified identity;
- atomic development selection and validated rollback selection.

The controller does not alter the certified HER package, manifest or
certification baseline.

### Durable state and locking

`orchestrator/her_rebuild_manager.py` now provides foundations for:

- the explicit rebuild state machine;
- validated one-way transitions;
- atomic durable job records and a latest-job pointer;
- same-fingerprint join and different-fingerprint rejection;
- per-requester origin and idempotent terminal-notification state;
- interrupted pre-activation recovery;
- conservative interrupted-rollback handling;
- cross-process build locking;
- stale Cargo PID protection and lock metadata.

This module deliberately does not yet call `RebootManager`, an Agent runtime or
a delivery transport.

### Tests

The first checkpoint covers:

- source/profile/host preflight;
- deterministic and change-sensitive fingerprinting;
- target/cache exclusions;
- toolchain/profile/feature identity;
- toolchain discovery and missing-toolchain failure;
- secret-free Cargo environment;
- argument-array build contract;
- compiler diagnostic redaction;
- successful, failed and timed-out fake Cargo builds;
- source mutation during build;
- immutable candidate evidence and tamper rejection;
- failed verification rejection;
- atomic selection and rollback;
- full state-machine persistence;
- join/deduplication semantics;
- per-requester terminal delivery;
- interrupted job recovery;
- build-lock exclusion and stale Cargo handling.

Run:

```text
python -m pytest -q tests/test_her_rebuild.py tests/test_her_rebuild_manager.py
python -m ruff check orchestrator/her_rebuild.py orchestrator/her_rebuild_manager.py tests/test_her_rebuild.py tests/test_her_rebuild_manager.py
git diff --check
```

Validation recorded for this checkpoint:

- rebuild-focused suite: 35 passed;
- rebuild/reboot/adapter/certification expansion: 153 passed;
- full HASHI run from this pre-Zelda base: 2212 passed, 3 skipped and 10 failed;
- four of those failures were reproduced as isolated-worktree environment
  effects (the tracked `.20` binary lacks an executable Git mode and the
  worktree has no local `.venv`); targeted reruns recovered them after temporary
  local test setup;
- the remaining six failures are existing `.20` native-contract gaps in provider
  error/stream failure and image-result handling, which are part of Zelda's
  in-progress `.22` work and do not import or execute the new rebuild modules.

No temporary executable-mode or `.venv` setup was retained in this worktree.

## Intentionally deferred until Zelda's current work is committed

Do not connect this branch to the live runtime until it is rebased onto Zelda's
final HASHI and HER commits.

The deferred integration set includes:

- importing the finalized HER source under `native/her`;
- adding `[profile.hashi-dev]` to that integrated Rust workspace;
- real HER quick-verification probes;
- `adapters/her.py` development-candidate resolution;
- `HERRebuildManager` kernel ownership and live asyncio coordination;
- command authorization and `/rebuild`, `/rebuild status`, `/rebuild help`;
- command registry/menu/help/audit wiring;
- Agent active-run quiescence;
- targeted existing `/reboot min` adoption;
- post-adoption identity/session/Tool Gateway checks;
- automatic rollback restart;
- durable Telegram/HChat terminal delivery;
- real warm Cargo timing and canary evidence.

Likely overlap files include:

```text
adapters/her.py
orchestrator/manager_registry.py
orchestrator/reboot_manager.py
orchestrator/command_specs.py
orchestrator/command_registry.py
main.py
```

Any Tool Gateway file currently modified in Zelda's primary worktree must be
treated as owned by that work until her commit is complete.

## Safe continuation procedure

1. Let Zelda finish and commit the current HASHI and HER `.22` work.
2. Record the final HASHI commit and final native HER source commit.
3. Rebase this branch onto the final HASHI commit.
4. Resolve overlap by preserving Zelda's behavior and adapting the independent
   controller interface, not by replacing her files wholesale.
5. Import the exact finalized HER source with license and provenance.
6. Add the dedicated Cargo profile and run a real controller-only build.
7. Implement quick verification before adapter selection.
8. Connect selection, command, quiescence, existing reboot and rollback in that
   order.
9. Run focused tests, the HASHI regression suite and a designated development
   Agent canary.
10. Keep production certification and package promotion separate from
    `/rebuild`.

## Current safety statement

At this checkpoint:

- `/rebuild` is not registered or user-visible;
- no live Agent can invoke it;
- no Rust build has been launched against Zelda's source;
- no HER binary or selection record has been changed;
- no Agent has been rebooted;
- Aptenra has not been modified by this branch.
