# HASHI Provider-Agnostic Orchestration (PAO) System Design

| Field | Value |
|---|---|
| Status | **Authoritative PAO module specification** |
| Effective date | 2026-09-29 |
| Parent architecture | [HASHI System Architecture](../ARCHITECTURE.md) |
| Scope | HASHI outer control plane, Conversation Sessions, Engine binding, capabilities, workflows, Jobs, and cross-agent coordination |

## 1. Definition

Codex MCP isolation inventories standalone servers with plugins disabled, matching
both CLI execution and the app-server bridge. Plugin-provided transports must
not become enabled-only top-level MCP overrides after their plugin is disabled.

On qualified Codex CLI 0.160+, an owned command-scoped native shell hook routes
process termination to HASHI's managed process tools. It ignores user config,
installs no persistent profile and preserves ordinary coding/inspection commands.
Managed termination protects the current Function and all runtime ancestors.
The hook is a guardrail; specialized native programs and vendor hook failure
behavior do not constitute an OS enforcement boundary. HASHI3 qualification
includes an actual denied native termination with a surviving canary process.

Terminal errors carry one bounded versioned public failure result through Session
persistence and Frontend Connector projection. It carries the canonical Engine,
observable effect counts and the owner's recovery decision. Completed tool calls
and verified effects remain separate. Unknown effects never suggest replay;
unknown future public error codes remain visible. See the [2026-10-06 repair](repairs/NIGHTLY_20261006_HASHI3.md)
for source, offline checks and runtime adoption recorded separately.

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
HERV3. PAO may store or forward a Model Provider preference through a typed
Engine contract, but it must not absorb provider-specific request, thread, or
reasoning semantics.

## 2. Responsibilities

PAO owns the following product domains.

### 2.1 Agent and runtime control

- Agent identity, directory, lifecycle, and active runtime binding;
- Engine Provider discovery, selection, availability, and migration aliases;
- one revisioned Workzone profile per authenticated owner and Agent, independent
  of Conversation Session selection;
- startup, stop, restart, hot-reload coordination, and runtime health; and
- Fixed/Flex working-mode policy, retired outer-composition migration, and any
  future runtime composition that spans Engines.

Agent deletion is a PAO-owned lifecycle operation in shared Functions. A
Function generation that implements the preview, confirmation, cleanup, and
receipt contract advertises `agent_deletion` as supported by default; there is
no separate per-instance opt-in. Frontends derive availability from the
authenticated capability response and hide the action when the capability is
absent. Active or running Agents and other reported blockers remain protected.

Agent creation is also a PAO-owned lifecycle operation. Its explicit Instance
Configuration template and effective selection/publication contract are defined
in [Agent creation policy](HASHI_AGENT_CREATION_POLICY.md). Creating an Agent with
`is_active=false` publishes only its validated configuration and workspace.
Creating one with `is_active=true` succeeds only after the isolated Function
Worker start contract accepts it. If startup fails, the created Agent remains
available for recovery but is written back inactive through the revisioned
configuration owner; the caller receives a lifecycle failure rather than a
false ready result. Configured, active, and running remain distinct states.

A successful user-directed Agent stop records a PAO lifecycle marker in the
shared Functions handoff. The Backend API projects that configured-but-stopped
Agent as `status=stopped`, distinct from an unexpected offline Worker and from
`is_active=false`. Starting the Agent clears the marker. Shared Functions
replacement and recovery carry the marker with the running-Agent topology;
normal instance startup still follows configured active Agents. External
frontends may use the status to hide stopped Agents without changing durable
configuration or treating a failed stop as successful.

The stable process kernel belongs to the Core engineering layer. The Agent and
runtime policies operated through that kernel belong functionally to PAO.
The current working-mode contract is defined in
[Fixed and Flex Working Modes](FIXED_FLEX_WORKING_MODES.md).

### 2.2 HASHI Conversation Sessions

[Asynchronous Run questions](HASHI_RUN_QUESTIONS.md) defines PAO-owned,
authenticated clarification/preference questions that remain in one active Run.
Answers provide data and never confer tool or side-effect authorization.

PAO is the sole owner of:

