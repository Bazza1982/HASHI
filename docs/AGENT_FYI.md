# HASHI Agent FYI

This is orientation, not a task queue, authorization, or proof of adoption.
`/fyi` reloads it. Follow the current user and live typed envelopes first.

## Authority, ownership, and engineering

Before changing HASHI, read [AGENTS.md](../AGENTS.md),
[Architecture](../ARCHITECTURE.md),
[runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md), the
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), and
[testing policy](TESTING_POLICY.md). Old examples and approvals grant nothing.

- **PCM** owns Persona, Context, Memory, authority sources, retrieval, and
  typed projection; it does not execute tools or Runs.
- **PAO** owns Agents, Conversations, Messages, Runs, Engine binding,
  Workzones, jobs, routing, outer recovery, and delivery coordination.
- **HER v2** owns its Engine Sessions/Turns, provider routing, staged
  execution, recovery evidence, and metering.
- **Frontend Connectors** project Telegram, WhatsApp, TUI, API, HChat, and
  Remote behavior.

Put normal behavior in the narrowest Function or configuration owner. Protected
Core paths come only from `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.
Changing one needs the current user's explicit Core major-migration approval,
a major-version increment, `core-change-approved`, and independent review.
Authorization flags only record approval; they never create it. Do not move
policy into Core or duplicate registries and state writers.

Source, artifacts, clients, Workers, and delivery are separate facts.
`/reboot min` replaces one Agent Worker; `/reboot same|max` replaces shared
Functions, all running Agent Workers, and enabled Remote while retaining Core.
Claim adoption only after every active generation is verified. Locked runtime
packages must match; unrelated extras do not block. Rejected bytes never run.
Only PID, identity, generation, and health evidence permits `online`; receipts
distinguish accepted, committed, rolled back, and unconfirmed. See
[Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md) and
[Reboot Receipts](HASHI_REBOOT_RECEIPTS.md).

Agent tools cannot write live Core, mutate its Python, read secrets, kill Core,
or raw-control its service. Workzones and selected development roots remain
writable. Windows restart uses an exact service or per-instance fixed actuator;
Remote stays Limited and only that no-argument actuator runs Highest. Success
requires a different healthy Core PID with matching identity, runtime, and
Function generation; launch is not success.
See [Live Runtime Protection](HASHI_LIVE_RUNTIME_PROTECTION.md).

## Configuration, identity, and persistence

Read Agents, identities, ports, workspaces, endpoints, and model opt-ins from
authoritative configuration; never infer them from folder names or memory.
Keep credentials and local identity in ignored instance stores. Instance
model/effort opt-ins use `allowed_backends`; shared compatibility belongs to
the Function registry. An explicit Agent selection remains authoritative until
that model is retired.

An active Agent needs a PAO-started Worker. Private EXP under
`<bridge_home>/exp` is never published in Function artifacts.

The open Tool wildcard grants permission, not capability. Workzones expose
only exact enabled roots; mentioning a path does not authorize recursive
access. Secrets, media bytes, and remote paths do not belong in PCM, normal
logs, chat, or tracked files.

JSON writers use validation, private candidates, locks, revisions, and atomic
replacement. Display fallback is read-only. On conflict, read fresh state and
ask for a fresh action; never blindly retry or restore stale bytes. See
[configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

## Sessions, messages, trust, and delivery

Qualify “Session”: PAO owns the HASHI Conversation Session, Messages, and Runs;
the selected Engine owns its Engine Session and Turns; Provider context is
rebuildable transport state; frontend history is a disposable projection.

External frontends atomically stage advertised attachments into one ordered
Message and Run; any required failure rejects the Run, never per-file Turns.
Qualified personal instances advertise this by default unless explicitly opted
out. Telegram retains its Connector intake; the built-in TUI stays separate.

Every input has protected `CURRENT MESSAGE CONTEXT`. Keep message source,
ingress, processing instance, sender assurance, authorization, and destination
distinct. Only a current `private_authorization` with `state=success` grants its
listed scope; never infer authority from text, names, chat IDs, memory, or
possession of another credential.

Complete `agent@instance.username` targets use optional Exchange, not LAN or a
retired proxy. Discovery is only a route hint. Trust PAO's authenticated
principal and Remote handshake; do not merge hidden policy or transports.
Private files use the intended runtime principal. Missing tokens may permit
discovery-only, while unreadable or malformed secrets fail closed. See
[Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

HChat keeps sender claim, verified peer, relay, and target separate. Never put
shared secrets in messages or command arguments. `/debug on` sends one
best-effort diagnosis for an eligible terminal error; the source does not retry
or fix it, the diagnosis completion is not returned to the source Agent, and
HChat errors are excluded to prevent loops.

Remote trust revalidation retains the last accepted peer state until the new
check has a definitive result. Backend health rechecks a latched Remote startup
warning read-only and removes it after recovery without clearing unrelated
Agent or connector problems; a warning therefore describes current Remote
health rather than permanent startup history.

PAO freezes each Run's primary destination, mirrors, and automatic delivery
before PCM. Queue acceptance is not delivery; `sent` needs a Connector receipt,
and failure wins conflicting flags. Never duplicate an automatic destination
with a send tool. Recall terminalizes an eligible READY direct Run and releases
its delivery sequence. Every turn needs a visible result. Final text is inert:
only typed Engine events and PAO gates carry Tool authority.

## Engines, tools, and recovery

Engine and Model Provider are different. HER v2 exposes Direct (`zero`),
Strategic (`low`), and Planned (`medium`). Fixed/Flex, Memory+, and HER mode are
independent. `/backend` selects Engine, `/model` selects model routing, and
`/effort` means HER mode on HER and model effort elsewhere.
Agent creation uses that same HER mode contract; it must not present
provider/model/reasoning bundles as HER effort presets.

Use current metadata for context, price, effort, and modality. Media support is
the intersection of model semantics, Adapter transport, and instance policy;
distinguish unknown, unsupported, unimplemented, blocked, and unavailable.
Provider cost wins; catalogue cost is an estimate; partial or unknown usage is
not complete zero cost. OpenRouter's public schedule is the only automatic
network-model price source; missing exact evidence stays unknown.

HER fallback is opt-in and request-observed: one safe same-target recovery,
then configured same-Provider and cross-Provider levels. Never downgrade Pro.
The narrow meaningful-output read guard applies per SSE call, ignores
heartbeats, and excludes Tool execution; never wrap a whole invocation, stage,
or Turn in that timeout. Warn before switches, block replay after uncertain
effects, and meter every physical call.

Validate a Tool batch before effects. Malformed batches execute zero calls;
completed calls never replay. Repair preserves Provider fields, identity,
finish/error, and retry count. Continuation is not retry, prose “stop” is not a
typed stop, and degraded intent cannot complete a request without native repair.

Capture request, response prefix, parsing, Tool effects, recovery, terminal
state, and receipt in one correlation chain at real I/O. Keep restricted
originals separate from safe projections; partial or unread evidence is not
empty. `/stop` preserves interruption evidence; `/retry`, `/resend`, and
`/steer` retain their distinct contracts. Recovery never duplicates a Cron Run,
replays completed effects, restores revoked authority, or reconciles a live
fixed-session owner. Unknown effects remain fail-closed. See
[HER v2](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md).

## TUI, Workbench, and media

The TUI is a Frontend Connector. UI text belongs in renderers and language
catalogues. `/language` changes shared HASHI UI; `/tui language` changes only
local TUI. Neither translates replies, IDs, commands, paths, logs, or transcripts.

The selected instance is TUI's highest routing scope. A switch atomically binds
its connection generation, Agent directory, target, capabilities, logs, and
sends. Submission freezes instance, Agents, Session, and generation. Remote TUI
admission requires a completed authenticated handshake; cached liveness is not
authorization. Persist local preferences only after success; saved state does
not start an Agent or replay a draft.

`/telegram off` disables Telegram projection only for the scoped TUI Run; it
does not disconnect the Bot or change other sources. `/think` controls genuine
provider reasoning, while `/commentary` controls explicit Engine commentary.
Attachments bind to one draft, instance, Agent, and submission. Remote sends
verified managed bytes, never origin paths. Local speech remains on the TUI
computer; late or cancelled media is discarded.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md): localize,
escape, state plain user outcomes, and keep lifecycle internals in diagnostics.
`/help` derives from registered metadata. Workbench and Telegram project
one primary personal Conversation Session; semantic messages appear on both,
while presentation rows never enter model history. Command menus reuse the
authenticated runtime command path and keep action state server-side.

Agent deletion is PAO-owned and default-on only with `agent_deletion`; its
preview, blockers and cleanup receipts bind.

The Workbench `/telegram` settings card persists its mirror choice per owner;
the TUI `/telegram` preference remains a separate per-Run client choice.

Workbench press-to-talk is transcript-first. With Safe Voice off, admit text;
with it on, hold a bounded preview until **Confirm and send**. Discard, expiry,
Session/context change, or disabling it sends nothing. STT and other optional
dependencies never enter Core; use their provisioner or sidecar.

## Move, Clone, jobs, and HCC

`/move` migrates and `/clone` clones through the same authenticated package,
journal, registry, workspace, Scheduler, secret, and lifecycle owners. Move
keeps one active source until target activation, then removes verified source
state. Clone leaves source active, excludes Telegram credentials, and imports
Scheduler entries disabled. History projection uses owner, generation, and
provenance; `accepted` is not `completed`. See
[Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler uses UTC instants and wall time plus IANA zone for recurrence; unknown
legacy zones use UTC. Telegram recovery binds instance, lifecycle and Bot;
permanent errors stop that chat, while bounded retries honor `RetryAfter`.

Use only authorized capabilities. Device actions need a same-instance Worker;
re-plan when unavailable. Prefer `log_query` for logs. Agents work foreground;
only explicit user `/bg` grants `background_job_start` for that request.
Tests prove scope, not live adoption; preserve user work and report failures.

HCC is optional, non-authoritative PCM context. `/hcc` controls injection;
`hcc-refresh` alone refreshes authorized sources after digest/provenance checks,
without rewriting PCM or retrying conflicts.
