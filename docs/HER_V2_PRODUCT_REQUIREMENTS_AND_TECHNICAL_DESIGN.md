# Hashi Engine Runtime v2

## Product Requirements and Technical Design Specification

| Field | Value |
|---|---|
| Status | Approved design baseline |
| Version | 1.2 |
| Date | 2026-08-22 |
| Product | Hashi Engine Runtime (HER) |
| Implementation baseline | HASHI `her-v2` at `6f5747503f478de912553d3e2a92926a5755c41c` |

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
- replanning triggers and limits;
- review limits;
- tool and permission policies.

#### 3.2.1 No unauthorised execution ceilings

HER v2 has no turn-count, elapsed-time, wall-clock, stage, provider-attempt,
tool-round, sub-agent-count, call-count, step-count, cumulative-token, or
output-token execution ceiling at any stage. This rule applies equally to
Immediate Response, Triage, Planning, Execution, Replanning, Review,
Verification, Finalisation, Persona packaging, Meditation, Dream, delegated sub-agents,
provider adapters, and tool-enabled provider loops. A provider operation must
never be defined so broadly that a transport timeout silently becomes a clock
around model generation, foreground tool execution, and subsequent model
continuation.

The only already-authorised controls that may stop or bound work are:

- an explicit user `/stop`, `/steer`, cancellation, or process-lifecycle stop;
- the configured meaningful-progress idle detector in section 19;
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
- the fixed high-risk periodic checkpoint in section 8.5, which gates only the
  next safe Execution tool boundary after 10 completed results or 300
  monotonic seconds and never caps results, elapsed runtime, provider attempts,
  active tools, or completion;
- the Replanning, Reviewed closure, and Assured Verification/remediation
  iteration ceilings explicitly defined by HER execution-mode policy; and
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

The active user's goal is the highest authority for a turn. Planning, execution, replanning, and review exist only to fulfil that goal.

No stage may intentionally substitute a different objective. If intent is unclear or material authority is missing, Triage must classify the request as `CONFIRMATION_REQUIRED`. If execution later discovers information or authority that Triage could not have known was missing, Execution may return `USER_INPUT_REQUIRED`; the current turn then reaches `PENDING_USER_INPUT` without changing its recorded classification.

### 3.5 Execution continuity

HER prefers useful progress over perfection of intermediate artefacts:

- give an eligible transient provider failure one fresh-connection recovery
  attempt, subject to the side-effect replay rules in section 18;
- treat structured-envelope correction as a separate semantic repair path,
  bounded by the user meaningful-progress idle boundary;
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
infer a classification from prose. Plain text is accepted directly only for
Immediate Response. Finalisation must return its combined structured result.
JSON-string control-character repair accepts only the otherwise unchanged
object produced by a non-strict JSON decoder; it does not complete truncated
structures or infer fields. A retryable, side-effect-free stage receives the
previous validation defect so it can correct the envelope instead of blindly
repeating the same request. Main Execution is never replayed merely to repair
its envelope. A provider failure during main Execution may be recovered only
before any tool call or after completed, provably read-only tool calls.
Finalisation may use its one provider recovery attempt, but every attempt must
receive the same immutable Execution evidence.

### 3.6 Work, commentary, Persona packaging, and delivery

HER v2 keeps five boundaries distinct:

1. Planning, Execution, Replanning, Review, and Verification may include one
   optional neutral `commentary` string in a successful structured result.
   Internal checkpoint assessment cannot produce commentary.
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

   Text outside that block is unavailable to commentary packaging, required
   clarification rendering, Immediate Response, and Finalisation.
4. Commentary delivery accepts only the typed output of Persona packaging.
   Generic workflow delivery is not a commentary transport.
5. Finalisation is one combined model call that normalises Execution when
   necessary and renders the final message from the same result using the
   extracted Persona block. A pre-execution Triage clarification retains its
   separate typed required-message renderer because no Execution result exists.

Commentary is optional presentation, never workflow authority. A missing,
empty, malformed, oversized, packaging-failed, or delivery-failed commentary
cannot invalidate, retry, reclassify, replan, stop, or complete a stage. Missing
or invalid Persona markers use deterministic minimal guidance based on the
configured HASHI display name and the form of address `您`. A failed or invalid
combined Finalisation is a technical `ERROR` with a deterministic local report;
a failed Triage-clarification renderer preserves the validated question and
cannot change workflow state.
When the Persona block is unavailable to Immediate Response, its prompt uses
the same configured display name and polite form of address `您` as its entire
fallback Persona guidance; it never falls back to the rest of `system_md`.

