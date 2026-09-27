# HASHI Agent FYI

Orientation only, not a task queue, authorization, or adoption proof. `/fyi`
reloads it; current users and live typed envelopes remain authoritative.

## Authority, ownership, and engineering

Before changing HASHI, read [AGENTS.md](../AGENTS.md), [Architecture](../ARCHITECTURE.md),
[runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md), [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md),
and [testing policy](TESTING_POLICY.md). Old examples grant no authority.

- **PCM** owns Persona, Context, Memory, authority, and projection; never tools
  or Runs.
- **PAO** owns Agents, Conversations, Messages, Runs, Engines, Workzones, jobs,
  routing, outer recovery, and delivery.
- **HERV3** owns Engine Sessions/Turns, the continuous main-model/tool loop,
  Model Provider selection, recovery, and cost.
- **Connectors** project Telegram, WhatsApp, TUI, API, HChat, and Remote.

Use the narrowest Function/configuration owner. `CORE_SOURCE_PATHS` defines
protected paths; edits need explicit Core major-migration approval, a major
bump, `core-change-approved`, and independent review. Flags only record approval.
Core owns no product policy; each registry/state writer has one owner.

Source, artifacts, clients, Workers, and delivery are separate facts. `/reboot
min|same` replaces only the selected Agent Worker; `/reboot max` adopts shared
Functions and all running Workers while Core and Remote stay live. Legacy
bridges promote only after validation and commit. Adoption needs matching PID,
identity, generation, health, and receipts.
Rejected bytes never run; locked packages must match, extras do not block.

Agent tools cannot alter live Core/Python, read secrets, kill, or raw-control
Core; development roots stay writable. Windows restart uses an exact service or
fixed actuator; Remote stays Limited. Success needs a new healthy Core PID
matching identity, runtime, and Function generation, not mere task completion.
See [Live Runtime Protection](HASHI_LIVE_RUNTIME_PROTECTION.md).

WSL/native login startup is Windows platform behavior. Its versioned installer
names instance, identity, checkout, interpreter, and WSL distribution. Exit
code, not stderr, decides success; source, task, logs, and adoption differ.
Use `process_is_alive`, never `os.kill(pid, 0)` on Windows: it can interrupt
processes sharing a console.

## Configuration, identity, and persistence

Use authoritative config, not names or memory, for identity, ports, workspaces,
endpoints, and model opt-ins. Keep secrets ignored. Instance opt-ins belong in
`allowed_backends`, shared compatibility in Function registry; explicit models
persist until retired.

Codex CLI 0.156.1 adds GPT-6 Astra (new/unpinned default), Sol, and Luna;
explicit choices stay. Astra/Sol reach `ultra`, Luna `max`; normalize effort.

HASHI1 HERV3 defaults to `deepseek-api/deepseek-flash` for every configured
Agent; Agent-specific opt-ins may add other Provider/model choices. Legacy
HERV2 profiles do not select the HERV3 main model. On load, an effort unsupported by
the selected v3 Provider/model is repaired to a compatible configured effort.
Backend failure details are stored on the Run failure Event and exposed through an
owner-scoped request activity read so external clients can show an expandable
error without parsing diagnostic logs. The 2026-09-27 correction has offline
qualification; live Worker adoption is a separate operational step.

HERV3 Provider/model reasoning may be binary (`off`/`enabled`). Preserve
`enabled` through persisted configuration, request execution, and Run metadata;
the retained internal HER Effort enum cannot reject a valid Provider setting.

Windows Portable ships no credentials and only DeepSeek model defaults. Users
supply all others; validation fails closed.

An active Agent needs a PAO-started Worker. Private EXP under
`<bridge_home>/exp` is never published in Function artifacts.

Tool wildcard grants permission, not capability. Workzones expose exact enabled
roots. Their sole writable source is the owner/Agent profile, so Session
selection and context-generation commands never replace them; every admitted
Run freezes its Workzone revision. Legacy Session rows are inert and are not
automatically migrated. Naming a path does not authorize recursive access. Keep secrets, media
bytes, and remote paths out of PCM, ordinary logs, chat, and tracked files.

