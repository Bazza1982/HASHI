# HER v2 Fixed Backend Sessions and Provider-Neutral Strategy/Execution Implementation Plan

| Field | Value |
|---|---|
| Status | Proposed implementation plan; runtime code is not changed by this document |
| Date | 2026-08-30 |
| Product identity | HER v2 is a first-class fixed HASHI backend, alongside other fixed backends |
| External invariant | One HASHI conversation binding opens or resumes one durable HER backend session and thereafter sends only incremental turns, PCM deltas, resource deltas, and control events |
| Session invariant | One ordered HER-owned canonical thread spans all accepted user turns until explicit close, expiry, or unrecoverable terminal corruption |
| Provider invariant | HER may route internal stages through HASHI API, DeepSeek, OpenRouter, or any other capability-conformant model provider without changing HER core semantics |
| Codex boundary | Codex remains a separate HASHI backend; HER never selects Codex, Codex CLI, or Codex app-server as an internal model provider |
| Reasoning invariant | No HER lifecycle, authority, recovery, tool, evidence, or completion decision depends on reasoning being visible |
| Preserved systems | HASHI PCM, Strategy Playbook and Cards, Habits, Smart Tool, Tool Registry, permissions, workzones, attachments, evidence receipts, Ledger, Replanning, Review, Finalisation, cancellation, and audit |

## 1. Decision

HER v2 will become a sessionful fixed backend from HASHI's perspective.

HASHI opens or resumes a HER backend session once. After the initial session
snapshot is acknowledged, HASHI does not rebuild and resend the complete HER
prompt, transcript, PCM, Playbook, tool history, or provider state for every
user message. It sends only the new user message plus authoritative deltas and
typed control events.

HER owns the durable logical thread and all internal orchestration:

~~~text
HASHI conversation
  -> fixed backend binding: HER v2
     -> one HER Backend Session
        -> ordered user turns
        -> materialised versioned PCM
        -> canonical append-only event history
        -> context checkpoints and compaction
        -> Strategy / Planning / Execution / Replanning / Review routing
        -> provider and model selection for each internal invocation
        -> Smart Tool, evidence, permissions, cancellation, and recovery
~~~

HER thread continuity is HER-owned. It does not require any provider to expose
a native thread, session, response chain, hidden reasoning, or KV cache.
Provider-native continuity is an optional adapter optimisation.

The internal provider set is deliberately independent of HASHI's backend set:

- HER may call GPT-family models through `hashi-api`;
- HER may call DeepSeek through its configured API adapter;
- HER may call models through OpenRouter;
- HER may call any future provider whose adapter passes the same conformance
  contract; and
- HER does not call Codex. If HASHI selects Codex, HASHI is using the separate
  Codex backend rather than HER v2.

This plan retains a `ContinuousInvocation` inside each Primary Job, but it is
now an inner execution object. It no longer defines the lifetime of the fixed
backend session.

## 2. What “fixed backend” means

HER v2 qualifies as a fixed backend only when all of the following are true:

1. HASHI binds one conversation and Agent identity to one HER session ID.
2. The session survives multiple completed user turns.
3. HER, not HASHI, retains the canonical conversation and internal stage
   history.
4. HASHI sends a complete PCM snapshot only at session open or explicit
   rebase.
5. Ordinary later turns contain only the new user message and deltas since the
   last acknowledged revision.
6. HER can recover the logical session after HER process replacement without
   assuming provider-native state survived.
7. `/stop` or turn cancellation stops the active turn without automatically
   destroying the session.
8. Explicit close, expiry, identity change, authority-boundary change, or an
   unrecoverable integrity failure terminates the session.

A fixed backend does **not** require:

- one operating-system process for the entire session;
- one provider, model, or reasoning effort for the entire session;
- one provider-native thread for the entire session;
- visible model reasoning;
- one upstream inference per HER turn; or
- reuse of provider-native state when a stateless reconstruction is safer.

The externally visible continuity contract and the provider's physical
continuation mechanism are separate concerns.

## 3. Logical hierarchy and invariants

### 3.1 Hierarchy

~~~text
HerBackendSession
  ├── session identity, epoch, schema version, and lifecycle state
  ├── materialised PCM and authoritative revision
  ├── canonical session event log
  ├── context checkpoints and compaction records
  ├── Turn 1
  │    ├── Triage / Immediate decision
  │    └── optional HerPrimaryJob
  │         └── ContinuousInvocation
  │              ├── Strategy decision
  │              ├── optional Planning
  │              ├── Primary Execution
  │              ├── tool/control events
  │              └── terminal Primary result
  ├── Turn 2
  └── Turn N
~~~

Definitions:

- `HerBackendSession` is the fixed-backend lifetime bound to one HASHI
  conversation, Agent, and authority domain.
- `HerTurn` is one accepted user turn or one explicitly typed mid-turn steer.
- `HerPrimaryJob` is one unit of Primary work inside a turn.
- `ContinuousInvocation` is the logical Strategy-through-Primary-Execution
  lifecycle for one Primary Job.
- `ProviderInvocation` is one physical model call, native continuation, or
  reconstructed call sequence used to implement a HER stage.

### 3.2 Session invariants

For one active session:

~~~text
hashi_conversation_binding_count       = 1
her_backend_session_id_count           = 1
active_session_epoch_count             = 1
accepted_message_duplicate_count       = 0
pcm_revision_regression_count           = 0
canonical_history_sequence_gap_count   = 0
cross_session_state_leak_count          = 0
~~~

The same HASHI message ID and idempotency key can be retried, but it is
accepted at most once. A new session epoch invalidates stale writers and stale
provider handles.

### 3.3 Per-turn and per-job invariants

For a normal Strategy-required Primary Job:

~~~text
primary_job_count                       = 1
continuous_invocation_count             = 1
accepted_strategy_commit_count          = 1
tool_call_result_mismatch_count         = 0
unsafe_replay_count                     = 0
terminal_primary_result_count           = 1
terminal_user_turn_result_count         = 1
~~~

Invalid proposals, provider retries, and reconstructed provider inferences may
exceed one. They do not create additional accepted Strategy commits, Primary
Jobs, or terminal results.

## 4. Four-layer architecture

~~~text
HASHI Fixed Backend Contract
  - opens/resumes HER sessions
  - sends incremental turns and authoritative deltas
  - receives ordered activity and terminal events
          |
          v
HER Session Core
  - durable logical thread and PCM materialisation
  - ordering, idempotency, compaction, recovery, cancellation
          |
          v
HER Strategy/Execution Core
  - Triage, Strategy, Planning, Execution, Replanning, Review
  - Tool authority, evidence, plans, completion
          |
          v
