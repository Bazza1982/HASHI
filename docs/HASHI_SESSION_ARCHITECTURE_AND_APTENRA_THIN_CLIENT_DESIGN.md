# HASHI Persistent Session Architecture and Aptenra Thin-Client Design

| Document information | Value |
| --- | --- |
| Purpose | Define the authoritative persistent Session layer that makes HASHI the sole owner of user-facing agent conversation history, working context, memory, runs, and events, while reducing Aptenra to a packaging and presentation client. |
| Status | Final founder-approved design; core HASHI runtime implemented. Aptenra Session API v1 remains fail-closed until the qualification gate passes. |
| Version | 1.3 |
| Date | 27 August 2026 |
| Authority | Founder |
| Revision | 27 August 2026 — finalized short-term Session isolation, one permanent default Session per Agent, scheduled/manual memory promotion, non-destructive Session retention, current-Session fixed-backend handoff, and the fail-closed external API qualification boundary. |
| HASHI assessment baseline | `main@c0e10721711675df9921ca3e8744861c79165a7c` |
| Aptenra assessment baseline | `mother_debug@07d2e2e9ca1d697aed9487634441786da890878f` |
| Related specifications | `HASHI_PCM_SYSTEM_DESIGN.md`, `HASHI_PCM_UPGRADE_TEST_PLAN.md`, `HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md`, `HER_V2_AUTO_COMPACTION_DESIGN.md` |

## 1. Executive decision

HASHI will become the sole authoritative owner of all sent agent-chat state.
One user-facing Chat maps to one persistent HASHI Session. An Agent participates
in a Session but is not the Session identity.

Every Agent has one permanent default Session. A client or channel uses that
Session until the user creates or selects another Session. Sessions are the
Agent's isolated short-term conversation instances: while work is in progress,
one Session cannot receive another Session's unpromoted messages, working
memory, Memory+, capsules, tool state, Workzone, or backend thread.

The canonical hierarchy is:

```text
Session
  -> Run
      -> User Message
      -> HER turn / backend invocation
      -> durable Events
      -> final Assistant Message
```

Aptenra will not maintain or submit conversation history, agent working
context, agent memory, backend-session state, model-routing state, or result
correlation ledgers. It will create or select a HASHI Session, submit the
current user message, render HASHI's durable event stream, and retrieve the
canonical Session projection from HASHI.

This design is backend-neutral. HER v2, fixed CLI backends, API backends, and
future backends all receive the same HASHI-owned Session semantics. No caller
may need to know which backend HASHI selected.

Session isolation is not a permanent long-term memory silo. At the configured
local daily promotion time, or when the user invokes `/promote`, terminal
Session records advance into unified Agent memory. After promotion, every
Session for that Agent may retrieve the promoted material as Agent memory. Raw
Session messages are never injected wholesale into another Session prompt;
they remain completely retained in HASHI logs and become selectively
retrievable through the Agent memory index.

The initial Aptenra cutover starts with empty HASHI Sessions. Existing Aptenra
Chat history will not be migrated. Existing local data must not be destroyed
silently during development or rollback preparation.

## 2. Why this change is required

Current HASHI has a mature Persona-Context-Memory system and a mature HER v2
turn runtime, but it does not yet have a persistent user-conversation Session
domain.

At the assessment baseline:

- completed exchanges are stored in an Agent-wide timeline without a
  `session_id`;
- recent-history assembly selects the newest exchanges for the Agent rather
  than for one user Chat;
- Memory+ and the active Auto Compact pointer are Agent-workspace scoped;
- the Workbench request-activity projection is bounded, in memory, and cleared
  on restart;
- process-local request IDs are not durable Run identities;
- the API Gateway `session_id` is a short-lived provider-message cache, not a
  user Session;
- fixed CLI adapters may retain one backend thread on an adapter instance; and
- the HER v2 Ledger is correctly turn-scoped but has no parent user Session.

Those properties are safe for one effective conversation timeline per Agent.
They cannot isolate multiple Chats with the same Agent once the client stops
resending local history.

Aptenra currently compensates by retaining complete local Chats, selecting a
bounded window, maintaining a local owner map, and using an Aptenra-specific
HASHI API fork. That fork adds request-result, cancel, history, isolation, and
memory-suppression behaviour not present in the assessed HASHI1 mainline. The
target architecture removes that fork. Persistent Session support must be a
generic HASHI capability, not an Aptenra-specific code path.

## 3. Goals

The implementation must provide all of the following:

1. Multiple durable, isolated Sessions with the same Agent.
2. HASHI-owned Chat lists, titles, archive state, messages, attachments,
   working context, continuity capsules, runs, and events.
3. Backend-independent request, continuity, cancellation, steering, and result
   semantics.
4. Session-scoped recent history, fresh boundaries, Memory+, and Auto Compact.
5. Scheduled and manual promotion into unified Agent-level long-term memory
   with full provenance and idempotent per-Session watermarks.
6. Durable at-least-once Event delivery with idempotent exactly-once client
   projection across reconnects and HASHI restarts.
7. Stable, versioned, authenticated APIs suitable for Aptenra, Workbench,
   Telegram, WhatsApp, and future clients.
8. A pristine HASHI upstream integration boundary: downstream products may pin
   and configure HASHI but must not maintain behavioural runtime forks.

## 4. Non-goals

This design does not:

- move Aptenra Mail, projects, tasks, settings, or other non-Chat product data
  into HASHI;
- make HER v2 responsible for user Session lifecycle;
- resume an interrupted in-flight HER execution stack after process restart;
- expose backend thread IDs or provider caches to clients;
- require simultaneous execution of multiple Sessions for one Agent;
- migrate historical Aptenra Chats during the initial cutover; or
- make canonical raw audit evidence the user-facing Chat database.

## 5. Architectural ownership

