# Hashi Engine Runtime v2

## Product Requirements and Technical Design Specification

| Field | Value |
|---|---|
| Status | Approved design baseline |
| Version | 1.0 |
| Date | 2026-08-20 |
| Product | Hashi Engine Runtime (HER) |
| Implementation baseline | HASHI `origin/main` at `604b826ed0dbb8cb748a617cbcf4c7d0dd7406f4` |

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
switching, preflight, recovery, or runtime fallback. The historical public IDs
`her` and `claw-cli` may resolve forward to `her-v2` only; they cannot revive the
retired implementation.

## 3. Core Principles

### 3.1 Separation of concerns

HER effort levels are orchestration policies. They are not provider reasoning levels.

Provider reasoning controls remain provider-specific. HER selects the appropriate model profile and provider reasoning setting for each stage without redefining those provider controls as HER effort levels.

### 3.2 Provider neutrality

All execution parameters must be configurable, including:

- provider and model names;
- model role profiles;
- provider reasoning settings;
- the meaningful-progress idle window;
- replanning triggers and limits;
- review limits;
- tool and permission policies.

HER v2 has exactly two count-based orchestration ceilings: Replanning and
Review/remediation, as defined by effort policy. It has no total execution
clock, stage clock, retry-attempt ceiling, report-attempt ceiling, structure-
repair-attempt ceiling, tool-round ceiling, sub-agent-count ceiling, turn
ceiling, cumulative token budget, or per-request output-token ceiling. Legacy
HER/Claw fields representing those ceilings are invalid HER v2 configuration
and must be rejected rather than silently applied.

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

- retry a technically repairable, side-effect-free stage while it remains
  retryable and the turn has not crossed its no-progress idle boundary;
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
infer a classification from prose. Plain text is accepted only for inherently
user-facing Immediate Response and Finalisation outputs. JSON-string control
character repair accepts only the otherwise unchanged object produced by a
non-strict JSON decoder; it does not complete truncated structures or infer
fields. If a user-facing envelope still cannot be repaired, its original text
remains eligible for plain presentation so the message does not disappear. A
retry receives the previous validation defect so it can correct the envelope
instead of blindly repeating the same request.

### 3.6 Work, commentary, Persona packaging, and delivery

HER v2 keeps five boundaries distinct:

1. A reasoning stage performs its assigned work and may include one optional
   neutral `commentary` string in its successful structured result.
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

   Text outside that block is unavailable to the commentary packager, required
   message renderer, and Immediate Response model.
4. Commentary delivery accepts only the typed output of Persona packaging.
   Generic workflow delivery is not a commentary transport.
5. A validated Final Report or clarification enters a separate typed required
   message lane. Its Persona renderer receives only that report or question and
   the extracted Persona block. It cannot change the message kind, source event,
   workflow authority, terminal assessment, or required-delivery semantics.

Commentary is optional presentation, never workflow authority. A missing,
empty, malformed, oversized, packaging-failed, or delivery-failed commentary
cannot invalidate, retry, reclassify, replan, stop, or complete a stage. Missing
or invalid Persona markers and packaging failures use a deterministic minimal
package based on the configured HASHI display name, the form of address `您`,
and the unchanged validated source message. Required Final Reports and
clarifications use the same fail-open presentation rule: rendering failure
preserves the validated report or question and cannot change workflow state.
When the Persona block is unavailable to Immediate Response, its prompt uses
the same configured display name and polite form of address `您` as its entire
fallback Persona guidance; it never falls back to the rest of `system_md`.

This boundary governs interim commentary packaging, required Final Report and
clarification rendering, and the Persona input used by Immediate Response.
Immediate Response is not rewritten by either packager; it receives the same
extracted block directly and, for `DIRECT_RESPONSE`, becomes the sole final
answer. Clarification and Final Report retain dedicated required-message paths;
they do not become optional commentary merely because they share the isolated
Persona rendering core.
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
- Replanning creates a new plan version.
- Earlier versions remain historical evidence in HASHI logs.
- Only the Replanning stage may replace the active plan.
- Sub-agents may not change or replace the plan.

### 4.4 Primary Agent authority

The Primary Agent owns execution and the user-facing outcome. Review findings are advisory evidence. A reviewer cannot:

- change the user's goal;
- change the Triage classification;
- request clarification directly from the user;
- publish a user-facing final answer;
- independently authorise additional side effects.

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

## 5. HER Effort Levels

HER effort controls orchestration behaviour, not provider reasoning.