This boundary governs interim commentary packaging, Triage clarification
rendering, and the Persona inputs used by Immediate Response and Finalisation.
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
- every work classification has an immutable `STANDARD` or `HIGH_RISK`
  checkpoint policy, with a required reason for `HIGH_RISK`;
- planning may not redefine complexity;
- execution may not silently change the classification;
- replanning may change the approach but not the classification;
- review may not reopen Triage;
- a suspected misclassification is recorded as evidence but corrected only through a future turn.

This immutability is intentional. Triage quality is improved through prompt refinement, tests, and operational evidence rather than by allowing downstream stages to overrule it.

Checkpoint risk is independent of task complexity and execution effort. It
does not grant tool or side-effect authority. A malformed work Triage response
that omits the policy is repaired through the normal structured-output path or
fails truthfully; Runtime never silently substitutes `STANDARD`.

### 4.3 Plan authority

There is one active plan version at a time.

- Initial planning creates the first plan version.
- Replanning creates a new plan version.
- Earlier versions remain historical evidence in HASHI logs.
- Only the Replanning stage may replace the active plan.
- Sub-agents may not change or replace the plan.

### 4.4 Execution, Review, and Verification authority

Execution owns the substantive work and its disposition. Finalisation renders
the user-facing outcome without replacing a valid disposition. Review and
Verification findings are advisory evidence. A reviewer or verifier cannot:

- change the user's goal;
- change the Triage classification;
- request clarification directly from the user;
- publish a user-facing final answer;
- independently authorise additional side effects.

Review may use only tools marked read-only by the HASHI Tool Registry.
Verification uses the same inspection set plus the explicitly non-read-only
`verification_run` capability for validation in the authoritative workspace.
That exception is runtime-delegated, validation-only, and cannot be widened by
the verifier. Findings may cause Runtime to ask the Primary Agent to remediate;
the verifier cannot perform that remediation itself.

The periodic checkpoint is a separate internal control, not Review or
Verification. It is tool-free and may only continue Execution, require one
concrete user question, or halt further admission while retaining completed
evidence. It cannot reclassify, widen scope, mutate a plan, authorise a denied
tool, finalise, or contact the user directly.

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
The canonical wire values remain unchanged for configuration and API
compatibility. User interfaces show the descriptive names below.

| Display name | Wire value | Required orchestration behaviour |
|---|---|---|
| Fast path | `low` | Fast execution with minimal orchestration; no formal Planning stage |
| Planned | `medium` | Formal Planning followed by Execution |
| Adaptive | `high` | Planning, Execution, and configurable periodic or evidence-triggered Replanning |
| Reviewed | `xhigh` | Adaptive behaviour plus one tool-backed independent Review; a failed Review permits one Primary-Agent remediation and one read-only closure Review |
| Assured | `max` | Adaptive behaviour plus one tool-backed Review and a comprehensive Verification loop against the latest state, with at most three Verification attempts |

HER v2 does not impose a tool-call round or turn ceiling on tool-enabled
Execution or delegated sub-agent invocations. Once tools are authorised for a
stage, the provider loop continues until the model completes, the invocation
fails, or the request is cancelled. Agent-level Tool Registry permissions and
safety policy still apply, but a generic registry `max_loops` value is not a
HER v2 termination condition. Effort never changes this rule.

Execution mode determines the maximum orchestration path available. Triage classifications `DIRECT_RESPONSE` and `CONFIRMATION_REQUIRED` terminate through their dedicated paths without unnecessary planning, regardless of the selected mode.

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
classification changes the actual profile. Structure repair may follow its
source model.
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

Cron and heartbeat prompt work defaults to HER v2 `low` effort. These jobs
already persist their execution specification, so repeating formal Planning or
independent Review on every routine occurrence is unnecessary by default.

The scheduler attaches an explicit request-local context containing the job
kind, task id, and trigger (`scheduled`, `manual`, or `recovery`). An optional
job field, `her_v2_effort`, may override the default with any valid HER effort
value. The same policy applies whether the occurrence is automatic, manually
run from Telegram or Workbench, or replayed from recovery.

