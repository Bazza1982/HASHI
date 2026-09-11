# HASHI Provider-Agnostic Orchestration (PAO) System Design

| Field | Value |
|---|---|
| Status | **Authoritative PAO module specification** |
| Effective date | 2026-09-01 |
| Parent architecture | [HASHI System Architecture](../ARCHITECTURE.md) |
| Scope | HASHI outer control plane, Conversation Sessions, Engine binding, capabilities, workflows, Jobs, and cross-agent coordination |

## 1. Definition

Provider-Agnostic Orchestration (PAO) is HASHI's outer control plane. It turns
an authenticated user or system request into a governed HASHI Run, selects an
Engine (Harness) Provider, supplies that Engine with authoritative PCM and
capabilities, coordinates work outside the Engine, and projects durable results
back through Frontend Connectors.

`Provider` has a broad meaning in the PAO name:

- an **Engine Provider** supplies an agentic Engine or Harness; and
- a **Model Provider** supplies inference to an Engine.

PAO is agnostic to both. Its outer selection is normally an Engine Provider.
Model Provider routing normally belongs inside the selected Engine, especially
HER v2. PAO may store or forward a Model Provider preference through a typed
Engine contract, but it must not absorb provider-specific request, thread, or
reasoning semantics.

## 2. Responsibilities

PAO owns the following product domains.

### 2.1 Agent and runtime control

- Agent identity, directory, lifecycle, and active runtime binding;
- Engine Provider discovery, selection, availability, and migration aliases;
- startup, stop, restart, hot-reload coordination, and runtime health; and
- Fixed/Flex working-mode policy, retired outer-composition migration, and any
  future runtime composition that spans Engines.

The stable process kernel belongs to the Core engineering layer. The Agent and
runtime policies operated through that kernel belong functionally to PAO.
The current working-mode contract is defined in
[Fixed and Flex Working Modes](FIXED_FLEX_WORKING_MODES.md).

### 2.2 HASHI Conversation Sessions

PAO is the sole owner of:

- HASHI Conversation Session identity and authenticated ownership;
- Agent and frontend/channel binding;
- Messages, Runs, attempts, Events, consumer acknowledgements, and fencing;
- context generation, archive, fresh, fork, promotion, and recovery controls;
- Workzone state and revision; and
- the stable Conversation-to-Engine Session binding.

An Engine may own its internal Engine Session but must not become a second
owner of the enclosing Conversation Session.

### 2.3 Outer orchestration

PAO owns orchestration across Agents, Engines, Runs, Sessions, time, or HASHI
instances, including:

- Nagare multi-agent workflows;
- Superloop long-running control loops;
- Jobs, cron, heartbeat, scheduler recovery, and background jobs;
- HChat and Hashi Remote coordination;
- transfers, queues, callbacks, cancellation, and delivery coordination; and
- outer approvals, policy, audit, and operational governance.

HER v2's Strategy, Planning, Execution, Tool loop, and recovery inside one HER
Engine Session are **inner orchestration** owned by HER v2. The shared word
`orchestration` does not transfer that lifecycle to PAO.

### 2.4 Skills, Tools, permissions, and execution

PAO owns the HASHI-level capability registry and execution authority:

- discover which Skills and Tools exist;
- filter them by Agent, request, stage, Workzone, permission, and policy;
- grant or deny invocation;
- route approved invocations to the correct implementation;
- preserve PAO-level execution and side-effect evidence; and
- stop, fence, or reconcile work when required.

PCM projects the authorised catalogue into an Engine request. HER v2 decides
when to request an available Tool during its Turn and preserves HER-level Tool
evidence. Neither PCM nor HER may grant a capability withheld by PAO.

## 3. Non-responsibilities

PAO does not own:

- Persona, Context, or Memory content assembly, which belongs to PCM;
- an Engine's internal Strategy/Planning/Execution lifecycle;
- HER v2's Engine Session, checkpoints, Compact, or Model Provider routing;
- provider-native thread, response-chain, cache, or hidden state;
- frontend window state, layout, unsent drafts, or product-specific data; or
- platform- and instance-specific values that belong in configuration layers.

## 4. Provider and adapter boundaries

```text
PAO
  -> Engine Adapter
       -> Engine Provider
            -> optional Model Provider Adapter
                 -> Model Provider
```

An Engine Adapter presents a common PAO contract for Engine lifecycle,
capability negotiation, Session binding, incremental input, activity, terminal
results, cancellation, and recovery. It may translate a legacy `backend` API,
but compatibility naming does not change conceptual ownership.

A Model Provider Adapter belongs to the Engine that uses it. Provider-native
IDs and continuation state may optimise transport, but they are never PAO
Conversation Session authority.

