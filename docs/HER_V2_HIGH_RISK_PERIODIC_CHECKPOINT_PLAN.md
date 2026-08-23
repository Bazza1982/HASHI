# HER v2 High-Risk Periodic Checkpoint Plan

| Field | Value |
|---|---|
| Status | Implemented and offline-verified on the isolated feature branch |
| Date | 2026-08-23 |
| Scope | HER v2 work turns explicitly classified for high-risk checkpointing |
| Fixed cadence | 10 completed Execution tool results or 300 monotonic seconds, whichever becomes due first |
| Runtime activation | No reload, restart, or rollout is part of this plan |

## 1. Objective

Add a deterministic, provider-neutral safety checkpoint during continuing
high-risk Execution without restoring a tool-round limit, provider deadline, or
completion gate.

The checkpoint exists to reassess the current plan, accumulated evidence, and
authority before more high-risk work begins. It supplements rather than replaces
Tool Gateway policy, permission checks, explicit user confirmation, Review,
Verification, or Finalisation.

## 2. Locked decisions

The implementation and tests must preserve all of these decisions:

1. The cadence is fixed: a checkpoint becomes due after 10 new completed
   Execution tool results or after 300 monotonic seconds in the current
   Execution window, whichever condition is observed first.
2. A high-risk task that finishes before either condition becomes due receives
   no synthetic final checkpoint. Completion is not a checkpoint trigger.
3. Reaching a terminal Execution result without another safe tool boundary does
   not create a catch-up checkpoint, even if 300 seconds elapsed. Ordinary
   effort-selected Review, Assured Verification, and Finalisation still run.
4. The cadence controls only periodic checkpoints. Tool denial, approval
   requirements, missing authority, destructive-action safeguards, path and
   workzone boundaries, explicit `/stop` or `/steer`, and a genuine need for
   user input take effect immediately. They never wait for result 10 or minute
   5.
5. The two cadence values are scheduling thresholds, not execution ceilings.
   They may pause admission of the next tool action while a due checkpoint is
   assessed, but they never cancel a healthy provider call, interrupt an active
   tool merely because time passed, cap the total results, or force task
   completion.
6. Checkpoints are internal control events. They do not enter the commentary
   lane and do not create an additional user-facing message. A checkpoint that
   establishes a real need for user input reaches the user once through normal
   Finalisation and clarification delivery.
7. A periodic checkpoint is not Review or Verification, does not increment
   either counter, and cannot satisfy or suppress the ordinary assurance stages
   selected by HER execution mode.

## 3. Risk-selection contract

Task complexity, execution mode, and checkpoint risk are separate concerns.
Triage will return a required `checkpoint_policy` for every work
classification:

- `STANDARD`: no periodic checkpoint coordinator is installed;
- `HIGH_RISK`: install the fixed high-risk coordinator for Execution.

`checkpoint_reason` is required and non-empty for `HIGH_RISK`. Direct response
and confirmation-only turns do not execute and therefore use no periodic
checkpoint. The selected policy is recorded atomically with the immutable
Triage classification and cannot be downgraded later in the turn.

This field grants no authority. In particular, `STANDARD` cannot bypass any
HASHI safety or permission decision, while `HIGH_RISK` cannot authorise a tool
that the Agent or Tool Gateway disallows. An old or malformed work-classification
response that omits `checkpoint_policy` is structurally repaired or fails
truthfully; Runtime must not silently infer `STANDARD` and remove the added
safeguard.

## 4. Exact cadence semantics

### 4.1 What counts as a result

One result is one newly completed Tool Gateway invocation in an authoritative
Execution cycle. Count the receipt once whether it records success, a completed
tool error, or a policy denial. Use the receipt identity, not streamed
`tool_end` text, provider deltas, a model-authored claim, or a lifecycle event.

Do not count:

- tool starts or incomplete/cancelled calls;
- duplicate or replayed stream events;
- checkpoint-evaluator activity;
- Planning, Replanning, Review, Verification, or Finalisation results;
- commentary, heartbeats, reasoning, or timestamp-only audit writes.

The coordinator is shared by the Primary Agent and its bounded sub-agents in
the same authoritative Execution cycle. A sub-agent cannot evade the cadence by
using a separate provider request. The count is deduplicated by the exact
invocation/call receipt identity.

### 4.2 Time window

Use an injected monotonic clock. The first window starts when high-risk
Execution begins, not during Triage or Planning. A completed `CONTINUE`
checkpoint starts a fresh window and clears the completed-result count.