This resolution is request-scoped. It must not mutate the Agent's configured
effort, and a later ordinary request must still use that configured value.
It also must not select or rewrite provider model or reasoning settings.
Ordinary user turns, delayed messages, and nudge continuations do not receive
scheduled-job context and therefore retain the Agent effort. Deterministic
scheduler actions that bypass the Agent backend, including automation,
transcript export, and HER Dream, do not consume this policy.

Invalid `her_v2_effort` values are rejected before prompt work is queued. They
must not be silently coerced to `low` or allowed to change global Agent state.

## 6. Stage 1: Initial Processing

Initial processing applies to every HER turn. Two processes begin promptly and independently.

### 6.1 Immediate user response

The Immediate Response is generated by a lightweight model using a non-reasoning or lowest-overhead provider mode.

Its system prompt contains only the configured `[persona]` block and the
Immediate-specific behaviour below. The rest of `system_md` and the Bridge
`/sys` packaging are not supplied to this invocation:

- for an obviously direct conversational request, answer immediately;
- for work that must continue, provide only a short receipt acknowledgement;
- tool access and tool authority are absent and this is private behavioural
  information that must not be repeated to the user;
- absence of tools in this stage is not evidence that Execution tools are
  unavailable; only a later Execution stage may determine actual availability
  from real invocation results, including when the user supplied an
  "if tools are unavailable" reporting branch;
- never call a tool or emit tool syntax, a tool-control envelope, or an
  executable command;
- do not execute, plan, assess feasibility, discuss capability, claim an
  execution result, or narrate a concrete execution attempt, because the actual
  work belongs to a later stage.

Its purposes are to:

- respond immediately when the request can be answered directly;
- acknowledge receipt of work that will continue;
- demonstrate a conservative understanding of the request;
- avoid promising an outcome that has not yet been achieved.

The Immediate Response is a real user-facing message, not merely an internal event.

### 6.2 Triage

Triage uses a lightweight model with a high provider reasoning setting. It produces exactly one validated classification:

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

For `SIMPLE_TASK`, `COMPLEX_TASK`, and `HIGH_VOLUME_TASK`, Triage also returns
exactly one `checkpoint_policy`: `STANDARD` or `HIGH_RISK`. `HIGH_RISK` is used
when continuing Execution can materially and irreversibly affect data,
production, security or access, credentials, money, external communications,
or another high-consequence target, and it requires a concise
`checkpoint_reason`. Non-work classifications carry neither field.

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

Planning is mandatory for `medium`, `high`, `xhigh`, and `max` work turns. It is not used for `DIRECT_RESPONSE`, `CONFIRMATION_REQUIRED`, or ordinary `low` execution.

Planning uses a premium model with a high provider reasoning setting and considers:

- the immutable Triage result;
- the authoritative user goal;
- scope and constraints;
- success criteria;
- current conversation context;
- relevant historical context;
- advisory Habits;
- available providers, models, tools, permissions, and sub-agents;
- recovery and meaningful-progress strategy;
- testing and verification strategy;
- parallelisation opportunities;
- Review and Verification requirements implied by HER execution mode.

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

Execution follows the active plan when a formal plan exists. Low-effort execution follows a minimal execution directive derived from the immutable Triage result.

Execution uses a dedicated HER v2 system prompt, not the Agent's full
`system_md` and not its Persona. Its user message contains the complete request
context supplied by HASHI—the same recent turns, Memory+ content, cross-session
receipts, and other context visible to Planning—plus the active plan when one
exists and any completed delegated inputs. The system prompt directs Execution
to carry out the request faithfully with the tools available to that invocation,
follow the supplied plan, report only work that actually occurred, and return
exactly one JSON object.

The only Execution dispositions are:

- `COMPLETED`;
- `COMPLETED_WITH_LIMITATIONS`;
- `FAILED`;
- `USER_INPUT_REQUIRED`.

The result also records a truthful summary, work performed, verification,
evidence references, limitations, remaining work, and a clarification question
when required. Technical `ERROR` belongs to HER v2 Runtime and is not available
for Execution to select. Execution cannot request Replanning; HER v2 imposes
Replanning only through the configured execution-mode assurance policy.

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

### 8.4 Execution-discovered user input