| HER effort | Required orchestration behaviour |
|---|---|
| `low` | Fast execution with minimal orchestration; no formal Planning stage |
| `medium` | Formal planning followed by execution |
| `high` | Planning, execution, and configurable periodic or evidence-triggered replanning |
| `xhigh` | High behaviour plus independent review and at most one remediation cycle |
| `max` | High behaviour plus independent review and at most three review/remediation cycles |

HER v2 does not impose a tool-call round or turn ceiling on tool-enabled
Execution or delegated sub-agent invocations. Once tools are authorised for a
stage, the provider loop continues until the model completes, the invocation
fails, or the request is cancelled. Agent-level Tool Registry permissions and
safety policy still apply, but a generic registry `max_loops` value is not a
HER v2 termination condition. Effort never changes this rule.

Effort determines the maximum orchestration path available. Triage classifications `DIRECT_RESPONSE` and `CONFIRMATION_REQUIRED` terminate through their dedicated paths without unnecessary planning, regardless of the selected effort.

Each effective task route derives its base capability from configurable role
profiles such as `lightweight`, `premium`, and `reviewer`, then independently
selects a model slot and provider reasoning. HER does not hard-code provider
model names.

### 5.1 Runtime configuration command boundary

HER v2 presents two reusable model slots: Quick and Pro. `/provider` selects the
concrete call-provider engine that carries both slots. `/model` defines those
two models, then independently assigns a model slot and provider reasoning to
each effective task route. Execution is split into Simple, Complex, and
High-volume routes because classification changes the actual profile. Structure
repair may follow its source model. `/backend` selects `her-v2` without exposing
the internal `role-configured` sentinel.

`/effort` is a separate orchestration-policy command. Changing it must not read,
infer, normalize, or persist a provider reasoning value. Conversely, changing a
provider, model slot, or provider reasoning setting must not change HER effort.
Non-HER backends retain their established `/model` behaviour.

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
- review requirements implied by HER effort.

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
HER delivers that question, transitions directly from `EXECUTING` to
`PENDING_USER_INPUT`, and does not Review or Finalise the incomplete work.
Bounded sub-agents may not use this disposition to contact the user; they return
the missing-information finding to the Primary Agent as evidence.

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
- reviewer findings when remediation follows review.

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
Replanning, Review, and Finalisation do not receive or re-read them. Because
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

The reviewer uses a premium model with the maximum appropriate provider reasoning setting and a strict reviewer persona. Review independence is achieved through prompt, role, and context separation; a different model provider is not mandatory.

The reviewer receives:

- the original request;
- the immutable Triage result;
- active and historical plan references;
- execution evidence and deliverables;
- relevant limitations and permission boundaries.

The reviewer returns one outcome:

### 11.1 `PASS`

The work is complete and quality is acceptable. Proceed to Finalisation.

### 11.2 `CONDITIONAL_PASS`

The work is substantially complete but contains disclosed limitations, caveats, risks, or minor unresolved matters. Proceed to Finalisation and report those limitations clearly.

### 11.3 `FAIL`

The work is incomplete or below the required quality. If the remediation limit permits, perform Replanning and remediation before returning to Review.

Review findings are advisory. The Primary Agent remains responsible for execution and final reporting.

Review limits are strict:

- `xhigh`: one review and at most one remediation cycle;
- `max`: at most three review/remediation cycles.

After the limit is reached, HER proceeds to Finalisation regardless of the last review outcome and clearly reports unresolved findings.

## 12. Stage 6: Finalisation and Reporting

Finalisation applies to every turn, although `DIRECT_RESPONSE` reuses the Immediate Response and sends no additional final message.

### 12.1 Exit assessment

The Primary Agent assesses:

- the authoritative request;
- immutable Triage classification;
- active plan and relevant historical plan references;
- execution and tool evidence;
- replanning history;
- reviewer findings;
- unresolved limitations;
- the appropriate terminal state.

Reviewer findings must be considered critically rather than accepted blindly.

### 12.2 User-facing reporting

The report communicates, as applicable:

- results achieved;
- verification performed;
- remaining limitations;
- known issues and risks;
- assumptions;
- relevant review findings;
- the final task state.

Reporting must be honest and must not claim unverified work as complete.