Provider recovery and structured-envelope repair within the same logical
Execution cycle do not reset the window. Ending Execution ends the window. If a
later Review or Verification remediation starts a new authoritative Execution
cycle, that cycle starts a new window; no checkpoint is backfilled for the
completed earlier cycle.

### 4.3 Safe-boundary ordering

Due state is inclusive (`>= 10` or `>= 300.0`). It is evaluated:

1. before admitting a new tool invocation, so a time-due checkpoint can run
   before another action starts; and
2. after recording a completed result, before that result is released for the
   provider's next model/tool round.

The 10th completed result therefore makes one checkpoint due before the model
can begin an 11th action. If a single active tool crosses minute 5, the timer
does not kill it; the checkpoint runs after that tool reaches its ordinary safe
completion boundary and before new work is admitted.

When parallel tools are active, the coordinator closes new admission, allows
already-active calls to settle, and elects exactly one checkpoint leader. Count
and time becoming due together coalesce into one checkpoint whose audit record
contains both reasons. A completed checkpoint resets one window; there is no
burst of catch-up checkpoints for elapsed historical intervals.

## 5. Checkpoint assessment contract

The checkpoint is a distinct, tool-free internal HER substage using the
configured reviewer-capable profile. It has no principal lifecycle state, no
Persona, no commentary, no side-effect authority, and no ability to contact the
user or finalise.

It receives only bounded, auditable facts:

- immutable goal and Triage references;
- the checkpoint policy and reason;
- active plan and current Execution-cycle identity;
- cadence trigger, result count, and monotonic elapsed duration;
- exact completed receipt metadata plus bounded, redacted result summaries;
- current limitations, denials, and the prospective next tool action when one
  is waiting for admission.

It returns exactly one validated decision:

- `CONTINUE`: release the boundary and begin a fresh cadence window;
- `USER_INPUT_REQUIRED`: stop further Execution admission and carry one
  concrete question into the ordinary Finalisation/clarification path;
- `HALT`: stop further Execution admission and preserve a truthful failed
  Execution result with the checkpoint finding and all completed evidence.

The checkpoint cannot widen scope, mutate the plan, reclassify the task,
authorise a denied action, report task completion, or replace the Execution
disposition. Invalid or unavailable checkpoint assessment must never be
fabricated as `CONTINUE`; after its normal typed provider-recovery and structure
repair paths are exhausted, Runtime halts further high-risk tool admission and
reports the technical limitation truthfully.

## 6. Runtime and provider architecture

### 6.1 Request-local coordinator

Add a small `HighRiskCheckpointCoordinator` with an injectable clock, exact
receipt identities, active-call tracking, a single-flight checkpoint lock, and
a typed evaluator port. Keep it request-local; do not add a process-global
timer or mutate the shared Tool Registry used by other turns.

The coordinator must not use `ProgressTracker` as its clock or reset the user
meaningful-progress idle boundary merely because a checkpoint ran. A repeated
checkpoint is control activity, not proof of substantive progress.

### 6.2 Tool Gateway safe-boundary hook

Wrap the request-local evidence-recording registry, not provider stream events.
The intended order is:

```text
shared HASHI Tool Registry
  -> delegated authority view
  -> exact evidence-receipt recorder
  -> high-risk checkpoint boundary (HIGH_RISK Execution only)
  -> unbounded provider tool-loop view
```

The hook can await a due checkpoint before returning a completed result, which
keeps the existing provider conversation and tool-call/result pairing intact.
It does not split a request into artificial turns, replay a tool, persist a CLI
session, or change `max_loops=None`.

### 6.3 Typed interruption

`USER_INPUT_REQUIRED`, `HALT`, explicit stop, and evaluator failure need typed
control paths that every supported backend propagates without converting them
into an ordinary tool-error string. Runtime then:

1. closes tool admission and cancels controlled sibling/sub-agent work;
2. preserves the result that triggered the checkpoint and every completed
   receipt;
3. never retries already-observed side effects;
4. constructs the appropriate Execution outcome; and
5. continues through the existing Review/Verification policy when applicable,
   followed by exactly one Finalisation and required delivery.

No new `CHECKPOINTING` lifecycle state is needed. The principal state remains
`EXECUTING` until Execution continues, requires input, or ends.

## 7. Immediate-safety precedence

The safe-boundary coordinator must not become a batching layer for safety
decisions:

- Tool Gateway evaluates allow/deny/approval and scope on every invocation.
- A denied or approval-required operation is returned or escalated immediately;
  it is not queued until the next periodic checkpoint.
- Execution may return `USER_INPUT_REQUIRED` at any time with a concrete
  question; it need not wait for the coordinator.
