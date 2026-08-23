# HER v2 Execution-Limit Regression Cleanup Plan

| Field | Value |
|---|---|
| Status | Implemented and verified in source; runtime activation awaits separately authorised `/reboot` |
| Date | 2026-08-22 |
| Authority | `HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md` section 3.2.1 |
| Regression source | `839b10a5` (`Add tiered provider retry recovery to HER v2`) |
| Runtime activation | Requires a separately authorised `/reboot`; no restart is part of this plan |

> Scope note (2026-08-22): the later approved
> [Auto Compact design](HER_V2_AUTO_COMPACTION_DESIGN.md) authorises one narrow
> Tier 2/Tier 3 watchdog for an isolated, tool-free `CompactionRequest`. That future
> capacity-maintenance exception does not restore the generic provider retry
> tiers removed by this remediation and may not appear on `StageRequest`,
> Persona, learning, target providers, tools, or complete provider tool loops.

## 1. Objective

Remove the unauthorised HER v2 execution limits and their secondary failure
modes without adding another scheduler, budget system, permission reduction, or
global process manager.

The remediation must:

1. remove every elapsed provider-attempt deadline from every HER v2 path;
2. preserve typed, side-effect-aware fresh-connection recovery without using a
   clock to end an otherwise healthy attempt;
3. make foreground `bash` unbounded by default while preserving an explicitly
   requested per-invocation timeout;
4. guarantee that a foreground shell process tree is reaped on completion,
   explicit timeout, cancellation, `/stop`, `/steer`, runtime shutdown, and
   error unwinding;
5. preserve and display the primary provider failure, the recovery decision,
   and any applicable cleanup outcome as separate deterministic facts; and
6. retain broad Agent shell/write authority and all existing replay-safety
   protections.

## 2. Locked invariants

Implementation and tests must not weaken these rules:

- There is no turn-count, wall-clock, elapsed-time, stage, provider-attempt,
  tool-round, call/step, sub-agent-count, cumulative-token, or output-token
  execution ceiling in any HER v2 stage or adapter path.
- The existing authorised controls are limited to explicit stop/cancellation,
  the configured meaningful-progress idle detector, narrowly scoped transport
  inactivity/protocol guards, an explicitly requested single-tool timeout, the
  effort-defined Replanning and Review/remediation counts, the fixed high-risk
  10-result/300-second safe-boundary checkpoint scheduler, and one safe
  fresh-connection recovery after an eligible typed provider failure. The
  checkpoint cadence gates only continuing tool admission and never caps or
  cancels healthy work.
- Omitting a `bash` timeout means no timeout. No implicit `30`-second default or
  implicit `120`-second cap may be applied.
- `allow_side_effects=true` and the Agent's ordinary Tool Registry authority are
  unchanged.
- A recovery policy may decide whether a failed operation is replayable; it may
  not decide how long a healthy operation may run.
- Finalisation may phrase a failure but may not replace, hide, or downgrade
  Runtime's deterministic failure and cleanup facts.

## 3. Re-audit findings

| Area | Current defect | Required disposition |
|---|---|---|
| Recovery policy | `orchestrator/her_v2/retry.py` defines stage/context/local-provider tiers and `60/180`, `190/300`, `300/600` deadlines | Delete tiers, timeout maps, promotion logic, and deadline lookup; retain only the authorised recovery-count/failure policy that is actually needed |
| Stage request contract | `StageRequest` carries `retry_tier` and `attempt_timeout_s` | Delete both fields so adapters cannot receive a disguised HER execution clock |
| Main Runtime | `_invoke_stage` calculates a tier and `_await_provider_operation` wraps the complete `provider.invoke()` call in an absolute timeout | Await through `TurnControl` only; delete the outer deadline and its synthetic timeout classifier |
| Retry scheduling | Runtime rejects `Retry-After` values that exceed a fabricated recovery window | Remove the recovery window; keep cancellation and the authorised no-progress boundary |
| Ordinary provider adapter | `HashiStageProvider.invoke` rewrites backend `idle_timeout_sec` from the HER attempt deadline | Remove the override; retain only independently configured transport inactivity behaviour |
| Persona path | Persona packaging uses `asyncio.wait_for` with a tier timeout and injects that value into the backend | Remove both; preserve the same one-recovery and deterministic fallback behaviour |
| Learning path | Meditation/Dream maintenance uses `asyncio.wait_for`, tier labels, and recovery-window comparisons | Remove all elapsed windows and tier metadata while preserving typed one-recovery behaviour |
| Foreground shell | `execute_bash` silently defaults to 30 seconds, caps at 120 seconds, kills only the shell, does not await it after timeout, and has no cancellation cleanup | Make timeout optional; launch a dedicated process group; terminate and reap the exact group on every non-success exit |
| Tool schema/config | The schema advertises the wrong 30/120 contract and Tool Registry supplies a default cap | Advertise optional timeout semantics and apply a cap only when an operator explicitly configured one |
| Error chain | replay blocking replaces the primary provider code with `SIDE_EFFECT_REPLAY_BLOCKED`; the original code survives only in nested details | Keep the primary failure authoritative and expose replay blocking as a separate recovery decision |
| Backend result | adapter metadata exports only the outer terminal code/description | Export primary failure and recovery decision separately; do not require consumers to parse prose |
| Tests | deadline/tier assertions blessed the regression, while the named no-total-ceiling test ran for only about 0.2 seconds | Remove those assertions now; replace them only after the implementation is corrected with tests that cross the historical boundaries |