Execution may return `USER_INPUT_REQUIRED` only with a concrete clarification
question and truthful evidence explaining why progress cannot safely continue.
HER skips Review and Verification, passes the result through the single combined Finalisation
call, delivers its Persona-rendered clarification, and then reaches
`PENDING_USER_INPUT`. Bounded sub-agents may not use this disposition to contact
the user; they return the missing-information finding to the Primary Agent as
evidence.

### 8.5 High-risk periodic safe-boundary checkpoint

Only Execution cycles whose immutable Triage policy is `HIGH_RISK` install a
request-local checkpoint coordinator. A checkpoint becomes due at the first
observed inclusive threshold of 10 newly completed Tool Gateway receipts or
300 monotonic seconds in that Execution cycle. Successes, completed tool
errors, and policy denials count once by exact receipt identity; starts,
incomplete calls, duplicates, and non-Execution activity do not.

Due state is checked before admitting a new tool and after recording a
completed result. The coordinator closes new admission, lets already-active
tools settle, and runs exactly one tool-free assessment. The result that made
the checkpoint due is preserved before assessment, and the 11th action cannot
start first. Crossing 300 seconds never cancels an active tool or provider
operation. If Execution completes without another safe tool boundary, HER does
not invent a catch-up or final checkpoint.

`CONTINUE` begins one fresh count/time window. `USER_INPUT_REQUIRED` and `HALT`
use typed control paths that preserve all completed receipts and never replay a
side effect. An unavailable or invalid evaluator fails closed as `HALT`, with
the technical limitation reported truthfully. Immediate Tool Gateway denial,
approval, missing authority, `/stop`, `/steer`, audit failure, and cancellation
retain precedence over the cadence.

The Primary Agent and bounded sub-agents share one coordinator within an
authoritative Execution cycle. A later Review or Verification remediation
starts a fresh cycle. Checkpoints do not increment Review or Verification
counters, reset meaningful-progress idle state, enter Persona/commentary, or
replace the ordinary assurance and Finalisation stages. The normative detailed
contract is [HER v2 High-Risk Periodic Checkpoint Plan](HER_V2_HIGH_RISK_PERIODIC_CHECKPOINT_PLAN.md).

## 9. Stage 4: Replanning

Replanning is available only for `high`, `xhigh`, and `max`. Eligibility is an
effort policy, not a second classification gate. A `SIMPLE_TASK` remains
immutably classified as simple but may replan when execution evidence proves
that the original approach lacks a required capability. After that replan, HER
may select the configured primary execution profile without changing the
classification.

Its purpose is to restore alignment with the immutable user goal and Triage classification when execution evidence shows that the active approach is no longer adequate.

Replanning considers:

- the original request;
- the immutable Triage result;
- the active plan;
- completed and remaining work;
- tool and execution evidence;
- failures and newly discovered constraints;
- reviewer findings or failed Verification checks when remediation follows an
  assurance stage.

Replanning does not consult Habits again. Current execution evidence takes precedence over historical advice.

Triggers are configurable and may include:

- elapsed time since the previous plan decision;
- meaningful tool-call count;
- repeated execution failures;
- material new constraints;
- failed review requiring remediation.

Replanning limits are configurable. Default ceilings are:

- `high`: 50;
- `xhigh`: 100;
- `max`: 200.

These are safety ceilings, not targets.

## 10. Habit System

Habits contain accumulated operational experience such as:

- common mistakes;
- successful execution patterns;
- preferred approaches;
- known pitfalls.

Habits are advisory inputs to initial Planning. They are never user intent, execution evidence, or authority.

Habit retrieval is enabled by the single Habit–Meditation switch. It ranks only
the title and compact metadata, is bounded by the configured retrieval limit,
and uses only the current authoritative request after the final Bridge
current-request marker. Bridge conversation background is not retrieval input.
The selected Habit bodies may be disclosed to initial Planning only; Execution,
Replanning, Review, Verification, and Finalisation do not receive or re-read them. Because
`low` effort omits Planning, it omits Habit retrieval while retaining eligible
post-execution Meditation.

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

### 10.2 Dream

Dream is a background maintenance process that may:

- consolidate related Habits;
- merge duplicates;
- remove obsolete Habits;
- resolve conflicting guidance;
- promote useful candidates.

Dream is outside the critical live-execution path.

## 11. Stage 5: Review and Remediation

Independent Review applies to `xhigh` and `max` after execution has produced candidate deliverables.