- HASHI Conversation Session identity and authenticated ownership;
- Agent and frontend/channel binding;
- Messages, Runs, attempts, Events, consumer acknowledgements, and fencing;
- context generation, archive, fresh, fork, promotion, and recovery controls;
- the stable Conversation-to-Engine Session binding.

An Engine may own its internal Engine Session but must not become a second
owner of the enclosing Conversation Session.

Cancelling one reply is a PAO Run control operation in shared Functions. The
caller supplies its exact owner, Agent, Session, Run, and request identity; the
Agent Worker removes only that ready item or interrupts only its matching live
provider task. A queued Run cancelled between admission and queue insertion is
durably fenced before the later item can start. An accepted interrupt is pending
until the Worker settles the Run as stopped; a Run that finishes first retains
its actual terminal state. Frontend Connectors must show that state and never
translate a failed or slow Run cancellation into an Agent lifecycle stop or a
session-wide `/stop`. Prior side effects remain separate from the stop result.

Workzone configuration is Agent control state, not Conversation Session state.
`/new`, `/use`, `/fresh`, and `/archive` therefore cannot replace it. PAO freezes
the current Agent Workzone revision into each admitted Run so an in-flight Run
cannot observe a later menu change. Legacy Session Workzone rows remain inert
compatibility records and are not silently migrated or merged.

Configuration mutations may commit while Runs are active or queued. Every Run
already admitted keeps its frozen snapshot, while the next admission after the
commit reads the new revision. PAO defers runtime, Tool-root, and Engine-session
activation to a Run boundary. Explicit Workzone `reload`/`reset` remains
idle-only because it immediately resets the selected Engine Session.

A live phone call is another transport bound to the same Conversation Session.
Each role-labelled transcript fragment is durable PAO Session evidence as it
arrives and participates in later text and phone history. A terminal call card
is a Connector view derived from those fragments; it is not a second message
authority or a replacement for the role-preserving transcript.

Provider transcript deltas may be only a few hundred milliseconds long. PAO
retains each original fragment and derives readable utterances per speaker for
later Session history and the terminal call record. Simultaneous speech does
not break either speaker's continuous utterance into one line per delta; a
completed reply still separates successive turns. The projection preserves
the provider's exact words and their source IDs. The Phone Connector applies
the same rule to live captions. A brief interleaved acknowledgment does not
complete a reply merely because it falls between two parts of the other
speaker's sentence. This provider supplies deltas and timestamps, not stable
utterance boundaries: a short reply is complete when it has sentence-ending
punctuation; a longer reply can also complete a turn without punctuation.
The 1.2-second same-speaker gap remains a presentation boundary. Ambiguous
recognition is displayed as received, without claiming a verified turn break.
An existing call record may be reprojected
from its durable fragments under the same Message ID and ordinal. PAO raises
the Session history generation so clients refresh the corrected presentation
in place, without moving an old call to the end of the conversation. No audio
transcription is silently corrected. The live model hears provider
audio directly, while PAO action delegation uses the recognized user text, so
recognition errors remain a separate action-understanding risk.

#### Live Phone foreground arbitration contract (2026-09-30)

While one logical Live Phone call is engaged, PAO binds that authenticated
owner's instance-wide foreground conversation to the call's Agent and
Conversation Session. There is at most one such foreground binding per owner
and instance. The logical call is not the same object as an OpenAI session,
WebRTC connection, browser component, or Function generation; those are
replaceable transport epochs beneath it.

Every other Run or event is background relative to that call. This includes a
spoken request delegated to the Agent, concurrent text/API input, Scheduler
cron and heartbeat work, HChat, Remote coordination, and Agent-initiated work.
Background work keeps its normal Session, authority, persistence, and approval
boundaries. It may append attributable facts or reportable results to the
foreground model, but it cannot mutate, cancel, close, or compete with the
foreground conversation. A background result that would normally create a
separate user-facing reply is instead routed to the active foreground call;
if delivery is temporarily unavailable, PAO retains it for replay or later
ordinary delivery rather than ending the call.

