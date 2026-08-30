# HER v2 Continuous Strategy + Execution Implementation Plan

| Field | Value |
|---|---|
| Status | Proposed implementation plan only; no runtime implementation is authorised by this document |
| Date | 2026-08-30 |
| Repository baseline | `f3c5605e` (`dadf276b` Strategy Fast Path + `cf4c5704` Smart Tool Registry) |
| First implementation slice | HER v2 Strategic Fast / Low effort |
| Primary invariant | One Primary provider invocation, one request-local app-server process, one Codex thread, one active Codex turn, and any number of real HASHI tool calls inside that turn |
| Existing features preserved | HASHI PCM, Strategy schema v3, full 38-card Strategy Playbook, card picking, selected-card execution guidance, Habits, Smart Tool Registry, Tool Registry authority, evidence, permissions, Workzones, media handling, cancellation, and audit |
| Explicitly unchanged in v1 | Medium-and-higher Planning/Replanning/Review paths, public Chat Completions compatibility, and cross-request PCM/session behavior |

## 1. Decision

The first implementation will combine the current Strategist and Low/Fast
Execution into one continuous Primary job.

The Strategist remains a real logical phase, but it will no longer be a
separate model invocation that finishes with JSON and hands a reconstructed
prompt to a fresh Execution agent. Instead, the same agent will:

1. receive the current HASHI PCM, complete Strategy Playbook, Habit catalogue,
   request resources, execution capabilities, and Smart Tool surface once;
2. use the already-implemented Strategy instructions to resolve the goal,
   classify the request, select Strategy Cards, select Habits, and prepare the
   existing six-field `execution_brief`;
3. call one internal `hashi_strategy_commit` control function using the existing
   Strategy schema v3;
4. wait while HER validates and durably records that Strategy decision;
5. for a work classification, continue in the same Codex turn and use Smart
   Tool repeatedly until the goal is completed and verified; and
6. return the normal Low/Fast natural-language result from that same turn.

The target work path is:

```text
one outer HASHI request
  -> one HER ledger turn
     -> optional Immediate Response remains independent
     -> one continuous Primary Strategy + Execution job
        -> one app-server process
        -> thread/start once
        -> turn/start once
        -> read the full Playbook once
        -> select existing Strategy Cards
        -> hashi_strategy_commit(schema_v3)
        -> HER validates and records the existing StrategyDecision
        -> Smart Tool call -> real result -> same turn continues
        -> Smart Tool call -> real result -> same turn continues
        -> final Low/Fast response
        -> turn/completed
```

This is one Codex turn, not one upstream inference. Codex may perform another
model inference after a tool result. The required improvement is that every
inference remains part of the same active turn instead of HASHI starting a new
app-server, thread, and turn and rebuilding the task after every tool call.