Provider Continuation Adapters
  - HASHI API
  - DeepSeek
  - OpenRouter
  - future capability-conformant providers
~~~

### 4.1 Upstream `HerFixedBackendPort`

HASHI interacts with HER through a fixed-backend session interface. The exact
transport may be an in-process interface, stream, socket, or RPC, but its
semantics are stable:

~~~python
session = await her_backend.open_session(open_request)
turn = await session.append_turn(turn_delta)

async for event in turn.events():
    deliver_to_hashi(event)

result = await turn.result()
~~~

The port owns no provider concepts. It never exposes a DeepSeek request ID, an
OpenRouter generation ID, an OpenAI-compatible response ID, or other vendor
state as session authority.

### 4.2 HER session core

The session core owns:

- session identity, epoch, state version, and lifecycle;
- accepted-message ordering and idempotency;
- the materialised PCM and its revision;
- canonical visible history and typed internal events;
- per-turn immutable authority snapshots;
- context projection, checkpoints, and compaction;
- active-turn locking, queuing, steering, and cancellation;
- durable recovery state and session cleanup; and
- routing to the existing HER lifecycle.

### 4.3 HER Strategy/Execution core

The existing HER core remains authoritative for:

- Triage classification and `real_goal`;
- Strategy Playbook, eligible Cards, and schema v3 validation;
- active plans and Planning policy;
- Smart Tool and Tool Registry admission;
- evidence receipts and side-effect state;
- compulsory Replanning;
- sub-agent governance;
- completion, Review, Finalisation, and remediation; and
- truthful terminal results.

### 4.4 Provider adapters

Adapters translate a provider's physical API into provider-neutral events.
They may use native continuation, reconstructed message history, native tool
calls, or strict structured output. They never become a second lifecycle or
tool authority.

## 5. Fixed-backend session protocol

### 5.1 Operations

Version 1 exposes these logical operations:

| Operation | Purpose |
|---|---|
| `open_session` | Create a session from a complete initial snapshot and optionally start its first turn |
| `resume_session` | Reattach to an existing durable session at an acknowledged state version |
| `append_turn` | Add one new user turn after the prior turn has reached an allowed boundary |
| `steer_turn` | Add a typed incremental instruction to the currently active turn |
| `cancel_turn` | Stop the active turn while retaining the session unless policy requires closure |
| `rebase_session` | Replace the materialised PCM from an authoritative complete snapshot after a revision mismatch or recovery event |
| `close_session` | Close the logical session and release provider, tool, and storage resources |

Bare transport reconnect is not `resume_session`; reconnecting must prove the
session ID, epoch, last acknowledged state version, and caller authority.

### 5.2 Open request

`open_session` contains the only ordinary full PCM transmission:

~~~json
{
  "protocol": "hashi.her-fixed-backend.v1",
  "operation": "open_session",
  "hashi_conversation_id": "opaque HASHI conversation identity",
  "agent_id": "authoritative HASHI Agent identity",
  "session_binding": {
    "instance_id": "HASHI instance",
    "workzone_identity": "authorised workzone binding"
  },
  "pcm_snapshot": {
    "revision": 1,
    "digest": "sha256",
    "content": {}
  },
  "resource_snapshot": {
    "revision": 1,
    "digest": "sha256",
    "attachments": [],
    "permissions": {},
    "media_grants": []
  },
  "initial_turn": {
    "turn_id": "HASHI turn identity",
    "message_id": "globally unique message identity",
    "idempotency_key": "opaque",
    "user_message": {}
  }
}
~~~

The response acknowledges the exact state accepted:

~~~json
{
  "type": "session_opened",
  "her_backend_session_id": "HER-owned identity",
  "session_epoch": 1,
  "state_version": 1,
  "acknowledged_pcm_revision": 1,
  "acknowledged_pcm_digest": "sha256",
  "active_turn_id": "HASHI turn identity"
}
~~~

### 5.3 Incremental turn request

After open, ordinary new user turns contain only new data:

~~~json
{
  "protocol": "hashi.her-fixed-backend.v1",
  "operation": "append_turn",
  "her_backend_session_id": "HER-owned identity",
  "session_epoch": 1,
  "expected_state_version": 17,
  "turn_id": "new HASHI turn identity",
  "parent_turn_id": "last accepted HASHI turn identity",
  "sequence": 18,
  "message_id": "globally unique message identity",
  "idempotency_key": "opaque",
  "user_message": {},
  "pcm_delta": {
    "base_revision": 7,
    "target_revision": 8,
    "operations": []
  },
  "resource_delta": {
    "base_revision": 3,
    "target_revision": 4,
    "attachments_added": [],
    "attachments_revoked": [],
    "permissions_changed": [],
    "media_grants_changed": []
  }
}
~~~

An empty `pcm_delta.operations` is valid. HASHI does not resend unchanged PCM
sections, old messages, old tool results, the Strategy Playbook, or provider
history.

### 5.4 Turn acknowledgement

HER durably applies a valid delta and accepts the message before acknowledging
it:

~~~json
{
  "type": "turn_accepted",
  "her_backend_session_id": "same session",
  "session_epoch": 1,
  "turn_id": "same turn",
  "message_id": "same message",
  "state_version": 18,
  "acknowledged_pcm_revision": 8,
  "acknowledged_resource_revision": 4,
  "canonical_sequence": 18
}
~~~

Retrying an acknowledged idempotency key returns the same acknowledgement and
does not create another turn.

### 5.5 Steer, cancel, close, and rebase

- `steer_turn` references the exact active turn, its last accepted activity
  sequence, and a unique message ID. It is not guessed from an ordinary new
  turn.
- `cancel_turn` closes new work admission, settles or cancels active tools,
  cancels provider work, and records a terminal turn event. The session
  remains available for the next user turn unless policy closes it.
- `close_session` rejects new turns, cancels or settles the active turn,
  releases live adapter resources, writes the final checkpoint, and marks the
  session closed.
- `rebase_session` requires a complete authoritative snapshot, a new PCM
  revision, an expected session state version, and an auditable reason. It
  never silently discards accepted conversation events or evidence.

### 5.6 Typed protocol failures

The external protocol has explicit failures for:

- `unknown_session`;
- `stale_session_epoch`;
- `state_version_conflict`;
- `pcm_revision_conflict`;
- `sequence_gap`;
- `duplicate_message_conflict`;
- `turn_already_active`;
- `invalid_steer_target`;
- `rebase_required`;
- `session_closing`; and
- `session_closed`.

None of these causes HER to guess missing history or accept an authority
change from an unverified delta.

## 6. PCM and resource delta semantics

HASHI remains the sole authority for PCM, user messages, permissions, Agent
identity, attachments, workzones, and media grants. HER stores a versioned
materialised view so HASHI does not need to retransmit it.

