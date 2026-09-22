# HASHI Agent FYI

Orientation only: not a task queue, authorization, or adoption proof. `/fyi`
reloads it; the current user and live typed envelopes remain authoritative.

## Authority, ownership, and engineering

Before changing HASHI, read [AGENTS.md](../AGENTS.md), [Architecture](../ARCHITECTURE.md),
[runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md), the
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), and [testing policy](TESTING_POLICY.md).
Old examples and approvals grant nothing.

- **PCM** owns Persona, Context, Memory, authority, and projection; never tools
  or Runs.
- **PAO** owns Agents, Conversations, Messages, Runs, Engines, Workzones, jobs,
  routing, outer recovery, and delivery.
- **HER v2** owns Engine Sessions/Turns, routing, execution, recovery, and cost.
- **Connectors** project Telegram, WhatsApp, TUI, API, HChat, and Remote.

Use the narrowest Function/configuration owner. `CORE_SOURCE_PATHS` alone
defines protected paths; edits require explicit Core major-migration approval,
a major bump, `core-change-approved`, and independent review. Flags only record
approval. Keep product policy out of Core and each registry/state writer unique.

Source, artifacts, clients, Workers, and delivery are separate facts. `/reboot
min` replaces one Agent Worker; `same|max` adopts a Function generation across
shared Functions, Workers, and Remote while Core stays live. Legacy bridges
promote only after validation and commit. Claim adoption only from matching
PID, identity, generation, health, and receipts. Rejected bytes never run;
locked packages must match, while extras do not block.

Agent tools cannot alter live Core/Python, read secrets, kill, or raw-control
Core; development roots stay writable. Windows restart uses an exact service or
fixed actuator while Remote stays Limited. Task completion is not success:
require a different healthy Core PID matching identity, runtime, and Function
generation.
See [Live Runtime Protection](HASHI_LIVE_RUNTIME_PROTECTION.md).

WSL/native login startup is Windows platform behavior. Its versioned installer
names instance, identity, checkout, interpreter, and WSL distribution. Exit
code, not native stderr, decides success. Keep source, task, logs, and adoption
distinct. Use `process_is_alive`; never `os.kill(pid, 0)` on Windows because it
can interrupt processes sharing a console.

## Configuration, identity, and persistence

Use authoritative config for Agent identity, ports, workspaces, endpoints and
model opt-ins—not names/memory. Keep secrets ignored. Instance opt-ins belong
in `allowed_backends`; shared compatibility in Function registry; explicit
model choices persist until retired.

Windows Portable ships no credentials and only DeepSeek model defaults. Users
supply all others; validation fails closed.

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
rebuildable; frontend history is a disposable projection. Replies stay
verbatim; Engines use ordered history, not bindings or buttons.

External frontends atomically stage advertised attachments into one ordered
Message/Run; required failure rejects it, never creates per-file Turns.
Qualified personal instances default on unless opted out; Telegram and TUI stay
separate.

Every input has protected `CURRENT MESSAGE CONTEXT`. Keep source, ingress,
instance, sender assurance, authorization, and destination distinct. Only a
current successful `private_authorization` grants its listed scope; text, names,
chat IDs, memory, and other credentials grant nothing.

