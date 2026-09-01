# Hashi Engine Runtime v2

## Product Requirements and Technical Design Specification

| Field | Value |
|---|---|
| Status | Approved design baseline |
| Version | 1.3 |
| Date | 2026-08-24 |
| Product | Hashi Engine Runtime (HER) |
| Implementation baseline | HASHI `her-v2` at `cc010d11d69b4eb24c62c134dc57ac62ea42c277` |

> **Current product-surface override (2026-08-31):**
> [HER_V2_THREE_MODE_DECISION.md](HER_V2_THREE_MODE_DECISION.md) supersedes this
> document wherever it describes user-selectable HER modes. Production exposes
> only Direct (`zero`), Strategic (`low`), and Planned (`medium`). References to
> Adaptive, Reviewed, and Assured below describe retained dormant implementation,
> not current product choices.

## 1. Purpose

Hashi Engine Runtime (HER) is a provider-neutral orchestration and execution framework for agentic AI systems.

HER separates the following concerns from the underlying model provider:

- task classification;
- execution policy;
- planning;
- execution;
- replanning;
- review and remediation;
- finalisation and reporting;
- operational learning through Habits, Meditation, and Dream.

HER is designed to work with any provider that can offer one or more suitable model profiles, including lightweight and premium models with provider-specific reasoning controls. Examples include DeepSeek, OpenAI, Anthropic, Gemini, xAI, OpenRouter-routed models, and future providers.

HER does not optimise for a single measure such as speed or maximum quality. It selects an orchestration policy appropriate to the request:

- a greeting should receive an immediate response;
- a simple tool task should execute efficiently;
- a complex task should be planned and executed methodically;
- a high-volume task should use orchestration and parallel execution where beneficial.

## 2. Re-engineering Objective

HER v2 replaces the current tightly coupled HER workflow with a smaller, modular runtime built around:

- an authoritative triage decision;
- an explicit lifecycle state machine;
- a lightweight execution ledger;
- independently replaceable stages;
- provider-neutral model selection;
- HASHI-owned tools, permissions, delivery, logging, and audit;
- conversational recovery instead of restoring a failed execution stack.

HER v2 is the sole supported HER execution backend. The former monolithic HER
implementation is retired and must never be selected as an initialization,
switching, preflight, recovery, or runtime fallback. The historical public ID
`her` resolves forward to `her-v2`; the unrelated `claw-cli` ID is removed and
rejected rather than retained as an alias.

## 3. Core Principles

### 3.1 Separation of concerns

HER execution modes are orchestration policies. They are not provider reasoning levels.

Provider reasoning controls remain provider-specific. HER selects the appropriate model profile and provider reasoning setting for each stage without redefining those provider controls as HER execution modes.

### 3.2 Provider neutrality

All deployment-varying route and authority parameters must be configurable,
including:

- provider and model names;
- model role profiles;
- provider reasoning settings;
- target-model context capacity and Auto Compact maintenance policy;
- the meaningful-progress idle window;
- review limits;
- tool and permission policies.

The compulsory Adaptive-or-above Replanning cadence is deliberately not a
deployment-varying parameter: it is fixed at 10 completed results or 300
seconds and has no count limit.

#### 3.2.1 No unauthorised execution ceilings

HER v2 has no turn-count, elapsed-time, wall-clock, stage, provider-attempt,
tool-round, sub-agent-count, call-count, step-count, cumulative-token, or
output-token execution ceiling at any stage. This rule applies equally to
Immediate Response, Triage, Planning, Execution, Replanning, Review,
Finalisation, Persona packaging, Meditation, Dream, delegated sub-agents,
provider adapters, and tool-enabled provider loops. A provider operation must
never be defined so broadly that a transport timeout silently becomes a clock
around model generation, foreground tool execution, and subsequent model
continuation.

The only already-authorised controls that may stop or bound work are:

- an explicit user `/stop`, `/steer`, cancellation, or process-lifecycle stop;
- the configured meaningful-progress idle detector in section 19;
- the semantic cognitive-control boundary in section 3.2.2, which withholds
  ordinary tools only after three identical action/result cycles and asks the
  active model to finalise, declare a blocker, or record a distinct hypothesis;
- a connection, read-inactivity, or protocol-safety guard scoped inside the
  relevant transport/parser operation, which resets on qualifying activity and
  never encloses a complete HER stage or tool loop;
- a timeout explicitly supplied for one tool invocation by its authorised
  caller, where omitting the timeout means no default tool deadline;
- the isolated, tool-free Auto Compact model-call watchdog explicitly defined
  in section 19.4 and the
  [Auto Compact design](HER_V2_AUTO_COMPACTION_DESIGN.md); this exception cannot
  enclose a HER stage, target provider call, tool execution, or provider tool
  loop;
- the compulsory Replanning cadence in section 8.5, which applies to Adaptive
  (`high`) and above, runs at the next safe Execution boundary after 10
  completed results or 300 monotonic seconds, and never caps results, elapsed
  runtime, provider attempts, active tools, Replans, or completion;
- the single Review-driven remediation round for Reviewed (`xhigh`); Assured
  (`max`) instead has no Review/fix round ceiling and continues until `PASS` or
  `CONDITIONAL_PASS`; compulsory Replanning has no count ceiling; and
- exactly one safe fresh-connection recovery after an eligible provider
  failure, as defined in section 18. This is a recovery allowance after a typed
  failure, not permission to time-limit a healthy attempt.

No implementation, adapter option, compatibility field, test fixture, or audit
label may introduce an equivalent limit under another name such as `budget`,
`lease`, `window`, `deadline`, `max_tokens`, `max_turns`, or `max_loops`.
Legacy HER/Claw fields representing those ceilings remain invalid HER v2
configuration and must be rejected rather than silently applied. Any new
execution limit requires explicit user authorisation and an approved amendment
to this section before implementation or supporting tests are added.

#### 3.2.2 Lifecycle-wide cognitive control

Every tool-enabled HER v2 stage may use the same provider-neutral cognitive
control. This includes Direct, tool-enabled Strategy/Triage, Planning,
Execution, Replanning, Review, and delegated execution. It is not a Planning
special case and it is not a tool-round ceiling.

When enabled, one compact `TaskState` is shared by every stage in the Turn. It
contains the resolved goal, stable completion-criterion IDs, evidence-bound
facts, open/resolved questions, focus, discarded paths, blockers, and an
optional research working model. It is a projection of task conclusions, not a
second planner or a hidden reasoning transcript. Tool-enabled model turns add
an inline `_hashi_task_delta` to the ordinary tool call; Runtime strips that
reserved field before executing the real tool, validates exact evidence refs,
and applies the delta without an additional model call. Validated stage outputs
seed the same projection for tool-free Strategy and other lifecycle stages.

The controller records only typed decisions and observable evidence. It must
never store, reconstruct, request, or expose hidden chain-of-thought. Each
tool/result observation is canonicalised without outer transport metadata,
evidence receipt IDs, or advisory repeat warnings. Semantic tool arguments and
result data—including target identifiers and requested time ranges—remain
intact. A dead cycle requires three identical periodic sequences of semantic
actions and semantic results with no positively observed state change. Repeated
actions whose results continue to change are legitimate work and must not be
interrupted.
The exact-cycle detector remains the first safety net. A second deterministic
signal compares stable TaskState progress—satisfied criteria, resolved
questions, evidence-bound facts, discarded paths, and blockers—so different
actions cannot manufacture progress merely by changing parameters or wording.
Focus, plan text, confidence, and other label churn do not count. Pure cycles
of tools explicitly classified as polling are also exempt because
an unchanged external job state is valid evidence that waiting should continue.

On the first dead-cycle observation, ordinary tools are temporarily replaced
by one internal `hashi_cognitive_decision` boundary. The same active model must
choose `FINALIZE`, `REVISE_DIRECTION`, or `BLOCKED`. A revised direction is
accepted only when it names a stable new focus, a structurally different
direction, the expected state change, an explicit stop condition, and a narrow
set of already-authorised tools. Research and diagnosis may express the
direction as a hypothesis. Only those tools reopen. If the same progress basin
returns after that intervention, the typed condition becomes
`NO_MEANINGFUL_PROGRESS`; another revision is not accepted and the model must
finalise or report the blocker truthfully. The v1 `NEW_HYPOTHESIS` payload is
accepted only as a rolling in-flight compatibility alias.

This boundary does not count arbitrary calls, impose elapsed time, reduce the
Agent's underlying permissions, launch another stage, or fork the provider
thread. It makes a decision boundary explicit inside the existing continuous
tool conversation and retains normal `/stop`, cancellation, audit, and
lifecycle authority.

Provider-specific request construction belongs in provider adapters, not in the HER orchestration core.

Provider eligibility is determined by declared capability and enforceable
isolation, never by a hard-coded engine-name list. A stage that requests tools
requires both tool-use capability and a HASHI-controlled Tool Registry. A
tool-free stage may use a backend with no tool capability; a tool-capable
backend must prove that HASHI can disable its tools. Provider/model grants
remain exact security authority after canonical identifier resolution.

### 3.3 Modularity

Triage, planning, execution, replanning, review, reporting, Ledger persistence, Habits, Meditation, and Dream must have explicit interfaces and independently testable implementations.

