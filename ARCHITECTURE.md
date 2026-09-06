# HASHI System Architecture

The canonical architecture and engineering guideline is
[`docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`](docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md).
The normative Python/ABI and transactional function-generation decision is
[`docs/HASHI_PYTHON_RUNTIME_COMPATIBILITY.md`](docs/HASHI_PYTHON_RUNTIME_COMPATIBILITY.md).
The current HASHI3 implementation and promotion boundary is
[`docs/HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md`](docs/HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md).
The accepted HASHI3 Browser/Computer Worker, cross-WSL/Windows discovery, lease,
and silent-background-runtime target is
[`docs/HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md`](docs/HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md).

## 1. Purpose and authority

This document defines HASHI at the highest architectural level. It establishes
the vocabulary, functional ownership, engineering layers, Session boundaries,
and dependency direction that all more detailed documents must follow.

When documents conflict, authority is resolved in this order:

1. this Level 0 architecture;
2. the engineering-layer and module specifications linked below;
3. accepted subsystem decisions and current implementation specifications;
4. implementation plans and testing plans; and
5. release notes, checkpoints, migration records, and historical plans.

A specialised document remains authoritative inside its declared scope only
when it is consistent with the higher level. Historical documents remain valid
evidence of what was designed, tested, or released at the time; they do not
override the current architecture.

## 2. One system, two orthogonal dimensions

HASHI is described along two dimensions. They answer different questions and
must not be flattened into one hierarchy.

### 2.1 Functional ownership: what the system does

HASHI has four top-level functional modules:

1. **Persona-Context-Memory (PCM)**
2. **Provider-Agnostic Orchestration (PAO)**
3. **HASHI Engine Runtime v2 (HER v2)**
4. **Frontend Connectors**

### 2.2 Engineering layers: how the program is deployed and changed

HASHI has four engineering layers:

1. **Core** — the small, continuously running process kernel and stable
   contracts that let normal functions be adopted without a cold restart.
2. **Functions** — hot-reloadable product behaviour, including the primary
   implementations of PCM, PAO, HER v2, and Frontend Connectors.
3. **Platform Configuration** — adaptations required for Windows, Linux,
   macOS, WSL, packaging, and platform-side services.
4. **Instance Configuration** — machine- and deployment-specific identity,
   paths, ports, credentials, feature choices, and local resource locations.

The detailed engineering rules are defined in
[HASHI Layered Runtime Boundaries](docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md).

### 2.3 Classification rule

Every product capability and authoritative state must have:

- exactly one functional owner; and
- exactly one primary engineering-layer placement.

For example, Workzone state is owned functionally by PAO and is implemented
mainly in the Functions layer. PCM projects enabled Workzones into Context but
does not become their state owner.

Module-neutral Core and general-purpose utilities may have no functional-module
owner. They must not independently acquire product policy, duplicate
authoritative state, or become an informal fifth module.

## 3. Canonical terminology

### 3.1 Provider

**Provider** is a general term for a party or component that supplies an
execution capability. It must be qualified whenever the category could be
ambiguous:

- An **Engine Provider** or **Harness Provider** supplies an agentic runtime
  that turns model capability into agentic work. Examples include Codex CLI,
  Claude Code, Gemini CLI, Grok CLI, and HER v2.
- A **Model Provider** supplies model inference. Examples include HASHI API,
  DeepSeek, OpenRouter, xAI, or another capability-conformant inference
  service.

The broad word in **Provider-Agnostic Orchestration** deliberately includes
both categories. PAO must not embed the private semantics of either category.
At the outer runtime boundary PAO normally selects an Engine Provider. Inside
HER v2, HER selects and routes Model Providers. If a PAO surface stores or
forwards a Model Provider choice, it does so through a typed Engine contract
without taking ownership of that provider's internal semantics.

Unqualified `provider` is acceptable only where the category is already
unmistakable from the local scope.

### 3.2 Engine and Harness

An **Engine** and a **Harness** are conceptually the same thing: a runtime that
converts raw model ability into agentic work through context, tools, control
loops, persistence, and delivery contracts. HASHI documentation uses
**Engine** as the canonical short term and may write **Engine (Harness)** on
first use.

An Engine is not the same as a Model Provider. An Engine may use one or more
Model Providers, a local model, or a CLI-managed inference path.

### 3.3 Connector and adapter

- A **Frontend Connector** translates a HASHI protocol or transport into a
  user-facing channel while keeping HASHI state authoritative.
- An **Engine Adapter** binds PAO to one Engine Provider.
- A **Model Provider Adapter** binds an Engine, such as HER v2, to one Model
  Provider.

Adapters translate contracts. They do not become a second owner of the state
or policy they carry.

### 3.4 Agent, Session, Run, and Turn

- An **Agent** is a configured HASHI identity with PCM, permissions, runtime
  choices, and an Agent workspace.
- A **HASHI Conversation Session** is the PAO-owned user conversation and
  control boundary.
- An **Engine Session** is an Engine-owned logical thread used after PAO binds
  a conversation to that Engine.
- A **Provider Context** is rebuildable transport or native-thread state. It is
  never authoritative HASHI or Engine Session state.
- A **Run** is a PAO-owned accepted execution of one user Message.
- A **Turn** is an Engine's processing unit inside an Engine Session. HER v2
  owns HER Turns; other Engine Providers may expose different internal forms.

## 4. Functional modules

### 4.1 Persona-Context-Memory (PCM)

