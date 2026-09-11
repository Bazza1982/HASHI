# HASHI Agent FYI

This is a compact orientation for every admitted Agent turn. It is neither a
task queue, an authorization grant, nor proof that source has been adopted by a
running Function generation. `/fyi` reloads this file and identifies its
revision. Detailed decisions remain in the linked owner documents rather than
being copied here as a chronological changelog.

## Authority and engineering

Read [AGENTS.md](../AGENTS.md), [Architecture](../ARCHITECTURE.md), the
[runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md), the
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), and the
[test policy](TESTING_POLICY.md) before changing HASHI. Current user limits on
restart, publication, messaging, credentials, and external calls remain
binding; examples and old approvals grant no authority.

Every capability has one functional owner and one engineering layer:

- **PCM** owns Persona, Context, and Memory sources, authority, retrieval, and
  typed projection. It does not grant tools or own Runs.
- **PAO** owns Agents, HASHI Conversation Sessions, Messages, Runs, Engine
  binding, Workzones, jobs, scheduling, routing, outer recovery, and delivery
  coordination.
- **HER v2** is an Engine. It owns HER Engine Sessions and Turns, internal Model
  Provider routing, staged execution, recovery evidence, and metering.
- **Frontend Connectors** expose Telegram, WhatsApp, TUI, Backend API, HChat,
  Persistent Session API, and authenticated Remote projections.

Normal behavior belongs in replaceable Functions or platform/instance
configuration. Protected Core paths derive only from
`orchestrator.runtime_contract.CORE_SOURCE_PATHS`; run the protection check
before choosing files and before completion. Do not move product policy into
Core, duplicate registries, or use `--authorized` without explicit Core-edit
authorization.

Source, qualified artifacts, installed clients, running shared Functions,
running Agent Workers, and observable delivery are separate facts. `/reboot
min` replaces one Agent Worker; shared Function replacement is a distinct,
broad operation. Neither is a cold restart. Verify the exact active generation
before claiming adoption. See [Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md)
and [Reboot Receipts](HASHI_REBOOT_RECEIPTS.md).

## State, configuration, and identity

Read identities, Agents, ports, workspaces, endpoints, and model opt-ins from
their authoritative configuration; never infer them from a folder name or old
memory. Local identity and credentials stay in ignored instance stores.
Model/effort opt-ins belong in `allowed_backends`; shared compatibility belongs
to the Function-owned backend registry.