Removing or replacing one optional capability must not break unrelated stages. In particular:

- replanning can change without changing execution;
- review can change without changing planning;
- memory and Habit systems can evolve independently;
- Dream cannot be a required dependency of live request execution.
- optional Immediate Response failure cannot block a successful work or
  clarification Triage path;
- transport, Persona packaging, and provider envelope compatibility cannot
  become hidden execution authority.

### 3.4 Goal fidelity

Triage derives `real_goal` from the current request and relevant conversation
context according to the existing Triage prompt rules. `real_goal` is not the
legacy raw `$goal`: it is the authoritative operational goal for the turn.
After Triage validation, `state.goal` stores `real_goal`, and audit, permission,
Planning, Execution, Replanning, Review, Finalisation, and every other
downstream decision use that value. The raw request remains request evidence,
not the runtime goal.

The active `real_goal` is the highest authority for a turn. Planning,
execution, replanning, and review exist only to fulfil that goal. This design
does not add a separate rule that `real_goal` may not expand user authority;
the existing Triage derivation rules remain authoritative and are not narrowed
by a new post-hoc restriction.

No stage may intentionally substitute a different objective. If intent is unclear or material authority is missing, Triage must classify the request as `CONFIRMATION_REQUIRED`. If execution later discovers information or authority that Triage could not have known was missing, Execution must ask the concrete question truthfully in its natural-language response without changing the recorded classification.

### 3.5 Execution continuity

HER prefers useful progress over perfection of intermediate artefacts:

- give an eligible transient provider failure one fresh-connection recovery
  attempt, subject to the side-effect replay rules in section 18;
- route every model-authored JSON/schema rejection through the isolated,
  tool-free JSON Repair specialist, bounded by the user meaningful-progress
  idle boundary;
- replan when execution evidence invalidates the current approach;
- do not fail because an optional commentary message was not delivered;
- do not discard completed work because reporting failed;
- accept an incomplete Ledger when a turn terminates unexpectedly;
- preserve detailed evidence in HASHI logs rather than expanding the Ledger into an audit database.

Lifecycle order remains strict even when stage content is flexible.

### 3.7 Structured-response compatibility membrane

Provider transport shape is not HER authority. Before stage schema validation,
HER applies one deterministic compatibility membrane:

1. collect provider-native parsed data and JSON objects from formal assistant
   text;
2. apply only registered wrapper, alias, list, bounded JSON-string control
   character repair, and plain-presentation normalisations;
3. validate every candidate against the same target-stage semantic schema;
4. if no formal candidate validates, inspect provider-exposed reasoning only
   for a JSON control envelope and never expose that reasoning as user text;
5. accept exactly one semantic result, deduplicating equivalent copies;
6. reject multiple conflicting valid results as ambiguous;
7. audit the selected carrier and every rejected candidate without changing
   goal, classification, evidence, permissions, or lifecycle authority.

Reasoning recovery is therefore a bounded carrier fallback, not permission to
infer a classification from prose. Plain text is accepted directly for
Immediate Response, Primary Execution, and Finalisation. Primary Execution and
Finalisation deliberately have no JSON envelope or structure-repair
requirement; bounded sub-agents retain their existing structured JSON contract.
JSON-string control-character repair accepts only the otherwise unchanged
object produced by a non-strict JSON decoder; it does not complete truncated
structures or infer fields. A rejected JSON stage is not called again merely
to correct its report. Runtime freezes the first response and all completed
tool receipts, then gives the JSON Repair specialist exactly three quoted data
fields: `rejected_output`, `required_schema`, and `validation_error`. The
specialist inherits the frozen source provider/model target, has no tools,
attachments, Persona, task context, or side-effect authority, and may change
only syntax or schema representation. Its repaired output is validated against
the original stage validator while the original evidence receipts remain
attached. Primary Execution is never replayed merely to repair its response. A
provider failure during main Execution may be recovered only
before any tool call or after completed, provably read-only tool calls.
Finalisation may use its one provider recovery attempt, but every attempt must
receive the same immutable Execution evidence.

### 3.6 Work, commentary, Persona packaging, and delivery

HER v2 keeps five boundaries distinct:

1. Planning, Execution, Review, and non-cadence Replanning may
   include one optional neutral `commentary` string in a successful structured
   result. Every cadence-triggered Replanning must produce one commentary with
   completion percentage, plan-change status and reason when changed, and the
   next step. Runtime deterministically reconstructs it from validated fields
   when the model omits or malforms it.
2. The commentary lane validates and forwards that string. Lifecycle events,
   state transitions, retries, failures, and tool telemetry never synthesise
   Persona messages.
3. Persona source resolution extracts only one explicit Persona marker block
   from the configured `system_md` file:

   ```text
   [persona]
   ...presentation guidance only...
   [persona_end]
   ```

   Text outside that block is unavailable to commentary/clarification packaging,
   Immediate Response, Primary Execution, and Finalisation.
4. Commentary and Triage clarification share one isolated Persona Commentary
   Agent. Their delivery boundaries remain distinct: commentary accepts only a
   typed `PackagedCommentary`, while clarification retains its typed required
   message identity and must be delivered.
5. Direct and Primary Execution render their own natural-language user response
   with the extracted Persona block. Direct (`zero`) returns the sole Direct
   response; Fast path (`low`), Planned (`medium`), and ordinary Adaptive
   (`high`) deliver the Primary Execution response directly. Reviewed (`xhigh`)
   and Assured (`max`) expose it provisionally as a labelled
   `DRAFT RESPONSE`, then Review and Finalisation replace that
   exact placeholder. A pre-execution Triage clarification uses the shared
   Persona Commentary Agent and then returns to its required-message delivery
   path because no Execution result exists.

Commentary is optional presentation, never workflow authority. A missing,
empty, malformed, oversized, packaging-failed, or delivery-failed commentary
cannot invalidate, retry, reclassify, replan, stop, or complete a stage. Missing
or invalid Persona markers use deterministic minimal guidance based on the
configured HASHI display name and the form of address `您`. A failed or invalid
combined Finalisation is a technical `ERROR` with a deterministic local report;
a failed Triage-clarification Persona edit preserves the validated question and
cannot change workflow state.
When the Persona block is unavailable to Immediate Response, its prompt uses
the same configured display name and polite form of address `您` as its entire
fallback Persona guidance; it never falls back to the rest of `system_md`.

This boundary governs interim commentary packaging, Triage clarification
rendering, and the Persona inputs used by Immediate Response, Primary
Execution, and Finalisation.
Immediate Response receives the extracted block directly and, for
`DIRECT_RESPONSE`, becomes the sole final answer. Finalisation sends its
validated `final_message` directly through the required delivery path; it is
not sent through a second Persona model.
An Immediate Response may be sent provisionally before Triage only when the
transport explicitly advertises support for editing that exact message into a
final answer, clarification, or commentary. Discard is a narrower transport
capability and requires deletion support; an unsupported discard is reported as
presentation degradation and never changes workflow authority. Without edit
capability, HER keeps the response on the ordinary single final-delivery path.

## 4. Authority Model

### 4.1 User authority

The authoritative request is the user's current instruction together with the applicable conversation context and active system policy.

### 4.2 Triage authority

Triage is the sole authority for classifying a turn. Once the Triage result has been validated and recorded in the Ledger:

- the classification is immutable for that turn;
- planning may not redefine complexity;
- execution may not silently change the classification;
- replanning may change the approach but not the classification;
- review may not reopen Triage;
- a suspected misclassification is recorded as evidence but corrected only through a future turn.

This immutability is intentional. Triage quality is improved through prompt refinement, tests, and operational evidence rather than by allowing downstream stages to overrule it.

### 4.3 Plan authority

There is one active plan version at a time.

- Initial planning creates the first plan version.
- Replanning creates a new plan version only when `plan_changed=true`; an
  unchanged progress calibration preserves the active plan version.
- Earlier versions remain historical evidence in HASHI logs.
- Only the Replanning stage may replace the active plan.
- Sub-agents may not change or replace the plan.

### 4.4 Execution and Review authority

Execution owns the substantive work and truthful draft response. Applicable
Finalisation renders the reviewed user-facing outcome without rewriting the
Execution record. Review findings are advisory evidence. A reviewer cannot:

- change the user's goal;
- change the Triage classification;
- request clarification directly from the user;
- publish a user-facing final answer;
- independently authorise additional side effects.

Review receives the Registry-approved inspection tools plus the explicitly
delegated `verification_run` capability for validation in the authoritative
workspace. That exception is validation-only and cannot be widened by the
reviewer. Review may inspect and test but never remediate. A `FAIL` causes
Runtime to ask the Primary Agent to Replan and perform any correction.

The periodic cadence detector is not a model decision stage. It only observes
the fixed thresholds and safe boundaries, then unconditionally invokes the
ordinary tool-free Replanning stage. Replanning cannot reclassify, widen scope,
authorise a denied tool, or contact the user directly; it may replace the plan
only within the user's original goal and authority.

### 4.5 `/steer` authority

`/steer` is treated as stop plus new instructions.

When `/steer` is accepted:

1. the active turn and its sub-agents are stopped;
2. the old turn reaches terminal state `STOPPED` with reason `STEERED`;
3. no plan, classification, or goal from the stopped turn remains authoritative;
4. a new turn begins;
5. Triage derives the new goal from the latest relevant context together with the steer message;
6. the new goal may be a replacement, extension, or slight modification of the earlier goal.

