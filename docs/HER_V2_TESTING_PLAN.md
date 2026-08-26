# Hashi Engine Runtime v2

## Formal Testing Plan

| Field | Value |
|---|---|
| Status | Approved testing baseline |
| Version | 1.2 |
| Date | 2026-08-24 |
| System | Hashi Engine Runtime (HER) v2 |
| Testing approach | Intent-based, risk-focused, and scenario-driven |
| Governing design | [HER v2 Product Requirements and Technical Design](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md) |
| Implementation baseline | HASHI `her-v2` at `cc010d11d69b4eb24c62c134dc57ac62ea42c277` |

## 1. Purpose

This plan defines the minimum correct test coverage for HER v2.

Testing exists to prove that HER preserves its intended execution behaviour when models, tools, schemas, providers, messages, logs, concurrency, and timing are imperfect. It does not exist to maximise test count, assertion count, line coverage, snapshot volume, or schema permutations.

The suite must prove that HER:

- preserves the active user goal;
- treats the recorded Triage classification as immutable for the turn;
- applies the configured execution policy without confusing HER effort with provider reasoning;
- maintains strict lifecycle order;
- keeps useful work moving through non-critical imperfections;
- stops at genuine safety, authority, or technical boundaries;
- distinguishes `ERROR`, `FAILED`, and all other terminal states correctly;
- preserves completed work when reporting or review fails;
- records all available reasoning traces for audit;
- honours HASHI ownership of tools, permissions, logging, delivery, and process control;
- never resumes an old in-flight execution stack after process restart.

## 2. Mandatory Test Justification

Every material HER test must identify:

1. the design intention it protects;
2. the realistic risk, defect, or edge condition it represents;
3. the externally observable behaviour required from HER;
4. the behaviour HER must not exhibit;
5. why failure would represent a material architecture defect.

A test must not be created merely because:

- a function exists;
- a line is uncovered;
- a field has several syntactically valid values;
- a framework suggests a pattern;
- a coding agent can easily generate more cases;
- a larger test count appears more comprehensive.

If a proposed test cannot name a protected invariant, boundary, production defect, or material failure mode, it should not be added.

## 3. Testing Golden Rule

> Test whether HER's architecture intentions survive real-world imperfections, not merely whether individual components return technically valid output.

Short form:

> Test the intention, then test the implementation.

The runtime principle also applies to testing:

> Prefer execution continuity over execution perfection while keeping Triage authority, lifecycle order, audit durability, permissions, and user-goal fidelity strict.

## 4. Scope

This plan covers:

- Immediate Response and Triage concurrency;
- task classification and immutable Triage authority;
- effort-policy and provider-role routing;
- Planning and plan versioning;
- Execution and Tool Gateway coordination;
- sub-agent orchestration and authority;
- compulsory Replanning cadence, three-question calibration, and absence of a
  Replan ceiling;
- Review validation and remediation behaviour;
- Finalisation and reporting;
- lifecycle and terminal-state selection;
- Ledger minimality and log references;
- reasoning-trace audit durability;
- retry, timeout, false-progress, cancellation, and stop behaviour;
- `/steer` as stop plus a newly triaged turn;
- process-restart reconciliation without execution-stack resumption;
- Habits, Meditation, and Dream boundaries;
- provider-neutral configuration;
- capability-negotiated provider and read-only tool routing;
- Auto Compact capacity detection, independent `/model` routing, protected
  context, raw retention, atomic commit, and Tier 2/Tier 3 isolation;
- structured-output carrier compatibility, ambiguity rejection, and repair.

This plan does not attempt to test:

- every possible model answer or wording variation;
- the internal quality of third-party providers;
- every independent business workflow of every HASHI tool;
- every optional JSON-field combination;
- workflows outside HER's declared authority;
- hidden provider reasoning that the provider does not expose.

When a provider does not expose reasoning, the suite tests that HASHI records the trace as unavailable rather than fabricating it.

## 5. Testing Priorities

| Priority | Focus | Purpose |
|---|---|---|
| P0 | Authority, lifecycle, audit, stop, and terminal invariants | Release-blocking architecture rules |
| P1 | End-to-end turn behaviour | Prove complete user-observable flows |
| P1 | Fault injection and concurrency | Prove behaviour under realistic imperfections and races |
| P1 | Tool and agent boundaries | Prevent unauthorised execution or authority escalation |
| P2 | Deterministic components | State tables, parsers, counters, policies, and configuration |
| P2 | Production regressions | Permanently protect material defects already observed |
| Observation | Model-dependent quality | Measure rather than pretend to prove deterministically |

## 6. Requirements Traceability

Every locked invariant in the governing design must map to at least one test or one explicitly approved production observation.

The release report must provide a traceability table with:

```text
design_requirement_id
invariant
test_or_observation_id
test_level
fault_class
release_blocking
latest_result
```

Traceability completeness is a release requirement. Raw test count is not.

## 7. Architecture Acceptance Invariants

The tests in this section form the HER Architecture Acceptance Suite. Any failure blocks release.

### 7.1 User-goal authority

Tests must prove:

- Triage derives `real_goal` from the current request and context under the
  existing prompt rules, rather than copying the legacy raw `$goal`;
- validated `real_goal` is stored in `state.goal` and is used by audit,
  permission checks, and every downstream decision;
- the active goal is established or clarification is requested through Triage;
- Planning, Execution, Replanning, Review, Habits, and sub-agents do not replace the goal;
- related or apparently better tasks are not substituted for the requested goal;
- reviewer findings cannot change the goal;
- all plan versions continue to reference the same turn goal.

Prohibited behaviour includes:

- rewriting the goal during Planning;
- treating a related objective as equivalent;
- allowing a sub-agent or reviewer to become goal authority.

### 7.2 Triage classification immutability

Tests must prove:

- Triage records exactly one valid classification;
- the classification is immutable once recorded;
- Planning, Execution, Replanning, Review, Finalisation, and sub-agents cannot reclassify;
- later evidence suggesting a classification error does not mutate the current turn;
- a future turn may use prior evidence, but receives a new Triage decision.

Tests must include an intentionally incorrect classifier result. The expected behaviour is not to correct it mid-turn, but to preserve the decision and handle the resulting execution honestly.

### 7.3 `/steer` authority

Tests must prove that `/steer`:

1. stops the old Primary Agent, tools, sub-agents, Replanning, and Review;
2. records the old turn as `STOPPED` with reason `STEERED`;
3. prevents any new old-turn work from starting;
4. creates a new turn identifier;
5. performs a new Triage pass;
6. derives a new goal from the latest relevant context and steer instruction;
7. does not inherit the old classification or active plan as authority.

### 7.4 Immediate Response and Triage

The suite must exercise both completion orders:

- Immediate Response finishes before Triage;
- Triage finishes before Immediate Response.

For `DIRECT_RESPONSE`, tests must prove:

- exactly one user-facing response is delivered;
- the Immediate Response becomes the completed answer;
- no Planning, tools, Execution, Review, or second final message occurs;
- the turn reaches `COMPLETED`.

For work classifications, tests must prove that the Immediate Response acts as
a conservative acknowledgement and work continues. When Triage finishes first,
the test must prove all of the following independently:

- execution starts without waiting for Immediate Response;
- the pending Immediate Response is not cancelled solely because Triage won the
  race;
- if it finishes while work is active, it is delivered exactly once as an
  acknowledgement;
- if the required final result finishes first, the pending Immediate Response is
  cancelled or suppressed as superseded and no late acknowledgement appears;
- supersession is audited separately from malformed or unavailable optional-stage
  degradation.