### 6.1 Initial snapshot and later deltas

- `open_session` or `rebase_session` supplies a complete snapshot.
- `append_turn` and `steer_turn` supply only changes since the acknowledged
  revision.
- Every delta declares `base_revision`, `target_revision`, and an integrity
  digest or equivalent authenticated envelope evidence.
- A delta applies atomically or not at all.
- Revision skips, regressions, unknown fields, or unauthorised mutations fail
  before model or tool work.

Use a typed PCM delta schema rather than arbitrary JSON Patch for authority
fields. The schema must distinguish replacement, addition, revocation, and
expiry, especially for permissions and resources.

### 6.2 Per-turn authority snapshot

When a turn is accepted, HER freezes a per-turn authority snapshot from:

~~~text
last materialised session PCM
  + accepted PCM delta
  + accepted resource delta
  + current HASHI system envelope
  = immutable authority snapshot for that turn
~~~

Internal Strategy, Planning, Execution, and Provider calls use this same
snapshot. A later turn may use a newer PCM revision without mutating completed
turn records.

A mid-turn permission delta is admitted only through an explicit `steer_turn`
or authority-update event and becomes effective at a safe boundary. It never
retroactively authorises an earlier denied or side-effecting tool request.

### 6.3 Transport incrementality versus provider reconstruction

The fixed-backend guarantee applies to HASHI -> HER transport:

~~~text
HASHI sends only new message + deltas after session open.
~~~

A stateless provider adapter may internally resend a projected context on a
later inference. That does not require HASHI to resend it, and it does not
create another HER session or duplicate accepted history. Provider token and
latency savings are measured separately from fixed-backend transport savings.

## 7. HER-owned canonical thread

### 7.1 Canonical event log

HER maintains an ordered, append-only, provider-neutral event log. Events
include:

- session opened, resumed, rebased, closing, and closed;
- PCM and resource deltas accepted;
- user message and steer accepted;
- turn started, phase changed, suspended, cancelled, and completed;
- Triage and Strategy decisions;
- plan activation and replacement;
- provider invocation started, advanced, failed, and completed;
- tool/control request admitted or rejected;
- exact tool/control result and evidence receipt;
- Replanning and completion decisions;
- Review and Finalisation outcomes;
- compaction checkpoint created; and
- recovery attempt and replay decision.

Each event carries session ID, epoch, state version, canonical sequence,
turn/job identities, event ID, timestamps, and a digest over material fields.

### 7.2 Materialised session state

`HerSessionStore` materialises at least:

- session lifecycle and schema version;
- HASHI binding and authority-domain fingerprints;
- current PCM/resource revisions and digests;
- last accepted message, turn, and canonical sequence;
- active or queued turn state;
- compacted provider-visible conversation projection;
- unresolved tool/control events;
- accepted Strategy and active plan for an active job;
- exact evidence and side-effect state;
- provider attempt and recovery status; and
- cleanup/expiry state.

Live HTTP streams, sockets, SDK clients, or provider handles are not
serialised. They are recreated or recovered through adapter policy.

### 7.3 Session persistence and expiry

The first release requires durable logical recovery across HER process
replacement. It does not require a live provider connection to survive.

Session retention, idle expiry, maximum durable age, user deletion, and
cleanup retry are explicit policy settings. Expiry never deletes unresolved
side-effect evidence or silently accepts a stale caller. Closed sessions are
not resumed under the old epoch.

## 8. Turn lifecycle inside a fixed session

### 8.1 New turn versus mid-turn steer

- `append_turn` starts a new HER turn after the previous turn reaches an
  allowed terminal or waiting boundary.
- `steer_turn` modifies the currently active turn under existing steer policy.
- An ordinary user follow-up is never heuristically converted into a steer
  after it has been accepted as a new turn.
- The first release allows one active Primary turn per session. Additional
  turns are rejected or placed in one explicit ordered queue; they never run
  concurrently against the same mutable workspace by accident.

### 8.2 Per-turn lifecycle

~~~text
TURN_ACCEPTED
  -> Immediate/Triage
  -> DIRECT_RESPONSE or CONFIRMATION_REQUIRED
     OR
  -> HerPrimaryJob
     -> Strategy
     -> optional Planning
     -> Primary Execution
     -> optional Replanning
     -> optional Review/Finalisation
  -> TURN_COMPLETED
~~~

The session history is available to the turn through a deliberate context
projection. A new work request normally receives a new Strategy decision.
Continuing conversation history does not mean one Strategy decision remains
authoritative forever.

### 8.3 Internal model and provider routing

HER may select a configured route independently for each internal invocation:

~~~text
Strategy    -> HASHI API / model A / configured reasoning
Planning    -> OpenRouter / model B / configured reasoning
Execution   -> DeepSeek / model C / configured reasoning
Replanning  -> HASHI API / model D / configured reasoning
Review      -> any separately configured conformant route
~~~

The exact provider/model/effort is frozen for each physical provider
invocation, not for the entire HER backend session. A stage can change route
only at a declared stage or recovery boundary. HER never changes a provider
inside an unresolved tool request.

A same-provider native continuation may combine stages as an optimisation when
the configured routes and authority rules permit it. The core contract does
not require that optimisation.

## 9. Strategy Playbook, schema v3, and work gate

The existing Strategy Playbook, Strategy Cards, picker instructions,
`StrategyDecision`, schema v3 parser, resolver, and audit record remain the
single source of truth.

### 9.1 Exact Strategy schema v3

The accepted decision retains the current fields exactly:

~~~json
{
  "classification": "existing classification enum",
  "real_goal": "immutable resolved goal",
  "selected_strategy_cards": ["exact eligible Card ID"],
  "relevant_habits": ["exact unchanged Habit reference"],
  "execution_brief": {
    "strategy": "overall strategic approach",
    "stages": ["major strategic stage"],
    "dependencies": ["important dependency"],
    "verification": ["verification approach"],
    "success_criteria": ["observable completion condition"],
    "replan_conditions": ["condition requiring replanning"]
  },
  "clarification": "conditional existing field"
}
~~~

Do not replace this with a second brief containing `plan`, `parallel_groups`,
or `sub_agents`. Those remain Planning/delegation data when needed.

### 9.2 Strategy commit envelope

The provider-neutral control operation wraps, rather than redefines, schema
v3:

~~~json
{
  "kind": "control_request",
  "name": "hashi_strategy_commit",
  "event_id": "unique logical event",
  "arguments": {
    "playbook_version": "frozen version",
    "playbook_digest": "frozen sha256",
    "strategy_decision_v3": {}
  }
}
~~~

