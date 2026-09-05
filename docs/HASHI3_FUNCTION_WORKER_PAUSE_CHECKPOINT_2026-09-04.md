# HASHI3 Per-Agent Function Worker Pause Checkpoint — 2026-09-04

Status: **historical pause snapshot; superseded by the 2026-09-05 HASHI3 live closeout**

The project resumed and completed its authorized implementation and
non-disruptive live gates on 2026-09-05. The current status, exact receipts,
installed Windows components, and still-open operator-dependent canaries are
recorded in
[`HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md`](HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md).
All live-state and remaining-work statements below are preserved as the
pause-time record and are no longer current.

This checkpoint is the continuation authority for the HASHI3 per-Agent
Function Worker migration. It records what exists in the checkout, what was
actually verified, what the still-running process has loaded, and the exact
work that remains. It is not a release or promotion approval.

The normative target remains
[`HASHI_PYTHON_RUNTIME_COMPATIBILITY.md`](HASHI_PYTHON_RUNTIME_COMPATIBILITY.md),
with process ownership in
[`HASHI_SLIM_CORE_ARCHITECTURE.md`](HASHI_SLIM_CORE_ARCHITECTURE.md). Present-tense
architecture statements in those documents describe the candidate contract;
they do not supersede the live-status boundary recorded here.

## Repository checkpoint

- Repository: `C:\Users\thene\projects\HASHI3`
- Branch: `main`
- HEAD: `02f6af378f0f` (`feat(runtime): establish transactional Python core contract`)
- Remote relation at pause: `main` is 29 commits ahead of `origin/main` and 0 behind.
- Pre-migration recovery branch:
  `backup/hashi3-pre-runtime-contract-20260903-2245` at
  `ba99e1b62ac23392205e1654ab3893c9c96e9a5b`.
- The Phase 1 runtime contract is committed at HEAD. The per-Agent Worker
  migration is intentionally still an unstaged, uncommitted worktree.
- At the 08:35 pause snapshot, after adding this checkpoint, the worktree
  contained 54 tracked changed paths
  (including retirement of two files) and 13 untracked paths. Nothing was
  stashed, reset, cleaned, committed, or pushed during pause closeout.

Do not run `git reset`, `git clean`, checkout-overwrite, rebase, or a session
reset when resuming. Begin with `git status --short --branch` and review this
checkpoint before touching the worktree.

## Candidate implementation preserved in the worktree

The source candidate currently includes:

- Core API 2, Function API 2, worker model `per-agent-process`, Worker protocol
  1, and generation schema 2 in `pyproject.toml` and runtime fingerprints;
- immutable content-addressed Function artifacts and source revalidation;
- a strict, size-bounded UTF-8 JSON duplex protocol with no pickle or shared
  Python objects;
- a real Function Worker bootstrap/host plus Core capability facades;
- stable `AgentRuntimeHandle` routing, route gates, in-flight draining,
  multi-Agent pointer publication, and same-artifact crash recovery;
- Core-owned Telegram polling and offset handling, with Agent handlers and
  outbound behavior inside the Worker;
- lifecycle, service, Workbench, API Gateway, scheduler, background-job,
  command, HChat, voice, and adapter call sites moved toward the stable handle;
- transactional `/reboot` preparation, activation, rollback, and retirement;
- retirement of `orchestrator/hot_reload.py` and
  `tests/test_hot_reload.py`; and
- replacement contract, generation, protocol, supervisor, ingress, lifecycle,
  reboot, health, and service assertions.

New candidate paths that must remain present are:

```text
orchestrator/file_permissions.py
orchestrator/function_contract.py
orchestrator/function_worker_bootstrap.py
orchestrator/function_worker_host.py
orchestrator/function_worker_protocol.py
orchestrator/function_worker_supervisor.py
orchestrator/telegram_ingress.py
scripts/check_function_worker_runtime.py
tests/test_function_contract.py
tests/test_function_worker_protocol.py
tests/test_function_worker_supervisor.py
tests/test_telegram_ingress.py
```

## Verification completed at pause

The checkout used CPython 3.12.13 from `.venv-wsl`.

Focused runtime/Worker boundary:

```bash
.venv-wsl/bin/python -m pytest -q \
  tests/test_runtime_contract.py \
  tests/test_function_contract.py \
  tests/test_function_generation.py \
  tests/test_function_worker_protocol.py \
  tests/test_function_worker_supervisor.py \
  tests/test_telegram_ingress.py \
  tests/test_reboot_manager.py
```

Result: **89 passed in 38.49 seconds**.

Curated Core gate:

```bash
.venv-wsl/bin/python -m pytest -q
```

Result: **436 passed in 92.65 seconds**.

Changed tracked and untracked Python paths both pass Ruff when checked with the
available user-level Ruff binary. `git diff --check` also passes. The project
virtual environment does not currently contain the `ruff` module; the
user-level binary was used. A repository-wide Ruff invocation is not a clean
baseline and reports existing errors in untouched paths, so no full-tree Ruff
claim is made.

## Live HASHI3 state deliberately left running

