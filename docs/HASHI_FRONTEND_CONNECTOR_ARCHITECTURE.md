# HASHI Frontend Connector Architecture

| Field | Value |
|---|---|
| Status | **Authoritative Frontend Connector module specification** |
| Effective date | 2026-09-25 |
| Parent architecture | [HASHI System Architecture](../ARCHITECTURE.md) |
| Scope | Built-in TUI, messaging connectors, Backend API, Persistent Session API, Remote projection, and compatible external clients |
| Implementation status | Standard FC source converged and adopted on HASHI1; connector-specific live acceptance remains scoped |

As of 2026-09-26, Functions contains the versioned connector-neutral contracts,
capability catalog, common ingress, canonical Event/Message projection, media
ownership, and per-endpoint outbox used by the production adapters in scope.
The qualified source has been adopted on HASHI1. This is not proof that every
configured external destination or physical device has passed live acceptance;
the authoritative rollout evidence and remaining gates are tracked in
[the standard-interface execution plan](HASHI_FC_STANDARD_INTERFACE_EXECUTION_PLAN_2026-09-25.md).

## 1. Definition

Frontend Connectors expose HASHI to users and compatible clients without
creating a second source of Agent, Session, Message, Run, Event, PCM, or Engine
state. A Connector translates between one user-facing transport and the typed
PAO/PCM/Engine contracts.

The Connector belongs to HASHI. A separately developed graphical product that
uses the Connector does not.

## 2. HASHI-owned connector surfaces

HASHI includes and maintains:

- the built-in reference TUI;
- Telegram and WhatsApp connectors;
- the local Backend API;
- Persistent Session API v1;
- HChat and required Hashi Remote client projections;
- shared attachment, approval, delivery, notification, and control contracts;
  and
- client-neutral capability negotiation and qualification tooling.

The built-in TUI remains part of HASHI and is not planned for extraction into a
separate product. It is the reference terminal client for local operation.

## 3. External-client boundary

Any compatible desktop, web, mobile, IDE, or operations client may use HASHI
infrastructure when it conforms to the published protocol and security rules.

HASHI owns:

- protocol versions and capability discovery;
- authentication, authorization, identity, and resource boundaries;
- canonical Conversation Sessions, Messages, Runs, and Events;
- Engine and PCM integration;
- attachments, approvals, controls, replay, and fencing; and
- transport-neutral error and terminal semantics.

An external client owns:

- window layout and presentation;
- unsent drafts and local convenience state;
- its packaging, installation, update, and release channel;
- product-specific data and final product-domain authorization; and
- disposable caches that can be rebuilt from HASHI state.

Terminal backend failures retain bounded user-safe provider fields on the
canonical Run failure Event. The owner-scoped request activity API exposes the
same durable fields to clients, including code, HTTP status, provider request ID,
retryability, and diagnostic log reference. Clients may collapse the error
summary but must allow the user to inspect those fields. Existing failure Events
written before this contract retain only the text that was originally stored.
The 2026-09-27 Lily failure demonstrated the gap: Telegram had HTTP 400 and a
provider request ID while the Run Event retained only its generic error text,
leaving Workbench with a one-error count. Focused regression now verifies that
the structured fields survive a SessionStore reopen and remain owner-scoped;
Workbench's request digest test verifies expansion and the bad-request advice.
Live adoption and a real browser click remain separate acceptance evidence.

No external client name, repository revision, installer, or private release
channel may be compiled into general HASHI admission policy. Compatibility is
defined by protocol conformance and declared limits.

Command-triggered continuations such as `/load` retain the authenticated
command's Session, owner, surface, and channel when they enter the normal Run
queue. The continuation's internal source names its purpose; it is not a new
frontend. A parked topic is marked loaded only after its continuation has been
accepted by the queue, so an admission failure leaves the topic available.

## 4. Authority and projection

```text
User interface
  -> Frontend Connector
  -> PAO Conversation Session / Message / Run / Event
  -> PCM and selected Engine
  -> PAO terminal state and delivery decision
  -> Frontend Connector projection
  -> User interface
```

Connectors may maintain delivery cursors, render caches, typing state, and other
bounded projections. They must not:

- maintain a competing authoritative sent-message archive;
- resend client-owned chat history as if it were canonical HASHI Context;
- infer authorization from possession of an opaque identifier;
- expose provider-native thread or request IDs as Session authority;
- let a late client or worker overwrite a fenced or terminal Run; or
- turn a confirmation-gated action into ordinary Session text. Such flows need
  a typed, owner-/Session-/generation-bound admission and decision contract so
  moving connectors cannot weaken the confirmation boundary.

