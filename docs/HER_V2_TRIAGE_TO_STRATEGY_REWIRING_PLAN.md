# HER v2 Triage-to-Strategy Rewiring Plan

| Field | Value |
|---|---|
| Status | Fast/Low implemented; Planned/Medium stage-tool boundary accepted and frozen; broader paths pending |
| Date | 2026-08-30 |
| Experiment environment | HASHI3 only |
| Current production effect | None until an authorised HASHI3 hot reload |
| Core change | Replace prompt-only Triage with a tool-capable Strategy stage that classifies the request, selects Strategy Cards, and prepares a concise execution brief |
| Activation | Fast/Low implementation is ready; activation still requires an authorised HASHI3 hot reload |

## Implementation checkpoint — 2026-08-30

The first experimental slice is implemented in HASHI3:

- the legacy `Stage.TRIAGE` wire stage now renders the Strategy prompt and
  validates schema v3;
- the Strategist receives the complete 38-card external Playbook, complete PCM,
  current request resources, downstream capabilities, and the normal HASHI
  tools/skills with current-request side-effect authority;
- the HASHI3 local Triage route is assigned to the Pro model slot;
- Low effort skips Planning as before and passes one `strategy_handoff` to
  Execution containing only the selected Card snapshots and six-field
  `execution_brief`;
- Planned/Medium now follows the separately measured no-tool Strategy,
  read-only Planning, then fully capable Execution boundary;
- High and higher paths retain their existing stage-tool behaviour until they
  are evaluated independently; and
- the HER v2 regression suite passes before activation.

## 1. Purpose

This document defines the implementation plan for upgrading the existing HER v2
Triage stage into a Strategy stage in HASHI3.

The new Strategist is not a renamed classifier and is not a detailed Planning
agent. It is an intelligent, tool-capable first work stage that:

1. receives the complete bridge-managed PCM and current request resources;
2. resolves the operative user goal;
3. retains the existing authoritative workflow classification;
4. reads the complete external Strategy Playbook;
5. chooses and composes the Strategy Cards that best fit the task;
6. uses the available tools and skills when they improve understanding or useful
   preparation;
7. selects relevant Habits; and
8. hands downstream Execution a concise strategic brief.

The change is deliberately narrow. It does not redesign Replanning, Review,
Finalisation, the Tool Gateway, PCM authority, Habits, Skills, or the Immediate
Response delivery contract.

## 2. Source decisions and precedence

This plan combines three sources:

1. the external Playbook design note;
2. the draft `system_strategy.txt`; and
3. the subsequent Strategy design decisions made for the HASHI3 experiment.

Where the earlier design note and the later decisions differ, the later
decisions govern this experiment.

In particular, the earlier note proposed a Runtime/Card Retriever that would
select 1–4 cards before the Strategist saw them. That proposal is superseded for
this experiment. The Strategist must have the complete Playbook and make the
semantic selection itself. Runtime may load, validate, version, hash, and inject
the Playbook, but it does not decide which cards are relevant.

## 3. Locked design decisions

### 3.1 Strategy receives the complete PCM

The Strategy invocation remains at the current initial Triage position and
receives the same complete authoritative PCM that HASHI already supplies at
that boundary.

The implementation must not replace the PCM with a reduced authority summary,
a rewritten user prompt, or a second generated `authority_context`. The current
typed envelope remains the single model-visible source of system instructions,
current user authority, conversation context, Workzones, available HASHI skills,
tools, attachments, date/time, and other bridge context.

After Strategy resolves `real_goal`, downstream state continues to use the
resolved goal as the immutable turn goal, as current Triage does.

### 3.2 Strategy receives the complete Playbook

The Strategy Playbook is an external, version-controlled data asset. It is not
embedded in the core prompt and its card IDs are not enumerated in schema v3.

For the HASHI3 experiment, Runtime injects the complete validated Playbook into
`$strategy_cards`. There is no pre-Strategy semantic filtering, embedding
selection, keyword routing, or hard-coded task-to-card mapping.

The Strategist owns:

- understanding the task;
- deciding which cards are relevant;
- combining primary and supporting strategies; and
- adapting card guidance to current evidence and context.

Runtime owns only:

- loading the Playbook;
- validating its general data shape;
- freezing the snapshot used by a turn;
- computing its version/hash for audit and replay; and
- validating that returned card IDs exist in that frozen snapshot.

Retriever-based loading may be tested later if the Playbook becomes too large,
but it is not part of this rewiring and must not become an invisible authority
in front of the Strategist.

### 3.3 Strategy has the normal HASHI tools and skills

The Strategy stage is part of execution, not a tool-free paper-planning phase.
It receives the tools and skills that normally flow through PCM and the HASHI
provider/tool boundary for the current request.

The Strategy invocation must use:

```python
allow_tools=True
allow_side_effects=<the same current-request execution authority>
```

