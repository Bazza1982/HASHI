# HASHI Agent FYI

This orientation is neither a task queue, authorization, nor adoption proof.
`/fyi` reloads it.

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
check before choosing files and completion. Treat them as immutable unless the
current user explicitly authorizes a Core major-version migration. Such a
migration also needs a major-version increment, the `core-change-approved`
label, and a matching independent review record. Do not move policy into Core,
duplicate registries, or treat `--authorized` as permission.

Source, artifacts, installed clients, running Workers, and delivery are separate
facts. `/reboot min` replaces one Worker; shared replacement is broader. Verify
the active generation before claiming adoption. See
[Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md) and
[Reboot Receipts](HASHI_REBOOT_RECEIPTS.md).

## State, configuration, and identity

Read identities, Agents, ports, workspaces, endpoints, and model opt-ins from
their authoritative configuration; never infer them from a folder name or old
memory. Local identity and credentials stay in ignored instance stores.
Model/effort opt-ins belong in `allowed_backends`; shared compatibility belongs
to the Function-owned backend registry.

Personal instances and new Agents default to the open HASHI Tool wildcard when
no instance override is declared. Explicit instance restrictions and backend
`tools.enabled=false` remain authoritative. A wildcard is permission, not proof
that an Engine, Workzone, or live Browser/Computer Worker supplies a capability.

HASHI JSON writers use validated private candidates, locks, revisions, and
atomic replacement. Display fallback is never writable; conflicts require a
fresh operation, and durability errors never restore stale bytes. Only named
migrations accept legacy roots. See
[persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Workzones expose only exact enabled roots; mentioning a directory does not
authorize recursive upload. Secrets, media bytes, and remote paths do not
belong in chat, PCM, normal logs, or tracked files.

## Sessions, messages, and delivery

Qualify the word Session:

- PAO owns the **HASHI Conversation Session** and each Message and Run.
- The selected Engine owns its **Engine Session** and Turns.
- Provider contexts are rebuildable transport state, not HASHI authority.
- Frontend history is a disposable projection, not another archive.

Complete `agent@instance.username` targets use only the optional independent
Exchange client. They never fall back to LAN or the retired proxy path. Trust
only PAO's signed Exchange principal, not a matching text header; Exchange v1
refuses private/resource proofs rather than downgrading them. The feature is
instance-configured, explicitly published, and disabled by default. See
`docs/HASHI_EXCHANGE_INTEGRATION.md`.

Every admitted input has a protected `CURRENT MESSAGE CONTEXT` projection.
`message_source` describes the initiating frontend; ingress transport,
processing instance, sender assurance, authorization, and output destination
remain separate. Do not infer identity or permission from message text, chat
IDs, a remembered prior Turn, or possession of some other credential.

HChat separates sender claim, verified peer, relay, and target. Private
authorization is per message; only current `state=success` scopes apply.
Never put shared secrets in messages or command arguments. Discovery is only a
route hint; trust and capabilities require an authenticated handshake. Keep
DNS-SD TXT records within 255 bytes. On Windows, bind private files to the
intended task principal; service-account setup must name it. Only a missing
token permits discovery-only; unreadable or malformed secrets are fatal. See
[Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

`/debug on <agent@instance> <journal>` is an instance-level, one-way failure
reporting preference. Each non-interrupted terminal error gets one best-effort
HChat diagnosis assignment; the source does not queue, retry, await a receipt,
write the journal, de-duplicate diagnoses, or fix the issue. HChat-origin errors
are excluded to prevent loops. `/debug <request>` remains a one-shot Skill run.

PAO determines one immutable Run route before PCM projection. The route states
the primary destination, mirrors, and whether ordinary reply delivery is
automatic. A plan to deliver, queue acceptance, and actual delivery are
different facts. `sent` requires a Connector receipt; failure wins over
contradictory success flags. When the current reply already has an automatic
destination, do not use a send tool to duplicate it. See
[delivery](HCHAT_DELIVERY_BOUNDARY_PLAN.md) and
[visibility](HASHI_AGENT_ACTIVITY_VISIBILITY.md).

Removing a READY direct request through recall or queue control terminalizes
its Session Run and releases its per-chat delivery sequence so later replies
cannot wait forever behind work that will never execute.

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

Adapters validate a tool batch before effects; malformed calls execute
zero times and completed calls never replay. Bounded repair preserves
Provider fields, finish/error, identity and retry count. DeepSeek uses native
calls; only complete official V3.2/V4/V4.1 DSML with advertised,
schema-valid tools may be recovered. Degraded intent is hidden and
must obtain native repair; prose cannot complete it. Continuation is not retry,
and model text saying “stop” is not a structured stop.

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

Workbench command menus reuse authenticated `runtime.slash`; the Worker derives
bounded cards from registered commands and keeps opaque, policy/binding/revision
checked actions server-side. See
[Frontend command interactions](FRONTEND_COMMAND_MENUS_V1.md).

Workbench press-to-talk is transcript-first. Safe Voice off admits the local
transcript as ordinary user text. Safe Voice on holds it in the selected Worker
for a bounded preview: only **Confirm and send** admits a request; **Discard**,
expiry, Session/context change, or turning Safe Voice off leaves no model
request. The reserved confirmation transport derives authority and the current
`workbench/default` Session inside the Worker; never put transcript text,
owner, Session or delivery policy in its command envelope.

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