The new turn receives a new immutable Triage decision.

### 4.6 Tool capability authority

The HASHI Tool Registry owns tool permission and safety metadata. HER requests
delegation by capability and intersects it with the Registry's current grants.
A custom or newly added tool may enter a read-only stage only when the Registry
explicitly reports it as read-only; an unknown tool fails closed. HER must not
maintain a separate tool-name safety list.

Every tool result used as assurance evidence receives an exact receipt bound to
the current stage, provider attempt, invocation, tool call, and output digest.
Only completed receipts from that exact invocation may be cited. A model-authored
reference, generic tool-use marker, stale receipt, or start event is not evidence.

## 5. HER Execution Modes

HER execution mode controls orchestration behaviour, not provider reasoning.
The existing `low` through `max` wire values remain compatible; `zero` adds the
Direct route. User interfaces show the descriptive names below.

| Display name | Wire value | Required orchestration behaviour |
|---|---|---|
| Direct | `zero` | Zero orchestration: exactly one fully capable Direct agent on the Quick model, with no Immediate Response, Triage, Planning, Replanning, delegation, Review, Verification, or Finalisation |
| Fast path | `low` | Fast execution with minimal orchestration; no formal Planning stage |
| Planned | `medium` | Formal Planning followed by Execution |
| Adaptive | `high` | Planning and Execution with compulsory Replanning every 10 completed tool results or 300 seconds at the next safe boundary |
| Reviewed | `xhigh` | Adaptive behaviour plus one independent Review; a failed Review permits exactly one Primary-Agent remediation, whose latest draft proceeds directly to Finalisation without another Review |
| Assured | `max` | Adaptive behaviour plus an unbounded Review/Replan/Execution loop against the latest draft until Review returns `PASS` or `CONDITIONAL_PASS` |

### Execution-mode terminology convention

The descriptive execution-mode name and its canonical wire value are two names
for the same policy, not separate settings. Normative prose uses both together:
Direct (`zero`), Fast path (`low`), Planned (`medium`), Adaptive (`high`),
Reviewed (`xhigh`), and Assured (`max`). A bare wire value is reserved for
schemas, configuration examples, persistence, commands, and code-level tests
where the serialized value itself is the subject. A bare descriptive name is
reserved for user-interface copy where exposing the wire value would be noise.

Terms such as "medium-or-above", "high-or-above", and "xhigh path" are legacy
shorthand. In product behaviour and acceptance criteria they mean,
respectively, Planned (`medium`) through Assured (`max`), Adaptive (`high`)
through Assured (`max`), and Reviewed (`xhigh`). They must not be interpreted as
task risk, classification, model capability, or provider reasoning.

HER v2 does not impose a tool-call round or turn ceiling on tool-enabled
Execution or delegated sub-agent invocations. Once tools are authorised for a
stage, the provider loop continues until the model completes, the invocation
fails, or the request is cancelled. Agent-level Tool Registry permissions and
safety policy still apply, but a generic registry `max_loops` value is not a
HER v2 termination condition. Effort never changes this rule.

Execution mode determines the maximum orchestration path available. Triage classifications `DIRECT_RESPONSE` and `CONFIRMATION_REQUIRED` terminate through their dedicated paths without unnecessary planning, regardless of the selected mode.

Direct (`zero`) is a distinct pre-Triage route, not a simpler classification
and not an alias for Fast path (`low`). Task difficulty never upgrades it. The
Direct agent receives the full Primary Execution Tool Registry, including
side-effect-capable tools,
while remaining bound by the user's actual authority and scope. It inspects and
checks its own work as useful, may use relevant Habits and Skills, and returns
natural language directly. A successful provider return always closes as
`COMPLETED`, including a question asking for missing information. No
`PENDING_USER_INPUT` Direct terminal is created. Provider reasoning/tool events
may still use the normal `/verbose` lane, but no HER acknowledgement,
commentary, or draft message is generated.

Each effective task route derives its base capability from configurable role
profiles such as `lightweight`, `premium`, and `reviewer`, then independently
selects a model slot and provider reasoning. HER does not hard-code provider
model names.

### 5.1 Runtime configuration command boundary

HER v2 presents two reusable task model slots, Quick and Pro. `/provider`
selects the concrete call-provider engine that carries them. `/model` defines
those two models, independently assigns a model slot and provider reasoning to
each effective task route, and exposes Compact enablement plus its Tier 2/Tier
3 timeout policy. Compact always follows the initiating Agent's active
Quick/Light provider and model at fixed high HER effort; it has no third
provider/model path and never silently falls back to Pro or a global default.
Execution is split into Simple, Complex, and High-volume routes because
classification changes the actual profile. JSON Repair inherits its rejected
source stage's frozen provider/model target and is not a separately
configurable public route.
Direct always follows the Quick provider/model target. Its independent provider
reasoning default is `high`; an explicit Direct route-reasoning setting may
override that default. HER never passes the `zero` effort label as provider
reasoning and never silently substitutes a model or reasoning value.
`/backend` selects `her-v2` without exposing the internal `role-configured`
sentinel.

`/effort` is a separate orchestration-policy command. `reviewed` and `assured`
are accepted aliases and persist as canonical `xhigh` and `max`; the other
descriptive names are accepted in the same way. Changing the mode must not read,
infer, normalize, or persist a provider reasoning value. Conversely, changing a
provider, model slot, Compact enablement, timeout tier, or provider reasoning
setting must not change HER execution mode. Compact follows changes to the
active Quick/Light route at invocation time. Legacy `inherit_pro` and explicit
Compact records migrate to that policy without preserving a third route.
Non-HER backends retain their established `/model` behaviour.

### 5.2 Scheduled-job execution policy

Cron and heartbeat prompt work always uses HER v2 Direct (`zero`) execution
mode. Direct passes the authoritative job instruction to one fully capable
Quick-model agent without Triage pre-processing, preventing a preprocessing
stage from dropping or reframing original instruction details.

The scheduler attaches an explicit request-local context containing the job
kind, task id, and trigger (`scheduled`, `manual`, or `recovery`). The Direct
policy is compulsory and the same whether the occurrence is automatic,
manually run from Telegram or Workbench, or replayed from recovery. Per-job HER
effort overrides are not accepted by the policy. Legacy `her_v2_effort` fields
are ignored and opportunistically removed at job mutation boundaries.

This resolution is request-scoped. It must not mutate the Agent's configured
effort, and a later ordinary request must still use that configured value.
It also must not select or rewrite provider model or reasoning settings.
Ordinary user turns, delayed messages, and nudge continuations do not receive
scheduled-job context and therefore retain the Agent effort. Deterministic
scheduler actions that bypass the Agent backend, including automation,
transcript export, and HER Dream, do not consume this policy.

Legacy `her_v2_effort` values, valid or invalid, cannot block prompt dispatch,
change the fixed Direct mode, or alter global Agent state.

## 6. Stage 1: Initial Processing

Initial processing applies to every non-Direct HER turn. Direct (`zero`)
completes through the single Direct route before this stage. For all other
execution modes, two processes begin promptly and independently.

### 6.1 Immediate user response

The Immediate Response is generated by a lightweight model using a non-reasoning or lowest-overhead provider mode.

Its one complete system prompt contains the filtered current request and context,
the configured `[persona]` block, and the Immediate-specific behaviour. The rest
of `system_md` and the Bridge `/sys` packaging are not supplied to this
invocation. The model independently selects one of two presentation modes while
Triage runs in parallel:

- `DIRECT RESPONSE` answers only requests that need no new evidence, tools,
  access, planning, execution, or side effect;
- `ACKNOWLEDGEMENT` gives only a short natural receipt when substantive work
  belongs to a later workflow stage;
- neither mode exposes its internal mode choice or other metadata;
- acknowledgement must not perform or simulate work, assess feasibility,
  describe tool availability, provide preliminary findings, or imply completion.

Its purposes are to:

- respond immediately when the request can be answered directly;
- acknowledge receipt of work that will continue;
- demonstrate a conservative understanding of the request;
- avoid promising an outcome that has not yet been achieved.

The Immediate Response is a real user-facing message, not merely an internal event.

### 6.2 Triage

Before Triage, Runtime retrieves the complete candidate Habit catalogue and
renders it as `$habit_catalogue`. This restores the earlier Habit retrieval
boundary: Triage derives `real_goal` and selects `relevant_habits` from that
complete catalogue. Runtime must identify and remove any override or broken
connection that bypasses this retrieval rather than inventing a replacement
selection design.

Triage uses schema v2. Its request and validated result explicitly carry
`real_goal`, `habit_catalogue`, and `relevant_habits`; an interface that accepts
only the legacy `$goal` is invalid. Triage also produces exactly one validated
classification:

#### `DIRECT_RESPONSE`

- No tools, planning, or further execution are required.
- The Immediate Response is accepted as the completed user-facing answer.
- HER must not send a second final message.
- The Ledger is finalised and the turn becomes `COMPLETED`.

#### `SIMPLE_TASK`

- Straightforward execution is required.
- Tools may be required.
- Lightweight execution models are preferred where capable.