Exact model media metadata follows the independent
[Model Capability Discovery](HASHI_MODEL_CAPABILITY_DISCOVERY.md) decision.
PAO owns the derived fact, each Engine Adapter owns its implemented transport,
and instance policy owns permission; native support is their intersection.
Message admission is cache-only and never waits for catalogue network I/O.
The same exact OpenRouter identity and bounded evidence feed PAO's derived
price fact. Execution Engine and metadata source remain separate: Provider
reported cost is actual, while a catalogue valuation for another channel is
an explicitly labelled OpenRouter reference estimate. Every usage row freezes
the price revision and observed cache dimensions used at completion.

## 5. State model

```text
Agent
  -> HASHI Conversation Session
       -> Message
            -> Run
                 -> attempt + fencing token
                 -> Engine binding
                 -> ordered Events
                 -> terminal result
```

The Conversation Session is durable across frontend reconnects and may outlive
an Engine process. A Run is accepted at most once for one idempotency boundary.
Late writers and superseded attempts fail closed.

When HER v2 is selected, PAO binds the Conversation Session and context
generation to one HER Engine Session. PAO sends a complete PCM snapshot at
open/rebase and authoritative deltas thereafter. HER owns the logical thread
inside that binding; PAO retains outer Message, Run, Event, and delivery
authority.

## 6. Module interfaces

### 6.1 PAO to PCM

PAO supplies typed facts such as the current request, Agent, Conversation
Session, context generation, Workzones, available capabilities, history
references, and requested projection mode. PCM returns a versioned full
snapshot or delta with explicit authority and provenance.

PAO must not privately reconstruct a competing PCM envelope.

### 6.2 PAO to Engine Providers

PAO supplies:

- Agent and Engine Session binding identity;
- the current accepted input;
- full or delta PCM/resources;
- granted Tool/Skill capabilities and permissions;
- cancellation, steer, compact, close, or other typed controls; and
- correlation, fencing, and delivery metadata.

The Engine returns typed activity, capability, evidence, usage, pending-input,
failure, and terminal events. Unstructured provider-specific state must not
cross this boundary as authority.

### 6.3 PAO to Frontend Connectors

PAO exposes authenticated discovery, Conversation Sessions, Messages, Runs,
Events, controls, attachments, approvals, and operational status. Connectors
may cache projections but must reconstruct them from PAO-owned state.

PAO also owns the admission-time delivery decision for each Run. A Connector
may request a projection policy only through a typed, versioned contract. The
current TUI contract is `hashi.frontend-delivery` version 1, has `scope=run`, is
bound to an ephemeral TUI client identity, and contains the boolean
`telegram.mirror` projection choice. PAO validates and snapshots it before the
Run enters the queue; changing frontend preferences later cannot mutate an
in-flight Run. Missing, malformed, forged, legacy, or non-TUI suppression input
fails visible.

Turning off that projection does not create a new Conversation authority. The
TUI continues to use the shared Conversation Session and PAO still commits its
Message, Run, Events and terminal result. Only the Telegram delivery projector
is skipped for that TUI-origin Run; Telegram-native input, Scheduler, HChat,
other clients and other Runs retain their own admission decisions. PAO does not
replay skipped projections when a later Run enables Telegram mirroring.

PAO projects live presentation facts through Agent metadata. Engine, model,
effort and current switches come from the active runtime; HER v2 alone supplies
its structured Quick/Pro Model Provider routing. Frontends may format these
facts but must not infer them from files or become another catalogue owner.

### 6.4 Per-message source and private authorization

PAO owns the `hashi.current-message-context` version 1 snapshot. Its provenance
fields are frozen at admission and the same current snapshot is persisted on
Message and Run. Immediately before an execution attempt, PAO revalidates the
authorization portion and atomically refreshes both records before recording
the attempt projection. Retried or recovered work retains the original message
identity and source evidence but revalidates proof expiry, revocation, target,
content digest, and resource binding before granting scope.
An exact idempotent retry may reuse the same nonce only for the same proof and
binding. Reuse on another binding is rejected by the persistent PAO nonce
ledger.

Optional private authorization uses `hashi.private-authorization-proof` version
1 with HMAC-SHA-256. One proof binds message ID, source and target Agent/instance,
the SHA-256 of the sender-authored content, requested resources, issue time,
expiry, and a random nonce. The sender explicitly selects at most 16 credential
IDs for one message; HASHI never auto-attaches every credential held by an
Agent. The receiver's protected configuration alone maps a credential to group,
scopes, allowed source/target identities, resources, expiry and revocation.
Sender-claimed scopes are not accepted.