The reviewer uses a premium model with the maximum appropriate provider
reasoning setting and a strict reviewer persona. Review independence is
achieved through prompt, role, and context separation; a different model
provider is not mandatory. The invocation has Tool Gateway access but is
strictly read-only.

The reviewer receives:

- the original request;
- the immutable Triage result;
- active and historical plan references;
- execution evidence and deliverables;
- relevant limitations and permission boundaries.

The reviewer must begin and end an evidence-backed assessment with
`workspace_inspect` snapshots. Passing, conditional, and failing claims must
cite exact completed receipts from the current Review invocation, and the two
snapshot digests must match. Workspace drift makes the result
`INCONCLUSIVE`. Review cannot pass from prose, boundary snapshots alone, or a
duplicated receipt.

The reviewer returns one outcome:

### 11.1 `PASS`

The work is complete and quality is acceptable. Proceed to Finalisation.

### 11.2 `CONDITIONAL_PASS`

The work is substantially complete but contains disclosed limitations, caveats, risks, or minor unresolved matters. Proceed to Finalisation and report those limitations clearly.

### 11.3 `FAIL`

The work is incomplete or below the required quality and is supported by
current tool evidence. If the remediation limit permits, the Primary Agent
performs Replanning and remediation.

### 11.4 `INCONCLUSIVE`

The available completed evidence cannot support a stable conclusion, including
when the workspace changes during the assessment.

### 11.5 `UNAVAILABLE`

The Review stage itself could not run or produce a valid tool-backed result.
Technical failure is reported as `UNAVAILABLE`; it must not be converted into
`CONDITIONAL_PASS`.

Review findings are advisory. They may trigger HER-controlled Replanning and
remediation, but they cannot replace a valid Execution disposition. Execution
remains responsible for its result and Finalisation remains responsible for the
user-facing report.

Review limits are strict:

- Reviewed (`xhigh`): one independent Review; on `FAIL`, at most one
  Primary-Agent remediation followed by exactly one read-only closure Review;
- Assured (`max`): one independent Review; on `FAIL`, at most one immediate
  Primary-Agent remediation before comprehensive Verification.

After the applicable limit is reached, HER continues with the mode's next stage
and clearly preserves unresolved findings. Review never overwrites the valid
Execution disposition.

## 12. Stage 6: Assured Verification

Comprehensive Verification applies only to Assured (`max`) work turns after
Review and any resulting remediation. It evaluates the latest Execution result
and current workspace state, not an earlier candidate.

Verification has Tool Gateway access with validation-only side-effect authority.
It cannot remediate, contact the user, or widen its own tool set. It uses:

- `workspace_inspect` for bounded status, diff, search, hash, artifact, and
  before/after snapshot evidence;
- `verification_run` for a configured recipe or direct process `argv`. Commands
  run in the authoritative current workspace without copying or sandboxing it.
  They inherit the HASHI process identity, filesystem access, environment,
  `HOME`, and network access. `argv` is executed without an implicit shell.

The runtime records cumulative wall-clock time across all authoritative
Execution attempts, including high-volume sub-agents and remediation. The
default effective verification timeout is:

`max(configured, requested, 300s, execution elapsed × 1.5 + 300s)`

The verifier may request more time but cannot reduce that result. The minimum
timeout is five minutes; configuration cannot reduce the execution multiplier
below 1.0 or the grace below 60 seconds. Thus a one-hour Execution receives a
5,700-second default verification budget, not a fixed short deadline.

Direct validation may create ordinary caches or test artifacts. Opening and
closing snapshots still detect unexpected candidate drift. Receipts record the
workspace scope, command source and argv hash, inherited authority and access
checks, timeout inputs/effective value, exit code, elapsed time, and cleanup.

Each required check records its claim, verifiability, method, observed result,
and exact current-invocation evidence receipts. A start without completion is
not evidence. A failed tool may support only `FAILED` or `INCONCLUSIVE`; it
cannot support a passing or unavailable claim. `VERIFIED`,
`PARTIALLY_VERIFIED`, and `FAILED` assessments require stable opening and
closing workspace snapshots.

The overall Verification outcome is one of:

- `VERIFIED`;
- `PARTIALLY_VERIFIED`;
- `FAILED`;
- `NOT_AI_VERIFIABLE`;
- `UNAVAILABLE`;
- `INCONCLUSIVE`.

