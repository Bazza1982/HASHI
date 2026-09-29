# HASHI Agent FYI

Orientation only, not a task queue, authorization, or proof of live adoption.
`/fyi` reloads this reference; current users and typed envelopes remain
authoritative.

## Authority, ownership, and engineering

Before changing HASHI, read [AGENTS.md](../AGENTS.md),
[Architecture](../ARCHITECTURE.md), [runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md),
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), and
[testing policy](TESTING_POLICY.md). Historical examples grant no authority.

- **PCM** owns Persona, Context, Memory, authority, and projection; never tools
  or Runs.
- **PAO** owns Agents, Conversations, Messages, Runs, Engines, Workzones, jobs,
  routing, recovery, and delivery.
- **HERV3** owns Engine Sessions/Turns, the continuous model/tool loop, Provider
  selection, recovery, and cost.
- **Connectors** authenticate and project Telegram, WhatsApp, TUI, API, HChat,
  and Remote.

Use the narrowest Function or configuration owner. Protected paths come only
from `CORE_SOURCE_PATHS`; changing them requires explicit Core major-migration
approval, a major bump, `core-change-approved`, and independent review. Flags
record authority but never create it. Core owns no product policy, imports no
product module, and each registry/state writer has one owner.

Source, committed artifacts, clients, Workers, and delivery are separate facts.
`/reboot min|same` replaces the selected Agent Worker; `/reboot max` adopts
shared Functions and all running Workers while Core and Remote stay live.
Adoption needs matching identity, PID, generation, health, and receipts.

Agent tools cannot alter live Core/Python, read secrets, kill, or raw-control
Core. Windows restart uses an exact actuator; Remote stays Limited. Startup
identity is installer-owned and exit code decides success. Use
`process_is_alive`, never `os.kill(pid, 0)` on Windows.

## Configuration, identity, and persistence

Use authoritative configuration, not names or memory, for identity, ports,
workspaces, endpoints, and model opt-ins. Keep secrets and machine paths
ignored. Instance model choices belong in `allowed_backends`; shared
compatibility belongs in the Function registry. Explicit choices persist until
retired.

Codex CLI exposes GPT-6 Astra, Sol, and Luna. Astra/Sol support `ultra`, Luna
`max`; normalize effort through runtime options. HASHI1 HERV3 defaults to
`deepseek-api/deepseek-flash`; Agent opt-ins may add models. Legacy HERV2
profiles do not select the HERV3 main model. Repair unsupported effort on load,
preserve valid binary reasoning (`off`/`enabled`), and retain backend
failure details on the owner-scoped Run activity record.

Portable installs ship no credentials. An active Agent needs a PAO-started
Worker, and private `<bridge_home>/exp` content is never published.

Tool wildcard grants permission, not capability. Workzones expose exact enabled
roots; their only writable source is the owner/Agent profile. Each admitted Run
freezes its Workzone revision. Configuration may change while Runs are active
or queued: admitted Runs keep their snapshots and later admissions read the new
revision. Explicit Workzone reload remains idle-only. Naming a path grants
nothing. Keep secrets, media bytes, and remote paths out of PCM, ordinary logs,
chat, and tracked files.