| Concern | Authoritative owner |
| --- | --- |
| Session identity, list, title, archive and deletion state | HASHI |
| Sent user messages and final assistant messages | HASHI |
| Run state, events, final result and control correlation | HASHI |
| Durable Event consumer identity, ACK and authoritative cursor | HASHI |
| Recent exchanges, continuity capsule and fresh boundary | HASHI Session layer |
| Session working memory and Session Memory+ | HASHI Session layer |
| Persona, system prompts, Agent configuration and Habits | HASHI Agent layer |
| Promoted unified Agent long-term memory | HASHI Agent memory service |
| HER plan, Ledger, stage execution and review | HER v2 inside HASHI |
| Provider, model, reasoning, backend and backend thread | HASHI |
| Tool orchestration and approval challenge state | HASHI |
| Final product-domain permission check before a data mutation | Owning Aptenra service |
| Window layout, unsent drafts and temporary upload staging | Aptenra |
| Mail, projects, tasks and other non-Chat product data | Aptenra |
| Packaging, installation, updates and Runtime Lock | Aptenra |

Aptenra may render an approval request, but HASHI owns the approval challenge,
its Run association, lifecycle, and audit record. The Aptenra product service
must still enforce its own data authorization before committing a side effect.

## 6. Domain model

### 6.1 Session

A Session is the durable user-conversation aggregate. It has a globally unique,
opaque `session_id` independent of any Agent or backend identifier.

Minimum Session fields are:

- `session_id`;
- owner principal and deployment/tenant scope;
- presentation Agent and participant Agents;
- status: `active`, `archived`, or `deleted`;
- title and title provenance;
- creation and update timestamps;
- monotonically increasing revision;
- active context generation;
- memory policy; and
- whether this is the Agent's permanent default Session; and
- optional channel binding metadata.

Version 1 normally has one presentation Agent at a time. Delegated Agents are
participants in Runs within the same Session. A later explicit Agent transfer
may change the presentation Agent without changing `session_id` or losing
history.

### 6.2 Message

Messages are immutable, ordered records with globally unique `message_id`
values. Corrections, edits, and deletions create revisions or tombstones rather
than silently rewriting an earlier record.

Each message records:

- `session_id` and optional `run_id`;
- role and author identity;
- typed content blocks;
- visibility and history eligibility;
- attachment and Artefact references;
- reply/revision references;
- content hash; and
- creation timestamp and Session ordinal.

Only visible user messages paired with visible final assistant messages form
completed historical exchanges. Commentary, thinking, tools, receipts,
placeholders, errors, and partial output remain typed Run Events and do not
become raw recent-history exchanges.

### 6.3 Run

A Run represents one accepted user request. It has a globally unique `run_id`
and exactly one authoritative root user message. A `waiting_user` Run may also
reference later typed clarification or approval-response Messages without
changing its root message or identity.

Minimum Run fields are:

- `session_id`, `run_id`, and `user_message_id`;
- presentation Agent;
- requested and effective execution mode;
- lifecycle state;
- idempotency key and normalized request digest;
- immutable request-time configuration and authorization audit snapshot;
- current attempt authorization snapshot and authorization policy version;
- parent, superseded, or resumed Run reference when applicable;
- current execution attempt, lease owner, lease expiry, and fencing token;
- HER turn/Ledger reference when applicable;
- timestamps, typed error, and interruption reason; and
- final assistant `message_id` when successful.

Run states include at least `queued`, `running`, `waiting_user`, `completed`,
`failed`, `stopped`, `superseded`, and `interrupted`.

One Session admits at most one active foreground Run by default. HASHI may also
serialize Runs across Sessions for one Agent while a backend requires it.
Concurrency is an implementation capability, not part of the continuity
contract; isolation is mandatory in either case.

### 6.4 Execution claim and fencing

Every execution occurs through a persisted Run attempt. A worker must claim a
queued Run with one compare-and-swap transaction that:

- verifies the expected Run revision and claimable state;
- increments the attempt number and monotonically increasing fencing token;
- records the worker identity and lease expiry; and
- changes the Run to `running`.

Claiming or resuming an attempt re-evaluates every mutable authorization input,
including principal and Agent capability, tool grants, attachment grants,
sensitivity policy, product-service access, and explicit revocations. The
request-time snapshot remains immutable audit evidence; it never entitles a new
attempt to reuse authority that has since been revoked. The effective
authorization snapshot and policy version are persisted on each attempt. A
revocation detected before or during admission fails closed and produces a
typed authorization Event. Product-domain mutations still perform their final
authorization at the owning service immediately before commit.

Worker heartbeats may renew only the matching lease. Every state mutation,
Event append, final commit, tool admission, approval result, and backend-binding
write carries the current fencing token. HASHI rejects a write from an expired
or superseded token even when the old worker is still alive.

Lease expiry does not automatically authorize replay. A provably unstarted or
read-only attempt may be reclaimed under policy. An attempt with an unknown,
incomplete, or side-effecting tool operation becomes `interrupted` and requires
user-directed recovery. Side-effect tool calls use a stable invocation key and
the Tool Gateway enforces idempotency where supported. If an external system
cannot provide idempotency, HASHI must not automatically repeat an uncertain
operation.

### 6.5 Event

Every user-visible or control-relevant Run transition is written to a durable,
append-only event stream. Each Event contains:

- globally unique `event_id`;
- `session_id`, `run_id`, and monotonically increasing Session sequence;
- kind, status, phase and delivery class;
- summary and typed detail payload;
- tool, file, progress, resolution, target-event, and provenance fields when
  applicable; and
- timestamp.

The Event store is the replay source for Aptenra. Transport delivery is
at-least-once. Aptenra produces an exactly-once UI projection by upserting
Messages by `message_id` and Events by `event_id`/sequence. The current bounded
`RequestActivityStore` may remain as an in-memory optimization but cannot be a
source of truth.

### 6.6 Attachment and Artefact

Before submission, Aptenra may stage local upload bytes. Once the message is
accepted, HASHI owns the durable content-addressed object or an authorized
reference to product-owned data. Access checks use the Session owner, Run, and
attachment grant. Raw bytes are never reconstructed from Chat text.

### 6.7 Backend binding

Backend session/thread identifiers are optional HASHI-internal bindings keyed
by at least:

```text
(agent_id, session_id, context_generation, backend identity)
```

They are optimization and continuation handles, never the source of truth. If
a backend binding is missing, invalid, revoked, or changed, HASHI reconstructs
the next invocation from canonical Session state. A binding from an older
context generation is never eligible for reuse.

## 7. Persistence design

The Session data plane must live in HASHI instance state, not inside an Agent
workspace that can be reset or replaced. SQLite with WAL and foreign keys is
acceptable for the personal/local profile. Team and enterprise deployments may
use another transactional store behind the same repository interface.