The existing API provider tool loops are awaited sequentially and already have
no loop-count ceiling. CLI adapters and `BackgroundJobManager` already use
dedicated process groups. They are reference behaviour, not targets for a broad
rewrite.

## 4. Minimal implementation sequence

Each phase is an independently reviewable commit. A later phase must not be
used to conceal a failing earlier one.

### Phase A — remove HER provider-attempt clocks

1. Reduce the provider recovery policy to the typed one-recovery rule; remove
   `RetryTier`, timeout tables, context-size promotion, local-provider
   promotion, and timeout lookup.
2. Remove `retry_tier` and `attempt_timeout_s` from `StageRequest` and every
   request/audit payload.
3. Replace Runtime's deadline wrapper with the existing cancellable await path.
   Delete `_provider_attempt_timeout_error`; provider adapters remain
   responsible for classifying genuine connection/read inactivity at the
   transport boundary.
4. Remove recovery-window arithmetic and the
   `retry_after_exceeds_recovery_window` outcome. A retry wait remains
   interruptible by stop and the configured meaningful-progress idle control.
5. Remove Persona and maintenance `wait_for` wrappers and their synthetic
   backend idle-timeout overrides. Keep their existing immutable-input,
   fresh-connection, and fallback rules.
6. Confirm that API HTTP client timeouts remain read/connect inactivity guards
   per network operation and never include foreground tool execution or a full
   tool/model loop.

### Phase B — make foreground shell lifetime exact and local

1. Parse `timeout` only when the tool call contains it. Validate it as a
   positive explicit value. Apply `timeout_max` only if that cap is explicitly
   present in tool configuration; remove the Registry's implicit `120`.
2. Start each foreground shell in its own session/process group and capture its
   exact PID/PGID immediately.
3. Keep process ownership inside `execute_bash`; do not introduce a global
   process registry. Because provider tool calls are awaited, cancellation
   already propagates through the adapter and Tool Registry to this coroutine.
4. On explicit timeout, cancellation, or exception after spawn, run one
   idempotent cleanup routine: signal the captured group, allow a short cleanup
   grace period, force-kill only that group if needed, await process exit, and
   finish draining/closing its pipes. The grace period bounds cleanup after
   cancellation; it is not an execution timeout.
5. Shield only the cleanup routine from cancellation. Re-raise cancellation
   after cleanup so `/stop`, `/steer`, and shutdown retain their current
   semantics.
6. Record the cleanup outcome in the existing tool audit path. Do not kill or
   reclassify jobs owned by `BackgroundJobManager`.

This local ownership design is preferred over a new turn-wide process manager.
A global registry is out of scope unless a failing integration test proves that
an awaited foreground process can escape the local cancellation barrier.

### Phase C — preserve the complete failure chain

1. Keep the original `StageInvocationError` code and human description as the
   primary failure. Store replay denial as structured recovery-decision details
   instead of replacing the primary code.
2. Reuse the existing exception/details and audit envelopes; do not add another
   exception hierarchy or model-based error judge.
3. Render terminal facts deterministically after Finalisation:
   primary failure, recovery decision, side-effect/replay status, applicable
   foreground cleanup result, attempt count, and request/turn reference.
4. Extend HER adapter metadata with separate primary-failure and
   recovery-decision fields. Preserve a compatibility terminal code only where
   an existing consumer requires it, while always exposing both facts.
5. Ensure cleanup failure can add a limitation but can never overwrite the
   provider failure or falsely claim that a process was reaped.

### Phase D — verification and activation gate

1. Run focused unit tests after each phase, then the complete HER v2 core,
   Runtime, adapter, unbounded-tool-loop, stop/steer, and Tool Registry suites.
2. Run syntax, `git diff --check`, and a repository search for forbidden
   attempt-tier/deadline fields and historical numeric deadlines in executable
   HER v2 code.
