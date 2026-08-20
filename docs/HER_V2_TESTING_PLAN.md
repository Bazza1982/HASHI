# Hashi Engine Runtime v2

## Formal Testing Plan

| Field | Value |
|---|---|
| Status | Approved testing baseline |
| Version | 1.0 |
| Date | 2026-08-20 |
| System | Hashi Engine Runtime (HER) v2 |
| Testing approach | Intent-based, risk-focused, and scenario-driven |
| Governing design | [HER v2 Product Requirements and Technical Design](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md) |
| Implementation baseline | HASHI `origin/main` at `604b826ed0dbb8cb748a617cbcf4c7d0dd7406f4` |

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
- Replanning triggers and limits;
- Review and remediation limits;
- Finalisation and reporting;
- lifecycle and terminal-state selection;
- Ledger minimality and log references;
- reasoning-trace audit durability;
- retry, timeout, false-progress, cancellation, and stop behaviour;
- `/steer` as stop plus a newly triaged turn;
- process-restart reconciliation without execution-stack resumption;
- Habits, Meditation, and Dream boundaries;
- provider-neutral configuration;
- structured-output tolerance and repair.

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

- the active goal is established or clarification is requested through Triage;
- Planning, Execution, Replanning, Review, Habits, and sub-agents do not replace the goal;
- related or apparently better tasks are not substituted for the requested goal;
- reviewer findings cannot change the goal;
- all plan versions continue to reference the same turn goal.

Prohibited behaviour includes:

- rewriting the goal during Planning;
- silently broadening scope;
- treating a related objective as equivalent;
- allowing a sub-agent or reviewer to become goal authority.

### 7.2 Triage classification immutability

Tests must prove:

- Triage records exactly one valid classification;
- the classification is immutable once recorded;
- Planning, Execution, Replanning, Review, and sub-agents cannot reclassify;
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

For work classifications, tests must prove that the Immediate Response acts as a conservative acknowledgement and work continues.

For `CONFIRMATION_REQUIRED`, tests must prove that clarification is requested without duplicating an adequate request already delivered by the Immediate Response, and the turn reaches `PENDING_USER_INPUT`.

### 7.5 Classification, effort, and provider routing

Tests must prove that classification and effort remain separate concerns:

- classification describes the task;
- HER effort selects the permitted orchestration path;
- provider reasoning settings come from provider-role configuration;
- effort labels are never passed as provider reasoning settings merely because their names appear similar.

The default routing policy should prefer:

- configured lightweight execution for `SIMPLE_TASK`;
- configured premium execution for `COMPLEX_TASK`;
- configured premium orchestration for `HIGH_VOLUME_TASK`.

Tests must allow explicitly configured, policy-compliant fallback or override behaviour. They must not hard-code one provider or model name as the only valid implementation.

Representative policy combinations must cover:

| Classification | Effort | Required path |
|---|---|---|
| `DIRECT_RESPONSE` | any | Immediate Response only |
| `CONFIRMATION_REQUIRED` | any | Clarification only |
| `SIMPLE_TASK` | `low` | Direct execution, no formal Planning |
| `SIMPLE_TASK` | `high` | Planning, then lightweight-preferred execution; classification unchanged |
| `COMPLEX_TASK` | `low` | Premium-preferred execution without mandatory Planning |
| `COMPLEX_TASK` | `medium` | Planning then execution |
| `COMPLEX_TASK` | `high` | Planning and eligible Replanning |
| `COMPLEX_TASK` | `xhigh` | Planning, Replanning, one Review and at most one remediation |
| `HIGH_VOLUME_TASK` | `max` | Premium orchestration, sub-agents, and up to three Review rounds |

This representative matrix replaces a full classification-by-effort Cartesian product unless a production defect justifies an additional combination.

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

REVIEWING [FAIL and remediation available]
  -> REPLANNING
  -> EXECUTING
  -> EXECUTION_COMPLETED
  -> REVIEWING

EXECUTION_COMPLETED or REVIEWING
  -> FINALISING
  -> TERMINAL