#### `COMPLEX_TASK`

- Multiple steps, uncertainty, dependencies, discovery, or elevated risk are present.
- Premium execution models are normally preferred.

#### `HIGH_VOLUME_TASK`

- Execution volume is substantial.
- Parallel orchestration or multiple sub-agents may be appropriate.
- A premium model acts as orchestrator.

#### `CONFIRMATION_REQUIRED`

- Intent is unclear, authority is missing, or the action carries elevated risk requiring confirmation.
- HER requests clarification or confirmation.
- The turn becomes terminal state `PENDING_USER_INPUT`.

### 6.3 Immediate Response and Triage race handling

The Immediate Response and Triage may finish in either order. HER must enforce the following rules:

- the Immediate Response is delivered at most once;
- if Triage selects `DIRECT_RESPONSE`, that response is the only user-facing completion message;
- if Triage selects a work classification, the response acts as acknowledgement and execution continues;
- when Triage finishes first for a work classification, work starts immediately
  without awaiting Immediate Response, but the pending response is not cancelled
  merely because it lost the race;
- a Triage-late Immediate Response is delivered exactly once as acknowledgement
  when it becomes ready while work is still active;
- if the final report, an authoritative clarification, stop, or other terminal
  resolution supersedes a still-pending Immediate Response, HER cancels or
  suppresses it and records the supersession instead of reporting a stage
  failure;
- if Triage selects `CONFIRMATION_REQUIRED`, the clarification request must not duplicate information already adequately requested by the Immediate Response;
- Triage remains mandatory and authoritative;
- work and clarification paths do not wait for a still-pending Immediate
  Response and do not fail when that optional response is malformed or
  unavailable;
- `DIRECT_RESPONSE` still requires valid Immediate Response content because it
  is the sole final answer.

## 7. Stage 2: Planning

Planning is mandatory for Planned (`medium`), Adaptive (`high`), Reviewed
(`xhigh`), and Assured (`max`) work turns. It is not used for
`DIRECT_RESPONSE`, `CONFIRMATION_REQUIRED`, or ordinary Fast path (`low`)
execution.

Planning uses a premium model with a high provider reasoning setting and considers:

- the immutable Triage result;
- the authoritative `real_goal`;
- the Triage-selected `relevant_habits`;
- scope and constraints;
- success criteria;
- current conversation context;
- relevant historical context;
- advisory Habits;
- available providers, models, tools, permissions, and sub-agents;
- recovery and meaningful-progress strategy;
- testing and verification strategy;
- parallelisation opportunities;
- Review requirements implied by HER execution mode.

The completed plan becomes binding. Only Replanning may replace it.

When planning completes, HER records the plan reference and sends an appropriate progress update. Failure to deliver that optional progress message does not fail the stage.

Planning failures are technical failures. Valid examples include:

- invalid structured output that remains unrepaired at the no-progress idle
  boundary or ends in a non-retryable provider failure;
- provider or network failure;
- schema validation failure;
- meaningful-progress idle expiration.

“The model could not think of a plan” is not a distinct valid failure category. It must resolve to a technical error, a request for required user input already identified by Triage, or a concrete plan.

## 8. Stage 3: Execution

Execution follows the active plan when a formal plan exists. Fast path (`low`)
execution follows a minimal execution directive derived from the immutable
Triage result.

Primary Execution uses one dedicated, dynamically rendered HER v2 system
prompt. It receives the complete request context, optional active plan, the
authoritative `real_goal`, Triage-selected `relevant_habits`, actual narrowed
tool catalogue, and only the explicit Persona guidance. It
performs the work and returns a non-empty natural-language user response. It
must distinguish completed, incomplete, failed, blocked, and unverified work
truthfully, but it does not return a machine disposition or JSON envelope.

A non-empty natural-language response means the Execution workflow ended
normally and is recorded as `COMPLETED`; it does not mean every requested
objective necessarily succeeded. Provider, permission, tool-infrastructure,
and empty-response failures that Runtime can identify remain technical
`ERROR`. Those failures retain the existing replay-safe transient recovery,
and Runtime logs and renders the complete final error after recovery is
exhausted. Execution cannot request Replanning; HER v2 imposes Replanning only
through the configured execution-mode assurance policy.

### 8.1 Simple tasks

- Prefer a lightweight capable model.
- Use only the tools and permissions required by the task.
- Continue until completion, an authorised stop, an unrecoverable technical error, or a justified unsuccessful outcome.

### 8.2 Complex tasks

- Prefer a premium model.
- Follow the active plan and record material evidence.
- Emit measurable progress through tool activity, stage transitions, meaningful Ledger transitions, or user commentary.

### 8.3 High-volume tasks

- A premium model owns orchestration.
- Sub-agents may use lightweight or premium models according to assigned work.
- The orchestrator owns task decomposition, assignment, monitoring, result aggregation, and compliance with the active plan.
- Sub-agents may execute and return evidence but may not replan or alter the goal.

Every bounded sub-agent receives `real_goal` and `relevant_habits` in its
handoff. Its prompt contract is defined by the single asset
`system_sub_agent.txt`. That asset owns the bounded assignment, authority,
goal, plan, relevant Habits, prior delegated results, and output schema; no
second sub-agent prompt asset, catalogue entry, loader, renderer, or call-site
wiring exists.

### 8.4 Execution-discovered user input

When Execution discovers missing user input, its natural-language response must
state the concrete question and the truthful reason work cannot safely
continue. This is a normally completed Execution response, not a technical
error. Bounded sub-agents retain their structured contract and may not contact
the user; they return the missing-information finding to the Primary Agent as
evidence.

### 8.5 Compulsory safe-boundary Replanning cadence