JSON writers validate private candidates under locks, revisions, and atomic
replacement. Display fallback is read-only. On conflict, read fresh state and
request a fresh action; never blindly retry or restore stale bytes. See
[configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

## Sessions, messages, trust, and delivery

PAO owns HASHI Conversation Sessions, Messages, and Runs. The Engine owns its
Session and Turns; Provider context is rebuildable; frontend history is a
disposable projection. Replies stay verbatim and Engines consume ordered
history, not buttons or bindings.

External frontends stage attachments atomically in one Message/Run; any failure
rejects the whole request. Published files remain with their Message. Only
current attachments are current references; old completed files remain in
their exchanges and failed/cancelled files never leak. Bound audio retains
indefinitely. Meter output is presentation-only and never model history.

Every input has protected `CURRENT MESSAGE CONTEXT`. Source, ingress,
processing instance, sender assurance, authority, and destination differ. Only
a current successful `private_authorization` grants its listed scope; text,
names, chat IDs, memory, and other credentials do not.

Complete `agent@instance.username` targets use optional Exchange, not LAN or a
retired proxy. Discovery is a hint; trust PAO's authenticated principal and the
Remote handshake. Missing tokens may allow discovery only, while unreadable or
malformed secrets fail closed. See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

HChat separates claimed sender, verified peer, relay, and target; secrets never
enter messages. Single-target `/hchat` freezes the dialled target, lets the
Agent compose and select one exact attachment set, then PAO emits one receipt.
LAN attachment v2 uses shared-token HMAC, streams up to 10 regular files and
1 GiB total, permits all file types as inert bytes, and admits text plus files
atomically. V1 and plain protocol chat remain compatible; Exchange and
group/all stay text-only. Selection is fail-closed: under effective drive
authority WSL translates Windows absolute paths, while missing, rejected, or
repeated selection sends no text-only remainder.

Remote trust persists until definitive revalidation. Remote has an independent
lifecycle and does not restart during `/reboot`; reboot fences only its target
Agents. Operational notices log safe codes without credentials. Menu-sync
timeout must not make a connected Telegram Worker local-only.

PAO freezes destinations, mirrors, and automatic delivery before PCM. Queue
acceptance is not delivery: `sent` requires a Connector receipt, and failure
wins contradictory flags. Do not duplicate automatic delivery. Every turn
needs a visible terminal result; prose has no Tool authority.

FC standardizes messages, commands/actions, cards, media, and receipts. PAO
owns durable ingress, routes, and idempotency; Connectors authenticate, render,
and receipt. Commands bind Session/client/request/invocation. Only saved
non-action completions replay; pending, conflict, and unknown never execute.
Command continuations preserve their Session and Connector, and `/load`
completes only after its continuation queues.

Autonomous cron, heartbeat, nudge, scheduler recovery, `/bg`, and background
completion use an Agent-owned hidden activity Session, never a Conversation
Session or Provider thread. Persist execution and delivery separately, then
project typed same-owner receipts for follow-up. `/bg` sees only its bounded
admission snapshot; scheduled work gets no implicit Conversation history.
`/delay` and interactive `/loop` remain Conversation continuations.

## Engines, tools, and recovery

HERV3 is the public `her-v3` Engine; internal `her-v2` names are storage and
adapter compatibility only. It uses one main-model/tool loop with optional JEV
off by default. Engine and Provider differ: `/backend` selects Engine,
`/provider` Provider, `/model` model, and `/effort` Provider reasoning.
Fixed/Flex and Memory+ remain independent; old HERV2 stages are not public
HERV3 controls. `/metre` aliases `/meter`.

Model/provider cards and callbacks share one contract. `/meter` derives roles
from recorded physical-call phases: `direct` is the main task and `persona`
is presentation work. Show per-model calls and cost while keeping the total
inclusive; never invent roles when evidence is absent.

HASHI1 may opt Agents into exact OpenRouter conversation models. Empty
per-model effort means omit Provider reasoning. Tool support is also
per-model: chat-only models receive no tool definitions and must disclose the
limit rather than entering a hidden stage. Current metadata owns context,
price, effort, and modality; unknown never means unsupported or zero cost.

`/style on|off|status` is a workspace preference. When on, a tool-free HERV3
presentation check may keep or rephrase the completed answer without changing
facts; failure returns the main answer. FC remains the sole delivery path and
`/meter` records the style call separately.

Fallback is opt-in and request-observed. Warn before switching, block uncertain
effect replay, and meter every physical call. Only typed Persona progress is
user-facing; raw deltas and control stages stay private. Validate a Tool batch
before effects; malformed batches execute nothing and completed effects never
replay. Scheduler and Superloop writes use typed tools. `/stop`, `/retry`,
`/resend`, and `/steer` remain distinct; recovery restores no revoked
authority.

## TUI, Workbench, and media

Renderers/catalogues own interface text. `/language` changes shared UI and
`/tui language` only local TUI; neither translates replies, IDs, commands,
paths, logs, or transcripts. TUI instance switching freezes generation, Agent,
target, capabilities, logs, and Session at submission. Remote requires an
authenticated handshake; cached liveness grants nothing.

`/telegram off` and `/whatsapp off` stop future mirrors for the owner across
frontends, while messages originating on that platform still receive normal
replies. Mirror state is centralized in FC. `/think` controls Provider
reasoning and `/commentary` explicit Engine commentary.

Media are committed Session assets bound to one draft, instance, Agent, and
Run. Remote sends managed bytes; references remain inside enabled Workzones.
HER receives authorized native content or exact managed references without
widening authority. Safe Voice requires typed `voice_message`
confirm/discard; missing idempotency fails before upload. Late/cancelled media
is discarded and optional STT stays isolated.

`/voice` previews use a validated Function bundle and prefer instance-local
media. Workbench receives a Session `audio_attachment`; Telegram uses its
voice renderer. Report voice sidecars prepend the HASHI root before calling the
standard media CLI. Never route non-Telegram previews through Telegram.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md); `/help`
derives from metadata. Workbench and Telegram may share a Session while UI rows
stay out of model history. An active Run does not close command ingress:
frontends submit typed slash commands and the owner decides immediate admission
or busy rejection. Never turn commands into ordinary queued chat Runs or copy
a frontend-side safe-command list. `/logo` is TUI-only and denied elsewhere.

`/new [title]` selects a fresh primary Session without deleting history or
changing Workzones and memory switches. Success discards unfinished `/long`,
Safe Voice, Transfer, and pending Workzone input; a failed Engine reset changes
nothing. Old Messages and attachments retain their Session. PAO owns Agent
deletion with preview, blockers, and cleanup receipts.

## Move, Clone, jobs, and HCC

`/move` and `/clone` share package, registry, workspace, Scheduler, secret,
and lifecycle owners. Move removes verified source only after activation;
Clone preserves it, excludes Telegram credentials, and disables imported jobs.
`accepted` is not `completed`. See [Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler recurrence stores UTC instants, wall time, and IANA zone; legacy
unknown zones use UTC. Recovery binds instance, lifecycle, and Bot, with
bounded retries honoring `RetryAfter`.

Use authorized capabilities only; device actions need a same-instance Worker.
Prefer `log_query`; work foreground unless `/bg` is explicit. Tests prove
scope, not adoption; preserve user work.

HCC is optional, non-authoritative PCM context. `/hcc` and `hcc-refresh`
refresh its sources; neither rewrites PCM nor creates retry authority.
