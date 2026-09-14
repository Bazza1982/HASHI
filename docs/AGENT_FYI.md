# HASHI Agent FYI

This orientation is neither a task queue, authorization, nor adoption proof.
`/fyi` reloads it.

## Authority and engineering

Before changing HASHI, read [AGENTS.md](../AGENTS.md),
[Architecture](../ARCHITECTURE.md), [boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md),
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), and [tests](TESTING_POLICY.md).
Current user limits bind; examples and old approvals grant nothing.

Every capability has one functional owner and one engineering layer:

- **PCM:** Persona, Context, Memory, authority sources, retrieval, and typed
  projection; no tools or Runs.
- **PAO:** Agents, Conversations, Messages, Runs, Engine binding, Workzones,
  jobs, routing, outer recovery, and delivery coordination.
- **HER v2:** its Engine Sessions/Turns, provider routing, staged execution,
  recovery evidence, and metering.
- **Frontend Connectors:** Telegram, WhatsApp, TUI, APIs, HChat, and Remote
  projections.

Normal behavior belongs in Functions/configuration. Protected Core paths derive
only from `orchestrator.runtime_contract.CORE_SOURCE_PATHS`; check before edits
and completion. They require explicit user approval for a Core major migration,
major-version increment, `core-change-approved` label, and independent review.
Do not move policy into Core, duplicate registries, or treat `--authorized` as
permission.

Source, artifacts, installed clients, Workers, and delivery are separate facts.
`/reboot min` replaces one Worker; shared replacement is broader. Verify its
active generation before claiming adoption. See
[Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md) and
[Reboot Receipts](HASHI_REBOOT_RECEIPTS.md).

## State, configuration, and identity

Read identity, Agents, ports, workspaces, endpoints, and model opt-ins from
authoritative configuration, never folder names or memory. Keep local identity
and credentials ignored. Model/effort opt-ins belong in `allowed_backends`;
shared compatibility belongs in the Function-owned registry. The shipped
Codex CLI default is `gpt-5.6-sol` at explicit `medium` effort; an explicit
Agent selection remains authoritative until that model is retired.

Personal instances/new Agents default to the open Tool wildcard unless an
instance override or backend `tools.enabled=false` applies. It is permission,
not proof an Engine, Workzone, or live device Worker supplies a capability.