Every Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`) Execution cycle
installs one request-local cadence coordinator after Execution starts. Fast path
(`low`) and Planned (`medium`) do not. Eligibility is determined only by the
selected execution mode.

A Replan becomes due at the first inclusive threshold of 10 newly completed
Tool Gateway receipts or 300 monotonic seconds in the current window.
Successes, completed tool errors, and policy denials count once by exact receipt
identity; starts, incomplete calls, cancellations, duplicates, and
non-Execution activity do not.

Due state is observed before admitting a new tool, after recording a completed
tool result, and when the provider offers an Execution completion candidate.
The coordinator closes new admission, lets already-active tools settle, and
unconditionally enters `EXECUTING -> REPLANNING`. It does not ask a checkpoint
model whether Replanning should occur. The result that made the cadence due is
preserved, and the 11th action cannot start first. Crossing 300 seconds never
cancels an active tool or provider operation. A completion candidate cannot
bypass a Replan that became due before that safe boundary.

Each Replanning call performs the three-question calibration in section 9.
A materially changed plan activates a new plan version; an unchanged progress
calibration preserves the active version and its in-flight work bindings. Every
call publishes exactly one Persona-rendered commentary with a stable checkpoint
ID.
If completion is below 100%, HER returns to `EXECUTING`, starts a fresh
10-result/300-second window, and continues from current evidence without
replaying completed side effects. If completion is 100%, HER stops adding work
and routes through Review when the effort requires it, otherwise through
Finalisation.

Immediate Tool Gateway denial, approval, missing authority, `/stop`, `/steer`,
audit failure, and cancellation retain their existing authority. The Primary
Agent and bounded sub-agents share one coordinator within an authoritative
Execution cycle. A later Review remediation starts a fresh Execution cycle.
The cadence does not consume the xhigh Review-remediation allowance, has no
Replan-count ceiling, and does not impose a time, token,
turn, tool-round, provider-attempt, or whole-workflow limit. The normative
detailed contract is the
[HER v2 Compulsory Replanning Repair Plan](HER_V2_COMPULSORY_REPLAN_REPAIR_PLAN.md).

## 9. Stage 4: Replanning

Replanning is available only for Adaptive (`high`), Reviewed (`xhigh`), and
Assured (`max`). Eligibility is an execution-mode policy, not a second
classification or risk gate. Once Execution has
started, every 10-result/300-second threshold unconditionally invokes this
stage at the next safe boundary. A `SIMPLE_TASK` remains immutably classified
as simple while Replanning may adjust its approach.

Its purpose is to restore alignment with the immutable user goal and Triage classification when execution evidence shows that the active approach is no longer adequate.

Every compulsory Replanning call answers and validates three questions:

1. **How complete is the original user goal?** Return an evidence-based integer
   `completion_percent` from 0 through 100 and its basis. Below 100 means
   authorised work remains. At 100, stop adding work and proceed to Review or
   Finalisation. Never under-do, over-do, or perform work outside authority.
2. **Is the current plan still suitable?** Compare the active plan with recent
   results, failures, constraints, and changed conditions. Return the complete
   plan, `plan_changed`, and a concrete `change_reason` only when changed. A
   changed plan activates a new version; an unchanged calibration preserves the
   active version. The immutable user goal remains authoritative.
3. **What should the user be told now?** Return a concise commentary containing
   the completion percentage, whether the plan changed, why when it changed,
   and the next step. Persona packaging must preserve protected facts; model or
   Persona failure uses the existing deterministic Agent display-name fallback
   under the same stable checkpoint commentary ID.

Replanning considers:

- the original request;
- the immutable Triage result;
- the active plan;
- completed and remaining work;
- tool and execution evidence;
- failures and newly discovered constraints;
- reviewer failure reasons when remediation follows Review.

Replanning does not consult Habits again. Current execution evidence takes precedence over historical advice.

The Execution cadence is fixed at 10 completed tool results or 300 monotonic
seconds, whichever occurs first. Review failures may also invoke Replanning for
their effort-specific remediation paths. There is no
Replanning attempt deadline, token budget, turn/loop ceiling, or total Replan
count limit, and periodic Replans never exhaust assurance remediation.

## 10. Habit System

Habits contain accumulated operational experience such as:

- common mistakes;
- successful execution patterns;
- preferred approaches;
- known pitfalls.

Habits are advisory inputs selected by Triage. They are never user intent,
execution evidence, or authority.

Habit loading is enabled by the single Habit–Meditation switch. Before Triage,
Runtime retrieves the complete valid candidate catalogue as
`habit_catalogue`; Triage selects `relevant_habits` against `real_goal`.
`real_goal` and the selected `relevant_habits` then remain explicit in the
prompts, runtime state, and handoffs for Planning, Execution, Replanning,
Review, and Finalisation. Downstream stages do not redo catalogue retrieval or
selection. This also applies to Fast path (`low`) when it bypasses Planning.

When the capability is disabled, or the request is marked
`habit_learning_eligible=false` or ephemeral, the runtime does not read the
catalogue, add Habit-specific Planning context, queue Meditation, or write a
learning audit that implies those actions occurred. Retrieval failure is
fail-open and does not create a synthetic `habit_planning_skipped` event.

Priority is:

`User intent > current execution evidence > Habits`

### 10.1 Meditation

Meditation runs after eligible execution cycles and creates candidate Habits from experience. It must not block final reporting or change the completed turn.

The Meditation stage role is configurable and may use any granted provider
profile. Its safety and independence come from its stage context, tool-free and
side-effect-free authority envelope, and separate background lifecycle—not
from a profile being literally named `lightweight`.

Meditation is turn-based: its durable prompt contains the bounded current
request, truthful Execution summary, evidence references, limitations, and
terminal state for that completed turn, not Bridge conversation background.
Reasoning traces and provider/tool audit detail remain in the audit boundary.
Acceptance by the ordinary final-delivery boundary and terminal persistence
precede the background model call; the later transport receipt remains separate
audit truth. A stable job identity deduplicates repeated scheduling for one
turn, while distinct turns that reuse a request ID remain independent.

If Meditation returns invalid JSON or violates its closed action schema, the
next model call is the shared JSON Repair specialist—not another Meditation
call. The durable source output is retained and the repaired JSON is passed
through the same deterministic Meditation validator before any Habit write.

### 10.2 Dream

Dream is a background maintenance process that may:

- consolidate related Habits;
- merge duplicates;
- remove obsolete Habits;
- resolve conflicting guidance;
- promote useful candidates.

Dream is outside the critical live-execution path.

If Dream returns invalid JSON or violates its closed catalogue-change schema,
the next model call is the same shared JSON Repair specialist—not another Dream
maintenance call. The repaired proposal still passes the full Dream validator,
catalogue fingerprint check, journal, and atomic commit boundary.

## 11. Stage 5: Review and Remediation

Independent Review applies to Reviewed (`xhigh`) and Assured (`max`) after
Execution has produced candidate deliverables.

The reviewer uses a premium model with the maximum appropriate provider
reasoning setting and an independent reviewer role. Review independence is
achieved through prompt, role, and context separation; a different model
provider is not mandatory. The invocation has Tool Gateway access for
inspection and validation, but cannot modify or remediate the work.

The reviewer receives:

- the authoritative resolved goal (`real_goal`);
- the Review kind, active `plan_id`, and any prior findings that a closure
  Review must independently reassess;
- the natural-language `draft_response`, structured Execution record, existing
  evidence references, and resulting deliverables;
- the exact Registry-approved Review tool catalogue.

`system_review.txt` is the sole normative prompt asset for the Reviewer model.
Runtime renders all of the inputs above into that one asset, installs it as the
isolated system prompt, and sends the authoritative goal as the data-only user
turn required by the provider. There is no generic `stage_request.txt` wrapper,
catalogue key, renderer, or call-site. Invocation serials, HER effort, and other
runtime metadata that do not affect Review judgement remain in audit records
rather than becoming a second Reviewer instruction source.

The reviewer independently inspects, tests, or verifies the result where
appropriate. It reports only evidence established from supplied context or its
own tool use, but objective tool verification is not mandatory for inherently
subjective or otherwise non-verifiable requests.

The reviewer returns one outcome:

### 11.1 `PASS`

The work is complete and quality is acceptable. Proceed to Finalisation.

### 11.2 `CONDITIONAL_PASS`

The work is substantially complete but contains disclosed limitations, caveats, risks, or minor unresolved matters. Proceed to Finalisation and report those limitations clearly.

### 11.3 `FAIL`

The work is incomplete, materially wrong, or missing required work. Runtime
passes the reason to Replanning and the Primary Execution agent for correction.

Review findings are advisory. They may trigger HER-controlled Replanning and
remediation, but they cannot rewrite the authorised goal or the truthful
Execution record. Execution owns the draft response and Finalisation owns the
reviewed user-facing report.

Review limits are strict:

- Reviewed (`xhigh`): one independent Review; on `FAIL`, exactly one
  Review-driven Replan/Execution remediation opportunity, then Finalisation of
  the latest post-repair draft without a closure Review;
- Assured (`max`): every `FAIL` triggers Replanning and Primary Execution,
  followed by a fresh Review of the latest draft. No fixed Review/fix round
  limit applies; the loop ends at `PASS` or `CONDITIONAL_PASS`.

Runtime-only provider or tool infrastructure failure is not a model-authored
Review classification and cannot cause an endless max-effort loop.

## 12. Review validation tool contract

There is no separate Verification stage. Independent Review receives the
validation capabilities needed to assess the latest Execution draft and
current workspace state. It cannot remediate, contact the user, or widen its
own tool set. Its delegated catalogue may include:

- `workspace_inspect` for bounded status, diff, search, hash, artifact, and
  before/after snapshot evidence;
- `verification_run` for a configured recipe or direct process `argv`. Commands
  run in the authoritative current workspace without copying or sandboxing it.
  They inherit the HASHI process identity, filesystem access, environment,
  `HOME`, and network access. `argv` is executed without an implicit shell.

All former Verification prompt assets are deleted. Runtime must not catalogue,
load, render, assemble, repair, or invoke a Verification prompt. All call sites,
schemas, tests, configuration, and documentation that imply such prompt wiring
must be removed or migrated to the Review contract above; deleting only the
files is insufficient. The validation tool name `verification_run` does not
denote a Verification model stage or prompt.

Runtime records cumulative wall-clock time across all authoritative Execution
attempts, including high-volume sub-agents and remediation. The default
effective timeout for a Review `verification_run` call is:

`max(configured, requested, 300s, execution elapsed × 1.5 + 300s)`

The reviewer may request more time but cannot reduce that result. The minimum
timeout is five minutes; configuration cannot reduce the execution multiplier
below 1.0 or the grace below 60 seconds. Thus a one-hour Execution receives a
5,700-second default verification budget, not a fixed short deadline.

Direct validation may create ordinary caches or test artifacts. Receipts record
the workspace scope, command source and argv hash, inherited authority and
access checks, timeout inputs/effective value, exit code, elapsed time, and
cleanup. These receipts remain workflow evidence, but the Review JSON contract
contains only `status`, `reason`, and `conditions`. A subjective or inherently
non-verifiable request may receive `CONDITIONAL_PASS`; lack of objective
verification alone is not a `FAIL`.

## 13. Stage 6: Finalisation and Reporting

Finalisation applies to Reviewed (`xhigh`) and Assured (`max`), plus the
exceptional Adaptive (`high`) boundary
where compulsory Replanning proves 100% completion before Execution has
produced a natural-language response. It does not run for ordinary Fast path
(`low`), Planned (`medium`), or Adaptive (`high`); those modes deliver Primary
Execution directly.

### 13.1 Exit assessment

Primary Execution provides a truthful natural-language response. A normally
returned response means the Execution workflow completed; Finalisation must not
invent a machine disposition for it. The exceptional Replanning-completion
path may still supply its deterministic completion record because no Execution
response exists.

Finalisation considers:

- the current request and its complete supplied context;
- the authoritative `real_goal` and Triage-selected `relevant_habits`;
- the complete raw Execution output;
- the natural-language `draft_response`, or the exceptional deterministic
  completion record when applicable;
- execution and tool evidence references;
- applicable reviewer findings and unresolved limitations. For xhigh, a
  pre-repair `FAIL` is repair input rather than a current unresolved judgement
  once the single permitted repair Execution has produced a newer draft;
- the marked Persona guidance used to render `final_message`.

Review findings must be considered critically rather than accepted blindly.

### 13.2 User-facing reporting

The report communicates, as applicable:

- results achieved;
- verification performed;
- remaining limitations;
- known issues and risks;
- assumptions;
- material conditions or uncertainty established by Review;
- the final task state.

Reporting must be honest and must not claim unverified work as complete.

Finalisation is one combined model stage. Its ordinary path is one provider
call; an eligible transient provider failure permits one fresh-connection
recovery call. Both attempts receive the current request, the same latest
`draft_response` when available, the same applicable `reviewer_findings`, the
same `completion_evidence`, and only the explicit `[persona]` block from the
configured Agent system file. It never receives the rest of that file and has
no Tool Gateway.

Finalisation returns only the natural-language user-facing response. Runtime
treats that non-empty response as presentation of the normally completed
Execution workflow, not as a second outcome classifier. The response is
rendered with the supplied Persona in the same call and must preserve Markdown,
code, links, paths, identifiers, numbers, facts, uncertainty, limitations, and
clarification.

Runtime deterministically enforces material Review disclosure after that model
call. If Finalisation omits a `CONDITIONAL_PASS` condition, runtime-only
`UNAVAILABLE` or `INCONCLUSIVE` state, or an unresolved `FAIL`, Runtime appends
a concise user-facing validation note and audits the append. It does not revive
an xhigh pre-repair `FAIL` after the permitted remediation produced a newer
draft, nor a `FAIL` whose completion question Replanning resolved at 100%.

For Reviewed (`xhigh`) and Assured (`max`), Runtime first sends `DRAFT RESPONSE`
plus the exact
Primary Execution text through the user Commentary lane only when transport
proves that exact message can later be edited. After an xhigh Review failure,
the one permitted repair Execution replaces the internal draft used by
Finalisation without publishing another provisional message. Finalisation then
replaces the original placeholder with the latest formal response. If
provisional delivery or edit is unavailable, Runtime falls back to the ordinary
single final-delivery lane.

Finalisation has no JSON envelope, so the JSON Repair specialist is not part of
this path. There is no separate final Persona renderer. A provider recovery is
another attempt at the same combined Finalisation operation, not a new
reporting workflow. A Direct Response remains Persona-authored by Immediate
Response.

### 13.3 Reporting failure

For one transient provider-failure sequence, Finalisation may make at most two
attempts: the initial attempt and one eligible fresh-connection recovery.
Finalisation's natural-language output has no structured-envelope correction
loop. Neither provider attempt has an HER elapsed-time deadline. Runtime freezes
and hashes the Finalisation input before the first attempt; every provider
recovery must reuse the same Execution invocation identity,
raw output, parsed result, evidence references, Review findings, goal,
classification, permissions, provider, model, and workzone. Execution is never
called again. If recovery is unavailable or exhausted, Runtime preserves
Execution evidence, selects technical `ERROR`, and
sends a deterministic local fallback report containing the typed error code and
human-readable description.

## 14. Lifecycle State Machine

### 14.1 States

The canonical lifecycle states are:

- `RECEIVED`
- `TRIAGED`
- `PLANNED`
- `EXECUTING`
- `REPLANNING`
- `EXECUTION_COMPLETED`
- `REVIEWING`
- `FINALISING`
- terminal states defined in Section 15

### 14.2 Valid principal transitions

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

REVIEWING [XHIGH FAIL]
  -> REPLANNING
  -> EXECUTING
  -> EXECUTION_COMPLETED
  -> FINALISING

REVIEWING [MAX FAIL]
  -> REPLANNING
  -> EXECUTING
  -> EXECUTION_COMPLETED
  -> REVIEWING

EXECUTION_COMPLETED or REVIEWING
  -> FINALISING
  -> TERMINAL

FINALISING [Execution requires user input]
  -> PENDING_USER_INPUT
```