For user-authored Runs, `message.content` remains the unchanged canonical Agent
input and audit record. A conforming client may also submit the optional
`message.display_text` presentation projection. HASHI stores that projection
atomically on the server-generated Message identity and uses it only in
user-facing transcript/history projections. Absence falls back to the stored
canonical text without content-based transformation; an explicit empty string
does not trigger that fallback. The idempotency
digest includes the projection, and the canonical Message API exposes both
values. Connectors must not derive this field by scanning, recognizing, or
removing marker-like user text.

## 5. Built-in TUI

The TUI is a permanent HASHI Frontend Connector and reference local client. It
supports local operation and trusted instance switching through Hashi Remote.

Current implementation boundary:

- when the explicit TUI Session ingress capability is advertised, TUI ordinary text resolves the
  owner-scoped primary Session and enters through Session Runs, locally or via
  authenticated Remote proxy; older instances and proxy generations retain
  the basic Backend API chat fallback before submission only. Single-file
  attachments use Session stage/upload/commit followed by one Run when explicitly
  advertised, locally or through the authenticated Remote proxy. Only a proxy
  rejection before canonical admission permits legacy fallback. Target-relative
  Workzone references are read only by the selected Agent's enabled Workzone
  adapter and committed as managed Session attachments. Commands still use
  compatibility chat routes, and display still polls
  transcript while canonical feed migration is pending;
- a write timeout or a connection loss after a request may have been sent is
  an unknown outcome: the TUI must not replay that write against another
  fallback URL or the legacy chat route. Direct writes pin the verified Session
  host; authenticated Remote proxy writes verify the target's local instance
  before submission and never replay an uncertain local or peer write;
- those routes keep the established `workbench/default` Conversation binding,
  so a TUI window is a projection of the same formal Conversation rather than
  the owner of a private TUI Session;
- its local command palette derives Agent commands from the canonical command
  metadata, shows complete typed syntax plus localized option/example guidance,
  and keeps TUI-only navigation and layout commands inside the Connector;
- after an exact command and a space, the palette shows at most five parameter
  matches at a time. Runtime-owned backend, model, provider, effort and Agent
  choices are derived from live metadata and the qualified Functions catalogue,
  while bounded command grammar stays in canonical command metadata;
- submitting a command or parameter prefix selects the first displayed prefix
  match, while an unknown slash command is rejected locally and never becomes a
  model prompt;
- unified slash-command response `messages` are rendered immediately by the
  TUI with target-safe Telegram HTML/Markdown conversion. They are not inserted
  into the transcript, so later polling cannot duplicate them. A successful
  no-argument command with typed parameters also receives a local, copyable
  Options/Usage/Example guide because Telegram inline keyboards do not render
  in the terminal;
- chat history remains a Rich/Markdown projection while exposing mouse
  selection, a visible selection style, selection-first `Ctrl+C`, and
  selection-fenced follow-tail behaviour. Native Windows copy preserves the
  full UTF-16 text, including supplementary characters, and transfers allocation
  ownership only after the clipboard accepts it;
- short sent/received sounds are a local, persisted TUI preference. Windows uses
  the native sound API and WSL/Linux uses an available PulseAudio or ALSA player;
- `/say` and automatic reply speech are TUI-only presentation. The selected
  instance generates bounded Ogg bytes using the Agent-owned semantic voice
  profile, while the launch computer owns the sole non-overlapping player.
  These controls never enqueue a chat command or create Telegram output;
- TUI auto-read is persisted per launch client, instance and Agent. The four
  available semantic profiles are discovered from the Agent voice owner and
  profile changes use that existing revision-safe state rather than a second
  TUI voice configuration;
- language, layout, sounds, auto-read, and the TUI typing indicator are local
  persisted Connector preferences. External platform mirrors use the FC owner
  setting shared by every ingress;
- the side panel is closed by default. `/sidepanel` opens the TUI's persisted,
  read-only information panel;
  `/sidepanel off` closes it and `/sidepanel refresh` refreshes it. The panel
  starts directly with the selected Agent's canonical token summary, then
  projects Scheduler and background jobs, system-prompt summaries,
  parked-topic summaries, and the live Agent directory. Its content is a real
  focusable scroll region with a visible scrollbar: the mouse wheel works over
  the panel, while click-to-focus enables Arrow, Page Up/Down, Home, and End.
  `/sidepanel auto on|off|toggle` controls a persisted, default-off automatic
  tour that advances one row per second, briefly holds at the bottom, then
  loops to the top; manual navigation temporarily pauses it. The panel remains
  an information surface only: actions stay as slash commands in the input,
  and the panel owns no competing state;
- the connection footer projects live Agent metadata for Engine, Provider,
  model, effort, Think, Verbose, Commentary and Connector state. HERV3 shows
  its one selected Provider/model target and model-reasoning effort; historical
  Quick/Pro routing is not a public v3 projection. The footer intentionally omits
  working mode; `/mode` remains its authoritative control surface;