The existing HASHI3 process was not stopped or replaced during this closeout:

- tmux session: `hashi3-runtime-contract`
- Core PID: `357422`
- process start: 2026-09-04 01:42:40 AEST
- Workbench: healthy on port 18804
- API Gateway: healthy and accepting requests on port 18805
- selected Agent: `agent1`, in expected local mode because the pilot uses a
  placeholder Telegram token
- active generation:
  `sha256:9ce3dde8bbed73abbf87f95658a5aeb54fabd5c224caa14a5a8df3d9e005697e`

This running process loaded the earlier Phase 1 contract:

```text
core_api=1
function_api=1
generation module_count=301
```

The current checkout declares Core API 2, Function API 2, Worker protocol 1,
and the per-Agent process model. That difference is intentional: **the Phase 2
source candidate has not been cold-started into the live Core**. No live Worker
PID, live targeted Worker cutover, or live Worker recovery result may be
claimed yet. An ordinary `/reboot` is not the adoption mechanism for these Core
and protocol changes; the already-authorized controlled HASHI3 cold restart is
still required.

HASHI1 and HASHI2 were not changed or restarted.

## Explicitly incomplete

The following remain acceptance work, not implied successes:

1. Independently review the preserved diff for Core/Function ownership leaks,
   synchronous compatibility shims, and missing RPC consumers.
2. Run `scripts/check_function_worker_runtime.py` to prove one real configured
   candidate reaches READY without taking traffic.
3. Perform the controlled HASHI3 cold start from the current source and confirm
   health reports Core API 2, Function API 2, Worker protocol 1, and a live
   per-Agent Worker PID.
4. Establish a two-Agent HASHI3 acceptance configuration and prove
   `/reboot min` changes only the selected Worker while Core, the other Worker,
   shared services, and active work remain stable.
5. Exercise repeated target cutovers, broad all-or-none publication, candidate
   import/construction/health failure, drain timeout, source mutation, active
   Worker crash recovery, and recovery exhaustion.
6. Validate Telegram ingress/offset behavior with an authorized real test bot,
   plus Workbench, API Gateway, Scheduler, background jobs, HChat, voice/media,
   and representative backend execution across the process boundary.
7. Run the explicit offline product suite because this is a broad refactor and
   changes `pyproject.toml`. Separately run only the contract/platform/live
   scopes required by the affected surfaces and available authorization.
8. Reconcile all documentation against observed live receipts, then decide on
   a reviewable commit and push. Do not promote anything to HASHI1 or HASHI2
   before this gate is complete.
9. Fix Workbench endpoint handling at the mechanism level on HASHI3. Runtime,
   health, routing, recovery, and Worker-facing paths must resolve the
   Workbench host/address and port from the canonical live instance
   configuration or discovery contract; they must not embed a particular
   Workbench address or port. Add assertions that exercise non-default and
   changed endpoints and reject cross-instance routing. This is deliberately
   deferred until the larger HASHI3 project resumes; do not implement it as a
   pause-only patch, and do not apply it to HASHI1.
10. Implement the accepted cross-platform device-control design in
    [`HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md`](HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md).
    Browser Bridge and `/usecomputer` keep separate Worker processes and
    privilege boundaries but share Core-level discovery, authorization, audit,
    task context, and lease/handoff semantics. The mechanism must work with
    HASHI Core running in WSL or native Windows, dynamically discover actual
    endpoints, reject cross-instance routes, and use persistent windowless
    background workers. Routine control actions must not spawn or flash a
    PowerShell, Command Prompt, Windows Terminal, Python, uv, or npm console.
    This is deferred HASHI3 work; it is not permission to patch HASHI1/HASHI2.

No real PostgreSQL, external-provider, Windows-native, or production Telegram
canary was run for this Phase 2 candidate.

## Safe resume sequence

1. Read this file and inspect `git status --short --branch`; preserve every
   existing tracked and untracked path.
2. Confirm PID 357422 and both HASHI3 health endpoints, or record truthfully if
   the external state changed while paused.
3. Review the candidate diff and rerun the focused 89-test boundary before any
   live adoption if the checkout changed.
4. Run the READY-only Worker probe. Fix and re-run offline checks without
   touching HASHI1/HASHI2.
5. Announce the controlled HASHI3 cold-start window, capture the old PID and
   health, then replace only HASHI3.
6. Continue through the live and multi-Agent failure matrix above. Update this
   checkpoint with receipts rather than overwriting pending items with design
   assumptions.
7. Include the deferred Workbench endpoint mechanism review and its new
   configuration/discovery assertions in the resumed HASHI3 work. Do not use a
   fixed address or port as the repair.
8. Continue with the linked cross-platform device-control plan: establish the
   shared capability/lease contracts first, then the persistent silent Windows
   worker and separate Browser Worker, and finally the WSL/native-Windows,
   no-popup, isolation, and handoff acceptance matrix.

Until those steps pass, the accurate summary is: **the per-Agent Function
Worker candidate is offline-green at its focused and Core gates, safely
preserved, and not yet live-adopted or promotion-ready**.