### 14.3 Strict ordering

Lifecycle transitions must follow an explicitly allowed edge. Examples of invalid events include:

- execution completion without execution start;
- review completion before an execution candidate exists;
- verification completion before an execution candidate exists;
- replanning before an active plan exists;
- plan replacement outside Replanning;
- classification replacement after `TRIAGED`.

An invalid transition is a technical `ERROR`. The turn terminates immediately. Content fields may be repaired or omitted according to stage policy, but lifecycle ordering may not be fabricated.

## 15. Terminal States

HER v2 uses the following unified terminal states:

| State | Meaning |
|---|---|
| `COMPLETED` | Required work and reporting completed |
| `COMPLETED_WITH_LIMITATIONS` | Work concluded with material disclosed limitations |
| `FAILED` | Execution ran correctly but concluded that the user's goal could not be achieved |
| `ERROR` | A technical failure prevented correct execution, including an unexpected process interruption or lifecycle violation |
| `STOPPED` | The user or authorised control path stopped the turn, including `/stop` and `/steer` |
| `PENDING_USER_INPUT` | Triage or later Execution determined that clarification, confirmation, or missing authority is required |

`INTERRUPTED` is not a separate terminal state. An intentional interruption is `STOPPED`; an unexpected technical interruption is `ERROR`.

`ERROR` and `FAILED` are distinct:

- `ERROR` means the runtime could not execute correctly;
- `FAILED` means execution operated correctly and concluded the goal was not achievable.

Reaching a terminal state concludes the turn. It does not guarantee perfect completion or user satisfaction.

## 16. Execution Ledger

### 16.1 Purpose

Each request has a lightweight Execution Ledger. The Ledger is the authoritative operational record of the turn's current lifecycle state. It supports:

- workflow control;
- current-state visibility;
- next-turn understanding;
- references to detailed evidence in HASHI logs.

The Ledger is not the complete audit record.

### 16.2 Minimal record

A representative current snapshot is:

```json
{
  "turn_id": "T001",
  "status": "EXECUTING",
  "classification": "COMPLEX_TASK",
  "plan_id": "P001-v2",
  "last_update": "2026-08-20T12:00:00Z",
  "log_refs": ["hashi-log:..."],
  "terminal_reason": null
}
```

The Ledger must not store:

- full reasoning traces;
- full planning, review, or verification output;
- complete tool request and response payloads;
- duplicated conversation history;
- a second complete audit trail.

Those records belong in HASHI orchestration logs. The Ledger stores only current control state and references.

### 16.3 Append-only truth

Historical facts are never silently rewritten. If later evidence corrects an earlier interpretation, HASHI appends a correction record and advances the current Ledger snapshot. Detailed history remains reconstructable from logs.

An incomplete Ledger is valid evidence that the turn did not reach a conclusion.

## 17. HASHI Logging and Audit

HASHI orchestration logs are the authoritative audit source. They must preserve all audit information available to HASHI and HER, including:

- user requests and steer instructions;
- Immediate Response output;
- Triage prompts, reasoning traces, output, validation, and classification;
- planning prompts, reasoning traces, plans, and repairs;
- execution prompts and available reasoning traces;
- provider requests and responses subject to secret-redaction policy;
- tool calls, tool results, permissions, and denials;
- sub-agent assignments and responses;
- replanning prompts, reasoning traces, and plan versions;
- reviewer prompts, reasoning traces, findings, and outcomes;
- verifier prompts, reasoning traces, check results, and outcomes;
- exact tool-evidence receipts with stage, attempt, invocation, call identity,
  completion status, safety classification, and output digest;
- finalisation reasoning traces and user-facing reports;
- lifecycle transitions, retries, timeouts, and errors;
- model, provider, effort, and request correlation metadata.

Reasoning-trace logging is a required HASHI audit principle. HER must capture every reasoning trace made available by the selected provider or produced as a visible/structured HER reasoning artefact. If a provider does not expose a reasoning trace, HASHI must record that it was unavailable rather than fabricate one.

Every returned provider response is persisted as `provider_response_received`
before schema validation. The record contains the redacted raw text/data,
provider/model identity, usage, evidence references, and reasoning-availability
marker. Available reasoning, or the explicit unavailable marker, is also
persisted before validation. Consequently, empty, malformed, or schema-invalid
responses remain auditable even when the stage cannot complete.

User-message delivery has two correlated records. HER first records delivery
intent with a stable `delivery_id`; the ordinary HASHI final-send boundary then
appends the actual transport receipt with that same identifier, the transport
disposition, delivery boolean, and chunk count. Acceptance into a deferred
final lane is not proof of delivery.

Logs must apply existing HASHI secret-redaction, access-control, retention, and workspace-isolation policies.

## 18. Failure, Retry, and Recovery

### 18.1 Stage-local provider recovery

HER permits exactly one fresh-connection provider recovery within the active
process and current logical stage. Recovery eligibility is selected by typed
failure and replay safety, never by an elapsed-time tier. Initial and recovery
attempts have no HER deadline. Runtime must not wrap a complete
`provider.invoke()` operation in a timeout and must not synthesize an attempt
timeout for an adapter, because a tool-capable provider invocation can include
model generation, any number of foreground tool calls, and later model
continuations.

Typed failures eligible for the one recovery are HTTP 408, HTTP 429, HTTP 5xx,
connection/DNS/reset failures, a scoped transport read-inactivity timeout, an
incomplete provider stream, an empty response, and a stream that never produces
a usable tool or final result. Configuration failures, HTTP 400, HTTP 401, HTTP
403, invalid URL or TLS configuration, audit persistence failure, and an
authorised user stop are not retried. Every terminal technical failure enters
`ERROR` with a stable error code, a redacted human-readable description,
attempt count, side-effect status, and a correlation reference.