For the current HASHI3 configuration, this means the same side-effect capability
envelope used by primary Execution rather than a Strategy-specific read-only
subset. Existing user/system authority, Workzone enforcement, Tool Gateway
permissions, provider constraints, and audit rules still apply naturally; the
rewiring adds no new Strategy-only restrictions.

The prompt should positively encourage the Strategist to use tools and skills
when they help it understand the real task, inspect current state, reduce
material uncertainty, or complete useful preparation. It should also encourage
continuity so downstream Execution continues from the resulting state instead
of unnecessarily repeating completed preparation.

### 3.4 Strategy stays strategic

The Strategist does not produce a detailed command plan, fixed tool-call script,
fine-grained DAG, budget, risk matrix, model policy, or sub-agent assignment
schema.

It produces a concise `execution_brief` containing only:

- the overall strategy;
- major stages;
- important dependencies and useful parallelism;
- verification approach;
- success criteria; and
- conditions that should trigger replanning.

Execution retains tactical judgement over exact commands, queries, files,
tools, and local decisions as evidence develops.

### 3.5 Only two Strategy fields are added to the output

The only new top-level semantic fields introduced by Triage-to-Strategy are:

```text
selected_strategy_cards
execution_brief
```

The existing `classification`, `real_goal`, `relevant_habits`, and conditional
`clarification` behaviour are retained. No task-properties object, reconnaissance
report, tool plan, budget, sub-agent schedule, or evidence matrix is added to
schema v3.

### 3.6 Prompt language remains positive

The Strategy prompt should focus on what a capable Strategist should do. It
should not grow a long Strategy-specific list of prohibited actions.

Higher-authority system/user instructions and the existing HASHI enforcement
layers continue to govern the work. The Strategy prompt should add only the
role-specific guidance required to understand, prepare, strategise, and hand off
the task effectively.

### 3.7 Internal wire naming remains compatible during the experiment

The first HASHI3 implementation keeps `Stage.TRIAGE`, `Route.TRIAGE`, the
`TRIAGED` lifecycle state, and existing persisted wire values for compatibility.

The model role, prompt asset, audit wording, tests, and user-facing labels may
call it Strategy/Strategist. A later purely mechanical migration can rename the
internal enum and persisted route after the Strategy behaviour is proven. That
rename is not part of this implementation.

### 3.8 Strategy uses a capable model route

The new role absorbs classification, Playbook interpretation, preparation, and
the strategic work previously split across Triage and Planning. It should not
remain implicitly tied to the old lightweight Triage default.

For the HASHI3 experiment, retain the compatible `Route.TRIAGE` key but present
and configure it as the Strategy route. Its default target should use the Pro/
premium-capable model slot that can consume the complete PCM and Playbook, use
tools, and perform the required strategic reasoning. Provider/model/reasoning
selection remains configurable through the existing HER route controls rather
than being hard-coded in the prompt.

Immediate Response remains on its independent lightweight route, so the user
still receives the low-latency parallel response while Strategy does deeper
work.

## 4. Current HASHI3 baseline

The current implementation behaves as follows:

```text
Request
  ├─ Immediate Response (parallel, no tools)
  └─ Triage (parallel, no tools)
       ├─ resolve real_goal
       ├─ classify
       └─ select relevant_habits

Work classification
  ├─ low effort: Execution directly
  └─ medium/high/xhigh/max: separate Planning → Execution
```

Important current facts:

- `system_strategy.txt` exists in the worktree but is not registered or loaded;
- `render_stage_prompt()` still renders `system_triage`;
- Triage uses schema v2;
- `TriageDecision` carries no Strategy Card or execution-brief data;
- `parse_triage()` accepts only the old contract;
- Runtime invokes Triage with `allow_tools=False`;
- the current Planning stage receives execution tools and sub-agent profiles;
- the current HIGH_VOLUME path depends on Planning's `sub_agents` and
  `parallel_groups` fields for Runtime-managed delegation; and
- Execution receives `active_plan`, relevant Habits, delegated results, and a
  tool catalogue, but no explicit Strategy handoff.

The untracked draft `system_strategy.txt` is user-authored input and must be
preserved during implementation.

## 5. Target workflow

```text
Authoritative request + complete PCM
              │
              ├───────────────┐
              ▼               ▼
    Immediate Response     Strategy
    user-facing lane       authoritative work lane
                              │
                              ├─ complete Playbook
                              ├─ complete Habit catalogue
                              ├─ current resources
                              ├─ normal HASHI tools/skills
                              ├─ downstream execution capabilities
                              └─ schema v3
                                      │
                                      ▼
                         validated StrategyDecision
                           ├─ classification
                           ├─ real_goal
                           ├─ selected card IDs
                           ├─ relevant Habits
                           └─ execution brief
                                      │
               ┌──────────────────────┼──────────────────────┐
               ▼                      ▼                      ▼
       DIRECT_RESPONSE      CONFIRMATION_REQUIRED        WORK
       existing final lane   existing clarification       Strategy handoff
                                                         → Execution
```

