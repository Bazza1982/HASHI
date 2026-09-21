# HASHI Agent FYI

This is orientation, not a task queue, authorization, or proof of adoption.
`/fyi` reloads it. Follow the current user and live typed envelopes first.

## Authority, ownership, and engineering

Before changing HASHI, read [AGENTS.md](../AGENTS.md), [Architecture](../ARCHITECTURE.md),
[runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md), the
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), and [testing policy](TESTING_POLICY.md).
Old examples and approvals grant nothing.

- **PCM** owns Persona, Context, Memory, authority, retrieval, and typed
  projection; it never executes tools or Runs.
- **PAO** owns Agents, Conversations, Messages, Runs, Engines, Workzones, jobs,
  routing, outer recovery, and delivery.
- **HER v2** owns Engine Sessions/Turns, routing, execution, recovery, and cost.
- **Frontend Connectors** project Telegram, WhatsApp, TUI, API, HChat, and Remote.

Put behavior in the narrowest Function/configuration owner. Protected paths are
only those in `CORE_SOURCE_PATHS`; changing one needs explicit Core
major-migration approval, a major-version bump, `core-change-approved`, and an
independent review. Flags record approval but never grant it. Keep product
policy out of Core and registries/state writers singular.

Source, artifacts, clients, Workers, and delivery are separate facts. `/reboot
min` replaces one Agent Worker; `same|max` adopts a Function generation across
shared Functions, Workers, and enabled Remote while Core stays live. Legacy
bridges promote receipts only after bootstrap/Core validation and commit.
Successors report the committed generation; claim adoption only from PID,
identity, generation, health, and receipts. Rejected bytes never run. Locked
packages must match; extras do not block.

Agent tools cannot alter live Core/Python, read secrets, kill, or raw-control
Core; development roots stay writable. Windows restart uses an exact service or
fixed actuator while Remote stays Limited. Tasks run Highest, but success needs
a different healthy Core PID matching identity, runtime, and Function
generation; task completion is insufficient.
See [Live Runtime Protection](HASHI_LIVE_RUNTIME_PROTECTION.md).

WSL/native login startup is Windows platform behavior. Use its versioned
installer with explicit instance, identity, checkout, interpreter, and WSL
distribution when applicable. Native stderr is diagnostic; the process exit
code decides success. Keep source, task, logs, and live adoption distinct. Use
`process_is_alive` across platforms, never `os.kill(pid, 0)` on Windows because
it can interrupt processes sharing a console.

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

The TUI is a Frontend Connector. UI text belongs in renderers and language
catalogues. `/language` changes shared HASHI UI; `/tui language` changes only
local TUI. Neither translates replies, IDs, commands, paths, logs, or transcripts.

The selected instance is TUI's highest routing scope. A switch atomically binds
generation, Agent directory, target, capabilities, logs, and sends; submission
freezes instance, Agents, Session, and generation. Remote admission requires a
completed authenticated handshake; cached liveness is not authorization.
Persist preferences only after success; saved state starts no Agent or draft.

`/telegram off` disables Telegram projection only for the scoped TUI Run; it
does not disconnect the Bot or change other sources. `/think` controls genuine
provider reasoning, while `/commentary` controls explicit Engine commentary.
Attachments bind to one draft, instance, Agent, and submission. Remote sends
verified managed bytes, never origin paths. Local speech remains on the TUI
computer; late or cancelled media is discarded.

HER v2 keeps each stage's authorised attachment manifest through Planning,
Execution, Replanning, Review, and Finalisation. Native-capable providers use
the native content; local fallback stages receive the exact managed reference
and must not guess a same-named workspace copy. This reference visibility does
not widen Tool or filesystem authority, and sub-agents still receive only their
explicitly delegated subset.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md): localize and
escape. Remote Agent lifecycle timeouts are outcome-unknown: use PAO's budget,
reconcile state, and never replay.
When reboot qualification finds unfinished Function source work, the ordinary
notice says a software update is still in progress, confirms saved settings and
the current Agent remain safe, and asks the user to retry after it completes.
Technical paths remain in diagnostics rather than the ordinary notice.
`/help` derives from registered metadata. Workbench and Telegram project one
personal Conversation Session; semantic messages appear on both, while
presentation rows never enter model history. Menus use the authenticated path
and server-side action state. Projection v2 persists menu state across
snapshots; v1 is unchanged, and another menu does not expire an earlier card.

`/new` selects a fresh primary Session without deleting prior Conversations.
Workbench history projects retained Sessions by owner and Agent; old messages
stay read-only and resolve attachments through their original Session. A
current transcript does not prove the Agent archive is empty.

Agent deletion is PAO-owned and default-on only with `agent_deletion`; its
preview, blockers and cleanup receipts bind.

The Workbench `/telegram` settings card persists its mirror choice per owner;
the TUI `/telegram` preference remains a separate per-Run client choice.

Workbench voice is transcript-first. Safe Voice off admits text; on holds a
bounded preview until **Confirm and send**. Discard, expiry, Session change, or
disabling sends nothing. Optional STT stays outside Core in a sidecar. Its
stdio protocol is always UTF-8 bytes, independent of Windows code pages or
Linux locale, and npm deployments include its isolated-runtime provisioner.

## Move, Clone, jobs, and HCC

`/move` and `/clone` share authenticated package, journal, registry, workspace,
Scheduler, secret, and lifecycle owners. Move keeps the source active until
target activation, then removes verified source state. Clone keeps it active,
excludes Telegram credentials, and imports Scheduler entries disabled. History
uses owner, generation, and provenance; `accepted` is not `completed`. See
[Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler uses UTC instants and wall time plus IANA zone for recurrence; unknown
legacy zones use UTC. Telegram recovery binds instance, lifecycle and Bot;
permanent errors stop that chat, while bounded retries honor `RetryAfter`.

Use only authorized capabilities. Device actions need a same-instance Worker;
re-plan when unavailable. Prefer `log_query` for logs. Agents work foreground;
only explicit `/bg` grants that request background work. Tests prove scope,
not live adoption; preserve user work and report failures.

HCC is optional, non-authoritative PCM context. `/hcc` controls injection;
`hcc-refresh` alone refreshes authorized, verified sources without rewriting
PCM or retrying conflicts.