A failed required check may trigger Primary-Agent Replanning and remediation,
followed by a fresh Verification of the resulting latest state. An
`INCONCLUSIVE` result may retry while attempts remain. The hard ceiling is
three Verification attempts, including configured values greater than three.
`PARTIALLY_VERIFIED` without a failed required check,
`NOT_AI_VERIFIABLE`, and `UNAVAILABLE` proceed with explicit limitations.

## 13. Stage 7: Finalisation and Reporting

Finalisation applies to every turn, although `DIRECT_RESPONSE` reuses the Immediate Response and sends no additional final message.

### 13.1 Exit assessment

Execution assesses whether the requested outcome was achieved and records one
of its four dispositions. Runtime deterministically maps that disposition to
the corresponding terminal state. Finalisation does not make a second outcome
decision when valid Execution JSON exists.

Finalisation considers:

- the current request and its complete supplied context;
- the complete raw Execution output;
- the parsed Execution result when valid;
- execution and tool evidence references;
- reviewer findings and unresolved limitations;
- Verification checks, receipts, outcome, and limitations when present;
- the marked Persona guidance used to render `final_message`.

Review and Verification findings must be considered critically rather than accepted blindly.

### 13.2 User-facing reporting

The report communicates, as applicable:

- results achieved;
- verification performed;
- remaining limitations;
- known issues and risks;
- assumptions;
- relevant review findings;
- whether the result is verified, partly verified, not AI-verifiable, or
  unavailable;
- the final task state.

Reporting must be honest and must not claim unverified work as complete.

Finalisation is one combined model stage. Its ordinary path is one provider
call; an eligible transient provider failure permits one fresh-connection
recovery call. Both attempts receive the current request, the same complete raw
Execution output, the same parsed Execution result when that envelope was
valid, the same optional Review and Verification findings, and only the explicit `[persona]`
block from the configured Agent system file. It never receives the rest of that
file and has no Tool Gateway.

Finalisation returns one JSON object containing `execution_result` and
`final_message`. When parsed Execution JSON exists, its disposition is the
source of truth and Runtime preserves it even if Finalisation attempts to
change it. When Execution returned malformed but meaningful output,
Finalisation may normalise the clear meaning into the Execution schema. When
the output has no usable meaning, `execution_result` is `null` and Runtime
selects technical `ERROR`. `final_message` is rendered with the supplied
Persona in the same call and must preserve Markdown, code, links, paths,
identifiers, numbers, facts, uncertainty, limitations, and clarification.

There is no independent structure-repair model and no separate final Persona
renderer. A retry is another attempt at the same combined Finalisation
operation, not a new reporting workflow. A Direct Response remains
Persona-authored by Immediate Response.

### 13.3 Reporting failure

For one transient provider-failure sequence, Finalisation may make at most two
attempts: the initial attempt and one eligible fresh-connection recovery.
Structured-envelope correction remains the separate semantic repair path from
section 3.7 and may issue another Finalisation-stage call under the user
idle-progress boundary; it does not replenish the one provider-recovery
allowance. Neither attempt has an HER elapsed-time deadline. Runtime freezes
and hashes the Finalisation input before the first attempt; every recovery or
structure-correction attempt must reuse the same Execution invocation identity,
raw output, parsed result, evidence references, Review and Verification findings, goal,
classification, permissions, provider, model, and workzone. Execution is never
called again. If recovery is unavailable or the applicable repair paths are
exhausted, Runtime preserves Execution evidence, selects technical `ERROR`, and
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
- `VERIFYING`
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

REVIEWING [FAIL with remediation available]
  -> REPLANNING
  -> EXECUTING
  -> EXECUTION_COMPLETED
  -> REVIEWING

REVIEWING [ASSURED]
  -> VERIFYING

VERIFYING [failed required check with remediation available]
  -> REPLANNING
  -> EXECUTING
  -> EXECUTION_COMPLETED
  -> VERIFYING

EXECUTION_COMPLETED, REVIEWING, or VERIFYING
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

Deterministic carrier recovery and structured-envelope correction occur before
provider recovery. A side-effect-free structured correction receives the prior
validation defect; it is a semantic repair path under the user idle-progress
boundary, not another allowance for transport failures.

Main Execution may use the provider recovery only when no tool has started or
when every started tool is provably read-only and has completed. Unknown,
incomplete, or side-effecting tool activity blocks automatic replay. A
side-effect-authorised Execution result is never replayed merely to repair its
output format; its raw output goes to Finalisation. Read-only sub-agents receive
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