- its cross-instance path proxies only a small named operation set through
  authenticated Hashi Remote peers. Side-panel reads use explicit, Agent-scoped
  allowlist operations rather than an arbitrary Backend API proxy; and
- it does not yet implement the complete Persistent Session API v1 multi-
  Session surface.

This current limitation must be stated plainly. Future TUI development should
adopt the richer Session/Event contract without changing the rule that the TUI
stays inside HASHI.

### 5.1 Connector-neutral delivery intent

Frontend delivery is a versioned, server-owned intent over one or more
connector endpoints. The canonical form identifies connector and opaque
endpoint IDs, delivery role, enabled state, and retry policy; it does not use a
Telegram-shaped field as the generic model. The intent is frozen when a Run is
admitted so later preference changes cannot redirect an in-flight reply.

The former TUI-only `hashi.frontend-delivery` version 1 value remains an input
compatibility format. The Backend API validates and binds it to the submitting
TUI client, then normalizes it to the connector-neutral version 2 form. New
preference writes use `frontend_delivery_preferences.json`; reads lazily
recognize `workbench_telegram_state.json` without rewriting it, and the first
successful write migrates the value under revision checking. A damaged or
conflicting document is not overwritten.

The Frontend Connector owns one persistent mirror switch per owner and external
destination. `/telegram on|off` and `/whatsapp on|off` update those switches
from any authenticated frontend, including Telegram and WhatsApp themselves.
TUI and Backend API submit no separate mirror choice; old client-bound delivery
policies remain readable but cannot override the central switch. A switch
applies to future Runs regardless of their ingress. Turning a mirror off never
suppresses a reply to a conversation initiated on that destination, nor does it
disable the destination's transport. A Run's destinations are frozen at
admission, so switching later does not replay or redirect it. Automatic
WhatsApp mirrors require exactly one configured allowed personal number; the
transport's ordinary replies still use their original chat endpoint.

TUI queue/typing state is an ephemeral Connector projection fenced by instance
generation, Agent, Session, Run and request identity. Durable Run status and
matching transcript metadata may advance or clear it; errors, terminal states,
stop/cancel, Agent switching, instance switching and shutdown clear it. The TUI
preference is independent of Telegram's `/typing` policy.

Footer state comes from the runtime's `presentation_status` projection carried
by the existing Agent metadata interface. The live runtime/Engine registry is
authoritative; offline configured Agents expose only their configured Engine
and model and mark unavailable live switches unknown. Connectors must not scan
scattered configuration files or duplicate model/effort catalogues.

## 6. Backend API and Persistent Session API

The **Backend API** is HASHI's local runtime API for built-in and authenticated
clients. The **Persistent Session API v1** is the canonical richer contract for
client-neutral multi-Session state, ordered Events, controls, attachments,
approvals, replay, and fencing.

The richer API is published only when its fail-closed qualification boundary is
satisfied. A client using basic chat routes must not be described as having the
full Persistent Session API contract.

The qualified v1 contract is enabled by default for personal instances. This
includes ordered single- and multi-attachment intake for all advertised media
forms and standard assistant attachment output. An instance may still set
`global.persistent_session_v1=false` as an explicit operator opt-out; missing
legacy configuration is not interpreted as an opt-out.

The detailed state and qualification contracts are defined in:

- [HASHI Persistent Multi-Session Frontend Design](HASHI_PERSISTENT_MULTI_SESSION_FRONTEND_DESIGN.md)
- [Multi-Session Frontend Insertion Plan](MULTI_SESSION_FRONTEND_INSERTION_PLAN.md)

### 6.1 Message-source and optional private-proof wire contracts

Backend API JSON and Persistent Session API submissions may include a public
`message_source` object with separate stable `id` and localized
`display_name`. Multipart chat accepts the same object as JSON text in its
`message_source` form field. Clients must discover the canonical reserved IDs,
pattern and limits from `/api/capabilities/message-source` or
`/api/v1/capabilities`; a custom valid ID needs no HASHI source release.
Structured declarations cannot claim reserved IDs or `hashi.` runtime names;
their owning Connector supplies those protected facts. A legacy `source` may
still map to a reserved source with accurately weaker assurance for
compatibility. Missing API declarations fall back to `api`; other indeterminate
ingress is projected as `unknown`. The legacy `source` field retains its routing
and media compatibility meaning.

HChat and protocol senders may explicitly select repeatable
`--private-credential ID` values and repeatable `--authorization-resource`
values. HASHI transports only short-lived typed HMAC proofs. The proof is bound
to the message content, identities, target and resources, and the receiver maps
it to non-secret scopes. No option means ordinary HChat with no additional
authorization; an unsuccessful private proof also does not turn into a general
communication denial. A private-proof cross-instance send refuses an
unauthenticated legacy fallback instead of silently dropping the proof.