For `CONFIRMATION_REQUIRED`, tests must prove that clarification is requested without duplicating an adequate request already delivered by the Immediate Response, and the turn reaches `PENDING_USER_INPUT`.

Fault tests must also prove that:

- malformed, unavailable, or still-pending Immediate Response does not block a
  valid work or clarification Triage result;
- Triage remains mandatory;
- `DIRECT_RESPONSE` fails truthfully when its required Immediate content cannot
  be validated;
- a provisionally delivered Immediate Response is discarded when mandatory
  Triage fails and the transport supports discard.

### 7.5 Classification, effort, and provider routing

Tests must prove that classification and effort remain separate concerns:

- classification describes the task;
- HER effort selects the permitted orchestration path;
- provider reasoning settings come from provider-role configuration;
- effort labels are never passed as provider reasoning settings merely because their names appear similar.

Throughout this plan, an execution mode is named with both its descriptive
label and canonical wire value: Direct (`zero`), Fast path (`low`), Planned
(`medium`), Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`). Bare
wire values are used only when a test specifically exercises serialization,
configuration, commands, or persistence. Range shorthand such as
"medium-or-above" must be read and asserted as the named set of execution
modes, never as task risk or provider reasoning.

Command, Telegram, Workbench, job, and status tests must also prove that HER
shows Direct, Fast path, Planned, Adaptive, Reviewed, and Assured while preserving the
canonical `zero`, `low`, `medium`, `high`, `xhigh`, and `max` wire values. Direct
must prove exactly one Quick-model call at default provider reasoning `high`, no
automatic effort upgrade or other HER stage, complete primary tool authority,
normal verbose progress, the existing attachment fallback, and a completed
terminal state for any successful natural-language return. Descriptive
aliases, especially `/effort reviewed` and `/effort assured`, must persist the
canonical value. Non-HER backends must retain their existing Effort labels and
model-aware choices.

The default routing policy should prefer:

- configured lightweight execution for `SIMPLE_TASK`;
- configured premium execution for `COMPLEX_TASK`;
- configured premium orchestration for `HIGH_VOLUME_TASK`.

Tests must allow explicitly configured, policy-compliant fallback or override behaviour. They must not hard-code one provider or model name as the only valid implementation.

Provider boundary tests must select by declared capability: tool use plus HASHI
tool isolation for tool-enabled stages, proven tool isolation for tool-capable
no-tool stages, and safe acceptance of a previously unknown engine that has no
tool capability. Engine-name allowlists are not an acceptance invariant.

Auto Compact routing tests must prove that Compact follows the initiating
Agent's active HER v2 provider and Quick/Light model at fixed high HER effort.
`/model compact` may select only the approved inherit-Quick policy, off, and the
Tier 2-or-3 watchdog; it must not create a third provider/model path. Legacy
`inherit_pro` or explicit route state migrates forward without a Pro/global
fallback. Missing adapter declarations or Compact capacity remain diagnostic,
while a missing active Quick grant is a configuration error. Provider reasoning
is mapped separately and lack of granular provider effort must not block
Compact.

Representative policy combinations must cover:

| Classification | Execution mode | Required path |
|---|---|---|
| `DIRECT_RESPONSE` | any | Immediate Response only |
| `CONFIRMATION_REQUIRED` | any | Clarification only |
| `SIMPLE_TASK` | Fast path (`low`) | Direct execution, no formal Planning |
| `SIMPLE_TASK` | Adaptive (`high`) | Planning, then lightweight-preferred execution; classification unchanged |
| `COMPLEX_TASK` | Fast path (`low`) | Premium-preferred execution without mandatory Planning |
| `COMPLEX_TASK` | Planned (`medium`) | Planning then execution |
| `COMPLEX_TASK` | Adaptive (`high`) | Planning and compulsory Replanning at every 10-result/300-second safe boundary |
| `COMPLEX_TASK` | Reviewed (`xhigh`) | Planning, Replanning, one tool-backed Review, and at most one remediation without a closure Review |
| `HIGH_VOLUME_TASK` | Assured (`max`) | Premium orchestration, sub-agents, and fresh tool-backed Review after each Review-driven remediation until `PASS` or `CONDITIONAL_PASS` |

This representative matrix replaces a full classification-by-effort Cartesian product unless a production defect justifies an additional combination.

One focused case must make an Adaptive (`high`) `SIMPLE_TASK` request Replanning,
prove that the second execution may use the configured primary execution
profile, and prove that the recorded classification remains `SIMPLE_TASK`.

#### 7.5.1 Scheduled-job request policy

Cron and heartbeat regressions must prove that:

- every prompt job resolves to HER v2 Direct (`zero`) for scheduled, manual
  Run, and recovery replay paths;
- valid, invalid, and stale `her_v2_effort` fields cannot bypass Direct or
  prevent an otherwise valid job from running;
- the Direct stage receives the authoritative job instruction without a
  Triage invocation or a Triage-produced replacement goal;
- the Agent's configured effort remains unchanged and is used by the next
  ordinary request;
- the policy does not rewrite saved provider/model/reasoning configuration;
  Direct uses its configured Quick route and later ordinary requests retain
  their normal route selection;
- prompt skills preserve the same scheduler context as direct prompt jobs;
- legacy overrides are removed on update, import, enable, and transfer mutation
  boundaries;
- nudge, delayed, and ordinary requests do not acquire scheduled-job effort
  from a source string or summary heuristic;
- Workbench and Telegram manual Run preserve the explicit cron/heartbeat kind;
- request and response audit metadata records configured effort, effective
  effort, resolution reason, job identity, and trigger.

Tests based on the former assumption that cron or heartbeat prompt work always
inherits the current Agent effort must be updated to assert this request-local
policy. Tests must not infer scheduled-job policy merely from `source` or
human-readable summary text. Existing nudge tests should remain as negative
coverage, because nudges are continuations rather than routine job executions.

The tool-loop regression is such a production-defect exception. It must prove
that every tool-enabled HER effort receives an unbounded request-local registry
view without mutating the shared Agent registry. A focused adapter regression
must then drive every currently supported Tool Registry API adapter beyond a deliberately low
underlying `max_loops` value and prove that execution still reaches the model's
own final response. The implementation must use a genuine unbounded loop
contract, not a large numeric sentinel.

### 7.6 Lifecycle ordering

The canonical lifecycle states are exactly:

- `RECEIVED`
- `TRIAGED`
- `PLANNED`
- `EXECUTING`
- `REPLANNING`
- `EXECUTION_COMPLETED`
- `REVIEWING`
- `FINALISING`
- terminal states

`TRIAGING` and `PLANNING` may exist as telemetry phases but are not additional authoritative lifecycle states unless the governing design is formally revised.

The principal allowed graph is:

```text
RECEIVED
  -> TRIAGED

TRIAGED [DIRECT_RESPONSE]
  -> FINALISING
  -> COMPLETED

TRIAGED [CONFIRMATION_REQUIRED]
  -> PENDING_USER_INPUT

TRIAGED [LOW work]
  -> EXECUTING

TRIAGED [MEDIUM/HIGH/XHIGH/MAX work]
  -> PLANNED
  -> EXECUTING

EXECUTING
  <-> REPLANNING

EXECUTING
  -> EXECUTION_COMPLETED

EXECUTION_COMPLETED [XHIGH/MAX]
  -> REVIEWING

REVIEWING [FAIL and remediation required]
  -> REPLANNING
  -> EXECUTING
  -> EXECUTION_COMPLETED

EXECUTION_COMPLETED [Assured (`max`) after remediation]
  -> REVIEWING