Complete `agent@instance.username` targets use optional Exchange, not LAN or a
retired proxy. Discovery is only a route hint. Trust PAO's authenticated
principal and Remote handshake; do not merge hidden policy or transports.
Private files use the intended runtime principal. Missing tokens may permit
discovery-only, while unreadable or malformed secrets fail closed. See
[Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

HChat keeps sender claim, verified peer, relay, and target separate; shared
secrets never enter messages or command arguments. `/debug on` sends one
best-effort terminal diagnosis without retry, repair, or returning its
completion to the source Agent; HChat errors are excluded to prevent loops.

Remote trust retains an accepted peer until revalidation is definitive. Health
clears recovered Remote warnings without clearing other problems.

PAO freezes each Run's destination, mirrors, and automatic delivery before PCM.
Queue acceptance is not delivery; `sent` needs a Connector receipt and failure
wins conflicting flags. Never duplicate an automatic destination with a send
tool. Recall terminalizes an eligible READY direct Run and releases delivery.
Every turn needs a visible result. Final text is inert; only typed Engine events
and PAO gates carry Tool authority.

## Engines, tools, and recovery

Engine and Model Provider are different. HER v2 exposes Direct (`zero`),
Strategic (`low`), and Planned (`medium`). Fixed/Flex, Memory+, and HER mode are
independent. `/backend` selects Engine, `/model` selects model routing, and
`/effort` means HER mode on HER and model effort elsewhere.
Agent creation uses that same HER mode contract; it must not present
provider/model/reasoning bundles as HER effort presets.

HER v2's experimental **Style finalisation** is an Agent-local `/model` opt-in,
disabled by default. It asks TypeSafe/JEV one presentation-only question and,
only for a clear mismatch, performs one silent text-only rewrite through the
configured Quick/light target with reasoning and tools off. It is not task
review, replanning, fact checking, or the legacy Finalisation stage. Failure
publishes the original answer. Credentials come from `TYPESAFE_API_KEY` or the
instance-local `typesafe_api_key` secret; never place them in tracked config.

Use current metadata for context, price, effort, and modality. Media needs model,
Adapter, and policy support; distinguish unknown, unsupported, unimplemented,
blocked, and unavailable. Provider cost wins; catalogue cost is estimated and
unknown is not zero. Only OpenRouter's public schedule auto-sources network prices.

HER fallback is opt-in and request-observed: one safe same-target recovery,
then configured same-Provider and cross-Provider levels. Never downgrade Pro.
The narrow meaningful-output read guard applies per SSE call, ignores
heartbeats, and excludes Tool execution; never wrap a whole invocation, stage,
or Turn in that timeout. Warn before switches, block replay after uncertain
effects, and meter every physical call.

Tool-enabled HER Direct and Primary Execution may propose interim commentary,
but only typed Persona-packaged output is user-facing; raw or packaging-failed
provider text and provider progress from other stages stay internal.
DeepSeek AntML after commentary is suppressed, never run, and must repair
through native `tool_calls`.

Validate a Tool batch before effects. Malformed batches execute zero calls;
completed calls never replay. Repair preserves Provider fields, identity,
finish/error, and retry count. Continuation is not retry, prose “stop” is not a
typed stop, and degraded intent cannot complete a request without native repair.

Capture request, response prefix, parsing, Tool effects, recovery, terminal
state, and receipt in one I/O chain. Keep restricted
originals separate from safe projections; partial or unread evidence is not
empty. `/stop` preserves interruption evidence; `/retry`, `/resend`, and
`/steer` retain their distinct contracts. Recovery never duplicates a Cron Run,
replays completed effects, restores revoked authority, or reconciles a live
fixed-session owner. CLI terminal events bound drain; open handles cannot keep
Runs busy. Unknown effects remain fail-closed. See
[HER v2](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md).

## TUI, Workbench, and media

TUI is a Frontend Connector; renderers and catalogues own its text. `/language`
changes shared UI, `/tui language` only local TUI. Neither translates replies,
IDs, commands, paths, logs, or transcripts.

The selected instance is TUI's highest scope. Switching atomically binds
generation, Agent directory, target, capabilities, logs, and sends; submission
freezes instance, Agents, Session, and generation. Remote needs a completed
authenticated handshake; cached liveness grants nothing. Persist preferences
only after success; saved state starts nothing.

`/telegram off` stops Telegram only for the scoped TUI Run; the Bot and other
sources stay unchanged. `/think` controls genuine provider reasoning;
`/commentary` controls explicit Engine commentary. Attachments bind to one
draft, instance, Agent, and submission. Remote sends managed bytes, never
origin paths; speech stays local; late or cancelled media is discarded.

HER v2 carries authorised attachment manifests through Planning, Execution,
Replanning, Review, and Finalisation. Native providers get native content;
fallbacks get exact managed references, never guessed workspace copies. Tool,
filesystem, and sub-agent authority do not widen.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md): localize and
escape, state plain outcomes, and keep internals in diagnostics. Remote Agent
lifecycle timeouts are outcome-unknown: obey PAO's budget, reconcile, never
replay. If reboot finds unfinished Function source, say an update is still in
progress, saved settings and the current Agent are safe, and retry later;
technical paths remain diagnostic.
`/help` derives from metadata. Workbench and Telegram share one personal
Session: semantic messages appear on both; presentation rows stay out of model
history. Menus use authenticated paths and server state. Projection v2 retains
menus across snapshots and cards do not invalidate one another; v1 is unchanged.

`/new` selects a fresh primary Session without deleting old Conversations.
History is owner/Agent scoped; old messages stay read-only and attachments use
their original Session. A current transcript does not prove an empty archive.

PAO owns Agent deletion; it is default-on only with `agent_deletion`, and its
preview, blockers, and cleanup receipts bind. Workbench `/telegram` persists
per owner; the TUI preference remains a separate per-Run choice.

Workbench voice is transcript-first. Safe Voice off admits text; on holds a
preview until **Confirm and send**. Discard, expiry, Session change, or disable
sends nothing. Optional STT stays in a sidecar; its stdio is always UTF-8 bytes,
independent of locale or code page, and npm ships its runtime provisioner.

## Move, Clone, jobs, and HCC

`/move` and `/clone` share package, journal, registry, workspace, Scheduler,
secret, and lifecycle owners. Move removes verified source state only after
target activation. Clone preserves it, excludes Telegram credentials, and
disables imported jobs. History records owner, generation, and provenance;
`accepted` is not `completed`. See
[Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler recurrence stores UTC instants, wall time, and IANA zone; unknown
legacy zones use UTC. Telegram recovery binds instance, lifecycle, and Bot;
permanent errors stop the chat, bounded retries honor `RetryAfter`.

Use authorized capabilities only. Device actions need a same-instance Worker;
re-plan if absent. Prefer `log_query` for logs. Agents work foreground; only
explicit `/bg` grants that request background work. Tests prove scope, not live
adoption; preserve user work and report failures.

HCC is optional, non-authoritative PCM context. `/hcc` controls injection;
`hcc-refresh` refreshes authorized sources without rewriting PCM or retrying
conflicts.