Connector evidence forwarded across a local Backend API hop is time- and
prompt-bound with the existing Remote network secret. It may preserve facts
observed by the owning Remote/TUI/HChat Connector, but it cannot convert a
declared sender into a uniquely authenticated Agent. PCM and external clients
receive only sanitized verification results, never shared secrets or raw
proofs.

### 6.2 Standard multi-attachment message admission

Persistent Session API v1 is the standard external-frontend boundary for a
message containing one or more attachments. This contract is client-neutral;
it must never contain product-specific frontend names or layout policy. The
built-in TUI may continue to use its deeper HASHI binding, and existing
Telegram intake keeps its transport-specific grouping behaviour.

A conforming external frontend must:

1. read `/api/v1/capabilities` and require the advertised
   `frontend_connector.multi_attachment` contract;
2. stage each attachment with
   `POST /api/v1/sessions/{session_id}/attachments`;
3. upload the exact bytes with `PUT .../{attachment_id}/content` and commit
   them with `POST .../{attachment_id}/commit`; and
4. create exactly one Run with one
   `POST /api/v1/sessions/{session_id}/runs` request.

The Run's ordered `message.content` array uses `text` parts and generic
`attachment` parts containing opaque `attachment_id` references. The legacy
`audio` part remains accepted for native voice compatibility. HASHI resolves
the committed metadata into its existing canonical multimodal request content;
no client path or inline bytes enter durable Message state.

One Run admission is atomic: every referenced attachment must belong to the
same owner and Session, be uploaded, committed, within advertised count and
size limits, and retain its array order. Any invalid reference rejects the
whole Run before an Agent Turn is enqueued. A frontend must never turn a failed
capability check into multiple legacy chat submissions, because that would
change one user action into multiple Messages and Turns. Upload staging is not
a Run and may be cleaned up later by retention policy.

### 6.3 Standard assistant attachment output

An Agent responding to a conforming external frontend publishes generated or
selected files through the frontend-neutral `frontend_send_attachments` tool.
One call contains an ordered `attachments` array and binds every item to the
current running Session Run. The terminal assistant Message then contains the
normal text plus those canonical attachment references, so all compatible
frontends consume the same projection instead of a product-specific callback.

The output mutation is owner-, Agent-, Session-, Run-, and instance-bound. It
accepts only files inside the Agent's authorized Workzones, applies the same
advertised count and byte limits as frontend intake, verifies content hashes,
and uses the tool-call identity for replay-safe idempotency. A reused identity
with different bytes is rejected. Multiple tool calls may contribute ordered
attachments to one reply, but the total limits still apply to the Message.

The public `frontend_connector` capability snapshot includes the
connector-neutral registry and versioned FC contract families. The standard
`frontend_send_attachments` tool accepts any active Session delivery route,
including Telegram, TUI and Backend API, while checking every source file
against authorized access roots. Media groups retain declared order, content
digests and Session retention policy. Explicit `telegram_send_file` remains a
Telegram-targeted compatibility action; it is not the generic multi-connector
attachment contract.

## 7. Retired Workbench boundary

Workbench is retired. A successor frontend is maintained separately and is not
part of HASHI.

Some compatibility identifiers remain:

- `orchestrator/workbench_api.py` implements the Backend API;
- `global.workbench_port` stores the Backend API port;
- established token/header names may retain `workbench`; and
- `/workbench/v1/*` may remain a compatibility route family in Hashi Remote.

These are implementation identifiers, not an active product boundary. New
documentation and user-facing text must say **Backend API** unless it is
explaining an exact compatibility name. Private external product names must not
appear in general HASHI architecture.

## 8. Connector neutrality

Shared contracts must be transport-neutral. A Connector may implement
transport-specific formatting, message limits, notification behaviour, or
interaction controls, but the underlying product state stays typed and owned by
PAO, PCM, or the selected Engine.

In particular:

- Telegram classes must not become the canonical representation of a generic
  Event or delivery action;
- WhatsApp limitations must not weaken another Connector's capabilities;
- TUI rendering choices must not become Session policy; and
- a Backend API response must not expose internal implementation state as a
  public contract accidentally.

### 8.1 Standard semantic boundary (2026-09-26)

FC defines one semantic interface for every frontend. A message is a message,
a command is a command, a display/card is a display, and an interactive button
is an action regardless of whether the Connector is Telegram, TUI, Backend API,
an external desktop frontend, or a future registered Connector.

PAO remains the authority for Sessions, Messages, Runs, Events, routing and
delivery state. Every normal ingress is normalized by a registered FC adapter
before PAO admission. Every durable user-facing output is first a canonical
Message/Event with per-endpoint outbox state; only then may a Connector render
and send it. Frontend applications do not inject objects into a runtime, and
runtime business code does not emit frontend-specific cards as a second source
of truth.