HER verifies that classification, `real_goal`, Habits, Playbook, Cards, PCM,
permissions, and workzone agree with the per-turn authority snapshot. The model
cannot use the commit to mutate them.

If formal Planning is required, the accepted Strategy decision becomes its
input and the validated `PlanningDecision` remains a separate object. On the
Low/Fast path, Execution receives the existing Strategy handoff without a
separate Planning object.

### 9.3 Work gate

Before an accepted Strategy commit when Strategy is required:

- Smart Tool work requests are denied with a typed control result;
- no work receipt or Replanning cadence increment is created;
- invalid Strategy proposals may be corrected;
- no plan becomes active; and
- no side effect is admitted.

After acceptance, work proceeds under the exact accepted Strategy and any
required active plan. A competing initial commit cannot replace it; only the
existing Replanning authority may replace executable plan state.

## 10. Provider-neutral continuation contract

### 10.1 Capability declaration

Every exact provider/model route declares capabilities from live conformance,
not provider-name assumptions:

~~~yaml
continuation_mode: native | reconstructed | unsupported
tool_request_mode: native | structured | none
recovery_mode: native_resume | reconstruct_safe | fail_safe
reasoning_transport: absent | visible_optional | opaque_required
supports_streaming: true | false
supports_cancellation: true | false
supports_parallel_tool_requests: true | false
~~~

These values describe adapter mechanics. They do not redefine HER sessions,
turns, plans, evidence, or completion.

`supports_sessions` may be a public capability of HER as a fixed HASHI
backend, but it is not a required capability of an internal model provider.
That distinction must remain explicit.

### 10.2 `ProviderContinuationPort`

The downward interface remains event-driven:

~~~python
handle = await provider.start_invocation(request)

while True:
    event = await handle.next_event()

    if event.kind == "control_request":
        result = await coordinator.handle_control(event)
        await handle.continue_with_control_result(event.event_id, result)

    elif event.kind == "tool_request":
        result = await coordinator.execute_tool(event)
        await handle.continue_with_tool_result(event.event_id, result)

    elif event.kind == "terminal_result":
        return coordinator.accept_terminal(event)

    elif event.kind in {"failure", "cancelled"}:
        return await coordinator.recover_or_fail(event)
~~~

Core event kinds are limited to:

- `control_request`;
- `tool_request`;
- `provider_activity`;
- `terminal_result`;
- `failure`; and
- `cancelled`.

### 10.3 Provider-native membrane

Provider-specific IDs, tool message fields, signatures, reasoning artifacts,
SDK objects, and continuation tokens remain inside the adapter. Core receives
only generic IDs and optional namespaced telemetry.

~~~json
{
  "provider_telemetry": {
    "provider_session_id": "opaque",
    "provider_invocation_id": "opaque",
    "native": {
      "provider-name": {}
    }
  }
}
~~~

Telemetry cannot drive Ledger transitions, replay permission, or recovery
authority.

## 11. Supported provider shapes

### 11.1 Reconstructed continuation baseline

Reconstruction is the portable baseline:

~~~text
provider inference 1:
  projected turn snapshot + provider-visible history
  -> control/tool/terminal event

HER:
  validates or executes
  -> append exact event and result once

provider inference N+1:
  same immutable turn authority + updated visible history
  -> next event
~~~

Required rules:

- every physical request belongs to the same HER turn, job, and logical
  invocation identity;
- provider inference index increases monotonically;
- assistant requests and HER results are appended exactly once with stable
  IDs;
- completed tools are never re-executed merely because history is resent;
- usage and cost are accumulated across physical requests;
- provider context limits trigger authorised compaction or typed failure; and
- hidden reasoning is never reconstructed or guessed.

### 11.2 Native provider continuation

If a provider supplies a reliable session, response chain, or active tool
continuation, its adapter may reuse it. Native state is an optimisation and
recovery input, not the HER session's source of truth.

Loss of native state does not delete the HER session. The adapter follows its
declared recovery mode using HER-owned visible history and evidence.

### 11.3 Strict structured tool continuation

Providers without native function calls may qualify through a strict schema
whose response is exactly one of:

~~~text
control_request | tool_request | terminal_result
~~~

The adapter validates the complete envelope before emitting a logical event.
Natural-language suggestions, parser repair ambiguity, undeclared tool names,
or a response blending tool work with a terminal result execute nothing.

If the exact model cannot reliably meet the schema, its
`tool_request_mode=none`. It may remain available for no-tool stages, but it
cannot claim safe Strategy + Execution support.

### 11.4 HASHI API adapter

`hashi-api` is a normal HER model-provider route for configured GPT-family
models. It is not Codex and does not create or control a Codex backend session.

The adapter may use the Gateway's existing model API and tool-call protocol,
but HER retains the fixed session, visible history, tools, evidence, and
authority. Gateway conversation or response IDs remain optional adapter state.

### 11.5 DeepSeek adapter

The DeepSeek adapter declares the exact capabilities of the selected model and
API mode. It preserves any provider-required assistant tool metadata or opaque
reasoning continuation artifact inside the adapter while exposing only the
provider-neutral event contract to HER.

### 11.6 OpenRouter adapter

OpenRouter is treated as a routing provider whose downstream model capability
may vary. Capability is resolved for the exact configured route/model and is
never inferred merely from `provider=openrouter`.

Provider- or model-specific tool-call fields, reasoning fields, usage data,
and finish reasons remain adapter-local and are normalised truthfully.

### 11.7 Future providers

Adding a provider requires:

1. a provider adapter with safe unsupported defaults;
2. the shared conformance suite;
3. reasoning-absent and provider-required-state tests;
4. cancellation and recovery behavior;
5. truthful usage and error normalisation; and
6. live qualification of exact model routes before enablement.

No HER core class or Strategy prompt changes merely to add a provider.

## 12. Explicit Codex boundary

Codex and HER v2 are peer backends selected by HASHI:

~~~text
HASHI backend selection
  ├── codex backend
  └── her_v2 fixed backend
       ├── HASHI API model provider
       ├── DeepSeek model provider
       ├── OpenRouter model provider
       └── other conformant provider
~~~

Accordingly, this feature does not:

- add Codex CLI or Codex app-server to HER provider routing;
- start Codex processes, threads, or turns from HER;
- add Codex native IDs to HER session or provider contracts;
- use Codex dynamic tools as the reference definition of continuity; or
- modify the existing Codex backend merely to make HER fixed-session capable.

Codex may remain a behavioral comparison for how HASHI treats a fixed backend,
but it is not an implementation dependency or internal HER provider.

## 13. Reasoning-visibility independence

### 13.1 Semantic rule

No core state, branch, validation, recovery decision, tool admission, audit
requirement, or completion criterion depends on private or visible chain of
thought.