The logical schema includes:

```text
sessions
session_participants
messages
runs
run_attempts
run_events
run_projection_records
deferred_tool_invocations
approval_challenges
event_consumers
event_consumer_cursors
session_context_generations
session_capsules
backend_bindings
channel_bindings
attachments
idempotency_records
delivery_outbox
agent_memory_records
memory_promotion_watermarks
memory_promotion_jobs
memory_promotion_schedules
```

Critical transaction boundaries are:

1. Accept user message, create Run, reserve idempotency key, and append the
   acceptance Event atomically.
2. Persist an assistant final message, mark the Run terminal, append the final
   Event, and enqueue delivery atomically.
3. Persist cancellation, steering, fresh-boundary, and archive transitions
   before acknowledging them to the client.
4. Claim or renew a Run attempt and its fencing token atomically; every later
   executor write verifies the same token.

The durable outbox prevents a committed final result from being lost between
database commit and transport delivery. It guarantees at-least-once transport,
not exactly-once network delivery. Replayed Events are deduplicated by
`event_id` and sequence, while final Messages are upserted by `message_id`.

## 8. Session API v1

The new API is generic and versioned. It is introduced alongside the existing
legacy Workbench API until all clients migrate.

### 8.1 Capability and Agent discovery

```text
GET /api/v1/capabilities
GET /api/v1/agents
```

Capabilities advertise the Session API version, Event schema version, content
limits, supported controls, attachment support, and available execution modes.
They do not require the client to understand provider or backend internals.
The Session capability remains absent until the complete readiness gate in
section 17.1 passes; a partially implemented store or route set is not an
advertised capability.

### 8.2 Session lifecycle

```text
POST   /api/v1/sessions
GET    /api/v1/sessions
GET    /api/v1/sessions/{session_id}
GET    /api/v1/sessions/{session_id}/snapshot
PATCH  /api/v1/sessions/{session_id}
DELETE /api/v1/sessions/{session_id}
```

Creating a Session identifies an Agent and optional client surface. HASHI
returns the canonical `session_id`. Listing and reading Sessions are cursor
paginated and owner-scoped. PATCH uses an expected revision or ETag to prevent
lost updates.

The snapshot response is a canonical rebuild point containing Session metadata,
visible Messages, all still-visible Run projections, unresolved approval and
clarification state, latest sequence, earliest retained Event sequence, and
snapshot revision. A Run projection includes terminal state, typed error or
interruption reason, uncertain-side-effect status, parent/supersession links,
and the final Message reference when present. These projection records remain
for as long as their corresponding visible Chat records and are not removed by
ordinary operational Event compaction. The snapshot allows a new client or an
expired Event cursor to reconstruct the same user-visible timeline without a
local Chat archive.

Delete semantics are deliberately non-destructive. `DELETE` creates a tombstone
and removes the Session from ordinary active lists, but Session messages,
promoted Agent memory, canonical logs, and audit evidence remain. The product
should normally call this action archive or remove-from-list so it does not
imply physical erasure. Ordinary Session commands never wipe retained evidence.

### 8.3 Messages and Runs

```text
GET  /api/v1/sessions/{session_id}/messages
POST /api/v1/sessions/{session_id}/runs
GET  /api/v1/sessions/{session_id}/runs/{run_id}
```

The ordinary Run request contains only the current message, an idempotency key,
optional attachment references, and an optional high-level execution mode:

```json
{
  "message": {
    "content": [
      {"type": "text", "text": "Current request"}
    ]
  },
  "idempotency_key": "01K...",
  "execution_mode": "medium"
}
```

It must not contain:

- conversation history;
- a client-owned Chat or Turn correlation alias;
- transcript offsets;
- assembled PCM;
- backend, provider, model, or backend-session identifiers; or
- memory-injection or memory-persistence bypass flags.

The idempotency key is unique within the owner and Session scope. HASHI stores
it with a normalized SHA-256 request digest covering canonical message content,
immutable attachment digests, execution mode, and every option that can alter
execution. Retrying the same key and digest returns the original HTTP status,
`message_id`, `run_id`, and current resource projection. The retry must not
create another message, execution, tool side effect, or final answer.

The same key with a different digest returns `409 idempotency_conflict` and
neither request is modified. An accepted key remains bound permanently to the
Session and is never reused within that Session. Failed, stopped, superseded, and interrupted Runs remain bound to
their original key. A deliberate retry or continuation uses a new key and an
explicit parent Run reference. Because `session_id` values are never reused,
a tombstone cannot make an old key identify a new Session.

### 8.4 Durable events

```text
GET /api/v1/sessions/{session_id}/events?after_sequence=<n>
GET /api/v1/sessions/{session_id}/events/stream
POST /api/v1/sessions/{session_id}/event-consumers
POST /api/v1/sessions/{session_id}/event-consumers/{consumer_id}/ack
```

Cursor polling is mandatory. Server-sent events or another streaming transport
may be offered as an optimization. Delivery may repeat. A stable consumer is
bound to the authenticated Aptenra installation or another authorized client,
and HASHI stores its authoritative acknowledged cursor. Aptenra may cache the
opaque `consumer_id` and last seen sequence as non-content UI/delivery metadata,
but correctness must not depend on that cache.

Every Event response includes `earliest_available_sequence`,
`latest_sequence`, and `snapshot_revision`. When `after_sequence` predates the
retained Event range, HASHI returns `410 event_cursor_expired` with those fields
and the Session snapshot route. The client rebuilds from the snapshot, creates
or resumes a consumer, and continues after the snapshot's latest sequence.
Clients never infer ownership by reading an Agent-global transcript.

ACK is monotonic and idempotent. It may advance only to a sequence actually
issued for that Session and never moves backwards. Session snapshots are read
from one consistent database revision and include the exact sequence after
which replay must resume, preventing a snapshot/replay race.

### 8.5 Controls

```text
POST /api/v1/sessions/{session_id}/runs/{run_id}/cancel
POST /api/v1/sessions/{session_id}/runs/{run_id}/steer
POST /api/v1/sessions/{session_id}/runs/{run_id}/responses
POST /api/v1/sessions/{session_id}/fresh
```