Connector-local layout, HTML, terminal styling, media rendition and native
interaction mechanics remain allowed. A semantic exception, such as TUI-local
`/agents`, must be declared in the FC registry and fail closed when it is not
registered. Telegram ephemeral progress and the no-Worker delivery-health
notice are likewise registered presentation-only exceptions; neither may
become a second command, Session, Message, or delivery-status authority.

Existing frontend programs keep their own standards. Compatibility endpoints
and transport APIs are thin Connector adapters around FC; a normal FC change
does not require editing Telegram, an external Workbench application, or HASHI
business behavior merely to reproduce the same command/card for another UI.

Each command-menu response binds its Session presentation identity to the
canonical command invocation, not to fallback text or a transport message
number. A replay of the same client/request returns the saved non-action result
and requires a refresh; it never reissues stale legacy or standard actions. A
new status request may safely render the same text with newly issued actions.
Native callback wrappers preserve the Connector-requested UI locale. Native
Telegram text also retains `telegram` as its admission source so its automatic
reply destination cannot be lost by generic text normalization.
Telegram command-menu registration is presentation setup after Bot connection.
A timeout or rate limit while setting the default menu must not demote a
connected Agent to local-only mode. The Worker records the failed stage and
retries menu registration independently until success or a permanent rejection;
shutdown cancels the retry task.

Final replies from standard non-Telegram Runs publish enabled meter and HER
presentations after the final Message through the same Session Event boundary.
Telegram mirroring is a destination choice, not a prerequisite for creating
those canonical display events.

### 8.2 Commands during an active Run

An active Agent Run does not close FC command ingress. A conforming frontend
must not suppress the command catalogue or a typed command invocation solely
because the Agent is generating. It submits the command through the same FC
adapter and lets the owning command decide whether it can apply immediately,
must reject while busy, or requires a separate lifecycle operation.

Commands never become ordinary chat Messages or queued Runs as a workaround.
The typed invocation keeps its Session and generation fence, and its result is
returned as the canonical command response and durable
`frontend.command_result` Event. Connector-local submission, cancellation, and
draft conflicts may still block the local control until their own outcome is
known; an unrelated active Agent Run may not.

### 8.3 Backend API answer preview feed (2026-09-27)

Provider text deltas remain internal. HERV3 may classify a safe visible delta
as `answer_preview`; FC then projects it as a typed, ephemeral `answer` event on
the v2 Session feed. Each projected event carries the canonical Session, Run,
and request identities. The durable terminal Event additionally carries the
canonical Message identity and is the only authoritative final answer.

Pull clients consume both lanes through
`/api/v2/frontend/sessions/{session_id}/feed` using independent durable and
ephemeral cursors. They may render a preview progressively, but must discard it
after an incomplete replay or gap and wait for the durable final. The final
replaces the preview rather than creating a second answer. Raw provider deltas
and the legacy request-activity endpoint are not alternate answer outputs.

This capability is additive to the Backend API Connector. It does not change
Telegram presentation or delivery. Source and focused offline validation are
recorded separately from running-Function adoption; this change did not restart
or replace a running HASHI generation.

### 8.4 Voice profile preview publication (2026-09-28)

Voice profile previews are versioned, immutable Function assets. A deployment
must package and validate the complete manifest; an instance-local
`media/_voice_previews` asset may override its matching product asset without
becoming part of the deployment template.

The preview action publishes through FC to the Session and context generation
that opened the command UI. Backend API/Workbench receives a standard
`audio_attachment` presentation. Telegram keeps its registered connector-local
voice-message rendering and is the only branch that calls the Telegram
transport. No frontend packages a private copy of the preview catalogue.

The user approved the Function, HASHI1 deployment-template, and local-instance
changes on 2026-09-28. Offline validation and running-Function adoption remain
separate; approval did not authorize a reboot or replacement.

## 9. Engineering-layer placement

Connector business behaviour belongs in the Functions layer. Stable process
handles may be retained by Core only when required for hot replacement.
Platform Configuration handles terminal, Windows/WSL, macOS, browser, or
sidecar adaptation. Instance Configuration supplies local ports, bind hosts,
tokens, enabled connectors, and peer identity.

No Connector may hard-code machine-specific paths or private client settings
into HASHI Functions or Core.

## 10. Current alignment debt

- The TUI keeps registered local commands and basic Backend API compatibility
  routes. They are intentionally thin FC adapters; a complete v2 multi-Session
  TUI is not required by the current standard-interface migration.
- Telegram-native classes remain inside its Connector renderer and registered
  presentation exceptions. They are not canonical Event or command types.