3. Run the real process-tree integration tests on the supported POSIX path and
   the platform-appropriate Windows fallback tests without broad `pkill` or
   unrelated-process matching.
4. Run the marked real-wall-clock canary across the largest former deadline.
5. Review the final diff against section 3.2.1 and this plan. Do not weaken the
   design document or tests to accommodate implementation behaviour.
6. Commit the functional remediation, but do not execute `/reboot` until the
   user separately authorises the required HASHI reload. Never use a hard/cold
   restart for this change.

## 5. Required regression tests

### 5.1 No unauthorised limits

- A controlled monotonic clock advances beyond 60, 180, 190, 300, and 600
  seconds while each representative HER stage continues and completes.
- A tool-enabled Execution crosses those historical boundaries over multiple
  model/tool rounds and is not cancelled.
- `StageRequest`, retry policy, and audit payloads expose no attempt tier or
  attempt deadline.
- Existing configuration tests continue to reject legacy turn/time/token/tool
  ceilings.
- A marked slow canary crosses the largest former wall-clock boundary; no
  sub-second test may claim equivalent coverage.

### 5.2 Foreground process lifecycle

- Omitted `bash.timeout` lets a command continue past the former default and
  maximum semantics.
- An explicit short timeout terminates shell, child, and grandchild, then proves
  all captured PIDs are gone and both pipes reach EOF.
- `/stop`, `/steer`, Runtime shutdown, task cancellation, and injected error
  paths perform the same exact-group cleanup before the turn becomes terminal.
- Normal completion reaps the process without sending termination signals.
- An unrelated process group and a managed background job survive foreground
  cleanup.
- Cleanup is idempotent under natural-exit/cancellation races and cannot target
  HASHI's own process group or a reused PID/PGID.

### 5.3 Failure truth and presentation

- A primary provider failure followed by unsafe replay produces both the
  primary code and `SIDE_EFFECT_REPLAY_BLOCKED` recovery decision in Runtime
  output, audit, and adapter metadata.
- A completed cleanup reports success; an injected cleanup failure is reported
  honestly without hiding the primary failure.
- Finalisation output cannot omit or replace the deterministic failure chain.
- Existing no-replay tests still prove that side-effecting or incomplete tool
  activity is never automatically repeated.
- Broad shell/write authority remains available when authorised.

## 6. Risk controls and non-goals

| Risk | Control |
|---|---|
| A new hidden deadline appears under another name | Delete deadline data from the request contract and add structural absence tests |
| An inactive provider can never terminate | Preserve only configured user idle and narrow transport inactivity guards; test their exact scope |
| Cleanup kills unrelated work | Use a new session, captured exact PGID, no name matching, no global `pkill`, and PID/PGID identity assertions |
| Cancellation interrupts cleanup | Shield the short cleanup barrier, then re-raise cancellation |
| Background jobs are killed with the turn | Leave `BackgroundJobManager` ownership unchanged and test survival |
| Cleanup error hides the provider error | Append cleanup as a separate fact/limitation; primary error stays authoritative |
| Tests merely mirror implementation | Cross historical boundaries with controlled time plus one real slow canary |

Non-goals for this remediation are checkpoint/resume of provider conversations,
automatic replay of side effects, reduced Agent permissions, a new execution
budget framework, a global foreground-process service, or changes to managed
background-job lifetime.

## 7. Completion criteria

The cleanup is complete only when all of the following are true:

- no executable HER v2 path contains the tier deadline machinery introduced by
  `839b10a5`;
- no foreground `bash` call receives an implicit timeout or leaves a descendant
  or pipe holder after any terminal path;
- the user-visible and machine-readable result preserve primary failure,
  recovery decision, and applicable cleanup truth;
- the full targeted and integration test matrix passes without weakening the
  design invariants; and
- the functional commit remains unloaded until an explicitly authorised
  `/reboot` activates it.

## 8. Verification record

The source remediation was completed on 2026-08-22 in independently reviewable
checkpoints:

- `1ee8bbd` removed provider-attempt tiers and elapsed deadlines;
- `61e4636` removed the implicit foreground-shell timeout and added exact local
  process-group cleanup; and
- `43eb6d9` preserved the primary provider failure, recovery decision, and
  cleanup outcome through Runtime, audit, adapter metadata, and user-visible
  diagnostics.

The focused and broad deterministic suites passed. The explicitly enabled
real-wall-clock canary completed one uninterrupted Execution in 601.44 seconds,
crossing the largest former 600-second attempt boundary before successful
Finalisation. No `/reboot` or `/restart` was performed as part of implementation
or verification.