- `/stop`, `/steer`, task cancellation, audit-persistence failure, runtime
  shutdown, and narrow transport/tool safety guards pre-empt coordinator waits.
- A cadence timer alone never interrupts an active tool. A separate immediate
  safety signal retains its existing authority to stop unsafe work.

## 8. Test design

### 8.1 Pure policy and clock tests

Use a controlled monotonic clock and parameter tables to prove:

- `STANDARD` never schedules a checkpoint;
- `HIGH_RISK` is not due at 9 results and `299.999s`;
- result 10 is due, exactly `300.0s` is due, and either condition is sufficient;
- simultaneous count/time triggers coalesce and retain both trigger facts;
- success, completed error, and denial receipts count; starts, incomplete calls,
  cancelled calls, and duplicate receipt identities do not;
- `CONTINUE` resets count/time once and does not create catch-up work;
- provider retry/structure repair cannot reset or evade a live window;
- a new remediation Execution cycle receives a fresh window;
- a checkpoint event alone does not reset meaningful-progress idle state.

### 8.2 Safe-boundary and concurrency tests

With fake tools and barriers, prove:

- the 10th result is recorded before checkpoint assessment and an 11th tool
  cannot start first;
- crossing 300 seconds inside one active tool does not cancel it, and the next
  action waits for the checkpoint;
- parallel calls already admitted can settle, new calls block, and one leader
  performs one checkpoint;
- count/time races, duplicate callbacks, and concurrent sub-agents cannot
  produce duplicate checkpoints;
- stop/steer cancels the evaluator and all coordinator waiters without a late
  release or late user message;
- a checkpoint never replays a completed side effect and preserves exact
  call/result pairing.

### 8.3 Runtime journeys

Add focused end-to-end journeys for:

1. high-risk work completes with 0-9 results before 300 seconds: zero periodic
   checkpoints, then normal Finalisation;
2. the same short task under Reviewed and Assured modes: zero periodic
   checkpoints, while ordinary Review, Verification where applicable, and
   Finalisation still occur with their existing counters;
3. high-risk work reaches result 10: one checkpoint, `CONTINUE`, further work,
   and no synthetic completion checkpoint;
4. time becomes due before the next tool: one checkpoint at admission, with no
   provider deadline or active-tool cancellation;
5. a tool-free provider operation runs beyond 300 seconds and completes: no
   final checkpoint is invented, preserving the no-completion-gate rule;
6. `USER_INPUT_REQUIRED`: no later tool starts, the concrete question appears
   once through Finalisation, and the terminal state is `PENDING_USER_INPUT`;
7. `HALT` or unavailable evaluator: no later tool starts, partial evidence is
   retained, side effects are not retried, and the limitation is reported;
8. normal-risk work exceeds both numerical thresholds: no checkpoint is
   installed and existing unbounded Execution behaviour is unchanged;
9. Tool Gateway denial, approval-required policy, missing permission, path
   escape, and Execution-discovered user input all act before the periodic
   threshold.

### 8.4 Adapter contract tests

For every supported tool-capable backend family, prove:

- the hook runs from one real Tool Registry completion, not a stream summary;
- typed checkpoint interruption is propagated rather than flattened into tool
  output;
- the provider conversation is neither split nor resumed from fabricated
  state;
- `max_loops` remains unbounded and total tool results remain uncapped;
- checkpoint assessment has no tools, no side effects, and no recursive
  checkpoint hook.

### 8.5 Audit and presentation tests

Audit records must cover `checkpoint_due`, `checkpoint_started`,
`checkpoint_completed`, and `checkpoint_interrupted_execution` with stable
event identities, trigger reasons, count, elapsed time, decision, and hashes or
bounded redacted summaries rather than raw secrets.

Presentation tests must prove that checkpoint events cannot enter generic
delivery or Persona commentary, a short task receives no added message, and a
real pause/clarification still produces exactly one required user message.

## 9. Contradictory or outdated tests to correct

The implementation must update narrow assertions whose old wording would
reject the approved policy, without weakening the underlying safety invariant:

1. `test_provider_operations_have_no_elapsed_attempt_deadline` currently
   monkeypatches global `asyncio.wait` and requires every timeout everywhere to
   be `None`. Narrow it to prove that the complete provider operation is never
   cancelled by an elapsed deadline. A controlled 300-second checkpoint
   scheduler wait is allowed because it gates only a safe boundary and is not a
   provider timeout.