The Finalisation model returns a neutral structured report, normally
`{"report": "..."}`. After the compatibility membrane and report schema have
validated that result, HER extracts the report and creates a typed required
final message. HASHI then renders that message through the isolated Persona
boundary using only the report and the explicit `[persona]` block. Rendering
must preserve Markdown, code, links, paths, identifiers, numbers, facts,
uncertainty, limitations, and the terminal boundary. The rendered text remains
the same required final delivery with the same stable delivery identity.

Persona rendering is presentation-only and fail-open. A missing Persona block,
provider error, invalid rendered envelope, or empty rendered result uses a
deterministic Persona fallback containing the unchanged validated report. It
does not retry execution, reopen Finalisation, or alter the selected terminal
state. Validated Triage and Execution clarification questions follow the same
required-message rule. A Direct Response is already Persona-authored by
Immediate Response and is never rendered a second time.

### 12.3 Reporting failure

If execution completed but reporting fails, HER retries a retryable reporting
failure without an attempt-count ceiling. Retries end only on success, explicit
stop, a non-retryable failure, or the meaningful-progress idle boundary.

If reporting cannot complete:

- execution evidence remains valid;
- completed work is not discarded;
- the terminal state is `COMPLETED_WITH_REPORT_PENDING`;
- HASHI logs the reporting failure and preserves the completed outcome for later user-visible reconciliation.

## 13. Lifecycle State Machine

### 13.1 States

The canonical lifecycle states are:

- `RECEIVED`
- `TRIAGED`
- `PLANNED`
- `EXECUTING`
- `REPLANNING`
- `EXECUTION_COMPLETED`
- `REVIEWING`
- `FINALISING`
- terminal states defined in Section 14

### 13.2 Valid principal transitions

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

EXECUTING [new user information or authority is required]
  -> PENDING_USER_INPUT

EXECUTING [side-effect result cannot be validated after isolated repair]
  -> RECONCILIATION_REQUIRED

EXECUTION_COMPLETED [XHIGH/MAX]
  -> REVIEWING

REVIEWING [FAIL with remediation available]
  -> REPLANNING
  -> EXECUTING
  -> EXECUTION_COMPLETED
  -> REVIEWING

EXECUTION_COMPLETED or REVIEWING
  -> FINALISING
  -> TERMINAL