```

A table-driven transition test must cover every allowed edge and a representative set of forbidden edges. It is unnecessary to create a separate handwritten test for every pair of unrelated states.

Tests must prove that HER never:

- silently repairs an invalid transition;
- synthesises a missing predecessor;
- reviews before execution produces a candidate;
- finalises while mandatory execution or Review handling remains active;
- bypasses mandatory Planning at `medium` or above;
- changes the active plan outside `REPLANNING`.

An invalid authoritative transition produces terminal `ERROR`.

### 7.7 Plan authority and versioning

Tests must prove:

- `medium`, `high`, `xhigh`, and `max` work turns create a plan before Execution;
- `low` may execute without a formal plan;
- exactly one plan version is active;
- Replanning creates a new version;
- the earlier version remains referenced in logs but is not accumulated into the active plan;
- only the primary HER workflow may enter `REPLANNING`;
- sub-agents and reviewers cannot activate a plan version.

### 7.8 Replanning purpose and limits

Tests must prove that Replanning:

- uses the immutable goal, classification, active plan, and current execution evidence;
- corrects execution drift without redefining the goal;
- does not consult Habits again;
- records a new plan version;
- returns to Execution rather than finalising directly.

Default safety ceilings are:

| Effort | Maximum Replans |
|---|---:|
| `high` | 50 |
| `xhigh` | 100 |
| `max` | 200 |

These limits must be tested through one parameterised counter/policy test. A real end-to-end loop should use a small configured ceiling such as one or two; the suite must not perform 50, 100, and 200 expensive model Replans merely to prove integer boundaries.

When a ceiling is reached, tests must prove only that no additional Replan begins and the latest valid plan remains intact. Continued execution, Finalisation with limitations, or another truthful terminal result may be valid according to available evidence. A test must not force one model conclusion where the design permits several.

### 7.9 Review independence and limits

Tests must prove:

- Review applies only to `xhigh` and `max` work turns;
- the Strict Reviewer is contextually independent from the task performer;
- the reviewer communicates only with the Primary Agent;
- the reviewer cannot contact the user, reopen Triage, change goal or classification, activate a plan, authorise side effects, or finalise;
- Review outcomes are `PASS`, `CONDITIONAL_PASS`, or `FAIL`;
- Primary Agent remains responsible for the outcome.

Limit tests must prove:

- `xhigh`: one Review and at most one remediation; no second Review after remediation;
- `max`: at most three Review/remediation rounds; no fourth Review;
- unavailable Review after bounded retries does not discard completed Execution or remain stuck indefinitely;
- Review technical failure is logged and Finalisation proceeds from Execution evidence.

### 7.10 Tool, permission, and workzone authority

Tests must prove:

- HER invokes tools only through the HASHI Tool Gateway;
- an unregistered or disallowed tool cannot be executed;
- HER, reviewers, and sub-agents cannot elevate their own permission mode;
- workzone and access-scope boundaries remain enforced;
- reviewer calls are side-effect-free unless the governing design explicitly changes;
- sub-agent authority is no greater than the authority delegated by the Primary Agent and orchestrator;
- tool denial becomes evidence and cannot be rewritten as successful execution.

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
| Work completed but reporting exhausted retries | `COMPLETED_WITH_REPORT_PENDING` |
| Primary Agent correctly concludes the goal was not achieved | `FAILED` |
| Technical failure prevents correct execution | `ERROR` |
| Primary Agent deliberately ends work because continuation is no longer justified | `ABANDONED` |
| User or authorised control stops the turn | `STOPPED` |
| Clarification or confirmation is required | `PENDING_USER_INPUT` |

One parameterised decision suite must cover all terminal states. Representative end-to-end scenarios must separately cover the states whose correctness depends on multi-stage evidence, including `COMPLETED_WITH_REPORT_PENDING`, `ABANDONED`, `STOPPED`, and `PENDING_USER_INPUT`.

Tests must prove:

- an unsuccessful but technically correct search is `FAILED`, not `ERROR`;
- exhausted provider availability is `ERROR`, not `FAILED`;
- Review `FAIL` does not automatically become terminal `FAILED`;
- intentional stop is `STOPPED`, not `ERROR`;
- unexpected interruption is `ERROR`, not a separate `INTERRUPTED` state;
- rejection of a request after correct execution judgement is `FAILED` unless a higher policy defines a different terminal category.

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
- reviewer reasoning;
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
- Replanning does not read Habits again;
- Meditation runs only after eligible execution and cannot modify the completed turn;
- Meditation failure does not block Finalisation or reporting;
- Dream runs outside the live critical path;
- Dream failure or unavailability does not block a user turn;
- Habits are advisory and cannot become goal, classification, plan, or tool authority.

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

- `xhigh` Review `FAIL`, one remediation, then Finalisation without a second Review;
- `max` Review rounds stopping at the configured limit;
- unavailable reviewer after retry exhaustion, with completed work preserved.

### Journey G: Completed work with reporting failure

Required:

- execution is not repeated;
- evidence remains available;
- reporting retries are bounded;
- terminal `COMPLETED_WITH_REPORT_PENDING`.

### Journey H: Stop and Steer

Cover concurrent tool/sub-agent work. Prove complete cancellation and distinguish `/stop` from `/steer` starting a new turn.

### Journey I: Process restart

Interrupt an active turn, restart the runtime, prove no stack resumption, and then issue a new “continue” request.

### Journey J: Audit persistence failure

Exercise primary log, fallback spool, total audit-persistence failure, and replay deduplication.

### Journey K: Tool authority denial

Attempt an unregistered tool, permission escalation, out-of-workzone access, and reviewer side effect. Prove all are blocked and audited.

### Journey L: Terminal truth

Use a compact parameterised harness plus representative E2E evidence to distinguish all terminal states, including `ABANDONED`.

## 9. Structured Output and Tolerance

Structured output imperfections must not create unnecessary hard failures, but tolerance must never weaken authority or lifecycle invariants.

The permitted handling sequence is:

1. parse normally;
2. apply an explicitly registered deterministic normalisation;
3. extract valid essential content only where unambiguous and safe;
4. retry with the required structure;
5. log the original defect and any repair;
6. produce `ERROR` only when required information remains unavailable and the stage cannot safely continue.

Candidate imperfections include:

- missing optional fields;
- approved additional fields;
- prose surrounding otherwise valid structured content;
- truncated output where bounded retry can repair it;
- explicitly approved casing or enum aliases;
- duplicated non-authoritative fields;
- correct substantive content in an invalid wrapper.

The suite must not create a case for every permutation. Each accepted repair rule requires a named compatibility reason or production regression.

Tolerance must never:

- fabricate the user goal or Triage classification;
- alter classification;
- synthesise lifecycle predecessors;
- invent a successful tool result;
- fabricate mandatory Planning completion;
- turn reviewer output into Primary Agent authority;
- silently accept unknown protocol drift.

Stage failure after repair exhaustion should be tested through a parameterised fault harness, with a small number of E2E representatives for Planning and Reporting where downstream behaviour materially differs.

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
- unsuccessful repeated structure repair;
- tool calls that produce no new evidence.

Tests must prove that false progress cannot keep a stalled turn alive indefinitely.

### 10.2 Stage and retry timeouts

Tests must cover representative Planning, Review, provider, tool, and reporting timeouts. They must prove:

- retries remain within the current stage;
- retries are bounded;
- retry does not change goal or classification;
- exhausted retry chooses the correct stage and terminal outcome;
- stage-local retry is not process-restart recovery.

### 10.3 Hard safety timeout

Tests must prove that the operational hard ceiling stops orphaned execution without redefining the user timeout as total runtime.

## 11. Commentary and User Delivery

Tests must prove:

- a successful Planning, Execution, Replanning, or Review result may carry one
  optional neutral commentary field;
- lifecycle transitions, stage-start events, failures, retries, tool telemetry,
  and finalisation do not synthesise commentary;
- missing, empty, malformed, or oversized optional commentary does not affect
  stage validation or workflow outcome;
- runtime passes neutral commentary through a commentary port and has no
  Persona source, renderer, packaging prompt, or Telegram dependency;
- Persona packaging receives only neutral commentary and the exact contents of
  the configured `<!-- HASHI:PERSONA:BEGIN -->` marker block;
- content outside that marker block cannot reach the packaging model;
- missing or invalid markers and packaging failure use the deterministic
  display-name + `您` fallback;
- packaging occurs before delivery and concurrent/replayed event IDs are
  delivered at most once;
- the Telegram commentary boundary accepts packaged commentary and rejects a
  raw commentary string;
- optional commentary failure is logged but does not fail execution;
- `/verbose` changes presentation, not workflow authority;
- a final answer is not delivered before Finalisation;
- `DIRECT_RESPONSE` is the sole exception because its Immediate Response is the final answer;
- a transport without explicit initial-resolution capability never receives a
  provisional Immediate Response and therefore cannot duplicate the final;
- reporting failure follows the dedicated retry and terminal policy.

Exact wording should not be asserted unless a safety, authority, or protocol requirement depends on it.
Immediate Response, clarification, and Final Report are outside the first
commentary-packaging release and retain their dedicated delivery tests.

### 11.1 Retired backend isolation

Tests must prove that `her`, `claw-cli`, and `her-v2` all resolve to the HER v2
adapter and that no registry, normalization, startup, switch, or recovery path
imports or initializes the retired HER adapter. HER v2 configuration failure
must fail closed; it must not activate the retired backend. Compatible Habit,
Meditation, and Dream files may be reused without importing the old execution
backend.

## 12. Test Levels and Consolidation Rules

### 12.1 Table-driven deterministic tests

Use table-driven tests for:

- lifecycle edges;
- terminal-state decisions;
- effort-policy selection;
- Replan and Review counters;
- retry limits;
- structured normalisation rules;
- stop eligibility by state.

### 12.2 Contract and boundary tests

Use contract tests for:

- provider-role configuration;
- Tool Gateway authority;
- Ledger/log separation;
- reasoning-trace correlation;
- fallback-spool durability and deduplication;
- sub-agent and reviewer permissions.

### 12.3 Fault-injection integration tests

Use controlled providers, tools, clocks, log sinks, and process boundaries to inject realistic failures without depending on live third-party instability.

### 12.4 End-to-end tests

Reserve E2E tests for behaviour that crosses meaningful boundaries, such as Immediate Response races, complete lifecycle paths, concurrent cancellation, restart reconciliation, Review remediation, and audit failure.

### 12.5 Live observations

Use live canaries to observe model-dependent qualities such as Triage accuracy, plan usefulness, reviewer value, latency, cost, and user-facing style. These observations do not replace deterministic invariant tests.

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
- 50, 100, or 200 real model Replans merely to prove counters;
- a full Classification × Effort Cartesian suite without a named risk;
- separate tests for scenarios that differ only in irrelevant wording.

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
- Replan or Review limits are exceeded;
- Review contacts the user or becomes workflow authority;
- sub-agents replan, finalise, or exceed delegated authority;
- HER bypasses Tool Gateway, permission, or workzone controls;
- `/stop` or `/steer` leaves controlled work running;
- process restart resumes an old execution stack or repeats side effects;
- required available reasoning traces are not durably audited;
- total audit-persistence failure permits external side effects to continue;
- Ledger becomes a duplicate audit log;
- completed work is discarded because Review, commentary, or Reporting failed;
- a workflow or lifecycle event directly invokes Persona packaging or authors
  Persona commentary;
- Persona packaging can observe unmarked Agent instructions, the user request,
  plans, reasoning traces, or execution evidence beyond the neutral commentary;
- raw provider or runtime commentary can bypass Persona packaging into the
  Telegram commentary lane;
- duplicate commentary event IDs can produce duplicate user delivery;
- false progress keeps stalled execution alive indefinitely;
- `ERROR`, `FAILED`, `ABANDONED`, `STOPPED`, or another terminal state is materially confused;
- Provider or model names are hard-coded into HER orchestration policy;
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
- Replan and Review limits verified;
- production defects converted into regression scenarios;
- scenarios where useful work survives non-critical imperfections;
- scenarios where HER correctly stops at a genuine hard boundary;
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
12. bounded retry and repair exhaustion;
13. Replan and Review limits;
14. completed-work preservation;
15. Habits, Meditation, and Dream authority boundaries;
16. provider-neutral role configuration;
17. at least one production-like canary with safe side effects disabled.
18. retired-HER unreachability through aliases, startup, switching, and failure
    handling.

This is a list of required coverage areas, not an instruction to multiply each area into hundreds of tests.

## 19. Final Acceptance Question

For every proposed test and every release, ask:

> When models, messages, tools, schemas, providers, logs, concurrency, or timing behave imperfectly, does HER still preserve the active user goal, immutable Triage authority, lifecycle integrity, auditability, useful completed work, and the most truthful terminal state?

If a test does not help answer that question, protect a locked invariant, reproduce a material risk, or prevent a known regression, it is probably unnecessary.