HER correctness uses only observable contract data:

- accepted user messages and PCM/resource deltas;
- structured Strategy, Planning, control, and terminal decisions;
- tool requests and exact results;
- provider status/activity/error events;
- canonical provider-visible message history;
- evidence, receipts, side-effect state, and plan state; and
- usage and reasoning-availability metadata when supplied.

### 13.2 Three distinct reasoning concepts

The implementation must not collapse these concepts:

| Concept | HER core treatment |
|---|---|
| Semantic hidden reasoning | Never required, recovered, inferred, or used as authority |
| Visible reasoning or summary | Optional, redacted/audited only under policy; not required for continuation |
| Opaque provider continuation artifact | Adapter-private transport state that may need exact round-trip without HER interpreting it |

Examples of opaque state may include encrypted reasoning items, thinking
signatures, provider-specific assistant fields, or continuation tokens.

### 13.3 Opaque state rules

- The adapter may retain opaque state only for the provider invocation that
  produced it and only as long as required by that provider's protocol.
- Core stores at most availability, digest, size, retention class, and optional
  namespaced telemetry.
- The state cannot authorise a tool, alter PCM, prove a side effect completed,
  or replace visible history.
- Raw chain of thought is not copied into canonical HER history merely because
  a provider exposes it.
- If required opaque state is lost, the adapter either reconstructs safely
  from visible state under declared capability or returns a typed failure.
- HER never fabricates missing reasoning or thinking artifacts.

### 13.4 Reasoning controls

Reasoning effort or provider thinking controls are optional per-invocation
adapter configuration. A provider that offers no such setting remains fully
compatible. HER does not invent a cross-provider reasoning level or assume
that identically named settings have identical semantics.

## 14. Tool and evidence authority

The provider-neutral work path remains:

~~~text
provider tool_request
  -> HER Strategy/plan/completion gate
  -> Smart Tool
  -> Tool Registry permission and safety checks
  -> real tool execution
  -> evidence receipt and side-effect state
  -> canonical session event
  -> provider adapter continuation
~~~

Preserved rules:

- no provider executes HASHI tools directly;
- Tool Registry remains the only source of execution permission and safety
  metadata;
- workzone, attachment, media, and permission checks remain in HASHI/HER;
- success, denial, approval-required, error, non-zero exit, and cancellation
  remain truthful;
- event ID and provider call ID pairing is exact;
- duplicate, unknown, late, or mismatched results fail closed;
- tool failure is returned to the model as a tool result, not mislabeled as a
  provider failure; and
- completed side effects are never replayed because a provider or HER session
  was reconstructed.

## 15. Planning, Replanning, delegation, Review, and Finalisation

Fixed session continuity changes transport and context ownership, not HER
policy.

- Low/Fast may execute directly from accepted Strategy schema v3.
- Medium/High paths retain formal Planning when current policy requires it.
- Each stage may use its configured provider/model route.
- Compulsory Replanning retains existing cadence and safe boundaries.
- A Replan replaces executable plan state only through existing validation.
- High-volume sub-agents remain bounded executions tied to an accepted plan.
- Review and Finalisation remain distinct policy stages but operate inside the
  same HER backend session and turn record.
- Review remediation creates a new job identity under the same user turn or a
  typed follow-on turn according to existing policy; it never rewrites earlier
  evidence.

For modes requiring completion control, `hashi_execution_complete` remains a
provider-neutral logical control operation. A terminal model message cannot
bypass a due Replan or completion gate.

## 16. Context projection and compaction

HER canonical history can outlive any provider context window. Every provider
invocation receives a deterministic projection containing only what its stage
requires:

- current immutable authority snapshot;
- applicable session context and completed exchanges;
- active Strategy/plan state;
- unresolved constraints and user corrections;
- relevant tool/evidence summaries and exact references;
- current Playbook/Card material when Strategy is being selected;
- only selected Cards and handoff data for isolated Execution paths; and
- provider tool schemas and stage instructions.

Compaction is HER-owned and provider-neutral:

1. write a canonical checkpoint with source sequence range and digest;
2. preserve system/authority material, unresolved work, accepted Strategy,
   active plan, permissions, evidence references, side-effect state, and user
   commitments;
3. remove no unresolved tool/control event;
4. retain raw canonical events according to audit/retention policy; and
5. prove reconstruction equivalence with deterministic tests.

Provider-native compaction may be used as an optimisation but cannot be the
only durable session state.

## 17. Failure, cancellation, and recovery

### 17.1 Failure classes

Distinguish:

- truthful tool/control result failure;
- provider/model response failure;
- provider transport failure;
- HER process failure;
- session storage failure;
- PCM revision or ordering conflict;
- cancellation; and
- unknown side-effect state.

Only provider or transport failures enter provider recovery. Tool failures are
normally returned to the active model so it can adapt.

### 17.2 Recovery snapshot

Durable recovery uses only observable HER-owned state:

- session ID, epoch, state version, and canonical sequence;
- materialised PCM/resource revisions and digests;
- accepted user messages and typed deltas;
- current turn/job/invocation phase;
- accepted Strategy and active plan;
- provider-visible history projection;
- completed tool IDs, receipts, and side-effect classification;
- unresolved request IDs;
- completion gate and Replanning state; and
- provider attempt metadata and optional opaque-state availability.

Hidden reasoning and provider KV state are never assumed recoverable.

### 17.3 Recovery modes

| Mode | Allowed behavior |
|---|---|
| `native_resume` | Resume only when the adapter proves exact pending-event continuity and no acknowledged work can repeat |
| `reconstruct_safe` | Start a new provider invocation from the canonical visible state and exact completed-work record |
| `fail_safe` | Terminate the active turn truthfully while preserving the HER session if it remains consistent |

After an unknown or incomplete side effect begins, automatic reconstruction is
blocked unless exact native recovery proves the acknowledged call will not be
repeated.

### 17.4 Cancel versus close

- Turn cancellation ends current work but ordinarily retains the session and
  prior context.
- Session close ends all turns, provider work, pending tools, and future input.
- Process reload reconstructs durable sessions and marks unrecoverable active
  turns with typed status; it does not discard the entire conversation by
  default.

## 18. Isolation and security

- One session is bound to one HASHI conversation, Agent, instance, and
  authority domain.
- Session IDs are unguessable and never sufficient without caller authority.
- A change of Agent, user, or incompatible authority domain opens a new
  session rather than mutating the binding.
- PCM and permission deltas are authenticated typed data.
- Provider adapters receive only the minimum stage projection.
- Provider-native state cannot cross HER sessions.
- Opaque reasoning/continuation artifacts follow provider-specific retention,
  encryption, and redaction policy.