```

### 13.3 Strict ordering

Lifecycle transitions must follow an explicitly allowed edge. Examples of invalid events include:

- execution completion without execution start;
- review completion before an execution candidate exists;
- replanning before an active plan exists;
- plan replacement outside Replanning;
- classification replacement after `TRIAGED`.

An invalid transition is a technical `ERROR`. The turn terminates immediately. Content fields may be repaired or omitted according to stage policy, but lifecycle ordering may not be fabricated.

## 14. Terminal States

HER v2 uses the following unified terminal states:

| State | Meaning |
|---|---|
| `COMPLETED` | Required work and reporting completed |
| `COMPLETED_WITH_LIMITATIONS` | Work concluded with material disclosed limitations |
| `COMPLETED_WITH_REPORT_PENDING` | Execution completed but user-facing reporting ended on a non-retryable failure or no-progress idle boundary |
| `RECONCILIATION_REQUIRED` | Execution may have changed external state, but the result could not be validated; automatic replay is forbidden |
| `FAILED` | Execution ran correctly but concluded that the user's goal could not be achieved |
| `ERROR` | A technical failure prevented correct execution, including an unexpected process interruption or lifecycle violation |
| `ABANDONED` | The Primary Agent deliberately concluded that continued execution was no longer justified and recorded the reason |
| `STOPPED` | The user or authorised control path stopped the turn, including `/stop` and `/steer` |
| `PENDING_USER_INPUT` | Triage or later Execution determined that clarification, confirmation, or missing authority is required |

`INTERRUPTED` is not a separate terminal state. An intentional interruption is `STOPPED`; an unexpected technical interruption is `ERROR`.

`ERROR` and `FAILED` are distinct:

- `ERROR` means the runtime could not execute correctly;
- `FAILED` means execution operated correctly and concluded the goal was not achievable.

Reaching a terminal state concludes the turn. It does not guarantee perfect completion or user satisfaction.

## 15. Execution Ledger

### 15.1 Purpose

Each request has a lightweight Execution Ledger. The Ledger is the authoritative operational record of the turn's current lifecycle state. It supports:

- workflow control;
- current-state visibility;
- next-turn understanding;
- references to detailed evidence in HASHI logs.

The Ledger is not the complete audit record.

### 15.2 Minimal record

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
- full planning or review output;
- complete tool request and response payloads;
- duplicated conversation history;
- a second complete audit trail.

Those records belong in HASHI orchestration logs. The Ledger stores only current control state and references.

### 15.3 Append-only truth

Historical facts are never silently rewritten. If later evidence corrects an earlier interpretation, HASHI appends a correction record and advances the current Ledger snapshot. Detailed history remains reconstructable from logs.

An incomplete Ledger is valid evidence that the turn did not reach a conclusion.

## 16. HASHI Logging and Audit

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

## 17. Failure, Retry, and Recovery

### 17.1 Stage-local retry

HER permits retry within the active process and current stage for technical failures such as:

- transient provider or network errors;
- invalid structured output;
- schema repair;
- retryable tool transport errors;
- report-generation failure.

Retry delays may back off, but attempt count and elapsed execution time are not
ceilings. A retry does not change the Triage classification or user goal.

Before a non-side-effect retry, HER records and supplies the prior validation
error to the next attempt. Compatible carrier recovery occurs before retry and
is recorded as such; it is not described as a model repair.

A stage invocation authorised to perform external side effects is never
replayed merely to repair its output format. If its returned envelope is
invalid, HER invokes a distinct `STRUCTURE_REPAIR` stage using the original
response as quoted evidence. That repair stage has no Tool Gateway registry and
no side-effect authority. If repair ends at a non-retryable failure or the
no-progress idle boundary without establishing a valid result,
the turn becomes `RECONCILIATION_REQUIRED`; it does not enter Finalisation,
claim completion, or automatically retry Execution.

### 17.2 No process-restart resumption

HER does not reconstruct or resume an in-flight execution stack after process restart.

If the process stops unexpectedly:

- the old turn becomes `ERROR` during reconciliation;
- its incomplete Ledger and HASHI logs are preserved;
- no old planner, executor, reviewer, or sub-agent continuation is restarted automatically.

A later user request such as “continue” starts a new turn. Its Triage stage may inspect conversation history, the previous Ledger, and HASHI logs to determine remaining work.

Recovery is conversational, not transactional.

## 18. Timeout Model

### 18.1 User timeout

The user timeout represents the maximum permitted period without measurable progress. It is not a total wall-clock runtime limit.

Measurable progress includes:

- a meaningful user commentary update;
- tool execution or a tool result;
- a genuine lifecycle or Ledger state transition;
- a replan event;
- completion of a substantive execution unit.

No-op retries, heartbeat-only Ledger writes, idle waiting, and unlogged internal loops are not progress.

### 18.2 Prohibited execution clocks and budgets

There is no stage timeout, retry timeout, whole-turn hard timeout, time budget,
turn budget, tool-round budget, sub-agent budget, cumulative token budget, or
output-token budget in HER v2. Meaningful activity may continue for arbitrarily
long elapsed time. A process-level operator stop and the no-progress idle
detector are liveness controls, not total-runtime ceilings.

Transport operations may retain connection/read inactivity and protocol safety
guards. Such guards must be scoped to the individual transport or parser, must
not be presented as an HER execution budget, and must not cancel a turn that is
still producing meaningful progress.

## 19. Sub-Agent Governance

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

## 20. Architectural Boundaries

### 20.1 HASHI-owned responsibilities

HASHI remains responsible for:

- transport and user-message delivery;
- Persona source resolution and presentation-only packaging;
- agent configuration and secrets;
- provider adapters and credentials;
- Tool Gateway registration and execution;
- permission and workzone enforcement;
- queues, cancellation, `/stop`, and `/steer`;
- meaningful-progress idle enforcement;
- audit logging and redaction;
- Workbench and operational status;
- hot restart and process lifecycle.

### 20.2 HER-owned responsibilities

HER v2 owns:

- Triage;
- effort-policy resolution;
- lifecycle-state validation;
- stage orchestration;
- lightweight Ledger management;
- plan version selection;
- Replanning triggers;
- review/remediation limits;
- final task-state selection.

HER v2 may validate and publish optional neutral commentary returned by a
successful reasoning stage. It may also submit a typed validated Final Report
or clarification to an injected required-message presentation interface. It
never reads Persona guidance or authors Persona prose inside the orchestration
state machine; HASHI owns source extraction, isolated rendering, and fallback.

### 20.3 Optional supporting systems

Habits, Meditation, Dream, and optional native executors connect through explicit interfaces. They cannot become mandatory hidden dependencies of the core state machine.

### 20.4 Compatibility facade

HER v2 remains registered through a thin HASHI compatibility facade. Internally
it behaves as an orchestration policy over provider and tool interfaces rather
than reproducing the retired monolithic backend design. Compatibility is limited
to forward ID aliases and approved Habit, Meditation, and Dream data formats;
it is not execution fallback compatibility.

## 21. Migration Strategy

Implementation starts from HASHI `origin/main` commit `604b826ed0dbb8cb748a617cbcf4c7d0dd7406f4` in a clean, dedicated branch and worktree.

The migration sequence is:

1. freeze this specification and canonical state-transition tests;
2. implement lifecycle types, Ledger, and transition validation without model calls;
3. implement Provider Profile and stage interfaces;
4. deliver `low` Direct Response and Simple Task paths;
5. add `medium` Planning;
6. add `high` Replanning;
7. add `xhigh` and `max` Review and remediation;
8. integrate sub-agents;
9. integrate Habits, Meditation, and Dream last;
10. certify normal-mode execution with HASHI permissions restricting canary
    side effects;
11. canary selected agents;
12. expand rollout only after certification;
13. remove every old-HER registration and prove historical IDs resolve only
    forward to HER v2.

Existing HER code, local experimental commits, and uncommitted Task Control work are reference material. They are not the implementation foundation of HER v2 and must be ported only when a v2 requirement and test justify them.

## 22. Acceptance Criteria

HER v2 is ready for production rollout only when:

- every Triage classification has deterministic transition tests;
- a recorded Triage classification cannot be mutated within the turn;
- Direct Response produces exactly one user-facing response;
- `/steer` terminates the old turn and starts a separately classified new turn;
- low, medium, high, xhigh, and max policies follow the required stage matrix;
- Replanning and Review loops cannot violate lifecycle order;
- retries have no attempt/time ceiling, terminate correctly on no-progress idle
  or non-retryable failure, and process restart does not resume an old stack;
- Ledger records remain minimal and auditable through log references;
- all available reasoning traces are logged and correlated to the turn;
- tools and permissions remain HASHI-owned;
- provider model names are configurable rather than hard-coded in HER core;
- reporting failure preserves completed execution evidence;
- stop terminates primary and sub-agent activity;
- the retired HER implementation is unreachable through `her`, `claw-cli`,
  backend switching, startup preflight, and initialization failure;
- lifecycle and workflow events cannot generate Persona commentary;
- Persona packaging receives no raw request, plan, reasoning trace, lifecycle
  snapshot, or unmarked `system_md` content. It receives exactly one eligible
  source message: neutral commentary, a validated Final Report, or a validated
  clarification;
- required Final Report and clarification rendering preserves delivery kind,
  source identity, validated content, terminal state, and fallback delivery;
- commentary packaging and delivery failures cannot alter workflow outcome;
- failed HER v2 initialization fails closed or uses an explicitly selected
  non-HER backend; it never rolls back to retired HER.

## 23. Locked Runtime Invariants

The following decisions are authoritative for HER v2:

1. User intent is the highest authority for the active turn.
2. Triage is authoritative and immutable for that turn.
3. Planning and Replanning may not change classification or goal.
4. `/steer` stops the old turn and starts a newly triaged turn with new instructions.
5. Lifecycle order is strict; stage content is flexible.
6. Stage-local retry has no attempt or elapsed-time ceiling; only explicit stop,
   non-retryable failure, or no-progress idle expiration may end it.
7. Immediate Response becomes the sole final user-facing answer for `DIRECT_RESPONSE`.
8. Review is advisory and never user-facing.
9. The Primary Agent owns execution and reporting.
10. The Ledger is minimal operational state; HASHI logs are audit truth.
11. All available reasoning traces must be logged.
12. Execution evidence outweighs Habits.
13. Replanning does not consult Habits again.
14. Missing optional commentary does not fail execution.
15. Only successful reasoning-stage output may originate neutral commentary;
    workflow events never originate Persona speech.
16. Persona packaging is presentation-only and receives only one eligible
    message plus the explicit configured Persona block: neutral commentary, a
    validated Final Report, or a validated clarification.
17. Commentary remains optional; Final Report and clarification remain typed
    required messages. Rendering cannot change their workflow authority.
18. Reporting failure does not discard completed work.
19. Recovery is conversational, not transactional.
20. HER core is provider-neutral and modular.

## 24. Golden Rule

> Less blocking, more progress — while preserving immutable Triage authority, strict lifecycle order, complete available reasoning audit, and fidelity to the user's active goal.