EXECUTION_COMPLETED or REVIEWING
  -> FINALISING
  -> TERMINAL

FINALISING [Execution requires user input]
  -> PENDING_USER_INPUT
```

A table-driven transition test must cover every allowed edge and a representative set of forbidden edges. It is unnecessary to create a separate handwritten test for every pair of unrelated states.

Tests must prove that HER never:

- silently repairs an invalid transition;
- synthesises a missing predecessor;
- reviews before execution produces a candidate;
- finalises while mandatory execution or Review handling remains active;
- bypasses mandatory Planning in Planned (`medium`), Adaptive (`high`),
  Reviewed (`xhigh`), or Assured (`max`);
- changes the active plan outside `REPLANNING`.

An invalid authoritative transition produces terminal `ERROR`.

#### 7.6.1 Execution prompt and result authority

Tests must prove that:

- every required external prompt asset exists, is non-empty, and declares
  exactly its approved placeholders independent of the process working
  directory;
- malformed or placeholder-drifted prompt assets fail closed before provider
  invocation;
- Execution receives a dedicated HER v2 system prompt rather than the Agent's
  full `system_md` or Persona;
- its user message retains the same complete HASHI-supplied request context as
  Planning, including recent turns, Memory+, and cross-session receipts, plus
  the active plan when one exists;
- the system prompt requires faithful tool-backed execution and exact JSON;
- the only accepted dispositions are `COMPLETED`,
  `COMPLETED_WITH_LIMITATIONS`, `FAILED`, and `USER_INPUT_REQUIRED`;
- `ERROR`, `REPLAN_REQUIRED`, `ABANDONED`, and any other value are rejected;
- Execution cannot request Replanning, while HER may impose Replanning through
  its effort/review policy;
- a valid Execution disposition is the source of terminal truth and cannot be
  changed by Review or Finalisation.

### 7.7 Plan authority and versioning

Tests must prove:

- Planned (`medium`), Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`)
  work turns create a plan before Execution;
- Fast path (`low`) may execute without a formal plan;
- exactly one plan version is active;
- Replanning creates a new version only when the plan materially changes;
- an unchanged Replan preserves the active version and in-flight bindings;
- the earlier version remains referenced in logs but is not accumulated into the active plan;
- only the primary HER workflow may enter `REPLANNING`;
- sub-agents and reviewers cannot activate a plan version.

### 7.8 Replanning purpose, compulsory cadence, and no ceiling

Tests must prove that Replanning:

- uses the immutable goal, classification, active plan, and current execution evidence;
- corrects execution drift without redefining the goal;
- does not consult Habits again;
- records a new plan version only for a materially changed plan and preserves
  the active version for an unchanged calibration;
- returns to Execution below 100% completion and stops adding work for
  assurance/Finalisation at 100%;
- is controlled by effort policy rather than excluding an immutable
  `SIMPLE_TASK` classification;
- may escalate execution capability without mutating classification; and
- runs unconditionally for Adaptive (`high`), Reviewed (`xhigh`), and Assured
  (`max`) at each inclusive
  10-result or 300-second safe boundary.

There is no Replan ceiling. Structural tests reject `max_replans`,
`replan_limit`, and `replan_limits`; controlled tests must complete more than
the retired 50/100/200 values without suppressing Replanning, Review, or
Finalisation. Reviewed (`xhigh`) retains its one-remediation boundary; Assured
(`max`) has no fixed Review/fix round limit.

### 7.9 Review independence, evidence, and limits

Tests must prove:

- Review applies only to Reviewed (`xhigh`) and Assured (`max`) work turns;
- the Strict Reviewer is contextually independent from the task performer;
- the reviewer communicates only with the Primary Agent;
- the reviewer cannot contact the user, reopen Triage, change goal or classification, activate a plan, authorise side effects, or finalise;
- Review calls have tools enabled but side effects disabled;
- the model-authored Review contract accepts exactly `PASS`,
  `CONDITIONAL_PASS`, or `FAIL`, with a non-empty reason and conditions only
  for `CONDITIONAL_PASS`;
- Review cannot change the valid Execution disposition;
- Review can use its delegated inspection and validation tools when
  appropriate, while subjective or inherently non-verifiable work can receive
  `CONDITIONAL_PASS` without a synthetic evidence gate;
- `system_review.txt` is the only Reviewer prompt asset and receives the
  authoritative resolved goal, Review kind, active plan reference, findings to
  close, latest draft, structured Execution record, evidence references, and
  exact delegated tool catalogue, without relevant Habits or active plan
  content;
- the provider user turn for Review is the authoritative goal as data, not a
  second instruction wrapper;
- no `stage_request.txt` file, prompt-catalogue entry, generic Review renderer,
  or call-site survives;
- tool activity and receipts remain independently audited and cannot be
  rewritten as successful validation by the reviewer;
- a technical provider or tool failure is runtime state, never a model-authored
  `CONDITIONAL_PASS` or invented Review decision.

Limit tests must prove:

- Reviewed (`xhigh`): one independent Review and at most one remediation, with
  no closure Review after that remediation;
- Assured (`max`): each `FAIL` causes Replanning and remediation followed by a
  fresh Review of the latest state, with no fixed Review/fix round limit;
- unavailable Review after bounded retries does not discard completed Execution or remain stuck indefinitely;
- Review technical failure is logged and Finalisation proceeds from Execution evidence.

### 7.9.1 Review validation and removal of Verification prompt wiring

Tests must prove that validation occurs through Independent Review and that no
separate Verification model stage or prompt remains. Repository-wide checks
must find no former Verification prompt asset and no catalogue, loader,
renderer, assembly, repair, invocation, schema, configuration, or test wiring
that refers to one. Deleting prompt files without removing runtime wiring fails
acceptance. The Review tool named `verification_run` remains a validation tool,
not a Verification prompt or stage.

`workspace_inspect` tests cover status, diff, bounded search, hashes, artifacts,
path escape rejection, and snapshot drift. Review `verification_run` tests
prove configured recipes and direct argv commands run in the current workspace
without a copy or implicit shell, with the documented timeout and inherited
runtime authority.

### 7.9.2 Compulsory safe-boundary Replanning

The complete oracle is the
[Compulsory Replanning Repair Plan](HER_V2_COMPULSORY_REPLAN_REPAIR_PLAN.md).
Deterministic tests use an injected monotonic clock and exact Tool Gateway
receipts to prove:

- Fast path (`low`) and Planned (`medium`) never install the cadence, while
  Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`) always install it;
- nine results and `299.999` seconds are not due; result 10 or exactly `300.0`
  seconds is due inclusively and forces Replanning at the next safe boundary;
- no checkpoint model chooses `CONTINUE`, ask, or halt; count and time becoming
  due together coalesce into one compulsory Replan with one stable ID;
- completed receipt errors and denials count once, while starts, incomplete
  calls, cancellations, and duplicate receipt identities do not count;
- result 10 is retained before Replanning and no 11th action begins first;
- a tool active at minute five is never cancelled; already-admitted parallel
  calls settle, new admission waits, and one single-flight leader Replans;
- each Replan validates `completion_percent`, `completion_basis`, the full plan,
  `plan_changed`, conditional `change_reason`, `next_step`, and commentary;
- every changed Replan activates the next plan version, while an unchanged
  Replan preserves the active version; either result resets its count/time
  window once without catch-up and never replays a completed side effect;
- a due completion candidate cannot bypass Replanning; below 100% starts a
  continuation Execution from current evidence, while 100% stops new work and
  routes through Review or Finalisation;
- missing/malformed Replan commentary and Persona fact loss use the existing
  Agent display-name deterministic fallback, and stable checkpoint-commentary
  identity guarantees exactly one delivery;
- Primary-Agent and bounded-sub-agent results share one window, and each later
  remediation Execution cycle receives a fresh window;
- immediate denial, approval, missing authority, permission, workzone, stop,
  steer, cancellation, and audit safeguards retain their existing authority;
- each tool-capable provider family preserves typed 100%-completion control and
  exact receipts without flattening, splitting the conversation, fabricating
  resume state, or changing the unbounded tool loop;
- more than every retired 50/100/200 ceiling can complete, a Replanning model
  operation may cross historical timeout values, and no time, token, turn,
  loop, provider-attempt, or Replan-count limit can suppress the cadence or
  whole workflow; and
- audit events `replan_due`, `replan_started`, and `replan_completed` use stable
  identities and bounded redacted summaries.

### 7.10 Tool, permission, and workzone authority

Tests must prove:

- HER invokes tools only through the HASHI Tool Gateway;
- an unregistered or disallowed tool cannot be executed;
- HER, reviewers, and sub-agents cannot elevate their own permission mode;
- path-addressed inspection tools retain workzone/access-root checks;
- `verification_run` fixes its working directory to the authoritative
  workspace but deliberately inherits the HASHI process's filesystem authority
  for runtimes, dependencies, services, and credentials outside that directory;
- reviewer authority remains non-remediating; validation commands may create
  ordinary test caches or artifacts in the authoritative workspace but cannot
  be represented as general corrective side effects;
- sub-agent authority is no greater than the authority delegated by the Primary Agent and orchestrator;
- tool denial becomes evidence and cannot be rewritten as successful execution.
- read-only delegation consumes Tool Registry capability metadata, so an
  explicitly read-only custom tool is accepted without adding its name to HER;
- unknown tools and tools lacking read-only metadata remain denied.

### 7.11 Stop authority

Tests must prove that `/stop`:

- stops Planning, Execution, tool calls, Replanning, Review, and high-volume orchestration;
- propagates cancellation to all controlled sub-agents;
- prevents new work and new sub-agents from starting;
- preserves already completed evidence in logs;
- records terminal `STOPPED`;
- is not reported as success or failure.

Cancellation should be table-driven across lifecycle states, with full concurrency end-to-end tests for at least active tool execution and high-volume sub-agent execution.

### 7.12 Process restart and conversational recovery

Tests must prove:

- an unexpected process interruption does not resume the old planner, executor, reviewer, tool call, or sub-agent graph;
- startup reconciliation records the incomplete old turn as `ERROR`;
- the incomplete Ledger and logs remain available;
- no external side effect is repeated automatically after restart;
- a later “continue” request creates a new turn and new immutable Triage result;
- the new turn may inspect the old conversation, Ledger, and logs without treating the old execution stack as live.

This is a mandatory release gate.

### 7.13 Error, failure, and terminal truth

The terminal-state decision table is:

| Condition | Required terminal state |
|---|---|
| Goal achieved without material limitations | `COMPLETED` |
| Goal substantially achieved with material limitations | `COMPLETED_WITH_LIMITATIONS` |
| Execution correctly concludes the goal was not achieved | `FAILED` |
| Technical failure prevents correct execution | `ERROR` |
| User or authorised control stops the turn | `STOPPED` |
| Triage or Execution requires clarification, confirmation, or missing authority | `PENDING_USER_INPUT` |

One parameterised decision suite must cover all terminal states. Representative end-to-end scenarios must separately cover the states whose correctness depends on multi-stage evidence, including `FAILED`, `ERROR`, `STOPPED`, and `PENDING_USER_INPUT`.

Tests must prove:

- an unsuccessful but technically correct search is `FAILED`, not `ERROR`;
- non-retryable provider unavailability is `ERROR`, not `FAILED`;
- Review `FAIL` does not automatically become terminal `FAILED`;
- intentional stop is `STOPPED`, not `ERROR`;
- unexpected interruption is `ERROR`, not a separate `INTERRUPTED` state;
- rejection of a request after correct execution judgement is `FAILED` unless a higher policy defines a different terminal category.
- execution-discovered user input skips Review, enters the combined
  Finalisation call, preserves evidence and classification, and terminates as
  `PENDING_USER_INPUT` with a Persona-rendered clarification.

### 7.14 Ledger minimality

Tests must prove that the Ledger contains only operational state and compact references, such as:

- turn identifier;
- immutable request/log reference;
- immutable classification;
- current lifecycle state;
- active plan reference where applicable;
- terminal status and reason;
- compact audit-log references.

Detailed prompts, reasoning traces, plans, tool payloads, retries, and diagnostics belong in HASHI logs.

Tests must prove:

- plan versions use concise references;
- corrections are appended to logs rather than rewriting historical truth;
- an incomplete Ledger clearly represents an incomplete turn;
- the Ledger does not grow into a second audit system;
- a Ledger update that only changes a timestamp is not measurable progress.

### 7.15 Reasoning-trace logging and audit durability

Reasoning-trace audit is a mandatory release gate.

Tests must prove that HASHI records every reasoning trace made available to HER, including as applicable:

- Triage reasoning;
- Planning reasoning;
- execution reasoning events;
- Replanning reasoning;
- Review reasoning;
- Finalisation reasoning;
- provider-exposed reasoning streams or structured reasoning artefacts.

Every record must be correlated to:

- turn and request identifier;
- stage and invocation role;
- provider and model;
- attempt/retry number;
- relevant plan version;
- timestamp and ordering metadata.

When a provider exposes no reasoning trace, tests must prove that HASHI records an explicit unavailability marker and does not fabricate a trace.

Audit persistence tests must cover:

1. primary log succeeds;
2. primary log fails and an approved durable local fallback spool succeeds;
3. primary log and fallback spool both fail.

HER may continue only when required audit records have been durably written to the primary log or approved fallback spool. When all approved audit persistence fails, HER must not begin or continue external side effects. The resulting terminal behaviour must follow the configured audit-failure policy and be reported honestly.

The fallback spool must later replay without duplicating audit records.

### 7.16 Habits, Meditation, and Dream

Tests must prove:

- user intent overrides conflicting Habit guidance;
- Execution evidence overrides Habits;
- the complete candidate `habit_catalogue` is retrieved before Triage;
- Triage schema v2 receives `habit_catalogue`, derives `real_goal`, and returns
  `relevant_habits` selected against that goal;
- `real_goal` and `relevant_habits` are explicit in prompts, runtime state, and
  handoffs for Planning, Execution, Replanning, Review, and Finalisation;
- Replanning and other downstream stages do not retrieve or select Habits again;
- disabled and request-ineligible turns neither read the catalogue nor add
  Habit-specific workflow context;
- Fast path (`low`) retains the Triage retrieval and selection path even though it
  skips Planning, and still schedules eligible Meditation
  only after final-delivery-boundary acceptance and terminal persistence;
- repeated Meditation scheduling for one turn produces one durable job, one
  model decision, and one idempotent Write;
- Meditation may share the premium stage's provider backend but selects the
  configured lightweight/flash model, never the premium/pro execution model;
- retrieval errors fail open without a synthetic `habit_planning_skipped`
  audit, while Meditation failures remain invisible to the completed turn;
- Meditation runs only after eligible execution and cannot modify the completed turn;
- Meditation failure does not block Finalisation or reporting;
- Dream runs outside the live critical path;
- Dream failure or unavailability does not block a user turn;
- Habits are advisory and cannot become goal, classification, plan, or tool authority.

Sub-agent contract tests must also prove that only `system_sub_agent.txt`
exists and is loaded, that it receives `real_goal` and `relevant_habits`, and
that no second sub-agent prompt asset or catalogue key exists.

## 8. Essential End-to-End Journeys

The suite should use a small number of complete journeys. Variants should be expressed as parameters or injected disturbances where they protect the same intention.

### Journey A: Direct Response concurrency

Run the same greeting with both Immediate Response/Triage completion orders.

Required:

- exactly one user-facing answer;
- immutable `DIRECT_RESPONSE` classification;
- no Planning, tools, Execution, or Review;
- terminal `COMPLETED`.

### Journey B: Confirmation Required

Use an ambiguous or authority-sensitive request.

Required:

- exactly one necessary clarification flow;
- no Planning or Execution;
- terminal `PENDING_USER_INPUT`.

### Journey C: Representative policy routing

Use the representative Classification × Effort matrix from Section 7.5 rather than duplicating every combination. At least one Medium turn must complete Planning and Execution end to end.

### Journey D: High-volume orchestration

Required:

- planned sub-agent roles;
- premium orchestrator ownership;
- bounded delegated authority;
- no independent sub-agent Replanning or final user response;
- correct synthesis and terminal selection.

### Journey E: Goal drift and immutable classification

Inject evidence suggesting a related but different objective and, in a separate parameter, an obviously incorrect Triage classification.

Required:

- goal and classification remain unchanged;
- Replanning may correct approach but not authority;
- outcome and limitations are reported honestly.

### Journey F: Review and remediation

Cover:

- Reviewed (`xhigh`) Review `FAIL`, one remediation, then Finalisation of the
  latest draft without a closure Review;
- Assured (`max`) Review `FAIL`, Replanning and remediation, then a fresh Review
  against the remediated latest state;
- more than three Assured Review/fix rounds followed by a fresh successful
  Review, proving the retired attempt ceiling is absent;
- honest conditional, subjective, and technically unavailable validation
  reporting;
- unavailable reviewer after a non-retryable failure or no-progress idle
  boundary, with completed work preserved.

### Journey G: Finalisation failure after completed Execution

Required:

- execution is not repeated;
- evidence remains available;
- Finalisation is invoked exactly twice when an eligible first provider attempt
  fails and the recovery also fails;
- both Finalisation attempts receive byte-equivalent immutable Execution
  evidence and the same invariant hash;
- no structure-repair or separate Persona model is invoked;
- invalid or failed Finalisation produces a deterministic local report and
  technical `ERROR` with the typed code and human-readable description.

### Journey H: Stop and Steer

Cover concurrent tool/sub-agent work. Prove complete cancellation and distinguish `/stop` from `/steer` starting a new turn.

### Journey I: Process restart

Interrupt an active turn, restart the runtime, prove no stack resumption, and then issue a new “continue” request.

### Journey J: Audit persistence failure

Exercise primary log, fallback spool, total audit-persistence failure, and replay deduplication.

### Journey K: Tool authority denial

Attempt an unregistered tool, permission escalation, an out-of-workzone path
through an inspection tool, Review remediation, and validation activity beyond
the delegated `verification_run` contract. Prove all are blocked and audited.
Separately prove that a validation child process retains its inherited
filesystem authority.

### Journey L: Terminal truth

Use a compact parameterised harness plus representative E2E evidence to distinguish all current terminal states.

## 9. Structured Output and Tolerance

Structured output imperfections must not create unnecessary hard failures, but tolerance must never weaken authority or lifecycle invariants.

The permitted handling sequence is:

1. collect provider-native parsed data and formal assistant-text candidates;
2. apply an explicitly registered deterministic normalisation;
3. validate all formal candidates and accept one semantic result only;
4. when no formal candidate validates, inspect provider-exposed reasoning only
   for a valid target-stage JSON control envelope;
5. reject conflicting valid candidates rather than choosing by field order;
6. retry side-effect-free stages with the previous validation defect included;
7. log the original defect, selected carrier, and rejected candidates;
8. pass malformed main Execution output directly to Finalisation without replay;
9. produce `ERROR` when Finalisation returns `execution_result: null` or fails.

Candidate imperfections include:

- missing optional fields;
- approved additional fields;
- prose surrounding otherwise valid structured content;
- truncated output where retry can repair it before no-progress idle expiry;
- explicitly approved casing or enum aliases;
- duplicated non-authoritative fields;
- correct substantive content in an invalid wrapper.
- an otherwise valid user-facing JSON envelope containing literal control
  characters inside a string;
- non-empty but invalid provider data beside valid formal assistant text;
- provider-native parsed objects and common text-content block shapes;
- a single target-stage JSON control envelope returned only in exposed
  reasoning when formal output is empty or invalid;
- plain text for inherently user-facing Immediate Response;
- malformed but meaningful Execution prose preserved in full for the single
  combined Finalisation call;
- string-or-list fields normalised without splitting a string into characters.

The suite must not create a case for every permutation. Each accepted repair rule requires a named compatibility reason or production regression.

Tolerance must never:

- fabricate the user goal or Triage classification;
- alter classification;
- synthesise lifecycle predecessors;
- invent a successful tool result;
- fabricate mandatory Planning completion;
- turn reviewer output into Primary Agent authority;
- silently accept unknown protocol drift.
- expose reasoning recovery text to the user;
- choose between semantically conflicting valid carriers.

The deterministic compatibility suite should be one compact table covering
registered carriers, wrappers, aliases, and list forms, plus explicit conflict
and unstructured-prose rejection cases. Runtime regressions separately prove
Sunny-style reasoning recovery, source audit, Immediate/Triage failure
isolation, and retry feedback. Do not duplicate the same carrier permutations
across parser, runtime, adapter, and end-to-end suites.

For a side-effect-authorised Execution response, tests must additionally prove:

- `provider_response_received` and the available reasoning trace are durable before validation;
- the original Execution provider is not replayed after a side-effecting or
  unknown tool starts;
- a provider fault before tools, or after completed provably read-only tools,
  receives exactly one recovery attempt;
- Finalisation receives the complete raw Execution output and original evidence references;
- malformed but meaningful output can be normalised by the combined
  Finalisation stage without replaying Execution;
- unusable output reaches Finalisation and then records technical `ERROR`;
- a valid Execution disposition cannot be changed by Finalisation;
- neither normalisation nor later recovery automatically replays the side effect.

Stage failure after side-effect-free validation retries should be tested through a parameterised fault harness, with a small number of E2E representatives where downstream behaviour materially differs. Finalisation has separate recovery-success and recovery-exhaustion tests, each asserting one Execution invocation.

## 10. Timeout, Retry, and Progress

### 10.1 User idle-progress timeout

Tests must prove that meaningful progress resets the user timeout while total runtime alone does not terminate a progressing turn.

Recognised progress includes:

- meaningful commentary;
- new tool activity or evidence;
- a genuine lifecycle or Ledger transition;
- a materially changed Replan;
- completion of a substantive execution unit.

False progress includes:

- repeated identical commentary;
- the same failed tool call without new evidence;
- timestamp-only Ledger writes;
- rewriting the same state;
- unchanged Replans;
- tool calls that produce no new evidence.

Tests must prove that false progress cannot keep a stalled turn alive indefinitely.

### 10.2 Failure-class provider recovery

Tests must prove exactly one safe fresh-connection recovery after an eligible
typed provider failure. Recovery eligibility is based on failure type and
replay safety; it must not vary by an elapsed-time tier, stage deadline,
provider deadline, context-size deadline, or local/remote deadline.

The fault matrix must cover retryable HTTP 408/429/5xx, connection and timeout
faults, empty responses, incomplete streams, no-response timeout,
reasoning-only timeout, and no-tool incomplete-stream timeout. It must cover
non-retryable configuration, HTTP 400/401/403, TLS/URL, audit-persistence, and
user-stop cases. Every representative verifies the typed code, redacted
human-readable terminal description, audit record, retry decision, and provider
request ID/Retry-After metadata when supplied.

Tests must prove:

- no Runtime, adapter, Persona, learning, or sub-agent path wraps a complete
  ordinary provider operation or tool-enabled provider loop in an attempt
  deadline; the separate tool-free Compact call is tested only under section
  10.4;
- connection/read-inactivity guards are tested at their narrow transport
  boundary, reset on qualifying activity, and never include foreground tool
  execution;
- provider reasoning or text cannot masquerade as user meaningful progress;
- retry preserves provider, model, goal, classification, role, provider
  reasoning, permissions, delegated tools, workzone, and plan;
- a structured correction receives the prior validation error and remains
  distinct from the one provider-recovery allowance;
- Finalisation recovery reuses immutable Execution evidence and Execution is
  invoked exactly once;
- read-only sub-agents recover once;
- main Execution recovers only before tool activity or after completed proven
  read-only tools, and never after unknown or side-effecting activity;
- Persona packaging, Meditation, Dream, and ordinary local providers use the
  same failure-class recovery contract without adding elapsed-time tiers;
- stage-local retry is not process-restart recovery.

### 10.3 No unauthorised execution ceilings

Required regressions prove that successful substantive work can continue beyond
every former `60/180`, `190/300`, and `300/600` second boundary as well as former
whole-turn, stage, tool-round, call/step, sub-agent-count, and token ceilings.
Fast tests must use a controlled clock that actually crosses those historical
boundaries; a sub-second test must not claim to prove absence of a multi-minute
ceiling. A slow integration canary must also cross the largest former real
wall-clock boundary before release.

Tests must additionally prove that omitting a tool timeout applies no default
deadline, while an explicitly supplied per-invocation timeout remains scoped to
that tool only. Removed legacy generic limit fields remain rejected rather than
silently restored. No test may encode, bless, or preserve an unauthorised limit
merely because the current implementation exposes one.

The fixed 10-result/300-second Adaptive-or-above cadence is tested as a
compulsory safe-boundary Replan interval, not as a limit. Regressions must prove
that it never cancels a healthy provider or active tool, never caps total
results, Replans, or runtime, and never invents catch-up cycles. A provider
completion candidate after the time threshold is itself a safe boundary and
must not bypass Replanning. A task that completes below both thresholds has no
synthetic Replan. Controlled 601-second and more-than-10-result journeys remain
unbounded while taking every due Replan.

### 10.4 Auto Compact capacity and timeout isolation

The complete oracle is the
[Auto Compact design](HER_V2_AUTO_COMPACTION_DESIGN.md). Release-blocking tests
must prove the following boundaries.

**Configuration and provider neutrality**

- `/model compact` exposes the effective active provider, Quick/Light model,
  fixed high HER effort, mapped provider reasoning, capacity provenance when
  known, and `tier_2`/`tier_3`/`auto`;
- provider or Quick/Light changes are followed at invocation time without an
  independent Compact route or silent Pro/global fallback;
- HER effort and provider reasoning remain separate, and enable-only providers
  are accepted without inventing granular reasoning effort;
- absent prompt-isolation, tool-disablement, semantic-reasoning, or Compact
  capacity declarations do not lock the route; request-local authority is
  still disabled and actual provider failures remain truthful;
- the active provider/model must have an exact Agent grant; no retired
  cross-provider confirmation or independent-route state may be consulted;
- an unknown engine with declared capabilities works without a name allowlist,
  while fabricated capacity metadata fails safely; known and unknown target
  capacity both retain the fixed 64,000–128,000 HASHI product window, and
  unknown Compact capacity uses conservative 32,000 estimated-token
  maintenance partitions.

**Context authority and atomicity**

- typed segments, rather than flat-prompt text heuristics, select one exact
  eligible source prefix;
- system/developer policy, current request, classification, goal, active plan,
  open tool transaction, permissions, unresolved side-effect truth, and
  required evidence remain byte-identical; automatic Compact also preserves
  the recent-dialogue guard, while manual Compact deliberately removes only
  that eligibility guard;
- the Compact call receives only eligible quoted source, stable identifiers,
  schema, and the permitted minimal relevance header—not Persona, unrelated
  system/developer rules, secrets, tool schemas, or open tool data;
- oversized eligible material is compacted hierarchically at semantic record
  boundaries, with exact source-ID coverage and no fixed chunk/merge ceiling;
- no-shrink output, malformed schema, missing evidence, injection text, archive
  failure, cancellation, and compare-and-swap races all leave the active
  context pointer unchanged;
- successful commit retains hash-valid raw source, injects the capsule exactly
  once, and cannot lose concurrently appended turns.

**Capacity and failure truth**

- fixed-boundary tests cover manual 63,999/64,000 and automatic
  128,000/128,001 token cases, a 64,000 target, known and unknown target
  capacity, and typed provider-capacity rejection;
- response headroom is capacity accounting, not a main-model output-token
  ceiling;
- every automatic Compact failure leaves the already assembled prompt and
  foreground task unchanged and emits a mandatory warning independently of
  `/verbose`;
  `CONTEXT_PROTECTED_SET_TOO_LARGE` and `CONTEXT_CAPACITY_EXHAUSTED` remain
  stable warning/audit codes without silent truncation or route switching;
- prompt assembly and post-turn handling never invoke automatic Compact;
- above 128,000 tokens, the first main Execution invocation schedules exactly
  one detached Compact task; sub-agent Execution and later retries do not
  schedule another;
- exhaust both permitted Compact attempts and prove that main Execution is
  invoked immediately with its original prompt, remains successful, and emits
  the warning even with `/verbose off`;
- a target request is replayed after a capacity rejection only when that failed
  request produced no tool call or side effect.

**Tier 2/Tier 3 watchdog isolation**

- Tier 1 is invalid; remote reasoning Compact resolves Tier 2 and a declared
  local/slow Compact may resolve Tier 3, with explicit configuration winning
  over `auto`;
- the dedicated defaults are tested at `190/300` seconds for Tier 2 and
  `300/600` seconds for Tier 3 using controlled time plus representative slow
  cleanup tests;
- exactly one eligible fresh-connection recovery receives the same frozen
  source, provider, model, reasoning, schema, and tool-free permissions;
- only `CompactionRequest` carries the tier/deadline; generic `StageRequest`,
  target providers, Persona, learning, sub-agents, tools, and complete
  compaction jobs remain free of that watchdog;
- timeout or `/stop` reaps the exact ephemeral local process tree, commits no
  partial output, and does not affect unrelated foreground or managed
  background work;
- newly validated, strictly reduced source coverage may reset meaningful
  progress, while Compact telemetry, retries, repeated coverage, and no-shrink
  maintenance never do.

**Provider behaviour**

- Gemini remains stateless and receives no persisted/resumed provider session
  through either ordinary or Compact calls;
- the initial OpenRouter and DeepSeek implementation retains current unbounded
  request-local tool loops and performs no mid-loop split or compaction;
- any later capability-gated safe-boundary hook has a separate regression suite
  proving current-turn preservation, complete tool-call/result pairing, no tool
  replay, and unchanged loop authority.

## 11. Commentary and User Delivery

Tests must prove:

- a successful Planning or Execution result may carry one optional neutral
  commentary field, while every Replanning invocation produces one required
  progress commentary;
- lifecycle transitions, stage-start events, failures, retries, tool telemetry,
  and finalisation do not synthesise ordinary commentary; only a successful
  validated Replan may invoke its deterministic required-field fallback;
- cadence due/start/completion events never directly enter commentary or
  generic user delivery;
- missing, empty, malformed, or oversized optional commentary does not affect
  stage validation or workflow outcome;
- missing, empty, malformed, oversized, or fact-damaged compulsory Replan
  commentary uses a bounded deterministic neutral fallback and the Agent
  display-name packaging fallback without another Persona model call, under
  one stable exactly-once event ID;
- runtime passes neutral commentary through a commentary port, sends combined
  Finalisation output through required delivery, and routes pre-execution Triage
  clarification through the same Persona Commentary Agent before restoring its
  typed required-message delivery identity;
- commentary and Triage-clarification Persona invocations use the same prompt,
  receive the exact configured `[persona]` marker block, and receive exactly one
  eligible source message;
- Finalisation receives that same marker block, the current request, and the
  complete Execution/review inputs, but no unmarked Agent instructions;
- content outside that marker block cannot reach the packaging model;
- missing or invalid markers use the deterministic display-name + `您`
  fallback in commentary, Immediate Response, Finalisation, and clarification;
- packaging occurs before delivery and concurrent/replayed event IDs are
  delivered at most once;
- the Telegram commentary boundary accepts packaged commentary and rejects a
  raw commentary string;
- optional commentary failure is logged but does not fail execution;
- `/verbose` changes presentation, not workflow authority;
- a final answer is not delivered before Finalisation;
- Finalisation returns one validated object containing `execution_result` and
  Persona-rendered `final_message`; no second model renders that message;
- valid Execution disposition is preserved even if Finalisation attempts to
  change it, while malformed meaningful Execution output is normalised once;
- combined Finalisation preserves Markdown, code blocks, inline code, links,
  paths, identifiers, numbers, facts, uncertainty, and limitations;
- provider-failed Finalisation receives only its one eligible provider
  recovery; invalid structured Finalisation follows the separate semantic
  correction path without replenishing that recovery allowance; exhaustion
  produces technical `ERROR` plus a deterministic local fallback;
- Triage-clarification renderer failure still preserves its validated question;
- Immediate Response receives the same `[persona]` block, never the rest of
  `system_md` or Bridge `/sys` packaging, and its model prompt contains no
  execution, planning, feasibility, or capability assessment task;
- Immediate Response has no tool authority, does not repeat that private control
  fact to the user, and never emits a tool call, tool-control envelope, tool
  syntax, or executable command;
- Immediate Response does not treat its own lack of tool authority as evidence
  that later Execution tools are unavailable, even when the user supplied a
  conditional tool-unavailable reporting instruction;
- `DIRECT_RESPONSE` is the sole exception because its Immediate Response is the final answer;
- the `DIRECT_RESPONSE` Immediate answer is not Persona-rendered a second time;
- a transport without explicit initial-resolution capability never receives a
  provisional Immediate Response and therefore cannot duplicate the final;
- stream callbacks return an explicit acceptance result across registered
  receipt shapes, and initial-resolution support is advertised when the
  provisional transport message can be edited; discard additionally requires
  delete capability;
- every ordinary final send writes its real outcome back to the HER v2 audit
  under the same stable `delivery_id` used by the deferred delivery intent;
- a deferred-lane acceptance is never asserted as an actual transport delivery;
- reporting failure follows the single-provider-recovery Finalisation policy
  while preserving one immutable Execution invocation and evidence set across
  any transport recovery or structured correction.

Exact wording should not be asserted unless a safety, authority, preservation,
or protocol requirement depends on it. Commentary, Triage clarification,
Immediate Response, and combined Finalisation share one Persona source contract
but retain separate typed delivery tests.

### 11.1 Retired backend isolation

Tests must prove that `her` and `her-v2` resolve to the HER v2 adapter, that
`claw-cli` is rejected, and that no registry, normalization, startup, switch,
or recovery path imports or initializes the retired HER adapter. HER v2
configuration failure must fail closed; it must not activate the retired
backend. Compatible Habit, Meditation, and Dream files may be reused without
importing the old execution backend.

## 12. Test Levels and Consolidation Rules

### 12.1 Table-driven deterministic tests

Use table-driven tests for:

- lifecycle edges;
- terminal-state decisions;
- effort-policy selection;
- compulsory Replan/cadence and Review behaviour;
- retry/no-progress behaviour;
- structured normalisation rules;
- compatible response-carrier selection and ambiguity rejection;
- stop eligibility by state.

### 12.2 Contract and boundary tests

Use contract tests for:

- provider-role configuration;
- capability-based provider eligibility and Tool Gateway authority;
- Tool Registry read-only capability metadata;
- transport receipt normalisation;
- combined Finalisation and Triage-clarification Persona contracts;
- Ledger/log separation;
- reasoning-trace correlation;
- fallback-spool durability and deduplication;
- sub-agent and reviewer permissions.

### 12.3 Fault-injection integration tests

Use controlled providers, tools, clocks, log sinks, and process boundaries to inject realistic failures without depending on live third-party instability.

### 12.4 End-to-end tests

Reserve E2E tests for behaviour that crosses meaningful boundaries, such as
Immediate Response races, complete lifecycle paths, concurrent cancellation,
restart reconciliation, Review remediation, and audit failure.

### 12.5 Live observations

Use live canaries to observe model-dependent qualities such as Triage accuracy,
plan usefulness, reviewer value, latency, cost, and user-facing style.
These observations do not replace deterministic invariant tests.

## 13. Tests That Should Generally Not Be Created

Avoid:

- exact model-wording assertions;
- snapshots of long prompts without a behavioural risk;
- every optional-field combination;
- superficial paraphrase duplicates;
- tests so heavily mocked that no real runtime interaction remains;
- assertions against private methods when a stable observable boundary exists;
- tests created only for coverage percentage;
- tests of third-party behaviour HER does not control;
- deterministic assertions about subjective Review quality;
- hundreds of expensive real-model Replans when a controlled coordinator and
  a smaller end-to-end journey already prove the absence of a ceiling;
- a full Classification × Effort Cartesian suite without a named risk;
- separate tests for scenarios that differ only in irrelevant wording.
- assertions that an optional profile must have a particular literal name;
- engine-name allowlist assertions where capability is the actual boundary;
- assertions that optional Immediate Response and authoritative Triage must
  share one failure fate;
- assertions tied only to one provider's envelope or one transport receipt
  class when registered compatible shapes have the same meaning.

An exception requires a documented material risk or production regression.

## 14. Required Material Test Format

Every material architecture, integration, E2E, or production-regression test should provide:

```yaml
test_id:
title:
design_requirement_id:
design_intention:
invariant_protected:
risk_or_failure_mode:
test_level:
user_request:
classification_input_or_result:
her_effort:
initial_conditions:
injected_disturbance:
required_behaviours:
prohibited_behaviours:
permitted_terminal_states:
required_terminal_state:
ledger_expectations:
logging_and_reasoning_expectations:
user_facing_expectations:
reason_this_test_matters:
```

When multiple model outcomes are valid, express behavioural constraints:

```yaml
must:
  - preserve_user_goal
  - preserve_recorded_classification
  - maintain_lifecycle_order
  - reach_a_truthful_terminal_state