The existing Immediate/authoritative-stage race contract remains unchanged:

- Strategy does not wait for Immediate Response;
- Immediate Response remains optional for work turns;
- `DIRECT_RESPONSE` still uses the valid Immediate result as the sole final;
- `CONFIRMATION_REQUIRED` still uses the authoritative clarification path; and
- work begins from the validated Strategy result without waiting for a late
  acknowledgement.

## 6. External Playbook design

### 6.1 Repository layout

Use a dedicated data location rather than `prompt_assets`, for example:

```text
orchestrator/her_v2/playbook_assets/strategy_playbook.json
orchestrator/her_v2/strategy_playbook.py
```

One JSON asset is the simplest first implementation and requires no new parser
dependency. Splitting cards into separate files can be considered later without
changing the prompt or output schema.

### 6.2 Playbook envelope

The Playbook asset should use a generic versioned envelope:

```json
{
  "playbook_version": "1",
  "cards": [
    {
      "id": "evidence-research",
      "version": "1.0",
      "title": "Evidence Research",
      "use_when": [],
      "avoid_when": [],
      "strategy": [],
      "topology": {},
      "validation": [],
      "failure_modes": [],
      "composition": {}
    }
  ]
}
```

The loader validates only generic integrity:

- the envelope and cards are objects/lists of the expected general shape;
- every card has a non-empty stable `id` and `version`;
- card IDs are unique within the snapshot; and
- the file can be rendered deterministically into the prompt.

It does not encode a fixed enum of known card IDs and does not decide semantic
relevance.

### 6.3 Full-Playbook injection

The complete frozen envelope is rendered into `$strategy_cards` for every
Strategy invocation. The audit record stores at least:

- `playbook_version`;
- a content SHA-256;
- the number of cards; and
- the exact selected card IDs after Strategy validation.

Schema v3 returns card IDs as strings, following the agreed minimal example.
Runtime can resolve the corresponding card versions from the frozen snapshot
for audit without adding version/role objects to model output.

## 7. Strategy prompt input contract

The target `system_strategy.txt` uses this compact set of placeholders:

```text
$goal
$strategy_cards
$habit_catalogue
$execution_capabilities
$request_resources
$schema_v3
```

### 7.1 `$goal`

Existing source: the complete current request/PCM supplied to the initial
authoritative stage.

No generated authority summary is inserted beside it.

### 7.2 `$strategy_cards`

New source: the complete frozen external Playbook envelope.

The prompt tells the Strategist to understand, select, and compose the supplied
cards. It does not suggest that Runtime has already selected candidates.

### 7.3 `$habit_catalogue`

Existing source: the complete bounded Habit catalogue retrieved before the
initial authoritative stage.

Habit representation remains unchanged during this work. The Strategist must
return the same reference representation supplied by the current catalogue.

### 7.4 `$execution_capabilities`

New Strategy input assembled from the capability data that current Planning
already uses, plus the available skills relevant to downstream Execution.

It should remain a concise factual catalogue of what downstream Execution can
use. It does not include HER review counts, model budgets, tool-call budgets,
or internal orchestration policy.

At minimum it should expose:

- registered execution tools;
- available HASHI skills/capabilities;
- configured execution/sub-agent profiles when applicable; and
- attachment/media capabilities needed for a viable strategy.

This catalogue guides the brief but does not narrow the Strategist's own tools.

### 7.5 `$request_resources`

New thin structured index derived from the existing request content and
attachment manifest.

It contains only stable resource references useful for strategy and handoff,
such as attachment ID, filename, modality, MIME type, and availability. It does
not duplicate the PCM or reinterpret authority.

### 7.6 `$schema_v3`

New Strategy structured-output schema replacing Triage schema v2.

Tools and Skills are not duplicated as a separate `$strategy_capabilities`
placeholder. The Strategist receives them through the existing PCM/provider
path and actual tool attachment.

## 8. Strategy schema v3

### 8.1 Canonical work-task shape

```json
{
  "classification": "COMPLEX_TASK",
  "real_goal": "Research leading agent frameworks and recommend an architecture.",
  "selected_strategy_cards": [
    "evidence-research",
    "comparative-analysis"
  ],
  "relevant_habits": [
    "habit_017"
  ],
  "execution_brief": {
    "strategy": "Research authoritative sources, compare frameworks using common criteria, verify important claims, then synthesize an architecture recommendation.",
    "stages": [
      "Define comparison criteria",
      "Research major frameworks",
      "Normalize findings",
      "Compare architectures",
      "Verify consequential claims",
      "Synthesize recommendation"
    ],
    "dependencies": [
      "Framework research may run in parallel",
      "Comparison requires completed research",
      "Recommendation requires comparison and verification"
    ],
    "verification": [
      "Prefer primary technical sources",
      "Cross-check consequential claims",
      "Check source dates for current claims"
    ],
    "success_criteria": [
      "Major frameworks are covered",
      "Comparison uses consistent criteria",
      "Recommendation is evidence-backed"
    ],
    "replan_conditions": [
      "Important evidence is unavailable",
      "Sources materially contradict each other",
      "Initial comparison criteria prove inadequate"
    ]
  }
}
```