Declared target capacity uses configured high/low watermark ratios. When
target capacity is unknown, HASHI still performs automatic maintenance through
a named absolute 64,000→48,000 estimated-token threshold; this threshold is product
policy, not invented provider metadata. Unknown compactor capacity uses
conservative 32,000 estimated-token source partitions. Automatic compaction is
never a prerequisite for the current model call. Protected-set overflow,
Compact unavailability, timeout, validation failure, or retry exhaustion keeps
the best safely assembled context, emits a mandatory user-visible warning, and
continues the selected model request. This remains true at 120,000 estimated
tokens or above. The provider's own capacity rejection remains truthful, but
HASHI does not pre-emptively turn a maintenance threshold into an execution
stop.

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
- Replanning triggers;
- Review, closure, Verification, and remediation limits;
- deterministic mapping from the canonical Execution disposition to terminal
  state, plus technical runtime terminal states.

HER v2 may validate and publish optional neutral commentary returned by a
successful reasoning stage. HASHI extracts Persona guidance and supplies only
the explicit marker block to isolated presentation invocations. Combined
Finalisation consumes that block while producing the canonical Execution
payload and final message in one call; only a pre-execution Triage clarification
uses the older required-message presentation interface.

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
4. deliver `low` Direct Response and Simple Task paths;
5. add `medium` Planning;
6. add `high` Replanning;
7. add Reviewed (`xhigh`) Review/remediation/closure and Assured (`max`)
   Review/Verification/remediation;
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
- low, medium, high, xhigh, and max policies follow the required stage matrix;
- cron and heartbeat prompt work defaults to request-local `low` effort across
  scheduled, manual, and recovery triggers; valid job overrides win without
  changing provider reasoning or leaking into later ordinary turns;
- Replanning, Review, and Verification loops cannot violate lifecycle order;
- passing assurance claims require completed receipts from the exact current
  stage invocation and stable before/after snapshots; fabricated, stale,
  cross-stage, incomplete, and failed passing evidence is rejected;
- Assured Verification runs configured recipes or direct argv checks in the
  authoritative workspace with inherited execution authority; its enforced
  timeout grows from cumulative Execution time and cannot be shortened by the
  verifier;
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
- combined Finalisation receives the current request and complete Execution
  inputs but only the marked Persona block from `system_md`; it preserves a
  valid Execution disposition and required-delivery identity;
- commentary packaging and delivery failures cannot alter workflow outcome;
- failed HER v2 initialization fails closed or uses an explicitly selected
  non-HER backend; it never rolls back to retired HER.

## 24. Locked Runtime Invariants

The following decisions are authoritative for HER v2:

1. User intent is the highest authority for the active turn.
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
8. Review is read-only; Verification has validation-only workspace authority;
   both are advisory and never user-facing.
9. The Primary Agent owns execution and reporting.
10. The Ledger is minimal operational state; HASHI logs are audit truth.
11. All available reasoning traces must be logged.
12. Execution evidence outweighs Habits.
13. Replanning does not consult Habits again.
14. Missing optional commentary does not fail execution.
15. Only successful reasoning-stage output may originate neutral commentary;
    workflow events never originate Persona speech.
16. Commentary and Triage-clarification packaging are presentation-only and
    receive one eligible message plus the explicit configured Persona block.
17. Combined Finalisation receives complete Execution inputs and that Persona
    block, preserves a valid Execution disposition, and produces the final
    required message without a second Persona call.
18. Reporting failure does not discard completed work.
19. Recovery is conversational, not transactional.
20. HER core is provider-neutral and modular.
21. A model-authored assurance claim is never evidence: only exact, completed,
    current-invocation Tool Registry receipts may support it, and a failed tool
    can support only a failed or inconclusive assessment.
22. Review and Verification never replace Execution's disposition; Finalisation
    reports verified, partly verified, not AI-verifiable, and unavailable work
    distinctly.
23. Auto Compact is HASHI-owned, tool-free, atomically reversible capacity
    maintenance; its dedicated Tier 2/Tier 3 call watchdog never becomes an
    ordinary HER stage, provider, tool-loop, or execution deadline.

## 25. Golden Rule

> Less blocking, more progress — while preserving immutable Triage authority, strict lifecycle order, complete available reasoning audit, and fidelity to the user's active goal.