must_not:
  - allow_reviewer_to_contact_user
  - allow_sub_agent_to_replan
  - exceed_configured_limits

may:
  - complete
  - complete_with_limitations
  - fail
  - abandon_with_recorded_reason
```

Small pure component tests do not require a large YAML fixture when their risk and requirement are already clear through a shared parameter table and descriptive test identifier.

## 15. Release Gates

### 15.1 Mandatory blockers

Release must not proceed if any of the following is possible:

- user goal is replaced without `/steer` starting a new turn;
- recorded Triage classification changes mid-turn;
- Direct Response emits more than one user-facing answer;
- lifecycle order is bypassed or silently repaired;
- mandatory Planning is bypassed;
- a plan changes outside Replanning;
- Replanning changes goal or classification;
- a due compulsory Replan is suppressed, any Replan ceiling exists, or the
  Reviewed (`xhigh`) remediation boundary is exceeded;
- Review contacts the user or becomes workflow authority;
- sub-agents replan, finalise, or exceed delegated authority;
- HER bypasses Tool Gateway, permission, or workzone controls;
- `/stop` or `/steer` leaves controlled work running;
- process restart resumes an old execution stack or repeats side effects;
- required available reasoning traces are not durably audited;
- total audit-persistence failure permits external side effects to continue;
- Ledger becomes a duplicate audit log;
- completed work is discarded because Review, commentary, or Reporting failed;
- a workflow or lifecycle event synthesises Persona content without a validated
  eligible source message, or authors Persona commentary directly;
- commentary or Triage-clarification packaging can observe unmarked Agent
  instructions or anything beyond one eligible source message and the explicit
  Persona block, or Finalisation can observe unmarked Agent instructions;
- combined Finalisation changes a valid Execution disposition, loses validated
  facts, or changes required-delivery semantics or stable delivery identity;
- raw provider or runtime commentary can bypass Persona packaging into the
  Telegram commentary lane;
- duplicate commentary event IDs can produce duplicate user delivery;
- false progress keeps stalled execution alive indefinitely;
- `ERROR`, `FAILED`, `STOPPED`, or another terminal state is materially confused;
- Provider or model names are hard-coded into HER orchestration policy;
- Auto Compact hard-codes Quick/Fast, mutates protected context or raw history,
  commits partial/unvalidated output, loses a concurrent append, or applies its
  Tier 2/Tier 3 deadline outside the isolated tool-free compactor call;
- Auto Compact failure, timeout, retry exhaustion, or an estimated-token
  threshold blocks the current selected-model call or fails without a mandatory
  user-visible warning;
- Auto Compact introduces Gemini session state, silently switches the target
  route, or changes the initial OpenRouter/DeepSeek unbounded tool-loop
  contract;
- Habits, Meditation, or Dream acquire user-goal or live-workflow authority.

### 15.2 Non-blocking production observations

The following should be measured but are not deterministic release gates by themselves:

- Triage accuracy on ambiguous prompts;
- average plan usefulness;
- average Replan value;
- reviewer usefulness;
- commentary quality;
- Habit usefulness;
- latency and provider cost;
- user satisfaction and response style.

Material observed defects must become focused regression tests.

## 16. Production Regression Policy

Every material production defect should create one permanent regression scenario that reproduces the architectural failure rather than only the wording or data that exposed it.

```yaml
issue_id:
date:
description:
root_cause:
design_requirement_id:
invariant_violated:
expected_behaviour:
test_case_reference:
```

Examples include:

- goal drift;
- classification mutation;
- duplicate Direct Response delivery;
- illegal lifecycle recovery;
- incorrect terminal state;
- missing reasoning audit;
- audit fallback duplication;
- restart repeating a side effect;
- reviewer authority breach;
- completed work discarded because of malformed output;
- valid structured control output trapped in an alternate provider carrier;
- optional presentation failure blocking authoritative execution;
- timeout false-positive or false-progress loop;
- `/stop` leaving a sub-agent running.

## 17. Measures of Testing Quality

Testing quality must not be reported primarily through:

- total test count;
- assertion count;
- lines covered;
- coverage percentage alone.

The release report should state:

- locked invariants covered;
- canonical lifecycle edges covered;
- terminal states covered;
- authority boundaries covered;
- realistic fault classes exercised;
- concurrency races exercised;
- audit paths exercised;
- compulsory Replan cadence and lack of a Replan ceiling, plus the distinct
  Reviewed (`xhigh`) and Assured (`max`) Review behaviours, verified;
- production defects converted into regression scenarios;
- scenarios where useful work survives non-critical imperfections;
- scenarios where HER correctly stops at authority, non-retryable, explicit
  stop, or no-progress idle boundaries;
- remaining observation-only risks.

Coverage percentage remains useful for locating accidental gaps, but it is not evidence that HER behaviour is correct.

## 18. Initial Minimum Correct Coverage

Before HER v2 is accepted, the suite must contain logically complete coverage of:

1. every locked runtime invariant;
2. every canonical lifecycle edge and representative illegal edges;
3. every terminal-state decision;
4. every task classification through at least one E2E or justified combined journey;
5. every HER effort through the representative routing matrix;
6. Immediate Response/Triage race ordering;
7. `/stop` and `/steer` authority;
8. process-restart no-resume behaviour;
9. Tool Gateway, permission, workzone, reviewer, and sub-agent boundaries;
10. primary and fallback reasoning-audit persistence;
11. meaningful-progress and false-progress Timeout behaviour;
12. unbounded retry with non-retryable and no-progress idle termination;
13. unbounded compulsory Replanning plus Reviewed (`xhigh`) and Assured (`max`)
    Review behaviour;
14. completed-work preservation;
15. Habits, Meditation, and Dream authority boundaries;
16. provider-neutral role configuration;
17. at least one production-like canary with validation-only workspace effects
    enabled and audited.
18. retired-HER unreachability through aliases, startup, switching, and failure
    handling.
19. runtime command separation: `/backend` never exposes `role-configured`,
    `/provider` atomically resolves Quick/Pro, `/model` defines Quick/Pro,
    independently assigns model/reasoning per effective route, and exposes the
    active Quick/Light Compact policy plus Tier 2-or-3 watchdog; `/effort`
    cannot mutate provider reasoning or Compact, and non-HER `/model` behaviour remains
    unchanged.
20. scheduled-job policy separation: cron/heartbeat prompt work always uses
    request-local Direct (`zero`) across scheduled, manual, and recovery entry
    points; legacy overrides cannot bypass it, while nudges and later user
    turns remain unaffected.
21. Auto Compact: typed capacity detection, protected authority, hierarchical
    source coverage, immutable raw retention, atomic commit/concurrency,
    truthful failure, compactor-only deadline isolation, Gemini statelessness,
    and unchanged initial OpenRouter/DeepSeek tool loops.
22. compulsory Adaptive-or-above Replanning: effort eligibility, exact 10/300
    cadence, three-question output,
    plan versioning, mandatory exactly-once Persona/fallback commentary,
    safe-boundary concurrency, 100% stop, receipt preservation, audit, and no
    Replan or workflow ceiling.

This is a list of required coverage areas, not an instruction to multiply each area into hundreds of tests.

## 19. Final Acceptance Question

For every proposed test and every release, ask:

> When models, messages, tools, schemas, providers, logs, concurrency, or timing behave imperfectly, does HER still preserve the active user goal, immutable Triage authority, lifecycle integrity, auditability, useful completed work, and the most truthful terminal state?

If a test does not help answer that question, protect a locked invariant, reproduce a material risk, or prevent a known regression, it is probably unnecessary.