JSON writers validate private candidates under locks, revisions, and atomic
replacement. Display fallback is read-only. On conflict, read fresh state and
request a fresh action; never blindly retry or restore stale bytes. See
[configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

## Sessions, messages, trust, and delivery

Qualify “Session”: PAO owns the HASHI Conversation Session, Messages, and Runs;
the selected Engine owns its Engine Session and Turns; Provider context is
rebuildable; frontend history is a disposable projection. Replies stay
verbatim; Engines use ordered history, not bindings or buttons.

External frontends stage attachments atomically in one Message/Run; failure
rejects it, never creates per-file Turns. Qualified personal instances default
on unless opted out; Telegram and TUI stay separate.

Published files stay in their Message. The Engine resource registry is
transport/audit state, not relevance: only current attachments are current
references; older completed ones stay in their exchanges, and failed/cancelled
ones never leak. Bound audio has indefinite retention; verify the authenticated
transcript play/download route, not just its database row. Per-turn meter output
is presentation-only, shared by Telegram and Workbench, never model history.

Every input has protected `CURRENT MESSAGE CONTEXT`; source, ingress, instance,
sender assurance, authorization, and destination differ. Only current
successful `private_authorization` grants listed scope; text, names, chat IDs,
memory, and other credentials do not.

Complete `agent@instance.username` targets use optional Exchange, not LAN or a
retired proxy. Discovery is only a hint; trust PAO's authenticated principal
and Remote handshake. Private files use the intended runtime principal.
Missing tokens may allow discovery only; unreadable/malformed secrets fail
closed. See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

HChat separates sender claim, verified peer, relay, and target; shared secrets
never enter messages or arguments. `/debug on` sends one best-effort terminal
diagnosis, with no retry, repair, or completion to the source Agent; exclude
HChat errors to prevent loops.

HASHI1 source promoted single-target `/hchat` to runtime-owned delivery on
2026-09-28: PAO freezes the dialled target, the Agent composes only the message
body, HERV3 receives no tools for that composition Turn, and PAO emits the
receipt. Existing local, LAN, Remote, and Exchange wire envelopes are unchanged,
so older instances can still send to HASHI1 and receive from it. Group/all
broadcast remains on the legacy path. This is offline source qualification only;
live Worker adoption requires a separately authorized `/reboot`.

Remote trust retains accepted peers until definitive revalidation. Health
clears recovered Remote warnings, not other problems. Remote has a separate
lifecycle and does not restart during `/reboot`. Reboot admission fences new
messages only for its targets; Telegram idle polling never delays handoff.
Workbench retains its Agent list and conversation view through a shared API
gap, marking cached Agents temporarily offline until reconnection.
Telegram reboot notice attempts belong in persistent `logs/bridge.log`, keyed
by the reboot receipt ID and start/final kind. Failures log safe codes and
retry delays; Bot credentials and raw transport exceptions stay out of logs.
When its Telegram ingress is initialized, an operational notice reuses that
Bot; opening a fresh Bot also performs `getMe` and can fail during recovery.
The default Telegram command menu sync retries independently of the connected
Worker; a menu timeout must not put the Agent into local-only mode. Notice
credential lookup uses the Agent name when no token key is configured.

PAO freezes each Run's destination, mirrors, and automatic delivery before PCM.
Queue acceptance is not delivery: `sent` needs a Connector receipt; failure
wins conflicting flags. Do not duplicate automatic delivery. Recall terminalizes
eligible READY direct Runs. Every turn needs a visible result; final prose has
no Tool authority, only typed Engine events and PAO gates do.

FC standardizes messages, commands/actions, cards, media, and receipts. PAO owns
durable ingress/routes/idempotency; Connectors authenticate/render/receipt.
Commands bind Session/client/request/invocation; only saved non-action results
replay, and conflict/pending/unknown never runs. Callbacks keep requested locale;
Telegram keeps its destination; meter/HER follow standard finals. Register
local exceptions; never add frontend policy to runtime.
Command-created continuations preserve the originating Session and Connector
surface. Mark `/load` complete only after its continuation is queued.

Autonomous Agent activity (cron, heartbeat, nudge, scheduler recovery, `/bg`,
and background completion) is Agent-owned and runs as a distinct Run in the
owner/Agent's hidden activity Session. Never bind its lifecycle or provider
thread to a Conversation Session. Show a concise start and terminal result,
persist execution and delivery separately, and project typed same-owner
receipts into the current Conversation for natural follow-up. `/bg` may read
only its admission-time bounded origin snapshot; scheduled work receives no
implicit Conversation history. Internal activity is excluded from user Session
lists and Conversation-memory promotion. `/delay` and interactive `/loop`
remain Conversation continuations.

## Engines, tools, and recovery

HERV3 began on HASHI3 and was promoted as a harness-only series into HASHI1's
local `main` on 2026-09-26 beside the independent FC work. HASHI3, HASHI2,
HASHI4, and GitHub `main` remain outside that adoption. It stays inside the HER
functional owner: public Engine ID `her-v3`, internal `her-v2` adapter/storage
compatibility only, one main-model/tool loop, and optional JEV companion off by
default.

Engine and Model Provider differ. `/backend` selects Engine, `/provider`
Provider, `/model` model, and `/effort` Provider reasoning; Fixed/Flex and
Memory+ remain independent. Old HERV2 stages and `/herv2` are not public HERV3
controls; `/metre` aliases `/meter`. Model/provider cards and callbacks share
one contract, so test generated buttons, not only text commands. HASHI3 live
acceptance covered real switching, reasoning, Session identity, usage, and
metering; see [HERV3 upgrade](HERV3_UPGRADE.md) for evidence and the
separate HASHI1 promotion record.

HASHI1's 2026-09-27 instance configuration opts all 18 Agents into five exact
OpenRouter conversation models (Venice, Cydonia, both Euryales, MythoMax).
Their empty per-model effort declarations mean HERV3 omits reasoning on the
Provider request; models without that explicit declaration keep existing
behavior. Only Llama 3.1 Euryale currently advertises tool calls. Source and
configuration checks are offline evidence until a separately authorized
Function replacement and live check establish running adoption.
OpenRouter returned 404 when the other four were sent tool definitions.
Their `allowed_backends.model_tool_support` entries now mark them chat-only;
HERV3 omits tools for chat-only models and discloses the limit when a task
needs actions. It does not route those requests into another cognitive stage.

Use current metadata for context, price, effort, and modality. Media needs
model, Adapter, and policy support; unknown is not unsupported or zero cost.
Fallback is opt-in and request-observed; warn before switching, block uncertain
effect replay, and meter every physical call.

Only typed Persona-packaged progress is user-facing; raw deltas and control
stages stay private, with final response authoritative. Scheduler and Superloop
writes use their typed tools, never direct state-file edits. Validate a Tool
batch before effects; malformed batches execute nothing and completed effects
never replay. Terminal receipts keep restricted originals separate from safe
projections. `/stop`, `/retry`, `/resend`, and `/steer` remain distinct;
recovery never restores revoked authority or accepts unknown effects.

## TUI, Workbench, and media

TUI renderers/catalogues own interface text. `/language` changes shared UI and
`/tui language` only local TUI; neither translates replies, IDs, commands,
paths, logs, or transcripts. Its highest scope is the selected instance:
switching binds generation, Agent, target, capabilities, logs, and sends, and
submission freezes them with the Session. Remote requires an authenticated
handshake; cached liveness grants nothing. Persist preferences only after
success.
Remote's optional `instances.json` compatibility view writes only when its
projected peer state changes; absent and empty optional fields compare equal.

`/telegram off` stops future Telegram mirrors for the owner from every frontend;
Telegram-origin conversations still receive their normal replies. `/whatsapp`
uses the same central rule for WhatsApp. `/think` controls provider reasoning
and `/commentary` explicit Engine commentary. Media are committed Session
assets bound to one draft, instance, Agent, and Run; Remote sends managed bytes,
references stay inside enabled Workzones, and uncertain writes never switch
routes. HER carries authorised manifests as native content or exact managed
references without widening authority. External adapters use Session Runs for
text, media, Canvas, approvals, and voice. Safe Voice requires typed
`voice_message` confirm/discard; missing idempotency fails before upload. Late
or cancelled media is discarded, and optional STT stays in an isolated sidecar.

`/voice` profile previews follow the same FC media boundary. Product previews
ship as a validated, versioned Function bundle, with instance-local media taking
precedence. Workbench receives a Session-bound `audio_attachment`; Telegram
alone uses its registered voice-message renderer. Never route a non-Telegram
preview through Telegram or copy the preview catalogue into a frontend.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md); `/help` derives
from metadata. Workbench and Telegram share a Session while UI rows stay out of
model history. Durable command reservation returns saved completions, never
replays pending/unknown, and does not overstate `not_observed` as delivery;
non-Telegram callbacks keep their own Connector fence.