Cancel and steer must verify the owner, Session, active Run, and expected Run
revision. A steer request contains one new user message, a new idempotency key,
and the expected old Run revision. One transaction fences and marks the old Run
`superseded`, appends the new user message, creates a child Run, and appends both
control Events. A late old worker cannot commit after the fencing token changes.
Any uncertain side effect remains disclosed on both Runs.

A `responses` request answers only the exact pending clarification input for a
`waiting_user` Run. It carries the expected clarification/message ID, expected
Run revision, new user message, and idempotency key. HASHI atomically appends
the response and creates a newly fenced execution attempt for the same Run. It
must not accept an unrelated response or revive a terminal Run.

`fresh` creates a new Session context generation. It stops automatic injection
of earlier Session history, capsules, and Session Memory+ without deleting raw
messages, logs, Agent long-term memory, or audit evidence. `fresh` is accepted
only when the Session has no active foreground Run; otherwise it returns
`409 session_run_active`. Its transaction increments the context generation,
revokes every older-generation backend binding, invalidates older compaction
candidates, and appends the boundary Event before acknowledging success. The
next Run must establish a new CLI/backend thread or a fresh isolated invocation.

`/new` is the client command for creating and selecting a different HASHI
Session. `/fresh` never creates a new Session, and backend-thread reset is an
internal consequence rather than a separate user-facing session concept.

### 8.6 Attachments

```text
POST   /api/v1/attachments/stage
GET    /api/v1/attachments/{attachment_id}
DELETE /api/v1/attachments/{attachment_id}
```

Staging returns an opaque attachment ID, immutable content digest, expiry, and
owner-scoped grant. A Run request references staged attachments; accepting the
Run atomically commits those grants to its user Message. Uncommitted staging
expires or may be explicitly deleted. A committed attachment follows Session
retention and cannot be revoked by deleting a stale staging grant.

### 8.7 Approval and waiting-user interaction

```text
GET  /api/v1/sessions/{session_id}/runs/{run_id}/approvals
POST /api/v1/sessions/{session_id}/runs/{run_id}/approvals/{approval_id}/decision
```

An approval decision includes the expected approval, deferred-invocation, and
Run revisions plus an idempotency key. HASHI verifies owner, challenge status,
expiry, requested scope, and that neither the originating Run nor deferred
invocation has been cancelled, superseded, executed, or invalidated. The client
does not supply or revive an executor fencing token. Approval changes are
durable Events. Product services still perform their final domain authorization
before mutation.

An approval challenge may be emitted only after the Tool Gateway has persisted
a fully materialized deferred invocation containing the canonical tool identity
and arguments, immutable input digests, stable invocation key, required grants,
preconditions, originating fencing token, and the exact approval scope. No
side-effecting call is admitted before the required approvals resolve. The same
transaction moves the Run to `waiting_user`, appends the challenge Event, and
ends the originating attempt's lease; its fencing token can no longer authorize
executor writes.

An approval decision does not resume an arbitrary model, HER, CLI, or process
stack. When all required decisions resolve, HASHI rechecks current authority and
preconditions, creates a newly fenced attempt, and asks the Tool
Gateway to execute exactly the persisted deferred invocation under its stable
invocation key. The resulting typed tool outcome is committed once and the next
model/HER continuation is reconstructed from canonical Run Messages and Events.
If the invocation cannot be completely materialized, its preconditions no
longer hold, or the tool cannot safely honor the invocation key, the Run becomes
`interrupted`; recovery requires a new child Run and no uncertain operation is
automatically repeated. This mechanism never claims to restore an interrupted
backend execution stack and must not repeat a pre-approval side effect.

Clarification answers use the `responses` endpoint in section 8.5. Approval and
clarification messages are typed distinctly even though both may place a Run
in `waiting_user`.

### 8.8 Channel bindings and Agent transfer

```text
GET    /api/v1/sessions/{session_id}/channel-bindings
POST   /api/v1/sessions/{session_id}/channel-bindings
DELETE /api/v1/sessions/{session_id}/channel-bindings/{binding_id}
POST   /api/v1/sessions/{session_id}/agent-transfers
```

Channel-binding mutations require an expected Session revision, proof of
control for the external channel, and an idempotency key. One external channel
conversation binds to at most one active Session in the same owner scope.

Agent transfer is accepted only with no active foreground Run. It verifies that
the target Agent is authorized for every item selected for continuity, then
atomically advances the context generation, updates participants and
presentation Agent, revokes all older-generation backend bindings, Memory+ and
compaction candidates, advances the Session revision, and appends a transfer
Event. Raw visible message history remains part of the Session, but it is not
automatically injected into the new Agent's context. An optional transfer
capsule may seed the new generation only from material authorized for the target
Agent, with source provenance, sensitivity filtering, and an auditable omission
record. No capsule, Memory+, long-term memory result, attachment grant, or tool
state derived under the old Agent crosses the transfer boundary merely because
the Session identity remains unchanged.

### 8.9 Uniform mutation idempotency

Every externally retryable mutation uses the same durable idempotency contract,
including Run creation, steer, clarification response, approval decision,
attachment commit/delete, channel-binding mutation, Agent transfer, cancel, and
`fresh`. The record key is scoped by authenticated owner, Session when present,
operation kind, and idempotency key. It stores a normalized digest covering all
semantic inputs, target resource IDs, expected revisions, attachment/input
digests, and authorization-relevant options.

The same key and digest returns the original typed status and resource/control
projection without repeating an Event, execution, decision, or side effect. The
same key with a different digest returns `409 idempotency_conflict`. Records
remain at least as long as the affected resource and its tombstone, and a key is
never rebound within that scope. Validation or authorization failures that make
no durable mutation may be retried under policy; once any mutation or Event is
committed, its idempotency result is durable. Internal deferred-tool invocation
keys use the stricter Tool Gateway lifecycle in sections 6.4 and 8.7.

### 8.10 Agent memory promotion

```text
GET  /api/v1/agents/{agent_id}/promotion
POST /api/v1/agents/{agent_id}/promotion
```

The command surface is:

```text
/promote status
/promote now
/promote all now
/promote auto on|off
/promote time HH:MM [timezone]
```