The official Codex app-server contract defines a thread as a conversation, a
turn as one user request plus the agent work that follows, and an experimental
dynamic tool call as a server request that the client answers before the same
turn continues. See the [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server#core-primitives).

## 2. Why the first slice is Strategic Fast / Low

HASHI3 already has a clean, measured Low/Fast baseline:

- `dadf276b` implemented Strategy schema v3, the versioned 38-card Playbook,
  intelligent card selection, and the selected-card `strategy_handoff`;
- Low effort already skips the separate Planning stage;
- Low effort has no compulsory Replanning or Review stage;
- `cf4c5704` added the minimal Smart Tool Registry without changing underlying
  Tool Registry authority; and
- the latest hidden Calendar Scheduling run retained 17/17 quality while
  identifying repeated Execution provider calls as the next major harness
  overhead.

Therefore v1 changes only two boundaries that still waste time and tokens:

1. the Strategist-to-Execution provider handoff; and
2. the new app-server/thread/turn created after every tool result.

Medium and higher effort modes remain on the current path during this first
release. That keeps Planning, high-volume sub-agent assignment, compulsory
Replanning, Review, and remediation out of the transport experiment. They may
adopt the same continuous transport later, after the Low path proves quality,
cleanup, and measurable efficiency.

## 3. Current HASHI3 baseline

### 3.1 Strategy is already implemented

The following are existing assets, not proposed new systems:

| Existing asset | Current responsibility |
|---|---|
| `orchestrator/her_v2/prompt_assets/system_strategy.txt` | Resolves the goal, classifies the request, reads the full Playbook, selects cards and Habits, and produces `execution_brief` |
| `orchestrator/her_v2/playbook_assets/strategy_playbook.json` | Version `2026-08-29.1`, containing 38 versioned Strategy Cards |
| `orchestrator/her_v2/strategy_playbook.py` | Loads, validates, freezes, hashes, renders, and resolves exact card IDs |
| `orchestrator/her_v2/models.py::StrategyDecision` | Typed schema-v3 result |
| `orchestrator/her_v2/structured.py::parse_strategy()` | Existing schema-v3 validator |
| `orchestrator/her_v2/runtime_support.py::_record_strategy()` | Records the resolved goal, Habits, selected card snapshots, execution brief, Playbook reference, audit event, and ledger classification |
| `orchestrator/her_v2/prompt_assets/system_execution.txt` | Tells the separate Execution agent how to use selected card snapshots and the execution brief |

The new thread design must reuse these semantics. It must not introduce a
second card catalogue, card ranker, semantic retriever, plan schema, card-count
rule, or competing Strategy validator.

### 3.2 Current card handoff

Today the Strategist reads the complete Playbook and returns exact Card IDs.
Runtime resolves only those IDs and builds:

```json
{
  "selected_strategy_cards": [
    {"id": "CODE_MODIFY", "...": "existing complete card snapshot"}
  ],
  "execution_brief": {
    "strategy": "...",
    "stages": [],
    "dependencies": [],
    "verification": [],
    "success_criteria": [],
    "replan_conditions": []
  }
}
```

That is the correct contract for two separate agents. In the continuous design
there is no second agent to hand the data to. The same active turn already has
the full Playbook and remembers which cards it selected. Runtime will still
build and persist this exact existing `strategy_handoff` for audit, recovery,
and legacy compatibility, but it will not inject a second copy into the same
turn.

### 3.3 Current discontinuous tool loop

The current `hashi-api` path behaves approximately as follows:

```text
provider round 1
  -> Gateway starts app-server A / thread A / turn A
  -> Codex requests a tool
  -> bridge returns HASHI_EXTERNAL_TOOL_DEFERRED
  -> bridge interrupts turn A and destroys app-server A
  -> HashiApiAdapter runs the real tool

provider round 2
  -> Gateway starts app-server B / thread B / turn B
  -> reconstructed messages include the earlier call and result
  -> Codex reasons again and requests another tool
  -> repeat
```

The exact current boundaries are visible in:

- `adapters/hashi_api.py::generate_response()`, which loops over Chat
  Completions responses and appends tool results to `messages`; and
- `adapters/codex_app_server.py::CodexAppServerToolBridge.run()`, which uses an
  ephemeral thread, returns `HASHI_EXTERNAL_TOOL_DEFERRED`, interrupts after a
  dynamic tool call, and tears down the process.

Adding a `session_id` or calling `thread/resume` would preserve some visible
history but would still create a new turn. It would not answer the original
pending `item/tool/call` request or preserve the active turn.

## 4. Locked v1 boundaries

| Question | v1 decision |
|---|---|
| Which path changes first? | Strategic Fast / Low only. |
| Is Strategy Card selection reimplemented? | No. Reuse the current full Playbook, current Strategist selection instructions, schema v3, parser, resolver, and recorder. |
| Is there still a separate Strategist provider call? | No on the enabled continuous path. Strategy is phase 1 of the same Primary turn. |
| Is there still a separate Planning call? | No for Low, exactly as today. |
| Can the model change after Strategy commit? | No. One Codex turn uses one configured provider/model/reasoning target. |
| Where do real tools execute? | In HER through the existing Smart Tool and Tool Registry chain, never inside the Gateway or app-server sandbox. |
| How does HER communicate with the Gateway while the turn is live? | One authenticated private WebSocket for the lifetime of the Primary job. |
| Is the thread reused across outer user requests? | No. The thread is request-local and ephemeral. |
| Does `HashiApiAdapter.supports_sessions` change? | No. Provider-turn continuity is not HASHI conversation-session persistence. |
| Does the public `/v1/chat/completions` route change? | No. It remains the legacy/stateless compatibility route. |
| Are new time, token, tool-call, or turn ceilings added? | No. Existing cancellation and safety policy remain authoritative. |
| What happens to Medium and higher effort? | They stay on the current path until a later, separately measured expansion. |

## 5. Exact Strategy Card use inside the thread

### 5.1 One Playbook injection

At `thread/start`/`turn/start`, the combined prompt receives the same immutable
Playbook snapshot currently supplied to Strategy:

```text
playbook_version
sha256
all 38 existing card records
```

The complete Playbook is supplied once because the agent must intelligently
select and compose cards. HASHI does not preselect cards and does not invent a
new retrieval layer.

After Strategy commit:

- HASHI does not send the full Playbook again;
- HASHI does not send a second selected-card handoff into the same turn;
- the selected IDs and `execution_brief` become the explicit active Strategy;
- the agent continues with the original Playbook context already present; and
- only a replay into a fresh process may receive the existing compact
  selected-card snapshots instead of the full Playbook.

This preserves the current design goal: the model reads the full Playbook to
choose cards once, while execution focuses on the chosen cards without a new
agent rereading the catalogue.

### 5.2 Reuse Strategy schema v3 as the commit payload

Add one internal dynamic control function:

```text
hashi_strategy_commit
```

Its arguments are exactly the already-implemented Strategy schema v3:

```json
{
  "classification": "SIMPLE_TASK | COMPLEX_TASK | HIGH_VOLUME_TASK | DIRECT_RESPONSE | CONFIRMATION_REQUIRED",
  "real_goal": "resolved current-turn goal",
  "selected_strategy_cards": ["exact existing Card ID"],
  "relevant_habits": ["exact unchanged Habit entry"],
  "execution_brief": {
    "strategy": "overall strategic approach",
    "stages": ["major strategic stage"],
    "dependencies": ["important dependency"],
    "verification": ["verification method or evidence standard"],
    "success_criteria": ["observable completion condition"],
    "replan_conditions": ["condition requiring reconsideration"]
  },
  "clarification": null
}
```

The implementation must call the current `parse_strategy()` validator and then
the frozen Playbook snapshot's current `resolve_cards()` method. It must use the
current Habit representation and current confirmation/direct-response rules.
No semantically new Strategy field is added.

On acceptance, Runtime calls the current `_record_strategy()` path, preserves
the current `strategy_handoff` and `strategy_playbook_ref`, and replies to the
same pending dynamic tool request with a compact result such as:

```json
{
  "accepted": true,
  "classification": "COMPLEX_TASK",
  "selected_strategy_cards": ["CODE_MODIFY", "TEST_QA"],
  "next": "continue_execution"
}
```

The reply does not repeat card bodies. They are already in the same turn.

An invalid commit returns a typed validation result to the same pending call.
The agent may correct and resubmit in the same active turn. A rejected commit
must not fabricate a `TRIAGED` ledger state.

### 5.3 Commit first, then work

For the Low/Fast continuous slice, `hashi_strategy_commit` is the required first
dynamic call. Smart Tool calls before a valid commit receive a typed
`HASHI_STRATEGY_NOT_COMMITTED` result and are not executed.

This deliberately implements the requested order:

```text
pick cards -> commit execution brief -> do the work
```

Tool-assisted reconnaissance that the separate Strategist could previously do
becomes an early Execution step after commit. This narrow change makes the
phase boundary host-verifiable and prevents task-changing work from occurring
before the Strategy decision is durable.

`hashi_strategy_commit` is a HER control call, not a Smart Tool call. It:

- cannot access files, the network, apps, or external systems;
- does not create a Smart Tool ledger row or Tool Evidence Receipt;
- does not count as an Execution tool result; and
- cannot grant permissions, change Workzones, or broaden the user goal.

### 5.4 How the selected cards guide Execution

The combined prompt must say explicitly that, after commit, the agent applies
the selected cards rather than merely reporting their IDs:

- each card's `strategy` and `topology` guide sequencing and tool use;
- each card's `validation` guidance informs actual verification;
- each card's `failure_modes` informs error recognition and adaptation;
- card composition guidance resolves compatible primary/supporting strategies;
- `execution_brief.stages` and `dependencies` anchor the execution path;
- `execution_brief.success_criteria` determine whether work is complete; and
- `execution_brief.replan_conditions` identify when the approach should be
  reconsidered from new evidence.

Cards remain advisory below system/user authority, permissions, and current
evidence, exactly as in the current Strategy prompt. A card never grants a tool
or side effect.

## 6. Combined prompt design

The existing `system_strategy.txt` card-selection logic is the source of truth.
Do not copy it into an independently maintained second prompt.

Refactor only the final Strategy output boundary so the same Strategy core can
be rendered in two modes during rollout:

1. **Legacy mode:** return the current schema-v3 JSON to the separate Runtime
   parser, preserving Medium-and-higher behavior.
2. **Continuous mode:** call `hashi_strategy_commit`, wait for acceptance, then
   follow the existing Execution contract and produce the natural-language
   Low/Fast result.

The combined prompt is assembled in this order:

1. current Strategy role, authority, goal-resolution, classification, card
   selection, Habit selection, and execution-brief instructions;
2. current full Playbook snapshot;
3. current execution capabilities and request resources;
4. the `hashi_strategy_commit` requirement and schema;
5. current Smart Tool catalogue and truthful tool-result rules;
6. selected-card execution rules;
7. current Persona guidance for the user-facing result; and
8. the current bridge-managed PCM/current request content already supplied by
   HASHI.

The combined prompt must not include both a legacy "return JSON and stop" rule
and a continuous "commit and continue" rule. Prompt tests must prove that only
one output contract is active.

## 7. Continuous transport

### 7.1 Why the current HTTP loop cannot continue the turn

The current Chat Completions request ends when it returns a tool call. HER then
executes the tool and sends another HTTP request. A still-active app-server turn
instead requires the Gateway to send a tool request to HER, wait for the real
result, answer the original app-server server request, and keep reading the same
turn.

Use one private authenticated endpoint:

```text
GET /v1/hashi/continuous-turn
```

The endpoint uses WebSocket because the Gateway and HER must exchange messages
in both directions while the original app-server turn remains open. This is an
internal HASHI protocol, not a public OpenAI-compatible API extension.

### 7.2 Minimal frame types

Only the following frame types are required in v1:

| Direction | Type | Purpose |
|---|---|---|
| HER -> Gateway | `start` | Send request ID, exact model/reasoning, initial messages/content, dynamic tool schemas, and context fingerprints |
| Gateway -> HER | `turn_started` | Return Gateway job ID, Codex thread ID, and Codex turn ID |
| Gateway -> HER | `tool_call` | Relay the exact pending app-server request ID, call ID, name, and arguments |
| HER -> Gateway | `tool_result` | Return the exact matched real result and content items |
| Gateway -> HER | `provider_activity` | Preserve current liveness/activity reporting without exposing private reasoning |
| Gateway -> HER | `completed` | Return final text, usage, IDs, and counts after `turn/completed` |
| Either direction | `cancel` / `cancelled` | Stop the active job and confirm cleanup |
| Either direction | `error` | Carry a typed protocol/provider failure |

Every frame carries the request-local job identity. Duplicate, missing,
mismatched, unknown, or late call/result IDs fail closed.

### 7.3 Gateway-to-app-server behavior

Add a separate `CodexAppServerToolBridge.run_continuous()` path. It will:

1. start one isolated app-server process in one temporary directory;
2. initialize once with the experimental API capability;
3. call `thread/start` once with the caller-owned dynamic tools;
4. call `turn/start` once;
5. read app-server events until `turn/completed`;
6. on `item/tool/call`, relay the call through the WebSocket and wait for the
   exact HER result;
7. answer that same app-server JSON-RPC server request with the real
   `contentItems` and success value; and
8. clean up the process and directory only after completion, cancellation,
   disconnect, or typed failure.

The continuous path must never:

- send `HASHI_EXTERNAL_TOOL_DEFERRED`;
- interrupt a work turn merely because a tool was requested;
- return an `assistant.tool_calls` batch to start another Chat Completions
  round;
- call `thread/inject_items` after each tool result; or
- create a new process, thread, or turn for normal continuation.

The current `run()` path remains unchanged for public/legacy compatibility.

### 7.4 HER adapter behavior

Add an explicit capability and method rather than overloading sessions:

```text
supports_continuous_tool_turn = true | false
generate_continuous_response(...)
```

`HashiApiAdapter.generate_continuous_response()` opens the WebSocket once,
executes every requested function through the request-local HER registry, sends
each real result back, and returns only when the Gateway reports completion.

It does not run the current outer `for loop_idx in count()` tool loop. The
existing `generate_response()` remains unchanged for all legacy paths.

## 8. HER runtime integration

### 8.1 Request-local coordinator

Add one small `StrategyExecutionCoordinator` owned by the current HER turn. It
stores only explicit control state:

```text
awaiting_strategy_commit
executing
direct_or_confirmation_terminal
completed / failed / cancelled
```

It also holds:

- the frozen existing Playbook snapshot;
- the accepted existing `StrategyDecision`;
- whether real work-tool admission is open;
- the Gateway job, Codex thread, and Codex turn IDs; and
- whether any real or side-effecting work has started.

It does not store hidden reasoning or claim to preserve provider KV state.

### 8.2 Low/Fast runtime branch

When all of the following are true:

- effort is Low;
- the feature flag is enabled;
- the selected provider supports continuous tool turns; and
- the configured combined profile is valid;

Runtime starts one combined Primary invocation instead of calling Strategy and
Execution separately.

At a valid work Strategy commit, Runtime atomically:

1. runs existing schema and card-ID validation;
2. runs existing `_record_strategy()`;
3. records the immutable classification and resolved goal in the current
   Ledger;
4. preserves the current `strategy_handoff` and Playbook reference;
5. transitions from the current initial state to `TRIAGED` and then
   `EXECUTING`; and
6. opens Smart Tool admission before returning the commit acceptance result.

Low effort continues to have `active_plan=None`; the implementation must not
invent a separate Planning result or fake a `PLANNED` lifecycle state. The
existing `execution_brief` is the active strategic guidance for this path.

When the same turn finishes, Runtime performs the current Low/Fast execution
completion, evidence merge, delivery, terminal transition, and Meditation
scheduling behavior.

### 8.3 Direct and confirmation decisions

The combined Strategy phase still uses the current five classifications.

- `DIRECT_RESPONSE`: the current prompt continues to avoid unnecessary cards
  and execution planning. The continuous work loop does not open. Runtime
  terminates the Primary Strategy turn and preserves the current
  Immediate/Direct response handling.
- `CONFIRMATION_REQUIRED`: cards, Habits, and execution brief remain empty and
  the existing concrete `clarification` is delivered. Runtime terminates the
  Primary Strategy turn and enters `PENDING_USER_INPUT`.
- Work classifications: the same turn continues into Execution.

The one-process/thread/turn acceptance invariant applies to the combined work
path. Direct and confirmation have no Execution loop to preserve.

### 8.4 One fixed model target

A Codex turn cannot change model or reasoning setting after Strategy commit.
The continuous Low path therefore receives one explicit combined provider
profile for the whole turn.

For the HASHI3 experiment, configure this profile to the same exact Luna model
and reasoning target used by the current Strategy/Execution comparison. Do not
silently select the larger of two profiles or switch models after
classification. Audit the configured target and show that the legacy
classification-specific Execution route is inactive for the enabled combined
job.

If the continuous capability or profile is invalid, fail before real work. Do
not silently fall back after a tool may have changed state.

## 9. PCM, Smart Tool, and authority preservation

### 9.1 PCM

HASHI PCM remains the only source of persona, current user authority,
conversation context, memory, date/time, Workzones, media references, and
system/developer instructions.

The current PCM strategy remains unchanged:

- HASHI assembles the authoritative typed envelope once per outer request;
- `HashiApiAdapter.supports_sessions` stays false;
- the Codex thread is not bound to a HASHI conversation session;
- a later user request receives a new PCM and a new provider thread; and
- no Gateway-side cache adds a second PCM copy.

The provider thread is only request-local execution continuity. It is not a
replacement for HASHI memory or PCM.

### 9.2 Smart Tool and Tool Registry

The work call path stays:

```text
Codex dynamic tool call
  -> HashiApiAdapter continuous client
  -> Strategy commit gate
  -> existing Smart Tool Registry
  -> existing evidence wrapper
  -> existing HASHI Tool Registry
  -> real result
  -> same pending app-server tool request
  -> same active Codex turn
```

The implementation preserves:

- the current provider-visible tool surface;
- current five-field Smart Tool results;
- current Tool Registry permissions and side-effect metadata;
- Workzone and attachment authorization;
- current command failure, non-zero exit, unavailable, partial, denial, and
  success semantics;
- current Smart Tool ledger rows and canonical Tool Evidence Receipts; and
- current repeat warnings without adding a blocking tool budget.

A normal tool error is returned to the same turn so the agent can adapt. It is
not converted into a transport failure or a new turn.

## 10. Failure, cancellation, and recovery

### 10.1 Normal correction inside the turn

The following remain in the same active turn:

- invalid Strategy arguments;
- an unknown or duplicate Strategy Card ID;
- a premature Smart Tool call;
- invalid tool arguments;
- a normal tool failure or unavailable dependency;
- a non-zero shell exit; and
- a policy denial.

Each returns a truthful typed result. None should restart app-server by itself.

### 10.2 User stop or steer

On cancellation, `/stop`, or an accepted `/steer` that supersedes the current
job:

1. close new tool admission;
2. settle or cancel active Tool Registry work under current rules;
3. send `turn/interrupt` to the still-active app-server turn;
4. close the WebSocket;
5. terminate the request-local process tree; and
6. preserve current Ledger state, receipts, and cleanup evidence.

No WebSocket task, pending tool future, app-server process, or temporary
directory may survive the HER turn.

### 10.3 Transport or process failure

A disconnect, malformed frame, incomplete app-server stream, or process exit is
a typed provider failure. Record:

- last confirmed Strategy/Execution phase;
- Gateway job, thread, and turn IDs;
- outstanding tool call IDs;
- whether any real or side-effecting work started; and
- cleanup outcome.

Recovery follows existing replay safety:

- before real work, one eligible fresh attempt may restart;
- after only completed proven read-only work, an eligible fresh attempt may use
  the persisted selected-card `strategy_handoff` and completed evidence;
- after unknown, incomplete, or side-effecting work begins, automatic replay is
  blocked; and
- a crashed app-server's hidden in-flight reasoning is not claimed to be
  recoverable.

## 11. Configuration and rollout gate

Add a default-off experimental block equivalent to:

```json
{
  "continuous_strategy_execution": {
    "enabled": false,
    "efforts": ["low"],
    "profile": "premium"
  }
}
```

The exact configuration shape should follow current HER v2 conventions, but
the semantics are fixed:

- scope is explicit;
- one profile owns the whole combined turn;
- provider capability is checked before work;
- Medium and higher modes are not implicitly opted in; and
- disabling the flag immediately restores the current `dadf276b` handoff path.

Do not remove the legacy path in this implementation.

## 12. Implementation map

| File or area | Planned change |
|---|---|
| `orchestrator/her_v2/prompt_assets/system_strategy.txt` | Refactor only the final output boundary so the existing card-selection core can serve legacy JSON mode and continuous commit mode without duplicated Strategy logic. |
| `orchestrator/her_v2/prompt_assets/system_execution.txt` | Reuse its selected-card application, truthful tool-result, Persona, and final-response rules in the continuous suffix. |
| `orchestrator/her_v2/prompts.py` | Render the combined Strategy/Execution contract and derive the internal commit schema from existing schema-v3 semantics. |
| `orchestrator/her_v2/prompt_catalog.py` | Register any mode/suffix placeholders and enforce exact rendering. |
| `orchestrator/her_v2/models.py` | Add only request-local continuous coordinator typing/capability fields; do not replace `StrategyDecision`. |
| `orchestrator/her_v2/structured.py` | Reuse `parse_strategy()`; expose a mapping validator if needed by the control function, without adding semantic fields. |
| `orchestrator/her_v2/strategy_execution.py` | Add one small request-local coordinator for Strategy commit, tool admission, phase state, IDs, and cancellation. |
| `orchestrator/her_v2/runtime.py` | Add the Low/Fast combined branch, commit-time `_record_strategy()` and lifecycle transitions, direct/confirmation termination, and final Low delivery. |
| `orchestrator/her_v2/runtime_invocation.py` | Carry the non-serialised coordinator and joined request IDs through the Primary invocation/retry boundary. |
| `adapters/her_v2_provider.py` | Add the internal commit control function, enforce commit-before-Smart-Tool, preserve current Smart Tool/evidence wrappers, and call the continuous adapter capability. |
| `adapters/base.py` | Add default-false `supports_continuous_tool_turn`, distinct from `supports_sessions`. |
| `adapters/hashi_api.py` | Add one WebSocket-based continuous method; execute calls through the existing request-local registry; leave the current Chat Completions loop unchanged. |
| `orchestrator/api_gateway.py` | Add the authenticated private continuous endpoint, frame validation, cancellation/drain handling, and joined metrics. |
| `adapters/codex_cli.py` | Expose the continuous app-server bridge operation while retaining current process tracking and MCP isolation. |
| `adapters/codex_app_server.py` | Add `run_continuous()` using real dynamic tool results and the same active turn; retain current `run()` unchanged. |
| Tests | Add focused Strategy, prompt, Runtime, adapter, Gateway, bridge, cancellation, and exact-count coverage. |
| Docs | Update the Codex bridge and HER v2 design/testing documents only after implementation is proven. |

Only one new HER core module is proposed. Do not add a general job framework,
global app-server pool, second Playbook loader, second Ledger, or duplicate Tool
Registry.

## 13. Delivery phases

### Phase 0: freeze the existing baseline

1. Freeze `f3c5605e` behavior and the current Smart Tool benchmark inputs.
2. Record exact PCM, model, reasoning, Playbook version/hash, Strategy-selected
   cards, Smart Tool schemas, permissions, Workzone, and benchmark environment.
3. Add process/thread/turn/provider-call telemetry to the current A arm without
   changing behavior.
4. Confirm the existing hidden verifier remains 17/17.

Exit gate: the current separate-turn baseline is reproducible.

### Phase 1: continuous dynamic-tool vertical slice

1. Implement the private WebSocket client and Gateway endpoint.
2. Implement `run_continuous()` alongside the current bridge.
3. Run the existing Execution prompt through one process/thread/turn with one,
   several, failed, and denied Smart Tool calls.
4. Implement usage aggregation, provider activity, disconnect, and cancellation
   cleanup.

Exit gate: real tool results return to exact pending calls, the turn continues,
and no deferred sentinel or post-tool Chat Completions request occurs.

### Phase 2: put existing Strategy inside the same turn

1. Refactor the current Strategy prompt output boundary without changing card
   selection semantics.
2. Add `hashi_strategy_commit` using the current schema v3.
3. Reuse current `parse_strategy()`, `resolve_cards()`, and
   `_record_strategy()`.
4. Enforce commit-before-work.
5. Continue the accepted work classification into the existing Low Execution
   contract in the same turn.
6. Preserve direct and confirmation terminal behavior.

Exit gate: no separate Strategist or Execution provider call exists on the
enabled Low work path, and selected cards materially guide work in the same
turn.

### Phase 3: canary and single-variable A/B

Run:

```text
A = current Strategy Fast + Smart Tool + independent provider turns
B = same Strategy Cards + same Smart Tool + continuous Strategy/Execution turn
```

Hold constant:

- exact Luna model and reasoning effort;
- PCM and user request;
- Playbook version/hash;
- Smart Tool implementation and schemas;
- permissions and Workzone;
- benchmark container/data; and
- hidden verifier.

Only the continuous feature flag differs.

Exit gate: all quality/safety gates pass and the exact-count invariant is
proven. Efficiency improvement must be measured, not assumed.

### Phase 4: optional later expansion

Only after the Low/Fast canary succeeds, write a small follow-up delta for
Medium and higher modes. That delta must decide how active Planning,
high-volume delegation, compulsory Replanning, Review, and remediation interact
with a still-open Primary turn.

Do not implement that expansion implicitly as part of v1.

## 14. Test plan

### 14.1 Existing Strategy parity

- The exact current Playbook version/hash and all 38 cards load unchanged.
- The combined prompt contains the current card selection and composition
  rules.
- `hashi_strategy_commit` accepts the same valid schema-v3 examples as
  `parse_strategy()`.
- Unknown Card IDs, duplicate IDs, malformed or duplicate Habit entries,
  invalid briefs, and invalid confirmation shapes are rejected.
- Runtime, not the model, resolves selected card snapshots.
- The persisted `strategy_handoff` matches the current separate path.
- No full Playbook or selected-card body is reinjected after commit in the same
  turn.

### 14.2 Phase and authority

- A Smart Tool call before commit is denied without execution.
- A valid commit records Strategy before the first real work receipt.
- A rejected commit does not record `TRIAGED` or open tool admission.
- The resolved goal, classification, selected cards, Habits, and brief cannot be
  replaced by a second initial commit.
- Direct and confirmation decisions cannot enter the work loop.
- PCM, permissions, Workzone, media, and Tool Registry remain authoritative.

### 14.3 Continuous bridge

- A normal Low work job starts exactly one Primary app-server process, one
  thread, and one turn.
- Any number of sequential dynamic calls receive real results in the same turn.
- Tool call/result IDs match exactly; duplicates and late results fail closed.
- No `HASHI_EXTERNAL_TOOL_DEFERRED` is emitted.
- No tool-triggered `turn/interrupt` occurs on a normal work path.
- No post-tool Chat Completions request occurs.
- Text, images, failure state, Smart Tool result shape, and evidence references
  survive content conversion.
- Usage is not double counted across cumulative app-server updates.
- The legacy `/v1/chat/completions` bridge remains behavior-compatible.

### 14.4 Runtime and cleanup

- Immediate acknowledgement behavior remains ordered correctly for work.
- Tool success, failure, unavailable, partial, non-zero exit, denial, and
  cancellation remain truthful.
- `/stop`, `/steer`, client disconnect, Gateway drain, app-server failure, and
  process shutdown leave no orphan task or process.
- Retry is allowed only under current replay-safety conditions.
- Low/Fast final delivery and terminal state remain unchanged.
- Medium and higher effort tests prove they still use their current path.

### 14.5 Live canary

The live protocol canary must include:

1. valid card selection and Strategy commit;
2. at least three dependent Smart Tool calls;
3. one real tool failure followed by in-turn adaptation;
4. actual verification guided by the selected cards/brief;
5. one final `turn/completed`; and
6. cancellation while a tool result is pending, with proven cleanup.

## 15. Observability and acceptance metrics

Join these identities:

```text
HER turn_id
  -> Primary invocation_id
  -> Gateway job_id
  -> Codex thread_id
  -> Codex turn_id
  -> dynamic call_id
  -> Smart Tool ledger row / Tool Evidence Receipt
```

Record:

- feature state and provider capability decision;
- process/thread/turn counts;
- Strategy commit time and selected Card IDs;
- Playbook version/hash;
- time to first real tool and final response;
- each tool's status, duration, and evidence reference;
- input, cached input, output, and thinking tokens;
- WebSocket frame counts and sizes without duplicating sensitive content;
- cancellation/failure phase and cleanup result; and
- whether any replay or legacy fallback occurred.

For one uninterrupted Low work job, acceptance requires:

```text
primary_provider_invocation_count = 1
primary_app_server_process_count  = 1
primary_codex_thread_count        = 1
primary_codex_turn_count          = 1
strategy_commit_count             = 1
deferred_tool_sentinel_count      = 0
post_tool_chat_completion_count   = 0
```

Smart Tool calls and internal model continuations may be greater than one and
must be reported honestly.

## 16. Definition of done

The v1 implementation is complete only when all of the following are true:

### Functional

- The enabled Low work path has no separate Strategist-to-Execution provider
  handoff.
- The same Primary turn selects cards, commits Strategy, executes, verifies,
  and answers.
- The complete Playbook is supplied once and card picking uses the current
  Strategist rules.
- The current schema v3, parser, resolver, recorder, and compact selected-card
  state are reused.
- Every real Smart Tool result is returned to the exact pending app-server call.

### Safety and compatibility

- PCM and current-request authority are unchanged.
- Smart Tool and Tool Registry behavior are unchanged after commit.
- No side effect can run before Strategy commit or be replayed after an unsafe
  failure.
- The request-local thread is never reused across outer requests.
- Direct, confirmation, Medium+, public Chat Completions, cancellation, and
  legacy rollback paths remain valid.

### Quality

- The existing hidden validation remains 17/17 where applicable.
- Card selection and final execution quality are non-inferior to the current
  separate Strategy Fast path.
- Tool failures and non-zero exits remain correctly recognized.
- The final result uses real evidence and the selected cards' verification
  guidance.

### Efficiency

- Normal work proves the exact one-process/thread/turn counts.
- HASHI no longer rebuilds and resends PCM, Playbook, Strategy handoff, plan,
  and complete tool history after every tool.
- Paired elapsed time, total tokens, thinking tokens, provider calls, and cost
  are reported against the frozen current baseline.

No particular saving is promised before the A/B run. The design removes the
known restart boundaries; the benchmark determines the actual benefit.

## 17. Non-goals and rejected shortcuts

The following are not part of v1:

- a new Strategy Card system, retriever, ranker, or schema;
- a global app-server pool;
- persistent Codex threads across HASHI user requests;
- replacing PCM or Memory+ with provider thread history;
- moving HASHI tools into Gateway/Codex built-ins;
- enabling continuous mode for Medium and higher effort without a follow-up
  design and tests;
- deleting the legacy Strategy/Execution path;
- adding workflow time, token, turn, or tool ceilings; or
- claiming that `thread/resume` alone is equivalent to one active turn.

The practical end state is:

> HASHI gives one Low/Fast Primary agent the current PCM, complete existing
> Strategy Playbook, Habit catalogue, request resources, and Smart Tool surface
> once. The agent uses the existing Strategist rules to select Strategy Cards,
> commits the existing schema-v3 decision, then applies those cards while it
> performs and verifies the work through real tool calls in one live Codex
> thread and turn. HASHI continues to own authority, tools, evidence, delivery,
> and memory boundaries.

Anything that starts a new Primary app-server, thread, or turn after a normal
tool result has not implemented this plan.