2. The no-unauthorised-ceilings tests and documentation must say explicitly
   that 10/300 schedules an internal checkpoint and never caps results, total
   runtime, stage duration, provider attempts, or tools. Keep the controlled
   601-second and unbounded-tool-loop regressions.
3. Provider-loop tests that prohibit every mid-loop hook are too broad. Replace
   that claim with the material invariant: no prompt split, tool replay,
   fabricated resume state, lost tool result, or loop cap. A request-local
   awaited safe-boundary hook is permitted.
4. Exact Triage schema fixtures, immutable Ledger snapshots, prompt-asset
   inventories, stage matrices, and fake providers must include the new
   checkpoint policy/substage rather than silently defaulting work turns to
   standard risk.
5. Commentary documentation and schema currently allow optional Verification
   commentary, while `COMMENTARY_STAGES` and the focused tests omit
   Verification. Resolve this existing contradiction in favour of the
   documented contract by adding Verification to the typed commentary lane and
   testing it separately. Checkpoint events remain excluded.
6. Keep, rather than delete, the tests asserting that lifecycle events, tool
   telemetry, retries, failures, and Finalisation cannot synthesise Persona
   commentary. The checkpoint is an internal control event and does not make
   those tests obsolete.

The separate in-progress authoritative-workspace Verification and Auto Compact
changes overlap several broad HER test files. Checkpoint coding must preserve
those user changes and patch only the assertions and helpers owned by this
feature.

## 10. Implementation sequence

### Phase A — contract and risk authority

1. Add the checkpoint-policy types, required Triage fields, parser rules,
   immutable Ledger record, prompt contract, and compatibility tests.
2. Add the tool-free checkpoint request/response schema and strict validator.
3. Update the product design and testing plan to list the periodic safe-boundary
   gate as an authorised control that is not an execution ceiling.

### Phase B — deterministic coordinator

1. Implement the request-local counter, monotonic clock, safe-boundary state
   machine, single-flight election, and typed decisions.
2. Complete pure boundary, reset, deduplication, race, and cancellation tests
   before integrating a provider.

### Phase C — Tool Gateway and adapter integration

1. Install the wrapper only for `HIGH_RISK` Execution, including delegated
   sub-agents in the same cycle.
2. Propagate typed interruption through each backend family and preserve the
   evidence receipt that caused the boundary.
3. Prove no tool replay, request splitting, authority widening, or loop cap.

### Phase D — Runtime outcome integration

1. Map checkpoint decisions to continuation, user-input, or failed Execution
   outcomes without inventing completion.
2. Preserve Review/Verification/Finalisation policy and exact required-delivery
   semantics.
3. Add audit records and the production-like journeys.

### Phase E — adjacent consistency cleanup and gates

1. Correct the outdated tests in section 9, including Verification commentary.
2. Run focused checkpoint, structured, runtime, adapter, prompt, commentary,
   and unbounded-loop suites; then run the established HER core and full offline
   regression gates.
3. Run Ruff, compile checks, prompt-asset validation, and final diff/whitespace
   review. Do not reload or restart a live instance without separate authority.

## 11. Completion criteria

Coding is complete only when:

- high-risk policy is explicit, immutable, and cannot silently default away;
- 10 results or 300 seconds produces exactly one safe-boundary checkpoint when
  continuing work exists;
- short completion never manufactures a checkpoint or user-facing message;
- immediate safety, permission, stop, steer, and user-input controls never wait
  for the cadence;
- ordinary Review, Verification, and Finalisation behaviour is unchanged;
- no healthy provider/tool operation is time-limited by the checkpoint;
- concurrent/sub-agent work cannot bypass or duplicate the checkpoint;
- completed side effects and tool results are never replayed or lost; and
- focused plus broad regressions pass without disturbing unrelated uncommitted
  work or activating the live runtime.

## 12. Implementation and verification record

Implemented on 2026-08-23 in the isolated
`feature/her-v2-high-risk-checkpoints` branch. The implementation includes the
explicit Triage/Ledger risk contract, strict tool-free checkpoint schema,
request-local coordinator, exact Tool Gateway receipt hook, provider-neutral
typed interruption, Runtime outcomes and audit records, Verification
commentary consistency fix, and the documentation updates in this plan.

Final offline gates on the isolated worktree:

- HER v2 focused inventory: 317 passed, 1 skipped;
- curated repository core gate: 215 passed;
- explicit offline product suite: 2525 passed, 4 skipped, 40 deselected;
- Ruff, Python compilation, prompt-asset coverage, and diff/whitespace checks:
  passed.

No live runtime was reloaded, restarted, or otherwise activated.