- Structured-output parsing grants no tool authority.
- Secret-bearing canonical events are redacted or encrypted according to
  existing HASHI policy.
- Expiry and cleanup terminate live providers and pending tools without
  deleting required evidence.

## 19. Observability and correctness metrics

### 19.1 Identity chain

~~~text
hashi_conversation_id
  -> her_backend_session_id / session_epoch
  -> her_turn_id
  -> her_primary_job_id
  -> continuous_invocation_id
  -> provider_invocation_id / provider_attempt_id
  -> event_id / provider_call_id
  -> Tool Evidence Receipt
~~~

### 19.2 Fixed-backend metrics

Record at least:

- session open/resume/rebase/close counts;
- session age, turn count, and idle duration;
- full PCM snapshot count and bytes;
- incremental PCM/message delta count and bytes;
- unexpected full-context retransmission count from HASHI;
- state-version and PCM-revision conflicts;
- duplicate/idempotent message retries;
- canonical event and compaction checkpoint counts;
- recovery and stale-epoch decisions;
- turn queue/steer/cancel behavior; and
- session cleanup results.

Happy-path fixed-backend assertions:

~~~text
unexpected_full_snapshot_count_after_open = 0
duplicate_accepted_message_count         = 0
canonical_sequence_gap_count             = 0
unacknowledged_authority_delta_count      = 0
cross_session_state_leak_count            = 0
~~~

### 19.3 Provider and reasoning metrics

Record:

- configured and effective provider/model per internal invocation;
- capability decision and recovery mode;
- physical inference count, latency, usage, cached tokens, and cost;
- tool/control request count and status;
- reasoning availability and transport class;
- opaque continuation artifact presence, size, and retention class without
  exposing its contents; and
- route changes at declared boundaries.

Provider-specific call counts are performance metrics, not HER correctness
definitions.

## 20. Implementation map

### 20.1 Fixed-backend/session layer

| File or area | Planned change |
|---|---|
| `orchestrator/her_v2/models.py` | Add session, epoch, state-version, turn-delta, PCM-delta, canonical-event, checkpoint, and typed failure models; no vendor-native required fields |
| `orchestrator/her_v2/interfaces.py` | Add `HerFixedBackendPort`, `HerSessionStore`, and provider-neutral continuation interfaces |
| `orchestrator/her_v2/backend_session.py` | New durable session coordinator for open/resume/append/steer/cancel/rebase/close, ordering, idempotency, and turn locking |
| `orchestrator/her_v2/session_store.py` | Provider-neutral event-log and materialised-state persistence implementation |
| `orchestrator/her_v2/context.py` | Build per-turn projections and provider-neutral compaction checkpoints from canonical state |
| `orchestrator/her_v2/runtime.py` | Run existing HER lifecycle inside a bound session and consume immutable per-turn authority snapshots |
| HASHI backend registry/dispatch | Advertise HER v2 as sessionful and bind HASHI conversation IDs to HER session IDs without changing the separate Codex backend |

### 20.2 Strategy and execution layer

| File or area | Planned change |
|---|---|
| `orchestrator/her_v2/structured.py` | Reuse exact Strategy schema v3; add only the wrapper control envelope and keep Planning/delegation schemas separate |
| `orchestrator/her_v2/strategy_execution.py` | Coordinate Strategy commit, work gate, plan state, tool/control events, completion, and recovery per Primary Job |
| `orchestrator/her_v2/prompts.py` | Render stage prompts from HER-owned session projections without requiring HASHI to resend history |
| `orchestrator/her_v2/prompt_catalog.py` | Validate any combined or stage-specific prompt assets and exact placeholders |
| `orchestrator/her_v2/runtime_invocation.py` | Carry session/turn/job identities and provider-neutral recovery state through internal invocations |

### 20.3 Provider adapters

| File or area | Planned change |
|---|---|
| `adapters/base.py` | Add safe provider-neutral capability fields, including reasoning transport and recovery |
| `adapters/her_v2_provider.py` | Resolve exact stage route, create provider projections, normalise events, and return one stage result without owning session authority |
| `adapters/hashi_api.py` | Implement conformant GPT-model continuation through HASHI API; no Codex session/process/thread integration |
| `adapters/deepseek_api.py` | Implement exact route capabilities and provider-required opaque assistant/reasoning state handling |
| `adapters/openrouter_api.py` | Implement per-model capability resolution and reconstructed/native continuation as proven |
| `adapters/registry.py` | Report exact route capability diagnostics and never infer support only from provider name |

No HER implementation change is planned for `codex_cli.py` or
`codex_app_server.py`. They belong to the separate Codex backend.

### 20.4 Documentation

| File or area | Planned change |
|---|---|
| `docs/HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md` | Make fixed-backend session semantics, incremental PCM, provider neutrality, and reasoning independence normative |
| `docs/HER_V2_TESTING_PLAN.md` | Add session, delta, recovery, provider, reasoning, and cross-turn conformance suites |
| Backend protocol documentation | Document HER v2 session operations and distinguish them from internal provider continuation APIs |

## 21. Delivery phases

### Phase 0: freeze current behavior and evidence

1. Preserve current HER v2 quality, Strategy Card, Smart Tool, provider-call,
   token, cost, and latency baselines.
2. Record current HASHI -> HER full-context payload counts and sizes.
3. Add behavior-neutral identities needed to correlate conversation, turn, job,
   provider call, and evidence.

Exit gate: the baseline is reproducible and instrumentation changes no
behavior.

### Phase 1: fixed-backend contract and durable session core

1. Add `HerFixedBackendPort` and exact operation/envelope schemas.
2. Add durable session store, epoch, state version, canonical sequence, and
   idempotency.
3. Implement open/resume/append/cancel/close with a deterministic fake HER
   turn runner.
4. Bind HASHI conversations to HER sessions.

Exit gate: two or more user turns run through one HER session without HASHI
resending old messages or the full PCM.

### Phase 2: incremental PCM, resources, steering, and rebase

1. Add typed revisioned PCM/resource deltas.
2. Materialise per-session state and freeze per-turn authority snapshots.
3. Implement conflict, duplicate, stale-epoch, and rebase behavior.
4. Implement explicit mid-turn steer and cancel-with-session-retention.

Exit gate: delta retry is idempotent, revision conflicts fail closed, rebase is
auditable, and later turns use the exact materialised state.

### Phase 3: provider-neutral invocation core

1. Add capability declarations and `ProviderContinuationPort`.
2. Add deterministic native and reconstructed fake adapters.
3. Run identical Strategy/control/tool/terminal scripts with absent, visible,
   and opaque-required reasoning transports.
4. Prove provider IDs and opaque artifacts cannot affect HER authority.