Promotion operates on one immutable snapshot per Session and advances a
`promoted_through_ordinal` watermark only after every derived Agent-memory row
and its provenance commit atomically. Repeating a job is idempotent. Automatic
promotion runs once per configured local date and catches up after downtime.
Only terminal Runs and committed visible Messages enter the snapshot; active
Runs remain for the next promotion. Manual promotion does not disable or move
the automatic schedule unless the corresponding schedule command is used.

## 9. PCM and memory scopes

The existing typed PCM authority model remains intact. Presentation order does
not flatten authority. The change is that conversation-derived context must be
resolved through one Session.

### 9.1 Session working context

Every external Run assembles:

- the current user request, always separate and protected;
- up to the newest ten completed exchanges in the same Session and active
  context generation;
- the bounded unresolved-Run recovery context defined below;
- the active Session continuity capsule when present;
- Session Memory+ or equivalent bounded work card;
- authorized Agent long-term memories;
- Agent Persona and system prompts;
- current time, Workzone, skills, tools, permissions, and runtime context.

Capacity pruning removes the oldest complete Session exchanges first. It must
never select history from another Session merely because both use the same
Agent.

### 9.2 Unresolved-Run recovery context

Completed exchanges remain the ordinary dialogue-history unit, but failure,
interruption, stop, supersession, or `waiting_user` must not erase the request
that still needs recovery. Before Triage, the Session service supplies a typed
recovery section for relevant non-successful Runs in the same Session and
active context generation.

The default bound is the newest three unresolved Runs and at most 12,000 Unicode
characters after deterministic rendering. Each entry contains:

- the authoritative original user `message_id` and bounded text;
- Run state, parent/supersession relationship, and last terminal reason;
- validated goal/plan summary when available;
- completed, uncertain, or prohibited side-effect summary;
- Ledger and evidence references rather than unbounded raw logs; and
- the exact pending clarification/approval identifier when `waiting_user`.

Only the Session service may resolve those references. It enforces owner,
Session, context-generation, sensitivity, and tool-evidence permissions before
rendering them. HER Triage cannot open arbitrary Ledgers or logs by file path.
If the recovery material exceeds budget, HASHI preserves the newest unresolved
request and side-effect status first, uses a provenance-bearing recovery
capsule for older material, and audits every omission.

A later ordinary request such as "continue" creates a new child Run and receives
this recovery section. A direct answer to an active `waiting_user` challenge
uses the response contract in section 8.5 and resumes the same Run with a new
fenced attempt.

### 9.3 Session memory

Ordinary messages and episodic work state first enter Session scope. Until the
Session promotion watermark advances past them, they are unavailable to every
other Session even when those Sessions use the same Agent.

Session Memory+ tracks decisions, unresolved work, goals, evidence pointers,
and continuation state for one Session. The current single Agent-wide work card
must not be injected automatically into unrelated Sessions.

### 9.4 Agent long-term memory

Agent long-term memory is the unified retrievable history of everything already
promoted from that Agent's Sessions, together with existing Agent memories.
Promotion is a time/scope boundary, not a sensitivity-classification or approval
gate. It records:

- source `session_id`, `run_id`, and `message_id`;
- promoting actor or schedule;
- source Session ordinal and context generation;
- creation and promotion timestamps; and
- retraction or supersession links.

Retrieval applies owner, tenant, Agent, promotion-watermark, and authorization
filters before semantic ranking. A semantic match never grants access. Complete
promoted material remains indexed and retrievable, but ordinary prompt assembly
selects only a bounded relevant subset rather than injecting the full history.

### 9.5 Habits, Meditation and Dream

Habits remain Agent-level behavioural guidance. Meditation and Dream keep their
existing learning behaviour and add no content-sensitivity approval gate. Their
Agent-level inputs and writes may use promoted Agent memory; work derived from
an unpromoted Session remains staged at Session scope until the same promotion
boundary advances. Every resulting Agent-level record retains Session
provenance.

The daily order is deterministic: freeze terminal Session snapshots, commit
idempotent promotion and watermarks, update local/central Agent-memory indexes,
then allow Meditation, Dream, and other Agent-level consolidation to consume the
newly promoted records.

## 10. Session-scoped Auto Compact

The existing Auto Compact safety contract remains: raw evidence is immutable,
the current request and open execution are protected, the model call is
tool-free, and activation uses compare-and-swap.

The storage key changes from one active pointer per Agent workspace to one
active pointer per Session context generation. A compaction candidate records:

- `session_id` and context generation;
- source message and Event ranges;
- covered-through Session ordinal;
- source digest and evidence references;
- previous capsule hash; and
- policy and model metadata.

A candidate from Session A can never win the pointer for Session B. A `fresh`
generation change invalidates all older in-flight candidates without deleting
their immutable evidence.

Compaction capacity checks occur before the first HER Triage or backend model
call for the new Run.

## 11. HER v2 integration

HER v2 remains a per-Run orchestration engine and does not own Session
lifecycle. HASHI freezes a `SessionContextSnapshot` before calling HER v2. The
snapshot includes the assembled Session context, context generation, source
hash, bounded unresolved-Run recovery context, permissions, Agent
configuration, and attachment manifest.

HER's Run and Ledger gain parent references for:

- `session_id`;
- `run_id`;
- authoritative user `message_id`;
- context generation and context hash; and
- optional parent/superseded Run.

Those references support audit and later conversational recovery. They do not
grant HER authority to mutate Session history or memory directly.

HER may inspect a previous Ledger only through the Session service's authorized,
bounded recovery projection. Raw Ledger and log paths are not model-visible
capabilities, and recovery evidence remains subject to the frozen Run budget
and the current attempt's effective authorization snapshot.

HER's existing restart rule remains authoritative:

- an interrupted in-flight HER Run becomes `interrupted` or `error` during
  reconciliation;
- incomplete execution stacks are not replayed automatically;
- its Ledger and HASHI evidence remain linked to the Session; and
- a later user request such as "continue" creates a new Run whose Triage may
  inspect the Session history and prior Ledger references.

## 12. Backend neutrality and isolation

HASHI is responsible for adapting Session semantics to every backend.

### 12.1 Stateless and HER/API backends

HASHI supplies the frozen current request and Session context required for each
Run. Provider caches may improve performance but are never authoritative.

### 12.2 Persistent CLI backends