- Retired Workbench compatibility names remain in source and configuration.
- Persistent Session API v1 remains fail-closed when its runtime qualification
  evidence is absent, even though qualified personal instances enable it by
  default.
- Source convergence and live adoption are separate. The 2026-09-26 FC source
  is adopted on HASHI1; Telegram, Relay, physical microphone and any other
  unavailable or unauthorized destination remain explicitly outside the
  completed Workbench live subset.

These are current implementation facts, not target architecture exceptions.

## 11. Future-development rules

New Connector work must:

1. keep HASHI state authoritative and frontend caches disposable;
2. use client-neutral, versioned protocols and typed Events;
3. separate user-interface presentation from HASHI execution policy;
4. enforce identity, permission, fencing, and idempotency at every mutation;
5. label basic API support separately from full Persistent Session API support;
6. preserve the built-in TUI as a HASHI component;
7. avoid private external product names and client-specific runtime branches;
   and
8. preserve compatibility identifiers only where migration requires them.

Local command responses capture both short replies and `send_long_message`
output in the request's async context. This keeps `/version` and other long
command results in the API `messages` response for TUI rendering. Capture is
scoped to the runtime and ends with the command; ordinary Telegram delivery
outside that context retains its normal chunking and delivery behavior.

Version provenance accepts Git identity only when the repository top level is
the HASHI code root. A portable/npm artifact nested inside a different checkout
uses its own build metadata and never inherits the enclosing repository branch.

### Local terminal themes (2026-09-10)

The reference TUI owns `/theme [retro|apple2|nintendo|win32|atm|reset]`.
Retro remains the default. The local preference writer preserves unrelated
settings; missing or invalid saved names fall back to Retro. Themes change CSS
surfaces and Rich semantic styles together, including already displayed chat
and log content, Markdown, code, command discovery and the information panel.
Changing appearance retains the input draft, selected Agent and Session. It
requires no backend call or Worker restart. Native terminal fonts and host
window settings remain user managed. Platform/client adoption is recorded
separately from headless renderer evidence.

### Terminal connection and management (2026-09-10 candidate)

The npm `hashi` entry delegates to the existing instance CLI. The parser owns
commands and options; the no-Python help cache is generated with
`python -m scripts.export_terminal_help`, while shell completion traverses that
same parser. Global selection, JSON, locale and non-interactive flags are
normalized before any operation. Unknown or conflicting targets fail before
instance creation. Start never runs onboarding or kills a process on timeout;
attach-only never starts one. One-shot JSON wraps the result, error and observed
effects, while log follow uses events. The existing registry owns default and
binding changes, including read-only default inspection and unbind.

The local `/connect` page and `hashi onboard` compatibility route use masked
secret controls and explicit consent. Discovery lists same-environment CLI
executables and catalogue models without claiming authentication. The selected
backend is probed through FlexibleBackendManager and its real adapter in a
disposable workspace. API choices configure HERV3's selected Provider/model
through its internal compatibility storage boundary; a model-list response is
not success. Unavailable models and failed streams remain errors. Probe, save,
reload request and actual chat readiness are separate facts.

Only Hashiko's connection fields and scoped credential references are merged.
Existing Agents, identities, history, optional integrations and FC mirror preferences
are retained. A connection revision is consumed once by the backend state
owner to supersede old overrides. New Hashiko uses workspace access and the
PAO-owned open HASHI Tool default; the disposable connection probe remains
tool-free. A local save may request only the existing controlled `/reboot min`
for idle Hashiko; rejection leaves adoption pending, never triggers a
whole-instance restart, and does not report ready.

Optional Telegram setup uses a separate masked Bot Token and positive numeric
user ID, verifies getMe, refuses conflicting existing ownership, and saves no
open-to-everyone fallback. It does not send a test message or change FC mirror
preferences. Real Telegram round trips require their own authorized evidence.

Fresh TUI startup focuses the chat input, so typing starts work immediately.
When the optional persistent Session status API reports `session_api_not_ready`,
the accepted chat continues through the existing transcript channel; the client
stops unsupported status polling without presenting the accepted task as failed.
Terminal purge confirmation and external-data denials use exit 77 and distinct
stable error codes while preserving the selected external instance.

Terminal log following reopens a replaced file and tolerates the gap during
rotation. Windows readers share deletion so they cannot block the writer's
rename. Chat selection offsets are assigned after highlight segments are split,
so continuous physical mouse drags retain the exact selected text across redraws.
These are Frontend Connector Functions changes; existing TUI clients need to
be reopened to adopt them.

Footer fact changes trigger layout measurement as well as repaint, preserving
all status fields after long model/route metadata arrives in a narrow window.

Local connection credentials are written only after the empty temporary file
has owner-only access; the fully flushed file is then atomically published.
This applies to first save and replacement on both POSIX and Windows.

