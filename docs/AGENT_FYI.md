# HASHI Agent FYI

This compact orientation is neither a task queue, authorization, nor proof of
runtime adoption. `/fyi` reloads it with a revision; details remain in linked
owner documents.

## Authority and engineering

Before changing HASHI, read [AGENTS.md](../AGENTS.md),
[Architecture](../ARCHITECTURE.md), [boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md),
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), and [test policy](TESTING_POLICY.md).
Current user limits remain binding; examples and old approvals grant nothing.

Every capability has one functional owner and one engineering layer:

- **PCM** owns Persona, Context, Memory, authority sources, retrieval, and typed
  projection; it grants no tools and owns no Runs.
- **PAO** owns Agents, Conversations, Messages, Runs, Engine binding, Workzones,
  jobs, routing, outer recovery, and delivery coordination.
- **HER v2** is an Engine. It owns HER Engine Sessions and Turns, internal Model
  Provider routing, staged execution, recovery evidence, and metering.
- **Frontend Connectors** expose Telegram, WhatsApp, TUI, APIs, HChat, and
  authenticated Remote projections.

Normal behavior belongs in replaceable Functions or configuration. Protected
Core paths derive only from `orchestrator.runtime_contract.CORE_SOURCE_PATHS`;
check before choosing files and completion. Do not move policy into Core,
duplicate registries, or use `--authorized` without explicit permission.

Source, qualified artifacts, installed clients, running Functions/Workers, and
delivery are separate facts. `/reboot min` replaces one Worker; shared Function
replacement is broader. Verify the active generation before claiming adoption.
See [Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md) and
[Reboot Receipts](HASHI_REBOOT_RECEIPTS.md).

## State, configuration, and identity

Read identities, Agents, ports, workspaces, endpoints, and model opt-ins from
their authoritative configuration; never infer them from a folder name or old
memory. Local identity and credentials stay in ignored instance stores.
Model/effort opt-ins belong in `allowed_backends`; shared compatibility belongs
to the Function-owned backend registry.

HASHI JSON writers use revision-aware persistence: compatible reads, UTF-8/LF,
private validated candidates, locking, revision checks, synchronization, and
atomic replacement. Display fallback is never writable state. Conflicts require
a fresh operation, not blind retry; create-only publication proves absence.
Only designated migrations accept legacy root arrays. Never restore stale bytes
after a durability error. See [persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Workzones expose only exact enabled roots; mentioning a directory does not
authorize recursive upload. Secrets, media bytes, and remote paths do not
belong in chat, PCM, normal logs, or tracked files.

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

HChat separates sender claim, verified peer, relay, and target. Private
authorization is per message; only current `state=success` scopes apply.
Never put the shared secret in message text or command arguments.

PAO determines one immutable Run route before PCM projection. The route states
the primary destination, mirrors, and whether ordinary reply delivery is
automatic. A plan to deliver, queue acceptance, and actual delivery are
different facts. `sent` requires a Connector receipt; failure wins over
contradictory success flags. When the current reply already has an automatic
destination, do not use a send tool to duplicate it. See
[delivery](HCHAT_DELIVERY_BOUNDARY_PLAN.md) and
[visibility](HASHI_AGENT_ACTIVITY_VISIBILITY.md).

Every turn needs a visible formal result. A validated, client-bound TUI
`hashi.frontend-delivery` v1 snapshot may disable one Run's Telegram mirror,
never its Conversation record or TUI result. Terminal replies are verbatim and
must not create acknowledgement loops.

## Engines, models, and recovery

Distinguish Engine and Model Providers. HER v2 exposes Direct (`zero`),
Strategic (`low`), and Planned (`medium`). Fixed/Flex, Memory+, and HER mode are
independent. `/backend` selects Engine; `/model` routes models; `/effort` means
HER mode on HER and model effort elsewhere.

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

Capture provider evidence at real I/O before parsing/raising: physical request,
response/stream prefix, parse decision, tool effect, recovery, terminal state,
and Connector receipt share one correlation chain. Keep restricted originals
apart from safe projections and exclude secrets. An unread/partial body is not
an empty response.

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

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md): one fact owner,
localization, escaping, accurate scope/state, safe navigation, actionable
errors. `/help` derives from registered metadata. Local status may fail while
chat works; report it once without failing the Run.

Workbench command menus reuse the authenticated Backend API command endpoint
and route a versioned `command_ui` envelope to the selected Agent Worker.
The Worker projects existing registered commands and callbacks into bounded
cards with opaque action IDs, current-policy, binding, revision and expiry
checks; raw callback data stays server-side. Projection and replay state are
memory-only, and governed profiles remain unsupported in v1. See
[Frontend command interactions](FRONTEND_COMMAND_MENUS_V1.md).

## Move, Clone, jobs, and tools

`/move` migrates; `/clone` clones. Both use the same authenticated package,
journal, registry, workspace, Scheduler, secret, and lifecycle owners. Move
keeps at least one active source Agent, activates the target only after source
stop, then removes verified source state. Clone leaves the source active, does
not copy Telegram credentials, and imports Scheduler entries disabled.
Identity-memory and workspace scopes remain explicit; full workspace uses the
decimal 1 GB preflight. `accepted` is not `completed`. See
[Agent Move](HASHI_AGENT_MOVE_V1.md).

Telegram delivery recovery belongs to an exact instance, Agent lifecycle ID,
and fingerprinted Bot identity—not a reusable Agent name or token-key label.
Legacy or mismatched records are quarantined without sending. Permanent
destination errors stop only that chat; transient recovery is bounded and
`RetryAfter` remains authoritative. A recovery notice is bookkeeping, never a
gate on ordinary delivery after the wait expires. Move preserves proven state
and full-workspace pending responses; Clone inherits neither. See
[Telegram Delivery Failover](TELEGRAM_DELIVERY_FAILOVER_DESIGN.md).

Use only tools/skills exposed for this turn; optional capabilities also require
current authority. Device tools appear only while a same-instance Worker has an
unexpired matching action. If it disappears, re-plan from typed
`capability_unavailable`; do not retry blindly. Prefer `web_fetch` for public
documents, but it cannot replace JavaScript, login state, or interaction. Use
HASHI Jobs for long processes.
Superloop receipt review needs
opt-in, matching identities, Session-pinned idempotency, and a concrete next
action for every unresolved check. Reports are not delivery. See
[Superloop](SUPERLOOP_FUNCTION_CONTRACT.md).

Tests prove only their selected scope. Prefer focused red/green evidence and
direct consumers; live canaries need explicit authority. Never claim a live
feature from a source file, documentation, unit test, saved setting, or old
transcript. Preserve unrelated checkout changes and report failures, skips,
unverified platforms, and adoption separately.