Every provider failure and retry decision is audited, including provider/model,
typed code, failure origin, HTTP status and provider request ID when available,
activity/inactivity summary, retryability, replay-safety decision, delay, and
whether the next attempt uses a fresh connection. A retry must preserve
provider, model, goal, immutable Triage classification, role, provider
reasoning, tool and side-effect permissions, delegated tool allowlist,
workzone, and active plan. Runtime hashes those invariants and records the same
hash on both attempts. Backoff and a provider-supplied `Retry-After` value are
scheduling inputs, not attempt deadlines or permission to create a recovery
window.

Deterministic carrier recovery occurs before model repair. When a JSON/schema
defect remains, Runtime freezes the source response and receipts and invokes
the isolated JSON Repair specialist. Invalid specialist output may be repaired
again under the user idle-progress boundary; it never replays the source stage
and does not replenish the one provider-recovery allowance.

Main Execution may use the provider recovery only when no tool has started or
when every started tool is provably read-only and has completed. Unknown,
incomplete, or side-effecting tool activity blocks automatic replay. A
side-effect-authorised Execution result is never replayed merely to repair its
natural-language presentation. Read-only sub-agents receive
the same single provider recovery. Finalisation receives at most one recovery
and reuses immutable Execution evidence; the Execution invocation count remains
one.

### 18.2 No process-restart resumption

HER does not reconstruct or resume an in-flight execution stack after process restart.

If the process stops unexpectedly:

- the old turn becomes `ERROR` during reconciliation;
- its incomplete Ledger and HASHI logs are preserved;
- no old planner, executor, reviewer, verifier, or sub-agent continuation is restarted automatically.

A later user request such as “continue” starts a new turn. Its Triage stage may inspect conversation history, the previous Ledger, and HASHI logs to determine remaining work.

Recovery is conversational, not transactional.

## 19. Timeout Model

### 19.1 User timeout

The user timeout represents the maximum permitted period without measurable progress. It is not a total wall-clock runtime limit.

Measurable progress includes:

- a meaningful user commentary update;
- tool execution or a tool result;
- a genuine lifecycle or Ledger state transition;
- a replan event;
- completion of a substantive execution unit.

No-op retries, heartbeat-only Ledger writes, idle waiting, and unlogged internal loops are not progress.

### 19.2 Scoped inactivity and explicit operation timeouts

Transport operations may retain connection-inactivity, read-inactivity, and
protocol-safety guards. Each guard must live inside the narrow transport or
parser operation it protects, reset on qualifying provider activity, and never
include foreground tool execution or a complete HER stage. A blank heartbeat
does not qualify as activity. These guards classify an actual transport stall;
they are not provider-attempt clocks.

A tool may have a timeout only when the authorised caller explicitly supplies
one for that individual invocation. Omission means that no default tool timeout
is applied. An explicit tool timeout does not create or imply a turn, stage,
provider, or later-tool budget.

### 19.3 Prohibited execution limits at every stage

There is no stage timeout, provider-attempt timeout, whole-turn hard timeout,
elapsed-time budget, turn-count budget, tool-round budget, call/step budget,
sub-agent-count budget, cumulative token budget, or output-token budget in HER
v2. This prohibition applies to normal execution, recovery, presentation,
background learning, adapters, and delegated work. Meaningful work may continue
for arbitrarily long elapsed time and across arbitrarily many tool/model rounds.
Only the expressly authorised controls listed in section 3.2.1 may stop or
bound it.

### 19.4 Auto Compact maintenance-call exception

Auto Compact is HASHI-owned context capacity maintenance, not a principal HER
lifecycle stage. It invokes the initiating Agent's active HER v2 Quick/Light
provider and model at fixed high HER effort through an isolated, tool-free
maintenance boundary. It has no independent provider/model route and cannot
fall back to Pro, a global default, or a different provider. Gemini remains
stateless, and this maintenance path does not split, cap, or replace the
existing OpenRouter or DeepSeek request-local tool loops.

Manual `/compact` also owns a separate model-free WIP recovery phase. Before
evaluating conversation capacity, HASHI converts each active bounded WIP
Journal snapshot into an idempotent quoted recovery turn in the current Session
and compare-and-swap clears that Journal only after the Session write is
durable. This phase runs at any token count. A write or compare-and-swap failure
preserves the Journal and stops the ordinary conversation compaction phase.
Every later HER v2 request that sees previous-turn WIP emits a mandatory visible
warning independently of `/verbose`; only bounded deterministic recovery
context, never raw Journal JSONL or a recursively assembled provider request,
may reach the provider.

Known and unknown target capacity use the same fixed product window. Below
64,000 effective tokens, manual `/compact` reports the exact not-needed reason;
from 64,000 tokens upward it executes. Automatic Compact triggers strictly
above 128,000 tokens, targets 64,000 tokens, and is scheduled only when the
first main Execution provider invocation begins. Exactly 128,000 tokens remains
inside the manual window. Prompt assembly and post-turn handling never invoke
automatic Compact. Unknown compactor capacity continues to use conservative
32,000 estimated-token source partitions.

Execution creates a detached maintenance task and immediately continues its
already assembled provider call. Compact unavailability, lock contention,
timeout, validation failure, retry exhaustion, or a non-shrinking
result cannot pause, fail, retry, or otherwise change the foreground task. Every
unsuccessful automatic outcome emits a mandatory user-visible warning even
with `/verbose off`; warning delivery is also outside the foreground task. A
successful atomic pointer commit affects later prompt assembly only. The
provider's own capacity acceptance or rejection remains truthful and is not a
Compact gate.

Each individual semantic compactor model call may use the expressly authorised
absolute watchdog defined by a dedicated Tier 2 or Tier 3 Compact policy. Tier
1 is invalid because semantic compaction requires reasoning. Tier 3 is
available for a provider/model that declares local or slow execution. The
deadline travels only on a separate compaction request and may not be copied to
`StageRequest`, provider profiles, Persona, learning, sub-agents, target model
calls, or tools. It does not become `/timeout`, a complete compaction-job
deadline, or a clock around a provider tool loop.

Compaction is atomic and fail-safe: current authority, active task state, open
tool transactions, and required side-effect evidence remain verbatim; raw
source history remains immutable; and any timeout, cancellation, provider
failure, schema defect, or commit race leaves the active context unchanged.
The complete configuration, hierarchy, validation, persistence, provider
boundary, failure, and test contract is authoritative in the
[Auto Compact design](HER_V2_AUTO_COMPACTION_DESIGN.md).

## 20. Sub-Agent Governance

Authority is ordered as:

```text
User
  -> Primary Agent
    -> HER Orchestrator
      -> Sub-agents
```

Sub-agents may:

- execute bounded assigned tasks;
- use explicitly granted tools and permissions;
- return results and evidence.

Sub-agents may not:

- modify the active plan;
- trigger Replanning independently;
- change the Triage classification;
- change the user goal;
- communicate a final answer to the user;
- create additional sub-agents unless explicitly authorised by the orchestrator policy.

Only the primary HER workflow may enter `REPLANNING`.

## 21. Architectural Boundaries

### 21.1 HASHI-owned responsibilities

HASHI remains responsible for:

- transport and user-message delivery;
- Persona source resolution and presentation-only packaging;
- agent configuration and secrets;
- provider adapters and credentials;
- Tool Gateway registration and execution;
- permission and workzone enforcement;
- queues, cancellation, `/stop`, and `/steer`;
- meaningful-progress idle enforcement;
- typed context assembly, provider/model capacity metadata, Auto Compact
  configuration, raw-context retention, and atomic continuity-capsule commits;
- audit logging and redaction;
- Workbench and operational status;
- hot restart and process lifecycle.

### 21.2 HER-owned responsibilities

HER v2 owns:

- Triage;
- execution-mode policy resolution;
- lifecycle-state validation;
- stage orchestration;
- lightweight Ledger management;
- plan version selection;
- compulsory Replanning cadence and plan-version control;
- Review, closure, and remediation policy;
- deterministic completion of a normally returned Execution workflow, plus
  technical runtime terminal states.

HER v2 may validate and publish optional neutral commentary returned by a
successful reasoning stage. Compulsory Replanning instead requires one update
and deterministically reconstructs it from validated fields when necessary.
HASHI extracts Persona guidance and supplies only the explicit marker block to isolated presentation invocations. Applicable
Finalisation consumes that block while producing the reviewed final message in
one call. A pre-execution Triage clarification uses
the same Persona Commentary Agent as interim commentary, then returns to its
typed required-message delivery path.

HER v2 prompt prose is stored as versioned UTF-8 assets under
`orchestrator/her_v2/prompt_assets/`. The loader resolves those assets relative
to its module, never the process working directory, and validates the complete
asset inventory plus each template's exact placeholder set before a prompt may
be rendered. Missing, empty, unknown, or drifted templates fail closed during
import or hot reload.

### 21.3 Optional supporting systems

Habits, Meditation, Dream, and optional native executors connect through explicit interfaces. They cannot become mandatory hidden dependencies of the core state machine.

### 21.4 Compatibility facade

HER v2 remains registered through a thin HASHI compatibility facade. Internally
it behaves as an orchestration policy over provider and tool interfaces rather
than reproducing the retired monolithic backend design. Compatibility is limited
to forward ID aliases and approved Habit, Meditation, and Dream data formats;
it is not execution fallback compatibility.