Credential material is stored only in the existing ignored `secrets.json`
boundary under `hashi_private_shared_credentials.credentials`. The minimal
management path is deliberate and local: generate or import a high-entropy
secret out of band into every authorized sender and receiver, define the
receiver-side mapping, and select its ID with HChat's repeatable
`--private-credential` option. Rotation replaces the shared secret; revocation
sets `revoked=true`; expiry uses an ISO-8601 timestamp or Unix time. Receiver
configuration is reread on every verification, so those changes affect queued
revalidation without a reboot. Raw values must never be placed in a prompt,
command argument, Agent configuration, tracked file, generic audit, or receipt.

Private authorization adds disclosure scope but never admits network traffic.
Ordinary HChat continues through its existing network/channel policy when no
private proof is supplied or when a proof is invalid, expired, or revoked.
Network authentication failure retains its existing fail-closed behavior.

## 7. Command ownership

Commands are connector entry points into domain contracts.

| Command family | PAO responsibility | Collaborating module |
|---|---|---|
| `/new`, `/fresh`, `/sessions`, `/use`, `/current`, `/archive`, `/fork` | Conversation Session lifecycle and selection | PCM supplies the resulting Context projection |
| `/backend` and compatible Engine selection | Engine Provider binding and migration | Selected Engine owns its internal Session |
| `/workzone` | Workzone state, validation, and revision | PCM projects enabled Workzones |
| `/handoff` | Session continuity operation | PCM assembles the continuity payload |
| `/clear` | Coordinate Session/media/Engine cleanup | Connector media and selected Engine participate |
| `/jobs`, `/loop`, `/bg` | Job and outer orchestration lifecycle | Connector renders status |
| `/stop`, `/steer` | Outer cancellation, fencing, and new-Run/Turn coordination | Selected Engine terminates its internal work |

HER-specific effort, Habit, Meditation, provider, and model settings reach HER
through a Connector/PAO control surface, but their internal meaning remains
HER-owned.

## 8. Workflow hierarchy

- **Nagare** is PAO's DAG-based multi-agent workflow capability.
- **Superloop** is PAO's long-running controller and closeout capability.
- **Minato** and **Shimanto** currently form a lightweight project/phase
  vocabulary, context envelope, registry, and logging integration.

Minato/Shimanto are not currently a complete project orchestration Engine and
must not be presented as a peer replacement for Nagare or Superloop.

## 9. Engineering-layer placement

Most PAO behaviour belongs in the Functions layer. Core retains only the
minimal long-lived process, locks, lifecycle handles, and rebuild contracts
needed to keep PAO functions replaceable without a cold restart. Platform and
Instance Configuration supply adaptation and local values; they must not fork
PAO policy.

The detailed placement rules remain governed by
[HASHI Layered Runtime Boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md).

## 10. Current implementation mapping

PAO is an architectural module, not yet one physical package. Its current
implementation is distributed across the orchestrator runtime, Session store,
Engine registry/adapters, command handlers, Nagare, Superloop, Jobs, HChat, and
Remote.

Current compatibility debt:

- `backend` is still used by some interfaces for both Engine Providers and
  Model Provider adapters;
- PAO ownership is not consistently named in files or older documents;
- some connector behaviour is physically coupled to Telegram types; and
- legacy Workbench identifiers remain around the Backend API.

These identifiers may remain while compatibility requires them. New policy and
documentation must use the architecture terms and must not create parallel
state owners.

## 11. Future-development rules

New PAO work must:

1. identify one PAO state owner and one persistence boundary;
2. use qualified Engine Provider or Model Provider terminology;
3. keep provider-specific semantics behind an adapter;
4. preserve Conversation Session authority outside Engines and frontends;
5. request PCM through the PCM contract rather than rebuilding it;
6. keep Engine-internal cognitive policy inside the selected Engine;
7. expose transport-neutral Events and controls where practical; and
8. remain hot-reloadable unless a change genuinely alters the Core contract.

### Agent transfer terminal ownership

The shared Functions `AgentMoveManager` owns durable confirmation intent,
execution retries and terminal delivery for Move/Clone. The existing coordinator
journal remains authoritative for migration state; Worker RPC and CLI enqueue
submit the same intent. The manager stops the source Worker and then continues
the coordinator after successful disablement, including self-moves. Accepted
work survives shared Functions replacement; recovery commands reconcile the
background receipt. Detailed workspace inventories remain in the coordinator
journal, not duplicated in the bounded background receipt.

### Health after local Clone and Move (2026-09-10)

PAO startup health is a projection of current Worker/connector state. A Worker
with no configured Telegram ingress is intentionally local, not a Telegram
failure. After startup, health reads reconcile through StartupManager: removed
successful Worker entries leave the projection, while failed/pending startup
entries and unrelated service failures remain visible. Reads must not reconcile
across startup or a shared Functions handoff. This does not alter Core lifecycle.