PCM owns the definitions, sources, authority order, retrieval, assembly,
versioning, and typed projection of:

- Persona;
- system and runtime Context; and
- short- and long-term Memory material made available to an Agent.

PCM may project available Skill and Tool catalogues into an Engine request. It
does not grant permission, execute a Tool, choose an Engine, own a Run, or own
the underlying Workzone and Session control state that it projects.

The module contract is defined in
[HASHI PCM System Design](docs/HASHI_PCM_SYSTEM_DESIGN.md).

### 4.2 Provider-Agnostic Orchestration (PAO)

PAO is HASHI's outer control plane. It owns:

- Agent identity and lifecycle;
- HASHI Conversation Sessions, Messages, Runs, Events, and context generation;
- Conversation-to-Engine binding and Engine selection;
- outer task and multi-agent orchestration;
- Nagare, Superloop, Jobs, Scheduler, HChat, and Remote coordination;
- Workzone control state;
- HASHI-level Tool capability, permission, invocation, and execution control;
  and
- recovery, cancellation, delivery coordination, and operational policy that
  spans Engines or frontends.

PAO does not assemble PCM content, implement an Engine's internal cognitive
lifecycle, or own a frontend's window and presentation state.

The module contract is defined in
[HASHI PAO System Design](docs/HASHI_PAO_SYSTEM_DESIGN.md).

### 4.3 HASHI Engine Runtime v2 (HER v2)

HER v2 is HASHI's native Engine. It is both a top-level HASHI functional module
and one Engine Provider selectable by PAO.

HER v2 owns:

- the durable HER Engine Session after PAO binds a HASHI Conversation Session;
- ordered HER Turns and materialised PCM/resource revisions;
- Direct, Strategic, and Planned execution policy;
- Strategy, Planning, Execution, Tool loops, Replanning where an internal
  policy permits it, Review where an internal policy permits it, and
  Finalisation;
- HER-internal Model Provider routing;
- HER recovery evidence, checkpoints, Compact, and Engine-level metering; and
- HER-specific Habit and Meditation behaviour.

HER v2 is fixed at the **PAO-to-Engine boundary**: a HASHI Conversation Session
has a stable binding to its HER Engine Session and later messages use
incremental PCM/resource deltas. HER remains flexible inside that boundary: it
may change Model Provider, model, reasoning setting, or process while
preserving its logical Session.

HER v2 does not own the enclosing HASHI Conversation Session, frontend state,
or provider-native thread state. Its current production surface has exactly
three modes: Direct (`zero`), Strategic (`low`), and Planned (`medium`). The
retained `high`, `xhigh`, and `max` implementations are dormant regression and
future-design material, not selectable product modes.

The detailed lifecycle is defined in
[HER v2 Product Requirements and Technical Design](docs/HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md),
subject to accepted current decisions linked from that document.

### 4.4 Frontend Connectors

Frontend Connectors expose HASHI through terminal, messaging, local API, or
authenticated remote protocols. HASHI includes:

- the built-in reference TUI, which remains part of HASHI;
- Telegram and WhatsApp connectors;
- the Backend API and Persistent Session API;
- HChat and necessary Remote projections used by compatible clients; and
- shared connector contracts for attachments, controls, events, and delivery.

An external desktop, web, mobile, IDE, or operations UI may use HASHI
infrastructure when it conforms to the published protocol. The Connector and
API belong to HASHI; the external UI, its packaging, layout, drafts, and
product-specific data do not.

Workbench is retired, and its successor is a separate repository. Legacy
identifiers such as `workbench_api.py`, `workbench_port`, and compatible route
or token names may remain until migrated, but they name compatibility surfaces,
not a HASHI frontend product. Architecture documentation must not name private
external products.

The connector boundary is defined in
[HASHI Frontend Connector Architecture](docs/HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).

## 5. Session and state authority

| State boundary | Authoritative owner | Meaning |
|---|---|---|
| HASHI Conversation Session | PAO | User/Agent/client binding, Messages, Runs, Events, Workzones, context generation, and Engine binding |
| HER Engine Session | HER v2 | Durable HER logical thread, accepted Turns, materialised PCM/resources, plans, Tool evidence, recovery, Compact, and Engine meter |
| Other Engine Session | Selected Engine Provider | Provider-specific logical thread under the PAO binding contract |
| Provider Context | No persistent product authority | Rebuildable model transport, response-chain, thread, cache, or process state |
| Frontend projection | Frontend Connector/client | Disposable view reconstructed from HASHI state; never a second canonical chat archive |

The word `Session` must be qualified wherever two of these boundaries could be
confused.

## 6. Control and data flow

```text
stable process core
    -> stable per-Agent route handles
        -> isolated, verified Function Worker generations
        -> local platform adoption
            -> local instance configuration
```

Changes should be local, derived from a single fact owner, and replaceable with
`/reboot` when they are functional. Python, dependencies, Core sources and
protocol/API versions require a planned Core migration. In-process module
reload is forbidden. Contributor workflow and required checks are in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

Core-owned Telegram and Workbench ingress sends slash control through the
versioned Function Worker RPC. Each Worker also owns a dedicated out-of-band
provider-interrupt lane, so stop, steer, focus, and retry can terminate active
CLI work before normal event-loop cleanup. Session Workzone slots publish only
their exact enabled roots to backends and tools; implementations must not widen
multiple roots to their common parent. HASHI Remote rescue remains a separately
deployed `L3_RESTART` sidecar, and its local hot-reboot hop must use the
token-protected Workbench admin command endpoint.