### 8.2 Field contract

| Field | Contract |
|---|---|
| `classification` | One existing HER v2 classification; classification remains immutable after validation |
| `real_goal` | Concise resolved operational goal; required for every resolved request |
| `selected_strategy_cards` | Unique list of exact card IDs selected from the supplied frozen Playbook; no hard-coded schema enum |
| `relevant_habits` | Existing Habit-reference representation selected from the supplied catalogue |
| `execution_brief.strategy` | Non-empty overall approach for work classifications |
| `execution_brief.stages` | Major strategic stages as strings, not exact commands |
| `execution_brief.dependencies` | Important sequencing, prerequisite, and parallelism relationships |
| `execution_brief.verification` | Verification approach and evidence standards |
| `execution_brief.success_criteria` | Observable conditions for satisfying the resolved goal |
| `execution_brief.replan_conditions` | Conditions under which Execution/Replanning should reconsider the approach |
| `clarification` | Existing conditional field, required only for `CONFIRMATION_REQUIRED`; it may be omitted otherwise |

### 8.3 Classification-specific semantics

For `SIMPLE_TASK`, `COMPLEX_TASK`, and `HIGH_VOLUME_TASK`:

- `real_goal` is non-empty;
- the execution brief is complete but proportionate;
- selected cards may be empty when no card materially improves the strategy;
  and
- no artificial schema maximum is placed on the number of selected cards.

For `DIRECT_RESPONSE`:

- card selection and the execution brief may be empty;
- no artificial work plan is generated; and
- the existing Immediate Response final path remains authoritative.

For `CONFIRMATION_REQUIRED`:

- `clarification` contains the concrete unresolved question;
- speculative card/habit selection is empty; and
- the execution brief uses the schema-defined empty representation.

### 8.4 Explicit non-fields

Schema v3 does not add:

- `task_properties`;
- `reconnaissance`;
- `observations`;
- `evidence_matrix`;
- `tool_plan`;
- `risk_matrix`;
- `execution_policy`;
- `budget`;
- `sub_agents`;
- `parallel_groups`; or
- a fine-grained step DAG.

Strategy tool receipts remain Runtime/audit evidence rather than model-authored
top-level fields.

## 9. Prompt revision plan

### 9.1 Preserve the strong existing sections

Keep the draft's current strengths:

- operative-goal resolution across multi-turn context;
- clear classification definitions;
- explicit authority order;
- Strategy Cards before Habits;
- card composition rather than forced single-card selection;
- AI-suited execution methods;
- verification and replanning awareness; and
- exact JSON-only output.

### 9.2 Add the new inputs

Add concise sections for:

- downstream `$execution_capabilities`; and
- `$request_resources`.

The complete Playbook remains under the existing `$strategy_cards` section.

### 9.3 Replace paper-planning language with positive tool-capable language

Replace the current limited-reconnaissance paragraph with positive guidance
equivalent to:

> Use the available tools and skills proactively when they help you understand
> the task, reduce material uncertainty, inspect the real working context, or
> prepare a stronger execution strategy. Ground the execution brief in current
> evidence and useful preparatory work. Carry forward material results so
> downstream Execution can continue from the resulting state without
> unnecessarily repeating completed work.

Avoid adding a Strategy-specific prohibition list. Existing authority and tool
enforcement continue to apply.

### 9.4 Tighten the Strategy/Planning distinction

Describe `execution_brief` as a strategic handoff rather than a detailed plan.
The prompt should encourage the model to express:

- the best overall approach;
- the work's major stages;
- dependency and parallelism structure;
- verification standards;
- successful end state; and
- replanning triggers.

It should positively leave tactical tool choice and local execution decisions
responsive to evidence encountered by Execution.

### 9.5 Record useful preparation without adding fields

When Strategy has already inspected, searched, changed, or prepared something
that materially affects the remaining work, the execution brief should reflect
the resulting state naturally:

- `strategy` states the current evidence-grounded approach;
- `stages` describes the major remaining work rather than blindly replaying
  already completed preparation; and
- verification/success criteria account for the current state.

No separate observations or completed-work field is added in v3.

## 10. Runtime data-model changes

### 10.1 Strategy decision type

Introduce `StrategyDecision`, or extend the existing decision type under a
compatibility alias, with:

```text
classification
real_goal
selected_strategy_cards
relevant_habits
execution_brief
clarification
```