Only an authenticated explicit hang-up action bound to the current call may
normally terminate it as `ended`. Provider expiry or closure, WebRTC/data-
channel loss, lease loss, navigation, page reload, Function replacement,
process failure, persistence failure, or a background task is a transport or
runtime fault. PAO and FC first recover or replace that transport epoch; an
unrecoverable case terminates as `failed` or `interrupted`, never as a user
hang-up. Provider duration limits therefore require pre-expiry rollover, not a
normal logical-call ending.

Each logical call has an append-only diagnostic timeline independent of the
ordinary Session-event sequence. It correlates PAO Runs, FC/browser states,
provider lifecycle and close reason, transport epochs, persistence failures,
recovery attempts, and the actor/reason for every termination request. It does
not duplicate transcript text, audio, instructions, credentials, or secrets;
the role-preserving Session transcript remains canonical.

Implementation checkpoint (2026-09-30): PAO now assigns one foreground call per
owner, routes attributable background messages and status through a durable
inbox, and retains legacy calls as recoverable unless the stored record proves
an explicit user hang-up. Authenticated context lookup returns the current
recoverable binding so the Connector can restore a call after older clients
cleared their local hint. Function replacement and Provider close preserve the
logical call; only an explicit user hang-up becomes normal completion.

The event reader suppresses historical terminal phase events while the
canonical call phase remains nonterminal, so an old cursor cannot close a
recovering call before receiving its recovery state. PAO merges pending
background messages and status events by source timestamp and stops the batch
when delivery or acknowledgement is uncertain.

The sideband durably stages each normalized transcript or typed delegation before
placing it in the process-only per-call projection queue. One SessionStore inbox
assigns a persistent order across both event types. Canonical transcript
projection and inbox removal share one transaction; delegation admission also
removes its staged row atomically. Startup replays pending inbox rows in that
order, including rows left between receipt and projection by Function
replacement. If staging fails, the reader applies backpressure and records
redacted retry evidence; it does not acknowledge the event into volatile memory.
The independent diagnostic audit stores lifecycle and error metadata only,
never transcript text. User hang-up and Provider cleanup bound WebSocket send
and close operations separately, so a stalled socket cannot block the cleanup
path or the normal user-requested terminal record.

The event feed reads canonical call phase and Session events from one SQLite
snapshot, so a concurrent user-ended event cannot be filtered using an older
phase. PAO selects one globally oldest page across pending background messages
and status events by their source timestamps; separate per-type page limits may
not overtake an older item still waiting in the other inbox.

### 2.3 Outer orchestration

The Phone action boundary interprets complete speech in bounded conversational
context using an explicitly configured semantic capability. Code validates its
typed result and target references; no keyword classifier or guessed confidence
grants action authority. Ordinary Agent ingress still owns permission and
execution. Reuse, revision and cancellation address a specific persisted action;
independent actions are not blocked by a one-task-per-call policy. A stalled
interpretation produces a visible unresolved result, never an invented success.
Action completion requires attributable effect evidence rather than an Engine's
terminal reply. Unknown commit outcomes stay unknown until reconciled.

The canonical PAO final Message remains available as the complete informational
answer to a query even when an independent effect inspection fails. Its text
does not certify a write or every source. Effect inspection deduplicates tool
receipts and bounds its verifier input; an over-budget inspection remains
unknown rather than discarding the final answer. A repeated request for all
findings reuses that Message and the existing action instead of starting a
duplicate query. Terminal stopped/interrupted queries never become verified
from partial reads. A stopped write can still have a verified independent
save/readback effect; the stop state and the effect outcome remain separate.

Phone reports cancellation in two stages: an accepted interrupt request and
the later terminal Run state. A queued task removed before start may settle
immediately; a running task is confirmed stopped only when its Run terminates.
An unconfirmed stop request remains uncertain and does not suppress later
progress. Completion before an interrupt is a distinct, factual outcome.
Progress speech is an Agent-controlled call preference: the foreground model
may turn it on or off from the caller's complete request. With progress on,
PAO emits one start notice and only bounded, user-presentable commentary from
real request activity; with it off, interim notices stop while required
approvals and final results remain. Activity is observed by cursor without an
extra model inference. A Phone-specific on choice may present an event already
typed as user commentary even when a separate Connector's display preference
is off; it never promotes private reasoning or tool telemetry to speech.
Backend model route facts may enter foreground context
as internal state, but a configured route or adapter response is not proof of
task success or independent vendor model attestation.