The facade in `adapters/her_v2.py` owns HASHI lifecycle integration only.
Provider invocation, delivery, and Persona bridges live in
`adapters/her_v2_provider.py`; stage invocation/recovery and
lifecycle/delivery/audit support live in the dedicated HER v2 runtime modules.
`HERv2Adapter`, `HashiStageProvider`, and the established test-facing bridge
exports remain available from the compatibility facade.

## 22. Migration Strategy

Implementation starts from HASHI `origin/main` commit `604b826ed0dbb8cb748a617cbcf4c7d0dd7406f4` in a clean, dedicated branch and worktree.

The migration sequence is:

1. freeze this specification and canonical state-transition tests;
2. implement lifecycle types, Ledger, and transition validation without model calls;
3. implement Provider Profile and stage interfaces;
4. deliver Fast path (`low`) Direct Response and Simple Task paths;
5. add Planned (`medium`) Planning;
6. add Adaptive (`high`) Replanning;
7. add Reviewed (`xhigh`) one-round Review remediation and Assured (`max`)
   unbounded Review/remediation;
8. integrate sub-agents;
9. integrate Habits, Meditation, and Dream last;
10. certify normal-mode execution with HASHI permissions restricting canary
    side effects;
11. canary selected agents;
12. expand rollout only after certification;
13. remove every old-HER registration and prove historical IDs resolve only
    forward to HER v2.

Existing HER code, local experimental commits, and uncommitted Task Control work are reference material. They are not the implementation foundation of HER v2 and must be ported only when a v2 requirement and test justify them.

## 23. Acceptance Criteria

HER v2 is ready for production rollout only when:

- every Triage classification has deterministic transition tests;
- a recorded Triage classification cannot be mutated within the turn;
- Direct Response produces exactly one user-facing response;
- `/steer` terminates the old turn and starts a separately classified new turn;
- Direct (`zero`), Fast path (`low`), Planned (`medium`), Adaptive (`high`),
  Reviewed (`xhigh`), and Assured (`max`) policies follow the required stage matrix;
- Direct (`zero`) performs exactly one Direct call on Quick with default
  provider reasoning
  `high`, never auto-upgrades, exposes full Primary Execution tool authority and
  existing attachment fallback, invokes none of the ordinary HER stages, and
  records every successful natural-language return as completed;
- Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`) unconditionally
  enter Replanning at each inclusive 10-result or 300-second safe boundary
  after Execution begins, while Fast path (`low`) and Planned (`medium`) never
  install the cadence;
- every compulsory Replan validates completion, plan suitability/change, and
  mandatory commentary fields; activates a new plan version only when the plan
  materially changes; preserves the active version when unchanged; delivers
  exactly one Persona-rendered or deterministic fallback update; and resumes or
  stops additional work according to whether completion is below or equal to
  100%;
- no risk label, checkpoint decision, Replan count, time/token/turn/loop limit,
  provider option, or test fixture can suppress a due compulsory Replan or cap
  the whole workflow;
- cron and heartbeat prompt work always uses request-local Direct (`zero`)
  execution mode across scheduled, manual, and recovery triggers; legacy job
  overrides cannot bypass it, change provider reasoning settings, or leak into
  later ordinary turns;
- Replanning and Review loops cannot violate lifecycle order;
- Reviewed (`xhigh`) performs no more than one Review-driven remediation round
  before Finalisation, while Assured (`max`) repeats Review/Replan/Execution
  until `PASS` or
  `CONDITIONAL_PASS` without a fixed round ceiling;
- Review may run configured validation recipes or direct argv checks in the
  authoritative workspace with inherited execution authority; its effective
  tool timeout grows from cumulative Execution time and cannot be shortened by
  the reviewer;
- eligible provider failures receive no more and no less than one safe
  fresh-connection recovery; typed exclusions, invariant hashes, and
  process-restart non-resumption are enforced without an attempt deadline;
- every HER stage, adapter, presentation path, maintenance path, sub-agent, and
  tool-enabled provider loop is free of unauthorised turn-, time-, token-,
  call-, step-, tool-round-, and sub-agent-count ceilings; the isolated
  tool-free Compact call is the only provider-attempt watchdog exception and
  remains scoped exactly as sections 3.2.1 and 19.4 require;
- Ledger records remain minimal and auditable through log references;
- all available reasoning traces are logged and correlated to the turn;
- tools and permissions remain HASHI-owned;
- provider model names are configurable rather than hard-coded in HER core;
- Compact resolves the initiating Agent's active HER v2 provider and
  Quick/Light model at high HER effort, supports Tier 2/Tier 3 watchdog
  isolation, keeps Gemini stateless, and leaves ordinary provider tool loops
  unchanged;
- Auto Compact preserves protected authority and open tool truth verbatim,
  retains raw source, atomically commits only validated capsules, and otherwise
  continues with the best safely assembled context plus a mandatory warning;
- reporting failure preserves completed execution evidence;
- stop terminates primary and sub-agent activity;
- the retired HER implementation is unreachable through backend switching,
  startup preflight, and initialization failure; `her` resolves to HER v2 and
  `claw-cli` is rejected;
- lifecycle and workflow events cannot generate Persona commentary;
- commentary and Triage-clarification packaging receive no raw request, plan,
  reasoning trace, lifecycle snapshot, or unmarked `system_md` content;
- applicable Finalisation receives the current request, `draft_response`, and
  complete execution evidence but only the marked Persona block from
  `system_md`; it preserves required-delivery identity;
- commentary packaging and delivery failures cannot alter workflow outcome;
- failed HER v2 initialization fails closed or uses an explicitly selected
  non-HER backend; it never rolls back to retired HER.

## 24. Locked Runtime Invariants

The following decisions are authoritative for HER v2:

1. Triage-derived `real_goal`, stored as `state.goal`, is the authoritative
   operational expression of user intent for the active turn.
2. Triage is authoritative and immutable for that turn.
3. Planning and Replanning may not change classification or goal.
4. `/steer` stops the old turn and starts a newly triaged turn with new instructions.
5. Lifecycle order is strict; stage content is flexible.
6. Each eligible ordinary provider operation has exactly one safe
   fresh-connection recovery, but neither attempt nor any HER stage has an
   elapsed-time, turn-count, token, call, step, tool-round, or sub-agent-count
   ceiling beyond the expressly authorised controls in section 3.2.1. The
   separate tool-free Compact request follows the narrow Tier 2/Tier 3 exception
   in section 19.4.
7. Immediate Response becomes the sole final user-facing answer for `DIRECT_RESPONSE`.
8. Review may inspect and run validation-only checks but cannot remediate; it
   is advisory and never user-facing.
9. The Primary Agent owns execution and reporting.
10. The Ledger is minimal operational state; HASHI logs are audit truth.
11. All available reasoning traces must be logged.
12. Execution evidence outweighs Habits.
13. Runtime retrieves the complete Habit catalogue before Triage; Triage
    selects `relevant_habits` against `real_goal`, and both values propagate
    through Planning, Execution, Replanning, Review, and Finalisation without
    downstream catalogue retrieval or reselection.
14. Every Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`)
    10-result/300-second boundary forces Replanning; the
    detector has no `CONTINUE`, ask, or halt decision.
15. Compulsory Replanning commentary is mandatory and exactly once. Missing or
    semantically damaged model/Persona prose uses the existing Agent
    display-name deterministic fallback from validated fields without changing
    the workflow outcome.
16. Missing optional commentary in other stages does not fail execution.
17. Only successful reasoning-stage output may originate neutral commentary;
    workflow events never originate Persona speech.
18. Commentary and Triage-clarification packaging are presentation-only and
    receive one eligible message plus the explicit configured Persona block.
19. Applicable Finalisation receives the complete Execution draft/evidence and
    that Persona block, then produces the reviewed final required message
    without a second Persona call.
20. Reporting failure does not discard completed work.
21. Recovery is conversational, not transactional.
22. HER core is provider-neutral and modular.
23. Review reports only what supplied context or its own independent checks can
    establish; lack of objective verification alone may justify
    `CONDITIONAL_PASS` but not `FAIL`.
24. Triage uses schema v2 carrying `real_goal`, `habit_catalogue`, and
    `relevant_habits`; the legacy goal-only interface is invalid.
25. Bounded sub-agents use only `system_sub_agent.txt`; no second sub-agent
    prompt asset or wiring exists.
26. No Verification prompt asset or runtime prompt wiring exists; independent
    Review owns validation.
27. Review uses only `system_review.txt`; no generic `stage_request.txt` asset,
    catalogue entry, renderer, or call-site exists, and all required Review
    context is rendered explicitly into the single system prompt.
28. Review never rewrites the authorised goal or truthful Execution record;
    Finalisation preserves the Review status, reasons, and conditions.
29. Runtime deterministically discloses material conditional, unavailable,
    inconclusive, or unresolved Review state when Finalisation omits it, without
    misreporting a repaired or Replanning-resolved prior `FAIL` as current.
30. Auto Compact is HASHI-owned, tool-free, atomically reversible capacity
    maintenance; its dedicated Tier 2/Tier 3 call watchdog never becomes an
    ordinary HER stage, provider, tool-loop, or execution deadline.

## 25. Golden Rule

> Less blocking, more progress — while preserving immutable Triage authority, strict lifecycle order, complete available reasoning audit, and fidelity to the user's active goal.
