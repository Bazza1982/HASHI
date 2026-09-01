# HASHI Persistent Multi-Session Frontend Design

| Document information | Value |
|---|---|
| Purpose | Define a client-neutral persistent Session service for agentic frontends |
| API | Persistent Session API v1 |
| Status | Implemented behind a fail-closed qualification boundary |
| Scope | Desktop, web, mobile, IDE, operations console and future clients |
| Architecture | [HASHI Frontend Connector Architecture](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md) |

## 1. Decision

PAO is the sole authoritative HASHI owner of sent agent-chat state. One user-facing
Chat maps to one persistent HASHI Session. An Agent participates in a Session
but is not the Session identity.

```text
Session
  -> context generation
  -> Message
  -> Run
      -> execution attempt + fencing token
      -> durable typed Events
      -> approval / attachment references
      -> final Assistant Message
```

A conforming frontend discovers capabilities, selects or creates a Session,
submits the current message, and renders HASHI's snapshot and Event stream. It
does not resend conversation history or maintain a second authoritative Chat
archive.

## 2. Client neutrality

The API is selected by protocol compatibility and advertised size limits. HASHI
does not branch on a client brand, executable name, repository, product
revision, installer, or distribution channel.

Clients identify their surface with a validated protocol identifier and may
provide an opaque client/channel key. These values bind projections and delivery
state; they do not grant authorization. Every operation rechecks the
authenticated owner and target resource.

Engine choice is also hidden from clients. HER v2 and other Engine Providers
receive the same outer HASHI Conversation Session identity and
context-generation semantics. Any Engine Session or Model Provider Context
behind that binding remains an internal boundary.

## 3. Ownership

| Concern | Owner |
|---|---|
| Conversation Sessions, Messages, Runs, Events and consumer ACK | PAO |
| PCM assembly, Context projection and Memory+ | PCM |
| Engine binding, outer routing and HASHI Tool authority | PAO |
| Engine-internal Turn orchestration and Model Provider routing | Selected Engine |
| Approval challenge lifecycle and fencing | PAO |
| Durable attachment object or authorized reference | PAO |
| Unsent drafts and window layout | Frontend |
| Packaging, installation and update selection | Integrator |
| Non-agent product data and final domain authorization | Owning service |

## 4. Domain invariants

### Session

A Session has an opaque ID, authenticated owner, Agent, status, revision,
context generation, title, memory policy and timestamps. Every Agent has one
permanent default Session. Archive and tombstone remove Sessions from ordinary
lists without deleting canonical customer state.

### Message

Messages are immutable, ordinal, typed and Session-scoped. A Message records its
author, source, context generation, content digest and optional Run. IDs and
ordinals are assigned by HASHI.

### Run and attempt

A Run binds one accepted user Message to an Agent execution. Idempotency is
scoped to the Session and request digest. Each claim increments the attempt and
monotonic fencing token. Every authoritative write from a worker carries the
current token; stale writers fail closed.

Queued or running Runs found after runtime restart are terminalized as
`interrupted`. Their user Messages remain intact, fencing advances, and one
durable interruption Event is appended. HASHI does not pretend to resume an
unknown in-flight execution stack.

### Event and consumer

Events are durable, typed and strictly sequenced per Session. Delivery is
at-least-once. A consumer polls from its authoritative acknowledged sequence;
ACK may not advance beyond Events actually issued to that consumer. A lost
client cache can recover from a new snapshot and Event consumer.

## 5. Context isolation and memory

Recent exchanges, Memory+, compaction pointers, capsules, backend bindings and
working files are scoped by Session and context generation. A Session never
receives another Session's unpromoted raw history.

`fresh` advances the context generation without deleting retained records and
is blocked while the Session has an active Run. Old backend threads and context
artifacts cannot enter the new generation.

Manual or scheduled promotion moves eligible terminal exchanges into unified
Agent memory using idempotent watermarks and provenance. Only promoted material
may become retrievable across Sessions.

## 6. API surface

The generic v1 surface provides:

- capability and Agent discovery;
- Session create/list/read/update/archive/tombstone;
- Message listing and canonical snapshot;
- Run creation, status and cancellation;
- ordered Event polling, consumer creation and ACK;
- fresh context generation;
- attachment staging and commit;
- approval decision; and
- promotion status, scheduling and execution.

Capability publication includes independent Session, Event, Control,
Attachment, Approval and Fencing versions. Routes return
`session_api_not_ready` until the complete qualification boundary is enabled.

## 7. Run submission

A frontend submits:

- `session_id` in the route;
- a unique idempotency key;
- the current typed Message content;
- an optional high-level execution mode;
- committed attachment references when supported; and
- optional parent/supersession identity when supported.

It does not submit prior Chat history, provider/model selection, backend thread
IDs, memory injection flags or caller-asserted authoritative Run identity.

The request digest covers every semantic input that could change execution.
Reusing the same key and digest returns the original acceptance; reusing the key
with a different digest returns `idempotency_conflict`.

## 8. Controls

Cancel is idempotent, moves a non-terminal Run to `stopped`, clears its worker
and increments fencing. A late worker cannot append a final response after the
Run is terminal.

Fresh is a Session boundary, not deletion. Archive is non-destructive.
Promotion is provenance-preserving. Every mutation must be safe under response
loss and retry.

Future steer, clarification, transfer and attachment-revocation controls extend
this same client-neutral state machine; they must not introduce client-specific
routes.

## 9. Attachments and approvals

Attachment metadata is owner- and Session-scoped, content-digest bound, and
committed explicitly. A complete integration must negotiate attachment support
before exposing upload UI. Unsupported content types or absent grants fail
closed.

An approval is bound to a Run, attempt, fencing token and exact scope. A
decision never revives an expired attempt. Product-owned services still perform
their final authorization immediately before their own side effect.

## 10. Security

Possession of an opaque ID is not authorization. Reads, mutations, controls,
attachments, approvals, promotion and Event subscriptions recheck owner,
deployment and Agent scope.

Client IDs and surface identifiers are routing metadata, not principals. Team
and enterprise deployments must bind requests to the existing identity and
policy services. Personal deployments may use a loopback capability token but
follow the same repository contract.

## 11. Compatibility and limits

A client is compatible when it supports every required capability version and
operates within advertised limits. Compatibility is not a product or revision
allow-list.

Qualification declares its history size, message size, attachment limits,
pagination behaviour and Event replay expectations. The controlled test harness
records the exact HASHI and client revisions only as evidence provenance.

Pagination, attachment transport, approval reconstruction and optional controls
must be advertised accurately. A partially implemented capability remains
absent rather than being published with reduced semantics.

## 12. Packaging, activation and rollback

The source package records the full HASHI SHA, every tracked artifact hash,
capability versions and last-known-good revision. A trusted publisher may attach
an offline signature. Integrators verify package hash, signature, startup health
and capability negotiation before selection.

Rollback changes executable selection, not canonical customer state. Session,
Message, Run, Event, ACK, attachment, approval, promotion and audit stores are
preserved. A rollback receipt proves that the last-known-good runtime can reopen
and project the same state.

## 13. Mandatory qualification assertions

1. Interleaved Sessions never exchange unpromoted context or backend state.
2. Idempotent acceptance never creates a second Run after response loss.
3. Stale fencing writes and expired approvals are rejected.
4. Restart reconciliation leaves no permanently non-terminal orphaned Run.
5. Event replay remains ordered and ACK never advances beyond issued data.
6. Snapshot plus Events reconstructs client-visible state after cache loss.
7. Fresh, archive and promotion preserve canonical records.
8. Cross-owner and cross-Agent access fails without confirming resource
   existence.
9. The generated history meets the declared size threshold, the current request
   appears once, and a cross-Session sentinel appears zero times in the provider
   envelope.
10. Missing capability, invalid signature, unhealthy runtime or state-losing
    rollback keeps the last-known-good path selected.

## 14. Public/private boundary

HASHI publishes only the generic protocol, implementation, tests and
qualification tooling. Product-specific revisions, packaging instructions,
device paths, defect journals, compatibility records and release locks belong
in the integrator's private repository.