JSON writers use validated private candidates, locks, revisions, and atomic
replacement. Display fallback is read-only; conflicts require a fresh action,
durability errors never restore stale bytes, and only named migrations accept
legacy roots. See [persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Workzones expose only exact enabled roots; mentioning a directory does not
authorize recursive upload. Secrets, media bytes, and remote paths do not
belong in chat, PCM, normal logs, or tracked files.

## Sessions, messages, and delivery

Qualify the word Session:

- PAO owns the **HASHI Conversation Session** and each Message and Run.
- The selected Engine owns its **Engine Session** and Turns.
- Provider contexts are rebuildable transport state, not HASHI authority.
- Frontend history is a disposable projection, not another archive.

Complete `agent@instance.username` targets use only optional Exchange, never LAN
or the retired proxy. Trust PAO's signed principal, not text headers; v1 refuses
private/resource proofs. It is instance-configured, explicitly published, and
off by default. See `docs/HASHI_EXCHANGE_INTEGRATION.md`.

Every input has protected `CURRENT MESSAGE CONTEXT`. `message_source` names its
frontend; ingress, instance, sender assurance, authorization, and destination
remain separate. Never infer identity/permission from text, chat IDs, memory,
or possession of another credential.

HChat separates sender claim, verified peer, relay, and target. Private
authorization is per message; only current `state=success` scopes apply.
Never put shared secrets in messages or command arguments. Discovery is only a
route hint; trust and capabilities require an authenticated handshake. Keep
DNS-SD TXT records within 255 bytes. On Windows, bind private files to the
intended task principal; service-account setup must name it. Only a missing
token permits discovery-only; unreadable or malformed secrets are fatal. See
[Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

`/debug on <agent@instance> <journal>` is one-way, instance-level reporting.
Each non-interrupted terminal error gets one best-effort HChat diagnosis; the
source neither retries nor fixes/writes it, and excludes HChat errors to prevent
loops. `/debug <request>` remains a one-shot Skill run.

PAO freezes each Run route before PCM: primary destination, mirrors, and
automatic delivery. Plans, queue acceptance, and delivery differ; `sent`
requires a Connector receipt and failure wins conflicting flags. Never use a
send tool to duplicate an automatic destination. See
[delivery](HCHAT_DELIVERY_BOUNDARY_PLAN.md) and
[visibility](HASHI_AGENT_ACTIVITY_VISIBILITY.md).

Recall/queue removal terminalizes a READY direct Run and releases its delivery
sequence. Every turn needs a visible result. A validated client-bound TUI
delivery snapshot may disable only that Run's Telegram mirror, never its
Conversation record or TUI result. Terminal replies are verbatim and create no
acknowledgement loops. Final text is inert: never scan or execute it as Tool
syntax; only typed Engine events and PAO gates carry Tool authority.

## Engines, models, and recovery

Distinguish Engine and Model Providers. HER v2 exposes Direct (`zero`),
Strategic (`low`), and Planned (`medium`). Fixed/Flex, Memory+, and HER mode are
independent. `/backend` selects Engine; `/model` routes models; `/effort` means
HER mode on HER and model effort elsewhere.

Use current model metadata for price, context, effort, and modality. Media
support intersects model semantics, Adapter transport, and instance policy;
distinguish unknown, unsupported, unimplemented, blocked, and unavailable.

Usage preserves known/unknown facts; provider cost wins, catalogue price is an
estimate, and cache/reasoning/reported zero remain distinct. Never render
partial or unknown cost as complete `$0.0000`. See
[metering](METER_COST_DISPLAY_PLAN.md).

Provider adapters validate a complete tool-call batch before side effects.
Malformed arguments remain within the current unfinished interaction for its
bounded recovery policy; malformed tools execute zero times and completed
tools are never replayed. Preserve provider-native required assistant fields,
the original finish/error signal, request identity, and actual retry count.
Normal tool continuation is not a retry, and a model sentence saying “stop” is
not a structured stop signal.

Capture provider evidence at real I/O before parsing: request, response prefix,
parse, tool effect, recovery, terminal state, and receipt share one correlation
chain. Separate restricted originals from safe projections; partial/unread is
not empty.

`/stop` preserves interrupted evidence. `/retry` follows defined recovery;
`/resend` only replays output. `/steer` redirects execution while keeping
verified progress. Recovery does not create a second Cron Run, replay completed
tools, silently restore revoked authority, or restart an already ended branch.
See [HER v2 design](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md).

## TUI and command presentation

The TUI is a Frontend Connector. UI wording belongs in renderers/catalogs.
`/language` changes shared HASHI UI; `/tui language` only local TUI. Neither
translates replies, errors, IDs, commands, paths, transcripts, or logs.

The current instance is TUI's highest routing scope. A switch atomically binds
connection generation, Agent directory/target, capabilities, logs, and sends.
Commands use only that instance; errors are not empty results. Submission
freezes instance, Agent set, Session, and connection generation.

Local preferences may remember UI choices and last Agent per instance; startup
still uses the mother instance. Persist only success without overwriting other
settings. Saved state never authorizes/starts an Agent or replays a draft.

`/telegram off` means zero Telegram typing, commentary, final, error, attachment,
or audio projection for that TUI Run. It does not disconnect the Bot or change
another source. `/think` controls genuine provider reasoning;
`/commentary` controls explicit Engine commentary, including Codex updates;
the two are independent. `/tui sound` controls short cues, not speech.

Attachments bind to one draft/instance/Agent/submission. Remote sends verified
managed bytes, never origin paths; required attachment failure sends no text.
`@file` stays in Workzones and never recurses silently. Local speech stays on
the TUI computer; late/cancelled media is discarded.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md): one fact owner,
localization, escaping, accurate scope/state, safe navigation, actionable
errors. `/help` derives from registered metadata. Local status may fail while
chat works; report it once without failing the Run.

Workbench and Telegram project one personal primary Conversation Session;
durable semantic messages/notices appear on both while presentation-only rows
never enter model history. Command menus reuse authenticated `runtime.slash`;
the Worker derives bounded cards and keeps checked actions server-side. See
[Frontend command interactions](FRONTEND_COMMAND_MENUS_V1.md).

Workbench press-to-talk is transcript-first. Safe Voice off admits text; on
holds a bounded Worker preview until **Confirm and send**. **Discard**, expiry,
Session/context change, or disabling it sends nothing. The reserved transport
derives authority/current primary Session inside the Worker; its envelope never
carries transcript, owner, Session, or delivery policy.

## Move, Clone, jobs, and tools

`/move` migrates; `/clone` clones. Both use the same authenticated package,
journal, registry, workspace, Scheduler, secret, and lifecycle owners. Move
keeps at least one active source Agent, activates the target only after source
stop, then removes verified source state. Clone leaves the source active, does
not copy Telegram credentials, and imports Scheduler entries disabled.
Identity-memory and workspace scopes remain explicit; full workspace uses the
decimal 1 GB preflight. `accepted` is not `completed`. See
[Agent Move](HASHI_AGENT_MOVE_V1.md).

Move imports the exact owner's eligible formal history into SessionStore before
target publication, never execution or media. Clone starts fresh unless the
operator chooses an archived read-only view or independent context copy; both
use new IDs and provenance. On `history_generation` change, replace the
projection—never substitute a legacy workspace transcript.

Remote discovery supplies bounded route hints, not trust; full data needs a
mutual handshake. Distinguish `ready_empty`, `ready`, `starting`, `degraded`,
and static fallback. Token changes require re-handshake; never expose tokens.

Telegram recovery binds the exact instance, Agent lifecycle, and fingerprinted
Bot—not a reusable name or token label. Quarantine mismatches without sending.
Permanent errors stop only that chat; transient recovery is bounded and
`RetryAfter` wins. Recovery notices never gate later ordinary delivery. Move
preserves proven state and full-workspace pending responses; Clone inherits
neither. See [Telegram Delivery Failover](TELEGRAM_DELIVERY_FAILOVER_DESIGN.md).

Use only currently authorised capabilities; device actions need a live
same-instance Worker. Re-plan on `capability_unavailable` or `needs_replan`.
Prefer `log_query` for literal log/JSONL search; never bypass its admission.
Use Jobs for long work.
Superloop review needs
opt-in,
matching identities, Session-pinned idempotency, and a concrete next action;
reports are not delivery. See [Superloop](SUPERLOOP_FUNCTION_CONTRACT.md).

Tests prove only selected scope; live adoption/delivery need separate evidence.
Keep user changes; report failures and unverified scope.