Exit gate: fake adapters produce equivalent HER semantic records and core
tests contain no provider-name branches.

### Phase 4: HASHI API real-provider vertical slice

1. Implement the conformant HASHI API adapter for configured GPT models.
2. Run multiple tool/control rounds under one HER turn and one HER session.
3. Preserve exact tool metadata, errors, usage, cancellation, and recovery.
4. Verify no Codex backend component is invoked.

Exit gate: a real HASHI API model completes Strategy + Execution using HER
session state and only incremental HASHI inputs after session open.

### Phase 5: Strategy, Planning, Execution, and policy integration

1. Wrap exact Strategy schema v3 in `hashi_strategy_commit`.
2. Preserve Low/Fast Strategy handoff and formal Planning where required.
3. Integrate work gate, Smart Tool, evidence, Replanning, sub-agents, Review,
   Finalisation, and remediation.
4. Allow stage-specific provider/model profiles inside one HER session.

Exit gate: all effort modes retain current authority and quality semantics
across multiple user turns.

### Phase 6: DeepSeek and OpenRouter conformance

1. Qualify one exact DeepSeek route.
2. Qualify one exact OpenRouter-routed model.
3. Exercise native, reconstructed, or structured modes according to proven
   capability.
4. Test no reasoning, visible reasoning, and provider-required opaque state.
5. Prove no completed side effect is replayed across reconstruction or route
   failure.

Exit gate: at least HASHI API plus one non-HASHI provider produce equivalent
HER logical outcomes under the shared conformance suite.

### Phase 7: recovery, compaction, canary, and default

1. Implement durable compaction checkpoints and process-restart recovery.
2. Test expiry, close, reload, `/stop`, `/steer`, and active-tool failure.
3. Canary exact provider/model capabilities separately.
4. Run same-task quality, latency, token, cost, and P95 comparisons.
5. Make HER v2 session mode the default only after semantic and operational
   gates pass.

Exit gate: HER v2 behaves as a reliable fixed backend across real multi-turn
sessions and qualified providers.

## 22. Test plan

### 22.1 Fixed-backend contract tests

- Session open accepts one full PCM snapshot and returns an epoch/version.
- Later turns contain only new user messages and deltas.
- Full unchanged PCM retransmission after open is detected in test telemetry.
- Duplicate message/idempotency retry creates no duplicate turn.
- Sequence gaps and stale epochs fail before model work.
- One active turn per session is enforced.
- Explicit steer reaches only the referenced active turn.
- Turn cancellation retains a usable session.
- Session close rejects all later turns.
- Resume after HER process replacement reconstructs canonical state.
- Session isolation prevents history, PCM, resources, and tools from crossing
  Agent or conversation bindings.

### 22.2 PCM/resource tests

- Valid deltas apply atomically and increment revision exactly once.
- Invalid base revisions return `pcm_revision_conflict` or `rebase_required`.
- Permission revocation reaches the next safe boundary and blocks later work.
- Permission grant never retroactively admits an earlier request.
- Attachment add/revoke and media grants follow exact authority.
- Rebase replaces materialised PCM without deleting accepted conversation or
  evidence events.
- Completed turns retain the PCM revision under which they ran.

### 22.3 Strategy and lifecycle tests

- Exact current Strategy schema v3 is accepted unchanged.
- Alternative `execution_brief` shapes are rejected.
- Playbook version/digest and selected Card IDs are validated.
- Work is denied before required Strategy commit.
- Planning/delegation data remains separate from Strategy schema v3.
- Low/Fast receives only selected Cards and the execution brief projection.
- One accepted initial Strategy and one terminal result exist per Primary Job.
- Replanning, Review, Finalisation, and remediation preserve current rules.
- A later user turn can create a new Strategy without losing earlier session
  context.

### 22.4 Provider-neutral tests

Run the same canonical scenario through fake native and reconstructed
adapters:

1. invalid Strategy proposal;
2. corrected Strategy commit;
3. successful read-only tool;
4. failed tool and adaptation;
5. side-effecting tool;
6. Replanning directive;
7. completion control; and
8. terminal result.

Assert identical Strategy, Ledger, evidence, replay state, completion, and
terminal semantics.

Architectural tests assert:

- HER core imports no DeepSeek/OpenRouter/provider SDK types;
- HER provider routing imports no Codex CLI/app-server implementation;
- no core schema requires a provider-native session ID;
- disabling one provider leaves core/session tests valid; and
- provider telemetry cannot drive authority.

### 22.5 Reasoning tests

- Provider sends no reasoning field at all.
- Provider sends a visible reasoning summary.
- Provider sends visible reasoning that policy redacts.
- Provider requires an opaque encrypted/thinking artifact on continuation.
- Adapter round-trips opaque state without exposing it to core.
- Loss of required opaque state follows reconstruct-safe or typed fail-safe
  policy.
- No test validates correctness by inspecting chain of thought.

### 22.6 Real adapter tests

For HASHI API, DeepSeek, and OpenRouter-qualified routes:

- exact capability handshake/configuration is recorded;
- at least three dependent tool/control rounds complete;
- one tool failure is adapted to truthfully;
- cancellation leaves no pending provider stream or tool;
- usage across all physical inferences is accurate;
- provider-specific assistant metadata remains adapter-local;
- a second user turn reuses the HER session without HASHI resending history;
  and
- reasoning disabled or unavailable does not change lifecycle compatibility.

### 22.7 Recovery and replay tests

- Restart before any tool safely resumes or reconstructs.
- Restart after completed read-only calls never repeats them.
- Restart after acknowledged side effect never repeats it.
- Unknown side-effect state blocks automatic replay.
- Provider route failure after safe boundary follows the frozen recovery mode.
- Session-level recovery does not create a competing Strategy commit.
- Compaction preserves unresolved work, evidence references, and authority.

### 22.8 Regression suites

Extend existing coverage for:

- HER adapters and lifecycle;
- Strategy schema and Playbook;
- HASHI API, DeepSeek, and OpenRouter tool loops;
- Smart Tool and Tool Registry;
- PCM, permissions, workzones, attachments, and multimodal routing;
- Replanning, Review, Ledger, WIP Journal, and cancellation; and
- the normal full repository test policy.

## 23. Acceptance criteria

### 23.1 Fixed backend

- HASHI registers HER v2 as a sessionful fixed backend.
- One HASHI conversation binding maps to one active HER session/epoch.
- Initial open sends one complete PCM snapshot.
- Later normal turns send only new messages and deltas.
- HER retains canonical conversation and internal lifecycle state.
- A cancelled turn can be followed by a new turn in the same session.
- HER process replacement can recover the logical session from durable state.
- Close/expiry cleans provider and tool resources deterministically.

