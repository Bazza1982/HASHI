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
- wrapper, audit, or other outer runtime composition that spans Engines.

The stable process kernel belongs to the Core engineering layer. The Agent and
runtime policies operated through that kernel belong functionally to PAO.

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