Prefer a new semantic type named `StrategyDecision` while retaining the
`Stage.TRIAGE` wire value during the experiment.

### 10.2 Turn state

Add bounded state for:

- the frozen Playbook version/hash;
- selected Strategy Card IDs;
- the validated execution brief; and
- the validated Strategy response needed for Execution handoff.

Do not duplicate the complete Playbook into the Ledger. The complete prompt and
response remain in orchestration logs; the Ledger stores only the current
control references required for recovery.

### 10.3 Validation

Replace `parse_triage()` with Strategy-aware validation that:

- preserves current classification and `real_goal` validation;
- preserves current conditional clarification behaviour;
- validates the six-field execution brief shape;
- rejects duplicate/empty card IDs;
- checks selected card IDs against the frozen Playbook snapshot without using a
  fixed enum; and
- preserves the current Habit reference contract.

JSON Repair receives schema v3 and repairs only the structured envelope. It
must not rerun the original tool-capable Strategy invocation, because that
invocation may already have produced side effects. Existing specialist repair
already follows the correct no-source-replay design and must remain covered by
tests.

### 10.4 Recording and audit

Replace or extend `_record_triage()` so it records:

- immutable classification;
- resolved `real_goal` and goal hash;
- selected card IDs;
- relevant Habits;
- execution-brief digest or bounded content;
- Playbook version/hash; and
- Strategy tool/evidence receipt references already captured by Runtime.

The classification remains authoritative and immutable for the turn.

## 11. Runtime invocation changes

### 11.1 Pre-Strategy preparation

Before the parallel Immediate/Strategy race:

1. retrieve the complete current Habit catalogue as today;
2. load and freeze the complete external Playbook;
3. build the concise downstream execution-capability catalogue;
4. build the thin request-resource index; and
5. render schema v3.

These are data preparation steps, not semantic card selection.

### 11.2 Parallel invocation

Continue invoking Immediate Response and the authoritative initial stage in
parallel, but render `system_strategy` instead of `system_triage`.

Invoke Strategy with:

```python
allow_tools=True
allow_side_effects=<same authority used by primary Execution>
```

Do not add a delegated-tool subset. The Provider/Tool Gateway receives the
normal tool set available to the current HASHI request.

### 11.3 Tool-capable modality

The selected Strategy route must support the request's authoritative input and
the normal tool path. Voice-origin routing must not silently force Strategy
back into a tool-free native-audio profile. If the configured native audio
model cannot use tools, Strategy should use the existing authoritative text or
transcript-capable tool route while Immediate Response may retain its native
audio presentation path.

This is capability preservation, not a new tool restriction.

### 11.4 Recovery and side effects

The Strategy invocation uses the existing provider activity tracker and
side-effect-aware replay rules.

- A successful tool-capable Strategy response is validated once.
- Invalid JSON uses isolated JSON Repair over the preserved response.
- Runtime does not replay the source Strategy call merely to repair formatting.
- Provider retry is allowed only when the existing tracker proves replay safe.
- Completed Strategy tool receipts remain attached to the turn.

No new retry limit or Strategy-specific safety ceiling is introduced.

## 12. Strategy-to-Execution handoff

### 12.1 Explicit Strategy context

Primary Execution must receive a rendered Strategy context containing:

```json
{
  "selected_strategy_cards": [],
  "execution_brief": {}
}
```

This is the validated Strategy result, not a second model-generated plan.

Add a clear Strategy handoff section to `system_execution.txt`. Execution should
use it as the high-level approach while retaining tactical judgement based on
current evidence and tool results.

### 12.2 Continuity after Strategy preparation

The same Workzone and external state continue into Execution. Strategy tool
receipts remain in the turn's audit/evidence state. The prompt tells Execution
to continue from the current state and use the brief's remaining stages rather
than assuming no preparation has occurred.

The v3 model output is not expanded with raw tool results. Material discoveries
that affect the approach belong naturally in the brief; detailed receipts stay
in Runtime/audit evidence.

### 12.3 Active-plan compatibility

Current Replanning and lifecycle code expects an `active_plan` envelope with
`plan` and `success_criteria`. For medium/high/xhigh/max work turns, Runtime can
derive the compatibility envelope deterministically from the validated brief:

```json
{
  "strategy": "<execution_brief.strategy>",
  "plan": ["<stage 1>", "<stage 2>"],
  "dependencies": ["<dependency 1>"],
  "verification": ["<verification rule 1>"],
  "success_criteria": ["<success criterion 1>"],
  "replan_conditions": ["<replan condition 1>"],
  "selected_strategy_cards": []
}
```

This is a mechanical compatibility mapping, not a second planning decision.
It lets current plan IDs, compulsory Replanning, Review evidence, and
Finalisation continue to work while the Strategy experiment proceeds.

Fast (`low`) may continue `TRIAGED -> EXECUTING` while receiving the explicit
Strategy context. Planned and higher modes may record `TRIAGED -> PLANNED`
after activating the mechanically derived envelope, without invoking a second
general Planning model.