### 23.2 Provider neutrality

- HER core requires no provider-native session, response, reasoning, or SDK
  type.
- HER can use qualified HASHI API, DeepSeek, and OpenRouter routes without core
  schema changes.
- Exact capability declarations, not provider names, determine eligibility.
- Native and reconstructed adapters produce equivalent HER logical records.
- Codex is absent from HER internal provider selection and implementation.
- Providers without safe tool capability remain truthfully limited rather
  than breaking the fixed-backend session contract.

### 23.3 Reasoning independence

- No lifecycle branch or acceptance test depends on reasoning content.
- Providers with absent reasoning complete normally.
- Visible reasoning is optional audit material only.
- Required opaque provider state is round-tripped adapter-locally without
  becoming HER authority.
- Missing opaque state never causes fabricated reasoning or unsafe replay.

### 23.4 Strategy, tools, and safety

- Existing Strategy schema v3 remains exact.
- Strategy Cards, Habits, Planning, and Replanning retain their current
  authority and representations.
- Tool Registry remains the only execution authority.
- Every tool/control result matches the exact pending event.
- No side effect duplicates across turns, providers, reconstruction,
  compaction, restart, or cancellation.
- Unknown side-effect state fails safely.
- Review and terminal hard gates show no regression.

### 23.5 Quality and efficiency

- Existing hidden quality validation remains non-inferior.
- Tool errors and non-zero exits remain correctly recognised.
- Session transport shows one full open snapshot and incremental later turns.
- Provider-specific latency, token, cost, and P95 results are reported
  separately.
- No performance claim is made for unqualified providers or before paired
  evaluation.

## 24. Migration and compatibility

During rollout:

- existing stateless HER invocation remains available behind an explicit
  compatibility flag;
- new conversations may opt into fixed-session mode by exact capability and
  canary policy;
- an active stateless invocation is never silently converted into a fixed
  session after tool work starts;
- fixed sessions are versioned and do not resume across incompatible schema
  changes without explicit migration/rebase;
- each internal provider route is enabled only after its exact model passes
  conformance; and
- Codex backend behavior remains unchanged.

The end-state removes repeated HASHI context reconstruction for supported HER
sessions. Legacy stateless mode may remain only for explicit compatibility or
recovery boundaries, not as hidden normal behavior.

## 25. Main risks and controls

| Risk | Control |
|---|---|
| HER session state becomes a second PCM authority | HASHI remains authoritative; every delta is revisioned, authenticated, acknowledged, and auditable |
| Duplicate incremental delivery creates duplicate work | Message ID, idempotency key, state version, canonical sequence, and exact event pairing |
| Fixed session leaks context across conversations | Binding fingerprints, epochs, strict isolation, expiry, and cross-session tests |
| Stateless providers force large internal retransmission | HER-owned projection/compaction, provider caching when safe, and separate transport versus provider metrics |
| Provider-native session becomes hidden core dependency | Canonical HER event log and reconstructed adapter conformance |
| Different stages use different providers and lose context | Deterministic stage projections from the same canonical session state |
| Opaque reasoning artifact leaks or becomes authority | Adapter-private retention, digest-only core metadata, redaction/encryption, and no semantic dependence |
| Provider offers no reasoning | Reasoning-absent conformance is mandatory |
| Provider lacks safe tool calls | Strict structured mode or honest `tool_request_mode=none` |
| Strategy schema is accidentally redefined | Exact schema v3 wrapper and regression tests against current parser |
| Reconstruction repeats a side effect | Evidence correlation, completed-event replay gate, unknown-state fail-safe |
| Turn cancellation destroys useful session context | Separate cancel-turn and close-session operations |
| Multiple concurrent turns race on one workspace | One-active-turn lock and explicit steer/queue semantics |
| Session grows beyond context limits | HER-owned checkpoints and compaction preserving authority/evidence |
| Codex architecture leaks into HER | Explicit peer-backend boundary and architectural import/route tests |
| Provider/model capability changes | Exact route qualification, live conformance, and typed pre-start failure |

## 26. Rejected alternatives

1. **Treat one Primary Job as the fixed backend session.** This loses
   continuity after every user response and forces HASHI to resend context.
2. **Bind HER continuity to a provider-native thread.** Stateless providers
   would be excluded and provider failure could destroy the logical session.
3. **Use Codex inside HER.** Codex is already a separate HASHI backend; HER
   uses model providers such as HASHI API, DeepSeek, and OpenRouter.
4. **Send the complete PCM and transcript on every turn.** This defeats the
   fixed-backend contract and creates duplicate-state risks.
5. **Let HER infer PCM changes from conversation prose.** Authority changes
   must arrive as typed HASHI deltas.
6. **Freeze one model/provider for the whole HER session.** HER must be able to
   route internal stages independently while retaining logical continuity.
7. **Require visible reasoning for continuity or recovery.** This excludes
   valid providers and depends on state HER does not own.
8. **Discard all reasoning-related provider fields.** Some providers may need
   opaque artifacts for protocol continuation; adapters must transport them
   without making them semantic core state.
9. **Store raw chain of thought in canonical HER history.** It is unnecessary
   for correctness and creates privacy, security, and portability problems.
10. **Let Provider adapters execute tools directly.** This bypasses Smart
    Tool, Tool Registry, permissions, evidence, and Replanning.
11. **Infer tool calls from free prose.** Only formal native or strict
    structured requests are safe.
12. **Automatically change Provider after side effects begin.** This can
    duplicate work and invalidate recovery evidence.

## 27. Definition of done

HER v2 is a fixed backend when:

> HASHI opens one HER session with a full authoritative snapshot. HER owns a
> durable provider-neutral logical thread across multiple user turns. After
> open, HASHI sends only new user messages, PCM/resource deltas, and typed
> control events. HER materialises current authority, routes Strategy,
> Planning, Execution, Replanning, and Review through qualified HASHI API,
> DeepSeek, OpenRouter, or other model providers, and preserves tools,
> evidence, recovery, cancellation, and completion without depending on
> provider-native sessions or reasoning visibility.

The first complete vertical slice is done when two user turns execute in one
HER session through a real HASHI API model route, the second turn receives no
repeated full PCM or transcript from HASHI, Strategy schema v3 remains exact,
and all tool/evidence gates pass.

Provider neutrality is proven when the same session and job conformance suite
also passes at least one real non-HASHI provider route, with both reasoning
absent and provider-required continuation state cases.

The broader rollout is done when durable restart recovery, PCM rebase,
compaction, cancellation-with-session-retention, DeepSeek/OpenRouter
qualification, hidden quality validation, isolation, and cleanup gates all
pass—and no HER core or internal provider path selects or depends on Codex.