#### Live Phone background-result handoff correction (2026-10-01)

The foreground voice and background execution belong to the same selected
Agent. PAO must retain each delegated Run's terminal state and, when produced,
its complete final Message as one addressable result correlated to the spoken
request. A short status receipt, UI card, or provider append acknowledgement
is not the result. The foreground must have a way to read the original result
without repeating the background Run; long results need explicit content
boundaries and continuation position so an answer requested in full cannot
silently shrink to a few items.

Handoff evidence distinguishes the persisted result, the material offered to
the provider, provider acceptance, the foreground's observed response content,
and local playback. Any assistant fragment proves only that some output was
generated; it does not prove the result was understood, covered in full, or
heard. If the provider offers no consumption acknowledgement, record that
limit honestly and verify content coverage at the product boundary. A later
foreground turn must be able to recover the same result from PAO state.

Phone handoff uses the canonical final Message ID as its durable source. The
call stores only that ID and a page position in its retry plan. The foreground
receives a bounded page labelled with its start, end, total length and next
position; asking to continue reads the next page from the same saved result
without a new Run. The position records material offered, not words heard.
When the caller asks about several completed reports, semantic routing selects
their distinct IDs and PAO stages each original. A summary request does not
authorize answering from a clipped opening excerpt. A new background Run is
reserved for work not already represented by a saved result.

Phone action interpretation may refresh effective PCM during a call. If that
projection is unavailable, PAO does not silently omit it and admit new work.
The current call may still identify and stop or inspect one of its already
known actions by its exact PAO action ID; these control operations cannot
create a Run. Other requests receive a visible uncertainty response until
effective context is available again. This preserves cancellation while
keeping incomplete authority from licensing a new task.

The resulting speech and player observations remain separate evidence. A
typed user-stop backend notification settles its Run as `stopped`; an ordinary
backend error settles as `failed`. Phone cancellation reports request sent
until the terminal Run state confirms the stop, and keeps any verified prior
write separate from the stop outcome.
For a stopped general execution, the receipt names the missing final answer
and any unverified effect without claiming a record was written. The action
kind controls this wording; a confirmed stop does not certify prior effects.
The spoken receipt does not promise another report or suggest a record check
for a general execution when no record effect was observed.

The 2026-10-01 Sunny incident is a failed acceptance case: completed Gmail and
Hong Kong news final Messages existed, while the then-running Phone relay sent
only short uncertainty receipts. Later source changes that read the canonical final
Message still require a real completed-Run handoff and spoken-coverage check;
provider acceptance or a generic speech fragment cannot close this case.
In the 2026-10-01 isolated Sunny call, the opening included all five morning
reports, but semantic routing chose a direct answer from excerpts and the
foreground spoke only two of the early report's three numbered focus items.
This is a separate red acceptance case for result recall, even though mail and
school facts were present and no duplicate Run was started.
After recall was required, all four requested originals were staged, but the
foreground still confused the report introduction's two main themes with its
three explicitly numbered focus items. For saved reports with numbered lines,
PAO now projects their source headings, exact numbered counts and item titles
as a deterministic outline alongside the original. This is a view of the
canonical text, not a second result store or proof of spoken coverage.
On a saved-result recall, PAO appends the original as factual context and
offers the caller's request for speech. A separate generic “correct your
earlier answer” instruction was rejected by live acceptance: it made Sunny
change a correct three-item answer into an incorrect two-item one. The spoken
transcript decides whether every requested item was covered.

PAO also owns the durable opening identifier and its separate request,
acceptance, output and player observations. Opening content comes from PCM,
wire commands from the selected Connector adapter. The same logical call
retains this record across transport replacement, and never repeats an opening
merely because a provider emitted ready again.

PAO owns orchestration across Agents, Engines, Runs, Sessions, time, or HASHI
instances, including:

- Nagare multi-agent workflows;
- Superloop long-running control loops;
- Jobs, cron, heartbeat, scheduler recovery, and background jobs;
- HChat and Hashi Remote coordination;
- transfers, queues, callbacks, cancellation, and delivery coordination; and
- outer approvals, policy, audit, and operational governance.

HERV3's continuous main-model/tool loop and recovery inside one HER Engine
Session are **inner orchestration** owned by HERV3. The shared word
`orchestration` does not transfer that lifecycle to PAO. Optional Strategy
Cards and Habits are advisory context, not PAO routes.

#### Scheduler time contract

PAO persists delayed and recovered due instants as UTC evidence. A recurring
cron definition retains its wall-clock expression plus an IANA `timezone`;
each occurrence is resolved against that date's zone rules. The documented DST
default selects the first occurrence in a fold and advances a gap to its first
valid minute. User-facing due/recovery text uses the job timezone and names it;
an unknown legacy timezone defaults to UTC, never the scheduler host timezone.
New and edited declarations use the revision-aware Scheduler writer and retain
unrelated fields. Merely reading a legacy declaration does not publish a
migration.

#### Scheduler recovery conversation contract (2026-09-30)

When persisted missed triggers require a decision, HASHI Scheduler creates one
system-authored Run through normal PAO admission in the owner's current primary
Conversation. The missed-trigger facts are the canonical Message content; the
Agent asks the user what to do, and the resulting question is delivered by FC.
Scheduled work remains in the Agent Activity Session; only the human decision
exchange belongs to the Conversation.

The user's later reply is always ordinary Conversation input, regardless of
whether it arrives from Telegram, TUI, Backend API, or another registered
Connector. PAO must not intercept it with transport-specific callbacks or
interpret it using numbers, keywords, exact phrases, or regular expressions.
The selected Engine reasons over the ordered Conversation and durable recovery
facts. A clear decision becomes one typed
`hashi_scheduler_recovery_resolve` invocation bound to the exact Agent and
batch; questions and ambiguous replies do not mutate Scheduler state. The
internal mutation endpoint accepts only the Agent Tool Gateway and remains
idempotent after a batch is resolved.

PAO tool execution resolves that Backend API endpoint from the Function
Worker's authoritative same-instance service topology. Isolated CLI Tool
Gateways receive a serialized snapshot derived from the same live endpoint.
Live topology takes precedence over compatibility snapshots; configured ports,
wildcard hosts, stale snapshots, and endpoints published by another instance
must never be guessed or accepted as substitutes.

### 2.4 Skills, Tools, permissions, and execution

PAO owns the HASHI-level capability registry and execution authority:

- discover which Skills and Tools exist;
- filter them by Agent, request, stage, Workzone, permission, and policy;
- grant or deny invocation;
- route approved invocations to the correct implementation;
- preserve PAO-level execution and side-effect evidence; and
- stop, fence, or reconcile work when required.

PCM projects the authorised catalogue into an Engine request. HERV3 decides
when to request an available Tool during its Turn and preserves HER-level Tool
evidence. Neither PCM nor HER may grant a capability withheld by PAO.

Scoped local discovery is a PAO Functions capability. Admission derives exact
preferred Workzone roots, Agent home, execution cwd and Workzone revision from
the same Run snapshot. Tool Registry has a separate, owner/Agent-bound home
read projection for `file_search`, `file_read`, `file_list` and `log_query`;
it never appends that grant to writable `access_roots`. Enterprise/privacy and
live-runtime read denials override it. A default search does not use a broad
configured access root. Explicit selected roots replace defaults. Cursor state
is bounded, ephemeral and bound to Run identity, query, scope and generation.
Registry/Gateway, HERV3 observer and Connector projection share factual progress;
elapsed time and heartbeat are not evidence of scanning work. Details and
rollout boundaries are in [HERV3 scoped search](HERV3_SCOPED_SEARCH.md).