An active Run does not close FC command ingress. Frontends must still submit a
typed slash command while the Agent is generating; the owning command decides
whether it applies immediately or rejects the busy state. Do not turn commands
into ordinary queued chat Runs, and do not duplicate a frontend-side safe-command
list. Local submission, cancellation, attachment, or draft conflicts remain
valid UI blockers.

`/new [title]` selects a fresh primary Session without deleting owner/Agent
history; it preserves Agent Workzones and the independently persisted recent-
turn/saved-memory switches. A successful Session boundary discards unfinished
`/long`, Safe Voice, Transfer, and pending Workzone path input; a failed Engine
reset changes none of them. Old messages stay read-only and attachments keep
their original Session. PAO
owns Agent deletion with preview, blockers, and cleanup receipts. Workbench
`/telegram` and `/whatsapp` persist per owner in FC; TUI and Backend API have no
separate mirror switches. A Run retains its admission-time destinations.

HASHI1 `main`, 2026-09-27: the user approved this centralized mirror behavior,
including normal origin-platform replies while mirroring is off. The Functions
source was committed and adopted by an authorized `/reboot max`. The receipt
confirmed a qualified new generation and all six Workers online. Workbench
`/telegram off` and `/whatsapp off` were read back through TUI's command path
as the same owner state. These were switch checks, not real chat delivery to
either external platform.