HASHI may resume a backend thread only through the Session-keyed backend
binding, including the exact `context_generation`. Switching Sessions or
generations must resume the corresponding eligible thread, establish a fresh
isolated invocation, or rebuild context from HASHI. Reusing one adapter's
thread across unrelated Sessions or across a `fresh` boundary is forbidden.

Fixed-backend handoff remains supported but is renamed backend handoff to avoid
confusing a provider thread with a HASHI Session. Its payload contains only the
current HASHI Session context plus already promoted Agent memory. It never reads
another Session beyond that Session's promotion watermark.

### 12.3 Backend changes

Changing backend, provider, model, or HER profile does not change the Session
API, IDs, history, or client behaviour. Effective configuration is recorded on
the Run for audit and reproducibility.

## 13. Restart and recovery

On HASHI startup, the Session service reconciles non-terminal Runs:

- accepted but never claimed Runs may be claimed normally;
- expired claims may be reclaimed only with a new fencing token and only when
  the previous attempt is provably unstarted or read-only and policy permits
  replay;
- started or uncertain Runs become `interrupted` and are never automatically
  replayed;
- a durable terminal Event is appended;
- Session messages, capsules, backend bindings, and earlier Events remain; and
- delivery resumes from the durable outbox.

Startup also evaluates every enabled Agent promotion schedule. If HASHI missed
the configured local promotion time, it runs one idempotent catch-up job for the
unprocessed local date before scheduling the next occurrence.

Aptenra reconnects, lists Sessions, reads messages, and resumes the Event cursor.
It does not restore a local execution owner map.

Operational Events may be compacted only after the configured minimum
retention and after all non-expired durable consumers have acknowledged the
range. The Session snapshot and canonical Messages remain the rebuild source.
Expired consumers do not retain Events forever; their next stale cursor receives
`410 event_cursor_expired` and must bootstrap from a snapshot.

Removing a Session closes streams and creates a deletion tombstone. Messages,
attachments, Events, logs, promotion provenance, and promoted Agent memory are
retained. Delivery workers must not send an outbox row after its Session
deletion fence. The default Session cannot be removed.

## 14. Security, identity and privacy

Every Session is owned by an authenticated principal and deployment scope.
Possession of a `session_id` is not authorization. Every read, mutation,
control, attachment access, memory retrieval, and Event subscription rechecks
ownership and Agent capability.

IDs are opaque and globally unique. They must not encode customer, device,
account, backend, or Agent names.

The personal/local profile may use a loopback capability token. Team and
enterprise profiles use the existing identity and policy services. All profiles
enforce the same repository contract.

Chat content, memory and attachment data receive encryption-at-rest support and
backup policy. Ordinary Session lifecycle operations are non-destructive.
Canonical logs and audit evidence retain the indefinite PCM retention contract.

## 15. Aptenra thin-client contract

Aptenra performs only the following agent-chat functions:

1. Discover HASHI capabilities and Agents.
2. Create, list, open, rename, archive, and tombstone HASHI Sessions.
3. Submit the current message and optional high-level execution mode.
4. Stage attachments and commit them through HASHI.
5. Render durable Events and canonical Messages, deduplicating by HASHI IDs.
6. Present HASHI approval challenges and return the user's decision.
7. Display clear unavailable, incompatible, and interrupted states.

Aptenra may persist unsent drafts, UI layout, an opaque Event consumer ID, and
an optional last-seen sequence. HASHI remains authoritative for the acknowledged
consumer cursor. Aptenra must not persist sent Chat text or Agent memory as an
independent source of truth. Version 1 should use an in-memory content
projection; any future disk content cache must be disposable, encrypted,
bounded, and provably reconstructable from HASHI. Losing every local consumer
hint must still allow recovery through a new consumer and Session snapshot.

Aptenra must remove specific provider/model routing for Primary, Review, Action,
Escalation, and Translator Agents. It may display the Agent and high-level
execution modes advertised by HASHI. Quick/Pro targets, provider reasoning,
review policy, tool permissions, and backend choice remain HASHI configuration.

Distinct Action, Escalation, or Translator Agents may remain when they provide
separate permission or persona boundaries. HASHI configures and invokes them;
Aptenra does not configure their models or coordinate their internal routing.

## 16. Channel bindings

The Session service is channel-neutral. Aptenra, Workbench, Telegram, WhatsApp,
browser, and future surfaces bind their external conversation identity to a
HASHI Session through `channel_bindings`.

Every Agent has one permanent default Session, and every authorized surface
initially binds to it. `/new` creates another Session and changes only the
invoking surface's binding; `/use <session_id>` may deliberately bind multiple
surfaces to the same Session. Scheduled tasks and proactive messages always
target the Agent's default Session. Channel projections must not maintain a
second full Chat archive. Delivery receipts and transport metadata remain Events
linked to the canonical Session message.

The common interactive command contract is `/new`, `/sessions`, `/use`,
`/current`, `/archive`, `/fresh`, and `/promote`. The terminal CLI exposes the
same lifecycle through `hashi session new|list|use|show|rename|archive` and
`hashi chat --session <session_id>`. Non-interactive requests must name a
Session or explicitly request a new one; they never fall back to an Agent-global
conversation timeline.

## 17. Compatibility and cutover

### 17.1 Versioned introduction

Session API v1 is added alongside legacy `/api/chat`, Agent transcript, and
request-activity routes. Existing HASHI channels continue through compatibility
adapters while they migrate.

All Session routes and workers remain behind one default-off
`persistent_session_v1` feature flag throughout Phases 1–4. Before the complete
readiness gate, the routes return `404` or `503 session_api_not_ready`, no
external Session can be created, and `/api/v1/capabilities` does not advertise
Session support. A database schema existing on disk is not readiness.

The readiness gate becomes true only when one build proves that Session
persistence, owner isolation, Session-scoped PCM/Memory+/Auto Compact, execution
claims and fencing, HER/backend propagation, durable Event replay/ACK/snapshot,
controls, attachments, approvals, and restart reconciliation are all installed
and their contract suite passes. Capability publication and route enablement
commit together. Aptenra fails closed when the pinned HASHI release lacks the
required Session, Event, control, attachment, approval, fencing, or memory-scope
versions.

### 17.2 Fresh Aptenra start

The first approved Aptenra release creates new empty HASHI Sessions. It does not
import existing Aptenra Chat history. Old local Chat state may remain as a
rollback backup during qualification but is not read into the new Session
system and is not deleted without a separately approved cleanup step.