Personal HASHI instances use an open Tool Registry default: when
`global.default_tools` is absent, `allowed` resolves to `["*"]`, and new-instance
creation paths persist that wildcard. The policy applies to every Agent whose
selected Engine exposes the HASHI Tool Registry. An explicit instance Tool
declaration still replaces the absent-value default, and a backend row with
`tools.enabled=false` still disables HASHI tools. The wildcard grants registry
permission only; Engine support, Workzone roots, device availability,
stage-specific policy, and per-invocation authority remain independent gates.
An isolated Tool route may not advertise a registered Browser/Computer Worker
from the capability Broker unless it can invoke that same Broker route. Without
the matching executor it fails closed with `broker_executor_unbound`; a
standalone diagnostic Tool Registry with no Broker snapshot may still use its
explicit local executor. Catalogue visibility and actual dispatch must name
the same execution source.

## 3. Non-responsibilities

PAO does not own:

- Persona, Context, or Memory content assembly, which belongs to PCM;
- an Engine's internal Strategy/Planning/Execution lifecycle;
- HERV3's Engine Session, checkpoints, Compact, or Model Provider routing;
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

OpenRouter's public model price list is the permanent, sole automatic source
of network-model price schedules, including estimates for models executed
through native Provider adapters such as DeepSeek. PAO does not query an
execution Provider's official pricing API, copy a Provider's peak/off-peak
schedule, or silently substitute a local price when the exact OpenRouter fact
is unavailable. A native moving alias may be bound explicitly to an exact
OpenRouter-managed `~provider/...-latest` price-list identity. A missing,
ambiguous, stale, or incomplete fact remains unknown. An amount reported on
the actual execution receipt remains observed billing evidence and takes
precedence over any catalogue estimate; it is not a second schedule source.

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

When HERV3 is selected, PAO binds the Conversation Session and context
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

PAO also does not infer an ordinary Message's semantic referent from words such
as `3`, `yes`, or `continue`. It preserves the accepted text and supplies
Session identity plus ordered history to PCM; the selected Engine reasons about
which earlier exchange is relevant. Typed slash/control operations remain
separate from ordinary conversation and must never be synthesized from reply
prose.

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

Terminal reply text is inert data at the PAO delivery boundary. PAO must not
scan it for provider Tool syntax or reinterpret it as an invocation; only typed
Engine events can carry Tool control. Provider-specific protocol validation
stays inside the Engine or its Adapter before a terminal result is returned.

### 6.3 PAO to Frontend Connectors

PAO exposes authenticated discovery, Conversation Sessions, Messages, Runs,
Events, controls, attachments, approvals, and operational status. Connectors
may cache projections but must reconstruct them from PAO-owned state.

PAO also owns the admission-time delivery decision for each Run. A Connector
may request a projection policy only through a typed, versioned contract. The
The former TUI `hashi.frontend-delivery` version 1 per-Run value is read-only
compatibility input. FC owns one mirror switch per owner and external platform.
PAO validates legacy client input but freezes the central FC preference when
it admits each Run. TUI remains in the shared Conversation Session, and
changing a switch cannot redirect or replay an existing Run. Turning a mirror
off affects every non-origin source for that owner, including scheduled work;
conversations started on the platform retain their primary reply route.

PAO projects live presentation facts through Agent metadata. Engine, model,
effort and current switches come from the active runtime; HERV3 supplies its
one selected Model Provider/model target and supported reasoning values.
Frontends may format these facts but must not infer them from files or become
another catalogue owner.

### 6.4 Per-message source and private authorization

PAO owns the `hashi.current-message-context` version 1 snapshot. Its provenance
fields are frozen at admission and the same current snapshot is persisted on
Message and Run. PAO first resolves the exact Session binding and freezes one
private `hashi.run-delivery-route` version 1 snapshot; only then may it build
the current-message snapshot. The route's primary destination, optional
mirrors, and automatic-delivery intent therefore cannot diverge from the
QueuedRequest when a client changes instance, Agent, Session, or mirror
preference after admission. Connector channel keys remain private and are not
projected into PCM.

`message_source` records the actual ingress, while `sender.kind` independently
distinguishes a human/client, HChat Agent, or HASHI system source. Scheduler,
heartbeat, proactive, and background-job events are `hashi.internal` system
messages even when their delivery route contains a Telegram chat. A Telegram
identifier is a route coordinate, not evidence that a human authored the
message.