Local connection adoption uses the authenticated `/api/admin/reboot-agent`
boundary and the existing PAO RebootManager. It binds one named Agent and a
saved connection revision, checks actual Worker activity, and returns a durable
operation ID. A busy Agent is left running while the local client waits up to
90 seconds; unresolved adoption remains explicitly pending. Telegram identity
is never fabricated to authorize a local configuration operation. The client
checks the selected instance identity before submitting and waits for the
authenticated PAO receipt to reach succeeded before dismissing the local page.
Acceptance alone never claims completed adoption.


## Basic chat projection and voice adaptation (2026-09-12)

The external chat UX task authorizes minimal Frontend Connector Functions
changes, not Core edits or production lifecycle actions. Basic transcript reads
resolve one authenticated canonical Session snapshot for its path, identities
and bounded recent-Run discovery. Untagged legacy JSONL receives path/epoch/byte
record identities; explicitly foreign Session/context rows are excluded. A
partial final line is not acknowledged. Invalid byte cursors report a bounded
reset snapshot and a completeness gap. No anonymous owner is created.

RequestActivityStore publishes only its existing classified public projection,
using current runtime think/commentary/verbose preferences and HER required
commentary/control semantics. Provider-internal delivery stays internal. The
actual pipeline attaches the preferences before routing its callbacks. Source
sequence, stable thinking-block identity and explicit retention/truncation facts
support client replay; this in-memory store is not a durable transcript.
Delivered JSONL records carry accepted Run/request/Session/context metadata from
the existing persistence path. Legacy append time is recorded time, not recovered
human-send time. API compatibility identifiers remain Backend API identifiers.

Basic uploaded voice uses the existing local STT owner before ordinary Session
admission. With Safe Voice off, the transcript continues directly through the
existing admission owner. With Safe Voice on, the selected Worker holds only
the transcript and its admission metadata in a ten-minute, in-memory
confirmation record; it retains no raw audio. The basic upload remains an
explicit pre-admission rejection until Workbench reads that record and confirms
it. Discard, expiry, Safe Voice being turned off, or a Session/context change
makes the record inert and never submits it automatically.

Text provenance and audio digest keep same-file retries independent of
temporary upload paths. The existing SessionStore acceptance transaction checks
the expected context generation again at confirmation; a concurrent reset
cannot silently admit old-session speech. Typed pre-admission errors remain
distinguishable from uncertain transport outcomes, including partially admitted
multi-file uploads. No database schema, authentication, provider, Core or
shared-ingress protocol is introduced.

### Targeted Workbench Safe Voice adoption (2026-09-13)

> Historical migration note: this admin-command transport was the first
> Workbench adapter and is superseded for Safe Voice by the Session API path
> documented below. Do not add new Safe Voice reads or decisions to this route.

The existing authenticated admin-command transport may carry the reserved
`__hashi_voice_confirmation_v1__:` envelope through the unchanged
`runtime.slash` RPC. This lets one selected Agent Worker adopt Workbench Safe
Voice with `/reboot min`; the shared Backend API can continue returning its
established `voice_safe_confirmation_required` pre-admission response.

The base64url JSON object is exactly one of:

- `{"version":1,"op":"read","idempotency_key":"..."}`; or
- `{"version":1,"op":"decide","pending_id":"...","decision":"confirm|discard"}`.

Only authenticated `workbench_api` calls in a personal deployment are
accepted. The Worker derives the configured positive actor and current
`workbench/default` Session. A caller cannot select an owner, Session,
generation, prompt, transcript or delivery policy through the decision
envelope. Every reserved request terminates before ordinary command parsing and
slash-command audit.

Read returns the bounded local-STT transcript, opaque pending identity,
expiration and authoritative Session/context. Confirm rechecks that binding and
calls the ordinary request admission owner exactly once; repeated confirms
return the same request identity while that Worker retains its receipt. Discard
is terminal and idempotent. Expired,
discarded, disabled, missing or wrong-generation records cannot enter the Agent.
An admission exception or a missing receipt after Worker replacement is
explicitly uncertain, never presented as a known success or known rejection.

External frontend reading state remains that frontend's metadata: HASHI does not
write Telegram read state on its behalf. The bounded activity store cannot prove
complete offline replay; source limitations must remain visible to the client.

Implementation and offline validation are scoped to the local candidate branch
feature/chat-connector-ux-20260912. Shared API source adoption, Worker source
adoption, qualified artifacts, current STT dependencies and terminal delivery are
separate facts. This task did not adopt a running generation or restart production.

### Workbench Safe Voice through canonical Session Runs (2026-09-25)