Existing Agent-global memories remain Agent memory and are immediately eligible
for retrieval. Existing Agent-global transcripts and logs remain retained
evidence but are not inserted into a new Session because their Session ownership
cannot be reconstructed reliably.

### 17.3 Source convergence

After Session API qualification:

- Aptenra vendors or packages the exact approved HASHI release;
- Runtime Lock and source hashes cover the complete HASHI closure;
- downstream Aptenra-specific HASHI API modifications are removed;
- configuration overlays remain supported; and
- HASHI source changes flow through normal upstream intake rather than the
  Aptenra repository.

## 18. Implementation phases

### Phase 0 — Decision and contract freeze

- Adopt this document as the authoritative target.
- Define JSON schemas, endpoint bodies, state transitions, error codes,
  capability versions, ownership rules, retention, fencing, per-attempt
  authorization refresh, uniform mutation idempotency, and deletion semantics.
- Mark Aptenra D-056 as superseded for the future Session integration.
- Add failing contract tests before implementation.

### Phase 1 — Session persistence and APIs

- Implement the transactional Session repository and schema.
- Implement lifecycle, Message, Run, durable Run projection, Event, attachment,
  approval challenge, uniform idempotency, ownership, and outbox APIs.
- Add restart reconciliation and pagination.
- Keep every Session route externally disabled and omit the capability.

### Phase 2 — Session-scoped PCM and memory

- Replace Agent-global recent-exchange lookup with Session lookup.
- Add Session context generations and Session Memory+.
- Add per-Session promotion watermarks, per-Agent local-time schedules, `/promote`
  commands, catch-up jobs, provenance, and unified Agent-memory indexing.
- Move Auto Compact pointers and capsules to Session scope.

### Phase 3 — Runtime, HER v2 and backend integration

- Carry `session_id`, `run_id`, and `message_id` through queued requests,
  Runtime, tools, audit, HER Ledger, and delivery.
- Freeze and hash `SessionContextSnapshot` before Triage.
- Implement context-generation-keyed CLI backend bindings.
- Implement atomic execution claims, leases, fencing, and Tool Gateway
  invocation idempotency.
- Implement per-attempt authorization refresh and durable deferred-tool
  invocation admission without backend-stack resumption.

### Phase 4 — Durable presentation and controls

- Project complete typed Runtime events into the durable Event store.
- Implement at-least-once replay/streaming, durable consumer ACK, complete
  terminal-Run snapshot/gap recovery, idempotent final projection, cancel,
  steer, clarification response, approval, transfer, and fresh contracts.
- Retain the in-memory activity store only as an optional cache.
- Keep the capability disabled until the Phase 4 integration gate passes.

### Phase 5 — Aptenra thin client

- Replace local Chat history with Session list/message/event APIs.
- Remove history selection, correlation aliases, transcript offsets, local
  active-turn ownership, and model-routing configuration.
- Retain only drafts, UI state, temporary uploads, and non-Chat product data.

### Phase 6 — Qualification and retirement

- Run the backend-neutral qualification matrix.
- Atomically publish the capability and activate the path from an empty Session
  store only after the complete readiness gate passes.
- Remove Aptenra-specific HASHI runtime patches and legacy client assertions.
- Preserve rollback evidence until the release exit gate is complete.

## 19. Mandatory acceptance assertions

### 19.1 HASHI Session contract

1. Session create/list/read/update/archive/tombstone survives a HASHI restart and
   enforces owner isolation.
2. Two interleaved Sessions using the same Agent never exchange raw messages,
   Session Memory+, capsules, fresh generations, tool state, or backend threads.
3. `fresh` increments the context generation and prevents every old CLI thread,
   backend binding, Memory+, capsule, and in-flight compaction candidate from
   entering the new generation.
4. The same idempotency key and request digest returns the same Message and Run;
   the same key with a different digest returns `idempotency_conflict`. Response
   loss, restart, terminal failure, and Session tombstoning do not create a
   second execution.
5. Two workers racing to claim or reclaim one Run produce one current fencing
   token. Every late state write, final, Event, backend write, and tool call from
   the stale worker is rejected. Uncertain side effects are not replayed.
6. Durable Events use at-least-once delivery and replay in order after disconnect
   and restart. Duplicate delivery produces one UI projection by ID. ACK state
   is server-authoritative.
7. An expired Event cursor returns the typed gap response; snapshot bootstrap
   reconstructs the same visible Messages, terminal and active Run projections,
   uncertain-side-effect disclosures, and pending interaction state before
   replay continues.
8. Cancel, steer, clarification response, approval, and fresh affect only the
   exact authorized Session and Run. Stale revisions, challenge IDs, fencing
   tokens, or cross-Session controls fail closed.
9. Steer atomically supersedes and fences the old Run while creating one child
   Message and Run with a new idempotency key.
10. The newest ten completed exchanges are selected only from the active
   Session; the current request is present exactly once and remains protected.
11. Failed, interrupted, stopped, superseded, and waiting-user requests remain
   available through the bounded, authorized unresolved-Run recovery context;
   they do not silently disappear because no successful final exists.
12. Auto Compact capsules and compare-and-swap pointers cannot cross Session or
   context-generation boundaries.
13. Ordinary episodic work remains Session-scoped until manual or scheduled
   promotion advances that Session's watermark. Promotion is idempotent, retains
   complete provenance, and makes the material retrievable by every Session for
   the same owner and Agent.
14. An interrupted HER Run becomes terminal without automatic side-effect
   replay, while the Session remains usable for a later continuation Run.
15. Every newly claimed or resumed attempt re-evaluates mutable authorization;
    revoking principal, Agent, tool, attachment, sensitivity, or product access
    prevents later admission even though the request-time audit snapshot remains
    immutable.
16. Approval persists one fully materialized deferred invocation before waiting.
    A replacement worker executes only that invocation by stable key and rebuilds
    model continuation from canonical records; arbitrary backend stacks and
    uncertain pre-approval effects are never replayed.
17. Attachment staging/commit/revocation, approval, channel binding, and Agent
    transfer enforce their declared ownership, revision, idempotency, and grant
    lifecycles. Transfer advances the context generation and no old-Agent
    derived context crosses without target-Agent authorization.