Immediately before an execution attempt, PAO revalidates the
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

The projected `output_destination` is a plan, not a receipt. It contains the
primary `surface`, a surface-only `mirrors` list, and `automatic`; the legacy
`telegram_mirror` boolean remains for compatible TUI readers. Telegram, HChat,
and WhatsApp record separate `assistant.delivery.outcome` events only after
their Connector observes success or failure. A failed optional mirror does not
reroute or erase a successful primary delivery, and a queued HChat hop is not
mislabelled as a confirmed peer presentation.

## 7. Command ownership

Commands are connector entry points into domain contracts.

| Command family | PAO responsibility | Collaborating module |
|---|---|---|
| `/new`, `/fresh`, `/sessions`, `/use`, `/current`, `/archive`, `/fork` | Conversation Session lifecycle and selection | PCM supplies the resulting Context projection |
| `/backend` and compatible Engine selection | Engine Provider binding and migration | Selected Engine owns its internal Session |
| `/workzone` | Owner/Agent Workzone state, validation, and revision | PCM projects the admitted Run snapshot |
| `/handoff` | Session continuity operation | PCM assembles the continuity payload |
| `/clear` | Coordinate Session/media/Engine cleanup | Connector media and selected Engine participate |
| `/jobs`, `/loop`, `/bg` | Job and outer orchestration lifecycle | Connector renders status |
| `/stop`, `/steer` | Outer cancellation, fencing, and new-Run/Turn coordination | Selected Engine terminates its internal work |
| `/debug on|off` | Instance-level automatic terminal-failure forwarding preference | HChat transports one diagnosis assignment; the Connector renders status |

HER-specific effort, Habit, Meditation, provider, and model settings reach HER
through a Connector/PAO control surface, but their internal meaning remains
HER-owned.

Automatic debug reporting is deliberately a one-way PAO delivery convenience,
not a diagnostic lifecycle. `/debug on <agent@instance> <journal>` persists the
instance destination. Each non-interrupted terminal Run failure makes one
best-effort HChat send containing the failure provenance, local diagnostic-log
location, journal reference, and a diagnose-without-fixing instruction. PAO
does not validate or write the remote journal, await acknowledgement, retry,
queue, track resolution, or de-duplicate issues. The receiving Agent owns
diagnosis and journal de-duplication. Its completion is not routed back to the
source Agent, so recording the report cannot start another Agent turn.
HChat-origin failures are excluded so a failed diagnostic assignment cannot
create a reporting loop. `/debug <request>` retains the separate one-shot
strict-debug Skill behavior.

`request_diagnostics` is the separate read-only evidence query. It joins one
request's fail-open terminal projection with the existing Tool audit/Smart Tool
ledger and BackgroundJob receipts. It reports Provider request/response IDs,
wire references, observed writes/effects, final state, and whether safe retry
evidence is present, absent, or unknown. It never retries work, changes HERV3
control flow, or makes diagnostic persistence a completion condition.

On HASHI3, `/stop` also persists an Agent-wide autonomous-wakeup fence in the
existing workspace state. Scheduler, nudge, delayed-message, startup, and
background-completion admissions are blocked until a later explicit user
request resumes new admissions. Completed background outcomes remain in job
records and are not automatically replayed. The stop generation invalidates in-flight admissions
that raced with `/stop`; another Session's already-running user request remains
untouched. Delayed records are preserved, not silently consumed. A failed
fence-state read fails closed for new admissions.

Terminal completions with observed tool effects attach a bounded reconciliation
to the existing Session result and terminal diagnostic projection. The outer
`BackendResponse` supplies the observed count/effect flag; the existing Tool
audit, Smart Tool ledger, and BackgroundJob receipts remain the evidence
owners for both success and failure. A file write is called confirmed only when
its Tool receipt includes a readback; other tool actions and untyped CLI exits
remain uncertain. User error text separates those categories and warns against
blind replay. Audit-log truncation is disclosed; absence of an audit row never
proves absence of an effect. Successful reconciliation is diagnostic only and
does not change execution, continuation, or retry admission.

### HASHI1 automatic debug-reporting trial (2026-09-13)