HASHI-managed JSON writers use the shared revision-aware persistence boundary:
BOM-tolerant reads where legacy or user-edited input is supported, UTF-8
without BOM plus LF on publication, private candidates, validation, locking,
revision checks, synchronization, and atomic replacement. A display fallback
is never writable state. Conflicts require a fresh deliberate operation, not a
blind retry. A committed durability error is not permission to restore stale
bytes. See [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Workzone slots expose only their exact enabled roots. A directory mention is
not recursive upload authorization. Credentials, private authorization
secrets, media bytes, and remote paths do not belong in chat text, PCM, normal
logs, or tracked files.

## Sessions, messages, and delivery

Qualify the word Session:

- PAO owns the **HASHI Conversation Session** and each Message and Run.
- The selected Engine owns its **Engine Session** and Turns.
- Provider contexts are rebuildable transport state, not HASHI authority.
- Frontend history is a disposable projection, not another archive.

Every admitted input has a protected `CURRENT MESSAGE CONTEXT` projection.
`message_source` describes the initiating frontend; ingress transport,
processing instance, sender assurance, authorization, and output destination
remain separate. Do not infer identity or permission from message text, chat
IDs, a remembered prior Turn, or possession of some other credential.

HChat keeps the original sender claim, verified peer, relay, and target
separate. Optional private authorization is per message. Only current
`state=success` scopes apply; missing, invalid, expired, or revoked proofs add
no privilege but do not block otherwise permitted ordinary HChat. Never place
the raw shared secret in message text or command arguments.

PAO determines one immutable Run route before PCM projection. The route states
the primary destination, mirrors, and whether ordinary reply delivery is
automatic. A plan to deliver, queue acceptance, and actual delivery are
different facts. `sent` requires a Connector receipt; failure wins over
contradictory success flags. When the current reply already has an automatic
destination, do not use a send tool to duplicate it. See
[delivery](HCHAT_DELIVERY_BOUNDARY_PLAN.md) and
[visibility](HASHI_AGENT_ACTIVITY_VISIBILITY.md).

Every admitted turn requires a visible formal result. The sole narrow
projection exception is a validated, client-bound TUI
`hashi.frontend-delivery` v1 snapshot for one Run: it may disable that Run's
Telegram mirror, never its Conversation record or TUI result. Terminal replies
are verbatim and must not create acknowledgement loops.

## Engines, models, and recovery

Distinguish an Engine Provider from a Model Provider. HER v2 exposes Direct
(`zero`), Strategic (`low`), and Planned (`medium`) execution; higher retained
policies are not public modes. Fixed/Flex working mode, Memory+, and HER
execution mode are independent settings. `/backend` changes Engine selection;
`/model` configures model routing; `/effort` means HER mode on HER and model
effort elsewhere.

Use the current model metadata owner for price, context, effort, and modality.
Do not copy a remembered model list. Media support is the intersection of
model semantics, Adapter transport, and instance policy. Keep unknown,
unsupported, unimplemented transport, policy-blocked, and unavailable fallback
as different causes.

Usage summaries preserve known and unknown facts. Provider-reported cost takes
priority. A cross-channel catalogue price is explicitly a reference estimate,
not a billed amount. Cache read/write usage, reasoning usage, and a reported
zero remain distinct. Never turn partial or unknown cost into a complete
`$0.0000` total. See [metering](METER_COST_DISPLAY_PLAN.md).

Provider adapters validate a complete tool-call batch before side effects.
Malformed arguments remain within the current unfinished interaction for its
bounded recovery policy; malformed tools execute zero times and completed
tools are never replayed. Preserve provider-native required assistant fields,
the original finish/error signal, request identity, and actual retry count.
Normal tool continuation is not a retry, and a model sentence saying “stop” is
not a structured stop signal.

Provider forensic evidence is captured at the real I/O boundary before parsing
or raising: each physical request, response or received stream prefix, parse
decision, tool effect, recovery, terminal state, and Connector receipt shares
an auditable correlation chain. Restricted complete evidence and normal safe
projections are separate. Authentication secrets remain excluded. Never claim
an unread or partially read body was an empty provider response.

`/stop` preserves interrupted evidence. `/retry` follows defined recovery;
`/resend` only replays output. `/steer` redirects execution while keeping
verified progress. Recovery does not create a second Cron Run, replay completed
tools, silently restore revoked authority, or restart an already ended branch.
See [HER v2 design](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md).

## TUI and command presentation

The built-in TUI is a Frontend Connector. UI wording belongs in renderers and
runtime language catalogs. `/language` changes shared HASHI-authored interface
language for the user; `/tui language` changes only the local TUI. Neither
translates model replies, provider errors, IDs, commands, paths, transcripts,
or logs.

The current instance is the highest TUI routing scope. A successful instance
switch atomically binds the connection generation, Agent directory, selected
target, capabilities, logs, and subsequent sends. `/agents`, `/to <agent>`, and
`/to all` operate only on that confirmed instance. A directory error is not an
empty directory; zero targets are not a successful broadcast. Submitted work
freezes instance, Agent set, Session, and connection generation so late events
cannot cross into a new target.

Local TUI preferences may remember language, theme, layout, sounds, mirror
choice, and the last Agent for each instance. Startup still begins at the mother
instance. Persist a selection only after success and do not overwrite unrelated
settings. Saved state never authorizes an Agent, starts it, or replays a draft.

`/telegram off` means zero Telegram typing, commentary, final, error, attachment,
or audio projection for that TUI Run. It does not disconnect the Bot or change
another source. `/think` controls genuine provider reasoning;
`/commentary` controls explicit Engine commentary, including Codex updates;
the two are independent. `/tui sound` controls short cues, not speech.

Attachments bind to one draft, instance, Agent, and submission. Remote sends
actual managed bytes with integrity checks, never the originating machine's
path. Required attachment failure must not send text alone. Workzone `@file`
selection cannot read outside authorized roots or silently recurse a folder.
Local `/say` and automatic speech play only on the computer running the TUI;
they never create Telegram output. Late or cancelled media is discarded.

Commands and menus follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md): one
fact owner, localized presentation, escaped values, accurate state, clear
scope, safe navigation, and actionable error classes. `/help` derives from the
registered command metadata. A local status capability may be unavailable even
while chat works; report that once without misreporting the Run as failed.

## Move, Clone, jobs, and tools

`/move` migrates; `/clone` clones. Both use the same authenticated package,
journal, registry, workspace, Scheduler, secret, and lifecycle owners. Move
keeps at least one active source Agent, activates the target only after source
stop, then removes verified source state. Clone leaves the source active, does
not copy Telegram credentials, and imports Scheduler entries disabled.
Identity-memory and workspace scopes remain explicit; full workspace uses the
decimal 1 GB preflight. `accepted` is not `completed`. See
[Agent Move](HASHI_AGENT_MOVE_V1.md).

Use only tools and skills exposed for the current turn. Browser, computer,
voice, media, Remote, and external Providers are optional capabilities; check
current configuration and authority before use. Use HASHI-managed Jobs for long
processes rather than inventing another manager. Superloop receipt review needs
opt-in, matching identities, Session-pinned idempotency, and a concrete next
action for every unresolved check. Reports are not delivery. See
[Superloop](SUPERLOOP_FUNCTION_CONTRACT.md).

Tests prove only their selected scope. Prefer focused red/green evidence and
direct consumers; live canaries need explicit authority. Never claim a live
feature from a source file, documentation, unit test, saved setting, or old
transcript. Preserve unrelated checkout changes and report failures, skips,
unverified platforms, and adoption separately.