18. Every externally retryable mutation obeys the uniform key/digest conflict
    and retention contract; duplicate delivery cannot repeat its Event, control
    transition, execution, or side effect.
19. Session capability remains unavailable through every incomplete Phase 1–4
    state and is published only when the full readiness gate is true.
20. Every Agent has exactly one permanent default Session. New channels bind to
    it, `/new` changes only the invoking channel, and scheduled or proactive work
    always targets it.
21. Automatic promotion runs once per configured local date, catches up after
    downtime, skips active Runs, and never exposes another Session beyond its
    committed promotion watermark.
22. Fixed-backend handoff includes the current Session and promoted Agent memory
    only; another Session's unpromoted content never enters the backend thread.
23. Tombstoning a non-default Session hides it and fences delivery without
    deleting Messages, logs, attachments, promotion provenance, or Agent memory.

### 19.2 Backend transparency

The same Session contract must pass with:

- HER v2;
- at least one persistent CLI backend; and
- at least one stateless API backend.

The client request and Event/Message semantics remain unchanged. Backend
selection may change output, latency, or capability, but never identity,
history isolation, ownership, or replay behaviour.

### 19.3 Aptenra thin-client contract

1. A cold Aptenra launch with empty local Chat storage reconstructs all visible
   Sessions and messages from HASHI.
2. Aptenra sends the current message, HASHI `session_id`, idempotency key, and
   permitted options only; it sends no conversation history or PCM.
3. Interleaved Events route solely by HASHI `session_id` and `run_id`.
4. Aptenra restart requires no durable local request-owner map.
5. Local persisted data contains no sent message text, assistant final text,
   Session Memory+, or Agent memory; opaque consumer/cursor hints are permitted
   but never authoritative.
6. Missing or incompatible HASHI Session capabilities fail closed with a clear
   product error and never fabricate history, success, or a final answer.

## 20. Legacy assertions and components to retire

After the new gates pass, Aptenra retires:

- the client-bounded history decision and `8/16` or `10/20` profiles;
- local durable Chat message archives and their 64-Chat/100-message limits;
- client correlation aliases and active-turn owner records;
- Agent transcript offset and final-result inference;
- Aptenra-specific request-result memory projections;
- ordinary-Chat `isolated`, ephemeral-backend, and `skip_memory_*` behaviour;
- direct Primary/Review/Action/Escalation/Translator model routing; and
- duplicate Telegram, WhatsApp, and proactive Chat-history projections.

HASHI retires or confines to compatibility use:

- Agent-global recent conversation as the default user-Chat source;
- automatic Agent-global episodic-memory writes before promotion;
- one Agent-workspace Memory+ card, `/fresh` watermark, or compaction pointer;
- process-local request IDs as external Run identities;
- the in-memory activity projection as a result source;
- one shared fixed-backend thread across unrelated Sessions; and
- client-specific branches in the generic Workbench API.

## 21. Release exit criteria

Implementation is complete only when:

- all mandatory assertions pass against a source-locked HASHI build;
- HASHI is the only durable source for sent agent-chat content;
- Session isolation holds across HER v2, CLI, and API backends;
- restart replay, complete Event gap/snapshot recovery, uniform idempotency
  conflicts, execution fencing, approval-boundary reconstruction, per-attempt
  authorization revocation, Agent-transfer isolation, unresolved-Run recovery,
  controls, memory scopes, and compaction are verified with fault injection;
- no incomplete implementation state advertises or accepts Session API v1;
- Aptenra contains no behavioural HASHI fork and no sent-message archive;
- the release begins with one empty default Session per Agent while existing
  Agent memory remains available; and
- rollback, backup, non-destructive retention, promotion, and audit boundaries are documented
  and tested.

Until those conditions hold, the current Aptenra bounded-history path remains a
legacy compatibility implementation and must not be described as the final
Session architecture.

## 22. Independent architecture review resolution

Zelda completed two independent architecture reviews on 26 August 2026. Version
1.1 resolved the first eight substantive findings:

1. `context_generation` is part of backend-binding identity, and `fresh`
   atomically revokes older threads and compaction candidates.
2. Network delivery is correctly specified as at-least-once; server-owned
   consumer ACK/cursors plus ID-based upsert provide exactly-once projection.
3. Failed, interrupted, stopped, superseded, and waiting-user requests enter a
   bounded, authorized unresolved-Run recovery context.
4. Atomic claims, leases, attempts, fencing tokens, and stale-worker rejection
   protect one-Run execution and external side effects.
5. Attachment, approval, waiting-user response, channel-binding, Agent-transfer,
   and atomic steer contracts are now explicit.
6. Event retention exposes earliest/latest sequences, typed expired-cursor
   recovery, snapshots, consumer expiry, and deletion/outbox lifecycle.
7. Idempotency persists a normalized request digest, defines conflicts and
   retention, and keeps terminal Runs bound to their original key.
8. One default-off feature flag withholds routes and capability publication
   until Session isolation, Runtime propagation, recovery, and Event contracts
   pass together.

Version 1.2 resolves the six additional findings from the second review:

9. Approval waiting persists a fully materialized deferred Tool Gateway
   invocation. Replacement attempts execute only that stable invocation and
   reconstruct later model work from canonical records; no arbitrary backend
   execution stack is resumed.
10. The immutable request-time authorization snapshot is audit evidence, while
    every attempt persists a newly evaluated effective snapshot and fails closed
    on revocation.
11. Session snapshots retain every still-visible terminal and active Run
    projection, pending interaction, typed failure, and uncertain-side-effect
    disclosure independently of operational Event compaction.
12. Agent transfer advances the context generation and permits continuity only
    through a provenance-bearing capsule filtered for the target Agent.
13. Clarification responses and approval decisions have exactly one mutation
    path each.
14. All externally retryable mutations share one durable key/digest/conflict and
    retention contract.

Version 1.3 records the Founder resolution after the reviews: short-term Session
context is completely isolated, every Agent has a default Session, and manual or
scheduled promotion later unifies Session experience into Agent memory without
a content-sensitivity gate. Fixed-backend handoff remains current-Session scoped,
and ordinary Session removal never destroys logs or memory. This version is the
final implementation contract; qualification gates still determine whether the
capability may be advertised.