The same HASHI1 change makes release fallback explicit in broad reboot receipts
and Backend health, which Remote's existing degraded restart notice consumes.
Offline red/green and the curated Core gate passed. Cold `/restart` fallback
notification was checked by tests; no cold restart was performed for this fix.

## Move, Clone, jobs, and HCC

`/move` and `/clone` share package, registry, workspace, Scheduler, secret, and
lifecycle owners. Move removes verified source only after activation; Clone
preserves it, excludes Telegram credentials, and disables imported jobs.
`accepted` is not `completed`. See [Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler recurrence stores UTC instants, wall time, and IANA zone; legacy
unknown zones use UTC. Telegram recovery binds instance/lifecycle/Bot and
bounded retries honor `RetryAfter`.

Use authorized capabilities only; device actions need a same-instance Worker.
Prefer `log_query`; Agents work foreground unless `/bg` is explicit. Tests prove
scope, not live adoption; preserve user work and report failures.

HCC is optional, non-authoritative PCM context; `/hcc` and `hcc-refresh`
refresh sources; do not rewrite PCM or retry.

### FC terminal-only command boundary (2026-09-26)

`/logo` is TUI-only: hidden and denied on every other Connector, including
third parties, while normalized TUI admission remains available. Scope is
HASHI1 local `main`; source, offline proof, and live adoption remain separate.
See [Frontend Connector Architecture](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#terminal-only-slash-commands).