## 13. Disposition of the old Planning stage

### 13.1 Normal SIMPLE and COMPLEX work

The target treatment path bypasses the separate general Planning model for
SIMPLE_TASK and COMPLEX_TASK. Strategy already owns the high-level execution
approach; a second model should not redesign it before Execution.

The code and prompt asset are retained during the HASHI3 experiment for easy
comparison and rollback, but are not on the normal treatment path.

### 13.2 HIGH_VOLUME compatibility

Current Runtime-managed sub-agent dispatch depends on Planning's detailed
`sub_agents` and `parallel_groups` output. Those fields intentionally do not
belong in the minimal Strategy schema.

For the first HASHI3 experiment, preserve the existing Planning call only for
HIGH_VOLUME_TASK as a tactical delegation materializer:

- it receives the validated Strategy brief as fixed guidance;
- it may translate the strategy into bounded assignments, exact profiles,
  attachment IDs, delegated tools, and execution waves;
- it must not reclassify the request, replace `real_goal`, or redesign the
  strategic approach; and
- primary Execution still receives the original Strategy brief plus the
  Runtime-attached delegated results.

This compatibility path prevents Triage-to-Strategy from accidentally removing
an existing high-volume capability. After Strategy behaviour is proven, a
separate experiment may decide whether assignment materialisation belongs in
Execution, a dedicated compiler, or an extended optional Strategy mechanism.
That decision does not require adding sub-agent fields to schema v3 now.

## 14. File-by-file implementation map

### 14.1 New Playbook data and loader

| File | Planned change |
|---|---|
| `orchestrator/her_v2/playbook_assets/strategy_playbook.json` | Add the complete version-controlled Strategy Card catalogue using the generic card structure |
| `orchestrator/her_v2/strategy_playbook.py` | Load, validate, freeze, hash, render, and resolve card IDs without semantic selection |

### 14.2 Prompt and schema layer

| File | Planned change |
|---|---|
| `orchestrator/her_v2/prompt_assets/system_strategy.txt` | Finalise positive tool-capable Strategy instructions; add execution capabilities/resources; keep full Playbook input; define strategic brief granularity |
| `orchestrator/her_v2/prompt_assets/system_execution.txt` | Add validated Strategy handoff and continuity guidance |
| `orchestrator/her_v2/prompt_assets/system_planning.txt` | Narrow the HIGH_VOLUME compatibility use to tactical assignment materialisation when invoked from Strategy mode |
| `orchestrator/her_v2/prompt_catalog.py` | Register and validate `system_strategy` placeholders; update Execution/Planning placeholder sets |
| `orchestrator/her_v2/prompts.py` | Add schema v3, render Strategy, render Strategy context for Execution, and retain JSON Repair support |

### 14.3 Canonical types and validation

| File | Planned change |
|---|---|
| `orchestrator/her_v2/models.py` | Add `StrategyDecision` and bounded execution-brief representation while retaining compatible stage/route wire values |
| `orchestrator/her_v2/structured.py` | Add `parse_strategy()` and schema-v3 validation; preserve Direct/Confirmation semantics and Habit compatibility |
| `orchestrator/her_v2/runtime_support.py` | Record Strategy result, Playbook reference, selected cards, and resolved goal |

### 14.4 Runtime and provider flow

| File | Planned change |
|---|---|
| `orchestrator/her_v2/runtime.py` | Load Playbook, build Strategy inputs, invoke Strategy with tools/side effects, retain race handling, create handoff, bypass general Planning, and preserve HIGH_VOLUME materialisation |
| `orchestrator/her_v2/runtime_invocation.py` | Preserve Strategy tool/evidence receipts and side-effect-aware recovery invariants; expose Strategy context where required |
| `orchestrator/her_v2/config.py` | Relabel/default the current Triage role as Strategy without changing persisted wire identity; ensure the selected route is capable of full context and tools |
| `orchestrator/her_v2/runtime_configuration.py` | Present Strategy in configuration/UI while retaining compatible route keys during the experiment |
| `adapters/her_v2_provider.py` | Verify that tool-capable Strategy receives the normal provider tool path and complete PCM; adjust only if current stage-specific handling blocks it |
| `adapters/her_v2.py` | Update presentation/audit labels only where the outer adapter exposes Triage naming or assumes tool-free Triage |

### 14.5 Documentation after implementation

| File | Planned change |
|---|---|
| `docs/HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md` | Update the initial-processing, Planning, Execution, lifecycle, audit, and Habit sections after the experiment contract is accepted |
| `docs/HER_V2_TESTING_PLAN.md` | Add Strategy schema, Playbook, tool, race, and handoff coverage |
| `docs/HER_V2_WIP_JOURNAL.md` | Record implementation checkpoints and verified HASHI3 experiment results |