- **Approval:** the user authorized this PAO/Functions change and live trial on
  HASHI1. No protected-Core source change is included.
- **Implementation:** branch `trial/debug-error-forwarding-hashi1` adds the
  instance preference, the common terminal-failure hook, the single existing
  HChat send, and the localized `/debug` status/configuration surface.
- **Offline verification:** the focused suite first failed at collection before
  the owner module existed and now passes 19 tests, including the complete
  Worker RPC path for unquoted and quoted Windows journal paths. A selected
  adjacent command, audit, UI, registry, and Skill run passes 93 tests (112
  together with the focused suite). Ruff, compilation with HASHI1's production
  interpreter, whitespace, the runtime-contract check, and the protected-Core
  guard pass. The development environment Core gate passed 655 tests; its 12
  failures were all the known mismatch between that test venv and the repository
  dependency lock.
- **Live verification:** reboot receipt
  `b444575dd05346d6b890afd007be8710` committed generation
  `sha256:7a38f179124b7cb0596ed4506c4c4be3b60a79d9057070401f278dd8d896a9c5`
  for all five active Agents (`temp`, `lily`, `feiyan`, `zhaojun`, and `sunny`).
  All Workers are ACTIVE, accepting, alive, and Telegram-connected; HASHI1 is
  ready and non-degraded. Core PID 16361 and shared Functions PID 16380 at
  generation
  `sha256:c799eabb5dac93097ef0c639b31a43c7c5235663dd76120ccbf508db319515fa`
  remained unchanged.
- **Activation:** `/debug` is ON for `zhaojun@HASHI1` with journal
  `C:\Users\operator\Desktop\HASHI_Nightly_Batch_Inbox.md`. Enabling it through
  `feiyan` and reading it through `lily` verified the shared instance setting
  and its persisted Windows path. Two earlier path-corruption attempts were
  disabled immediately, before any terminal error or debug report occurred.
  The first organic failure delivery and receiving journal update remain trial
  observations; no synthetic failure was generated for deployment verification.

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

### Agent transfer conversation authority (2026-09-13)

SessionStore remains the sole authority for formal Conversation history across
Move and Clone. Agent Move schema 5 carries an execution-free, owner-checked
continuity capsule instead of copying a database or reading a legacy workspace
transcript. Move imports eligible history before target publication and retires
source bindings only after verified cutover. Clone starts fresh unless the
operator explicitly chooses archived read-only inheritance or an independent
context copy. Stable origin references, a transfer journal, independent target
IDs, reference-counted origin claims, and one SQLite transaction own
deduplication and rollback. Rollback preserves later target messages and user
rebindings; source history retirement is the first destructive source-cleanup
boundary after target verification.

`history_generation` is the authoritative invalidation signal for derived chat
projections. Frontend Connectors that poll a transcript provide their known
generation and replace their local projection when SessionStore returns
`history_reset`/`cursor_reset`. This is a projection refresh, not a second
history owner; workspace `transcript.jsonl` never substitutes for missing
SessionStore state.

### Remote discovery and trust projection (2026-09-13)

The Remote Function owns discovery readiness and authenticated peer hydration.
mDNS carries bounded routing/identity hints and fixed digests; the mutually
authenticated handshake owns full capabilities, addresses, supervisor facts,
and Agent directory. Backend advertising/browsing health, bounded retries,
credential generation, trust counts, and static-seed fallback are projected
through Remote status without exposing shared-token material. A token or
advertised digest revision invalidates the old trusted projection until a new
handshake succeeds. Platform lifecycle helpers consume this status but do not
become another discovery or trust owner.
An empty successful backend snapshot retracts only that backend's observations;
it cannot leave a departed mDNS peer visible or erase a surviving fallback
route.

### Health after local Clone and Move (2026-09-10)

PAO startup health is a projection of current Worker/connector state. A Worker
with no configured Telegram ingress is intentionally local, not a Telegram
failure. After startup, health reads reconcile through StartupManager: removed
successful Worker entries leave the projection, while failed/pending startup
entries and unrelated service failures remain visible. Reads must not reconcile
across startup or a shared Functions handoff. This does not alter Core lifecycle.