New Workbench voice recordings enter the selected owner-scoped Session as an
audio attachment with `semantic_role=voice_message`, then one idempotent Session
Run. The route verifies the Session's Agent and context generation before
staging bytes. It reads the durable transcript event filtered by that Run ID;
Safe Voice still presents a confirmation preview, and confirm/discard use the
typed Session voice-transcript decision endpoint. The audio is never converted
to plain text by the connector, and no decision is automatic.

Attachment stage retries are bound to a client idempotency key and canonical
metadata digest. Identical retries reuse the same attachment; changed metadata
conflicts. Uploading the identical bytes and committing an already committed
attachment are safe replays, while expired or otherwise unavailable assets
cannot be revived. The Session API publishes this capability explicitly, and
Workbench fails closed for file delivery when it is absent.

Safe Voice confirmation is fenced atomically against both the originating Run
generation and the Session's current generation. Session events support a Run-ID
filter so a replay can find its own durable transcript event without mixing in
other messages. The Session API does not currently expose a durable transcript
expiry, so the Workbench does not invent one in its presentation contract.

The changes are source-only and offline-validated. Running Function adoption,
real microphone/file delivery, confirmation in the external Workbench, and the
remaining live acceptance manifest are separate pending checks; no runtime was
restarted as part of this slice.

### Targeted Worker chat-projection adoption (2026-09-12)

The Backend API's existing authenticated admin-command transport may carry the
reserved `__hashi_chat_projection_v1__:` envelope through the unchanged
`runtime.slash` RPC. Frontend Connector Functions interpret it only at the
selected Agent Worker; this follows the existing command-menu transport and
does not add a Core, Supervisor, Worker-host or shared-ingress protocol.

The base64url JSON object contains integer `version: 1`, `op: recent` with an
optional integer `limit` from 1 through 200, or `op: poll` with a required integer
`offset` from 0 through 9007199254740991. Other fields are rejected. Only the
`workbench_api` source in a personal deployment is supported. The Worker derives
the positive configured authorized actor and the current `workbench/default`
Session through the existing runtime Session owner; a caller cannot choose an
actor, owner, Session, context generation, or filesystem path.

Every supported result carries `chat_projection_version: 1`. Success returns
`ok: true` and `projection` with the same fields as the transcript HTTP routes.
Failure returns `ok: false`, a stable `chat_projection_*` error code and
`http_status`. Invalid requests and read failures are terminal before ordinary
command handling, command audit capture or model admission. The envelope and
transcript body are not written to command or failure logs.

HTTP and Worker callers share `build_chat_projection`, so message identity,
context fences, byte cursors, half-record handling and recent-Run discovery
retain one implementation. Reading creates no Messages, Runs or frontend reading
state; existing SessionStore resolution may initialize default Session/binding
metadata and its workspace when absent. A populated Session's persisted data is
unchanged by projection reads. Polling retains the existing transcript reader's
cursor and replay limits; this transport is not a new durable activity archive.

The current task explicitly permits a targeted `reboot min` for the named Agent,
while prohibiting instance/shared restart and Core edits. Implementation and
focused offline evidence are on `feature/chat-connector-ux-20260912`; a parent
integration task owns qualification and live adoption. No lifecycle action was
performed while implementing this transport. Initial real command-entry tests
failed before implementation (30 failed, one ordinary-text case passed); the
follow-up positive-actor invariant failed for zero and negative actor IDs before
the guard was tightened. Focused validation passed: 76 cases across
`test_chat_projection_transport.py`, `test_chat_connector_projection.py`,
`test_command_interaction_transport.py` and `test_session_api.py`; 23 direct
command-audit/admin consumers passed separately. The protected-Core guard and
whitespace check passed. Live Worker generation and user-terminal delivery remain
separate acceptance evidence.

### Terminal-only slash commands

The shared command catalogue contains `/logo`, whose effect targets the HASHI
server terminal. The command is TUI-only and stays out of shared command menus
(`menu_visible=False`). The Connector registry projects the mandatory
`connector_local` restriction for every non-TUI Connector, including dynamically
registered third-party Connectors; a registration cannot override it with
`route=standard`. Admission, compatibility adapters, capability responses and
registry snapshots all derive this restriction from the same owner.

External calls are rejected before terminal execution. The native Telegram
handler returns a localized unsupported notice. The TUI compatibility route
passes its normalized Connector identity to the handler and retains terminal
execution; a handler must not mistake that call for a native Telegram update.

### Manual desktop candidate (2026-09-30)

The opt-in, client-neutral manual desktop ingress and Windows worker projection are
specified in [Manual Desktop v1](HASHI_MANUAL_DESKTOP.md). The candidate reuses PAO
capability registration and resource leases, accepts an authenticated human actor,
and never creates a conversational Run. It is not a new frontend product inside
HASHI. Source/automated checks and physical-device acceptance remain separate;
feature availability defaults to disabled.