## 15. Implementation sequence

### Phase 0 — Freeze the HASHI3 baseline

1. Preserve the user-authored untracked Strategy prompt.
2. Record the current Git revision, prompt hash, stage routes, models, and
   reasoning settings.
3. Run the current focused HER v2 prompt/structured/runtime tests before
   changing behaviour.
4. Capture representative baseline traces for Direct, Simple, Complex,
   High-volume, Confirmation, and tool-using tasks.

### Phase 1 — Add the external Playbook

1. Convert the agreed Strategy Cards into the generic versioned Playbook asset.
2. Implement the loader and integrity validation.
3. Add deterministic snapshot/version/hash tests.
4. Confirm no prompt/schema code contains a fixed card enum.

### Phase 2 — Define schema v3 and parser

1. Add the minimal schema-v3 example and frozen repair schema.
2. Add `StrategyDecision`.
3. Implement classification-specific validation.
4. Validate card references against the turn's Playbook snapshot.
5. Preserve existing Habit representation and clarification behaviour.
6. Add JSON Repair tests proving that a malformed envelope does not replay the
   original Strategy tools or side effects.

### Phase 3 — Finalise and register the Strategy prompt

1. Apply the positive preparation/tool wording.
2. Add complete Playbook, execution capabilities, and request resources.
3. Replace planning-level output wording with the six-field strategic brief.
4. Register `system_strategy` in the prompt catalogue.
5. Render Strategy through the current compatible `Stage.TRIAGE` wire stage.

### Phase 4 — Enable the tool-capable Strategy invocation

1. Attach the normal HASHI tool path.
2. Use the same current-request side-effect capability envelope as Execution.
3. Verify complete PCM and attachment delivery.
4. Preserve Immediate/Strategy race behaviour.
5. Verify provider recovery and no-replay handling after tool activity.
6. Verify voice-origin Strategy still reaches a tool-capable route.

### Phase 5 — Wire Strategy state and Execution handoff

1. Record selected cards, brief, and Playbook reference.
2. Pass Strategy context into primary Execution.
3. Tell Execution to continue from current state and use tactical judgement.
4. Mechanically derive active-plan compatibility for modes that require plan
   IDs/Replanning.
5. Ensure Strategy tool receipts remain part of turn evidence and audit.

### Phase 6 — Remove duplicate general Planning from the treatment path

1. Bypass general Planning for SIMPLE_TASK and COMPLEX_TASK.
2. Preserve low-effort lifecycle semantics while still passing Strategy.
3. Preserve medium+ plan IDs through the compatibility envelope.
4. Restrict the remaining Planning invocation to HIGH_VOLUME tactical
   assignment materialisation.
5. Confirm Replanning continues to replace active plan versions without
   changing the original Strategy classification or goal.

### Phase 7 — Update adapters, labels, and documentation

1. Present the role as Strategy/Strategist in HASHI3 UI and audit summaries.
2. Retain compatible internal Triage route/state values.
3. Update the canonical HER v2 design and testing documents after behaviour is
   verified.
4. Record the experiment and any deviations from this plan.

### Phase 8 — HASHI3 experiment and decision

1. Run deterministic contract tests.
2. Run controlled representative tasks through current baseline and Strategy
   treatment where practical.
3. Review Strategy Card selection, brief quality, tool preparation, handoff,
   execution duplication, and final outcome.
4. Decide whether to keep, revise, or roll back the treatment before any other
   HASHI instance is changed.

## 16. Verification plan

### 16.1 Prompt and asset contracts

Verify that:

- every prompt asset placeholder is exact and fully substituted;
- Strategy receives the complete Playbook, not a Runtime-selected subset;
- the Playbook is external to prompt/schema code;
- card additions/removals do not require schema changes;
- Strategy receives complete PCM;
- execution capabilities and request resources are present; and
- no redundant generated authority or execution-policy context is injected.

### 16.2 Structured-output contracts

Cover:

- valid Simple, Complex, and High-volume briefs;
- Direct with an empty brief;
- Confirmation with conditional clarification;
- duplicate, empty, and unknown card IDs;
- malformed/missing brief fields;
- existing Habit reference behaviour;
- JSON in text/data/provider compatibility forms; and
- JSON Repair without source-stage replay.

### 16.3 Runtime journeys

Cover:

- Immediate wins the race, then Strategy produces work;
- Strategy wins the race while Immediate remains pending;
- Direct uses exactly one Immediate final;
- Confirmation supersedes/cancels a pending acknowledgement correctly;
- Simple work receives Strategy and skips general Planning;
- Complex work receives Strategy, plan compatibility, and Execution;
- High-volume work retains Runtime-managed sub-agent delegation;
- Strategy performs tool calls before structured output;
- Strategy performs a side effect and Execution does not repeat it merely
  because the stage boundary changed;
- Strategy provider recovery is blocked when side-effect replay is unsafe;
- `/stop` and `/steer` keep their current typed behaviour; and
- Review, Replanning, Finalisation, and terminal truth remain unchanged.

### 16.4 Tool and skill capability

Assert on actual `StageRequest` values:

```text
stage = TRIAGE (compatibility wire value)
role = Strategy/Strategist
allow_tools = true
allow_side_effects = current Execution authority
```

Verify that the provider attaches the same normal HASHI tools/skills available
for the request and does not apply a hidden Triage-only filter.

### 16.5 Multimodal and voice paths

Verify:

- Strategy can read authorised local attachment references;
- request resources contain accurate attachment IDs and modalities;
- native media routing does not strip the complete PCM;
- voice-origin Strategy remains tool-capable; and
- Immediate voice presentation remains independent of the Strategy tool route.

### 16.6 Replanning and evidence continuity

Verify:

- selected cards and the original brief remain available across Replanning;
- a Replan may change the active approach while classification and goal remain
  immutable;
- completed Strategy/Execution tool receipts remain deduplicated;
- plan-version activation remains append-only; and
- Finalisation receives correct completion evidence.

## 17. HASHI3 experiment design

The first experiment should answer whether the new Strategist improves real
execution, not merely whether it emits valid JSON.

### 17.1 Baseline

Current HASHI3 behaviour:

```text
tool-free Triage → optional formal Planning → Execution
```

### 17.2 Treatment

```text
tool-capable Strategy with complete Playbook
  → direct Strategy handoff
  → Execution
```

HIGH_VOLUME retains the tactical assignment-materialisation compatibility
step during the first experiment.

### 17.3 Representative workload families

Use real or realistic HASHI3 tasks covering:

- repository diagnosis and code change;
- current external research and recommendation;
- document/media inspection;
- browser/computer-use work;
- transactional or state-changing work;
- confirmation-required ambiguity;
- simple bounded execution; and
- high-volume parallelisable work.

### 17.4 Evaluation questions

For each run, review:

- Was `real_goal` resolved correctly?
- Was classification correct?
- Did the Strategist select useful cards from the complete Playbook?
- Did selected cards materially influence the brief?
- Was the brief strategic rather than an over-detailed command plan?
- Did tool use improve the strategy or useful preparation?
- Did Execution continue from Strategy's resulting state?
- Did Execution unnecessarily redo Strategy work?
- Did the brief help Execution verify the right outcome?
- Were replan conditions useful when evidence changed?
- Did the final result satisfy the user better than the baseline?
- What latency and token cost did the complete Playbook add?

No hard card-count, tool-count, token, or elapsed-time ceiling is introduced by
this experiment. Observed consumption is measured rather than used to suppress
the Strategist's reasoning.

### 17.5 Promotion decision

Promote the Strategy treatment inside HASHI3 only when:

- deterministic contracts pass;
- Direct/Confirmation and Immediate race behaviour do not regress;
- tool/side-effect recovery remains truthful and replay-safe;
- representative work shows useful card selection and handoff;
- Execution does not systematically repeat Strategy preparation;
- high-volume delegation remains available; and
- no material final-delivery, audit, media, or lifecycle regression appears.

Any rollout beyond HASHI3 requires a separate decision and implementation
authority.

## 18. Completion criteria for the implementation

The Triage-to-Strategy rewiring is complete only when all of the following are
true:

1. `system_strategy.txt` is the active initial authoritative prompt.
2. Strategy receives the complete PCM.
3. Strategy receives the complete external Playbook.
4. Runtime performs no semantic card preselection.
5. Strategy receives normal HASHI tools and skills with the current execution
   authority, including side-effect-capable tools when available.
6. Schema v3 adds only `selected_strategy_cards` and `execution_brief` to the
   existing Triage semantics.
7. The execution brief contains exactly the six agreed strategic fields.
8. Selected card IDs are generic references validated against the frozen
   Playbook rather than a hard-coded enum.
9. Primary Execution receives and uses the Strategy handoff.
10. Strategy preparation is not unnecessarily repeated across the stage
    boundary.
11. General Planning is bypassed for ordinary Simple/Complex treatment turns.
12. High-volume Runtime delegation is preserved during the initial experiment.
13. Replanning, Review, Finalisation, audit, media, and delivery contracts pass
    their regression suites.
14. HASHI3 experiment evidence is recorded before wider rollout is considered.

## 19. Activation and rollback

This document authorises no runtime activation.

Implementation changes should be committed in coherent, tested HASHI3
checkpoints. After implementation and verification, activation requires an
explicitly authorised HASHI hot reload. No hard/cold restart is part of this
plan.

Rollback during the experiment consists of restoring the prior prompt/schema
and current Triage→Planning route from the last verified HASHI3 checkpoint,
then performing only an explicitly authorised hot reload. HASHI1 and HASHI2 are
outside the scope of every implementation and experiment step in this plan.
