# HASHI Agent FYI

Orientation only. This is not a task queue, authorization, or proof of live adoption. /fyi reloads it. Current users and typed envelopes remain authoritative. Detailed rules live in [AGENTS.md](../AGENTS.md), [Architecture](../ARCHITECTURE.md), [runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md), [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), and [testing policy](TESTING_POLICY.md).

## Authority and ownership

PCM owns Persona, Context, Memory, authority order, and projections. PAO owns Agents, Conversations, Messages, Runs, routing, jobs, Workzones, recovery, and delivery. HERV3 owns Engine Sessions and Turns, its model/tool loop, Provider selection, and cost. Frontend Connectors authenticate and render transports. Use the narrowest Function or configuration owner. Core has no product policy or imports. Protected Core changes require explicit major-migration authorization, a major bump, the core-change-approved label, and independent review; flags only record authority already given.

Source, commits, artifacts, running Workers, and terminal delivery are different facts. Verify each claim separately. Agent tools cannot alter live Core/Python, read secrets, kill Core, or grant themselves authority. /reboot min|same replaces one Agent Worker; /reboot max adopts shared Functions and running Workers while Core and Remote remain live. Adoption requires matching identity, generation, health, and receipts. Follow the current instance's reboot authority and idle window. Windows restart uses the exact actuator and exit code; use process_is_alive for process checks.

## Configuration, identity, and persistence

Read identity, ports, Workzones, endpoints, and model choices from authoritative configuration. Never infer them from a folder name. Keep secrets and machine paths ignored. Instance model opt-ins belong in allowed_backends and resolve through runtime effort options; shared compatibility belongs in the qualified Function registry. Explicit choices persist until retired. Current model, price, effort, and modality metadata own their facts; unknown is neither unsupported nor zero cost.

Portable installs carry no credentials. An active Agent needs a PAO-started Worker. Private bridge-home experiment content is not published. Tool wildcard grants permission, not capability. Workzones expose exact enabled roots; each admitted Run freezes its Workzone revision. Later admissions see later revisions, and explicit reload stays idle-only. Naming a path grants nothing. Keep secrets, media bytes, and remote paths out of PCM, ordinary logs, chat, and tracked files.

JSON writers validate private candidates under locks, revisions, and atomic replacement. Display fallback is read-only. On conflict, read fresh state and request a fresh action; never blindly retry or restore stale bytes. See [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

## Sessions, trust, and delivery

2026-10-02 HASHI3 repair: Codex MCP inventory uses the same disabled-plugin state
as execution; trusted Telegram media and `/long` submission preserve ingress
identity. This prevents transport-less plugin overrides and media admission
rejection. Source validation and live adoption are recorded separately in
`docs/HASHI3_MCP_MEDIA_REPAIR_2026-10-02.md`; no HASHI4 adoption is implied.

PAO owns HASHI Conversation Sessions, Messages, and Runs. Engines own their Sessions and Turns. Provider context is rebuildable; frontend history is a disposable projection. Keep replies verbatim and consume ordered history, not UI buttons or bindings. External frontends stage all attachments for one Message/Run atomically; failed or cancelled attachments do not leak. Media remain bound to their Message.

2026-10-02 HASHI3 adds `frontend_publish_deliverable` for complete files that should appear while a Run continues. Reuse one stable publication ID on retry; the tool reports persisted publication and each frozen endpoint's current receipt state. `frontend_send_attachments` still binds files for the final reply. Published files stay accessible after later failure and are excluded from final attachment delivery. The Agent's FC Worker dispatches pending Telegram publication tasks even when an isolated tool gateway cannot access the live runtime; expired claims remain unknown until evidence-based recovery. See [the owning FC decision](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#64-incremental-assistant-deliverables) and [implementation record](HASHI_INCREMENTAL_DELIVERABLES_2026-10-02.md). The first live test proved Workbench's A-before-B display but exposed a pending Telegram mirror; Worker sweep adoption needs its own live receipt check.

Every input has protected CURRENT MESSAGE CONTEXT. Source, ingress, processing instance, sender assurance, authority, and destination differ. Only a current successful private_authorization grants its listed scope; names, message text, chat IDs, memory, and credentials do not. HChat separates claimed sender, verified peer, relay, and target; secrets never enter messages. Complete cross-instance targets use optional Exchange and the authenticated Remote handshake. Discovery is only a hint. See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations and automatic mirrors before PCM. Queue acceptance is not delivery: sent requires a Connector receipt, and failure wins contradictory flags. Do not duplicate automatic delivery. Each turn needs a visible terminal result; prose is not Tool authority. FC defines common messages, commands, cards, media, and receipts. PAO owns durable ingress, routing, and idempotency; Connectors authenticate, render, and receipt. Command continuations keep their Session and Connector; only saved non-action completions replay. Pending, conflict, and unknown states never execute.

Autonomous cron, heartbeat, nudge, recovery, /bg, and background completion use an Agent-owned hidden activity Session, not a Conversation or Provider thread. Store execution and delivery separately, then project typed same-owner receipts. /bg sees only its bounded admission snapshot. Scheduled work gets no implicit Conversation history. /delay and interactive /loop remain Conversation continuations.

On HASHI3, Telegram intake reports healthy only after a successful bounded poll and does not discard pending updates on ordinary Agent startup. HERV3 may quote bounded unfinished WIP evidence in the current turn, but no longer sends a recovery card before handling that turn. These are source changes until the owning Functions are separately adopted and checked live.

## Engine, tools, and recovery

HERV3 is the public her-v3 Engine; internal her-v2 names remain storage or adapter compatibility. Its main model/tool loop may use optional JEV, off by default. /backend chooses the Engine, /provider the Model Provider, /model the model, and /effort its reasoning. Fixed/Flex and Memory+ remain independent. /meter derives roles from recorded physical calls and includes all cost; do not invent phases or zero cost where evidence is absent.

Model and provider cards share one contract. Tool support is per model: chat-only models receive no tools and disclose that limit. /style is an optional workspace presentation check; it may keep or rephrase a completed HERV3 answer without changing facts. FC remains the delivery path, and style calls are metered separately. Fallback is opt-in and request-observed: warn before switching, block uncertain effect replay, and meter physical calls. Validate a Tool batch before effects; malformed batches execute nothing, and committed effects never replay. /stop, /retry, /resend, and /steer have distinct meanings; recovery restores no revoked authority. Only typed Persona progress is user-facing, never raw deltas or private reasoning.

## UI, media, and Phone

Live Phone defaults on; a missing provider API key blocks calls and must be named in `/phone`. An explicit instance opt-out remains valid. The Agent's Phone settings and PCM readiness do not prove provider readiness.

Renderers and catalogs own interface text. /language changes shared UI and /tui language only local TUI; neither translates replies, IDs, commands, paths, logs, or transcripts. A TUI instance switch freezes generation, Agent, target, capabilities, logs, and Session at submission. Remote requires authentication, not cached liveness. /telegram off and /whatsapp off disable future owner mirrors while originating-platform replies still deliver normally. /think controls Provider reasoning and /commentary controls visible Engine commentary.

Media are Session assets bound to one draft, instance, Agent, and Run. Remote sends managed bytes. HERV3 receives authorized native content or managed references without widening authority. Safe Voice uses typed confirm/discard, and missing idempotency fails before upload. Late or cancelled media is discarded. /voice previews use a validated Function bundle and instance-local media where possible; Workbench receives a Session audio attachment and Telegram its voice renderer.

Phone is the foreground of the selected HASHI Agent; that same Agent performs delegated work in the background. PCM must supply effective instructions, Persona, HCC, memory, recent Conversation, and relevant completed cron/job facts at the first turn. Capacity fitting preserves access to saved originals, records what reached the provider, and fails explicitly if mandatory context cannot fit. A recent-result index is an address book, not an answer; questions about completed reports read the scoped full PAO original without a new Run. Activity results exclude the scheduled task's input prompt. Numbered source sections retain their count and order. Long results expose page boundaries and continuation; an offered page is not confirmed speech. Old Phone speech is marked unverified and cannot outrank a saved report. See [PCM design](HASHI_PCM_SYSTEM_DESIGN.md), [PAO design](HASHI_PAO_SYSTEM_DESIGN.md), and [Frontend Connector design](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).

A Phone call stays the owner's foreground Conversation until explicit hang-up; other Runs and events remain attributable background input. Durable provider transcript fragments become ordered Session speech records, while the call card derives its display. Brief acknowledgments within a caller sentence do not create a false turn. Keep a privacy-bounded lifecycle audit separate from transcript content. Provider append acceptance, speech fragments, player activity, and device listening are separate evidence. An answer must be checked for actual content coverage before reporting spoken delivery.

Phone actions use the caller's complete meaning, not keywords. Validate durable call/proposal origin, keep one action's ordered steps together, and prevent urgency from duplicating work. Verified completion needs attributable execution evidence; a final answer alone cannot prove a write. Queries may use the complete canonical PAO final Message even when an effect check is uncertain, but do not portray an uncertain write as committed. A stopped Run needs terminal confirmation; separate its state, partial reads, final answer, and any verified saved effects. Unknown effects are not blindly retried. The caller may switch optional brief progress updates on or off; required approvals and final results still arrive. Progress reports only real user-presentable activity at a restrained cadence.

Phone provider and model details live in qualified Function adapters. Settings affect the next call; recovery retains the current provider. A single opening uses effective Persona and language after provider/media readiness, yields to user-first speech, and does not repeat on recovery. Never hardcode a family title. A collapsed or moved Phone window cannot hang up or recreate the call; 收起通话 only collapses it. A PCM refresh failure still permits stop and inspection of a known action but rejects new work. Admission follows the primary Session fence before media and Worker invocation. Provider sideband input is durably staged and replayed in order; staging failure applies backpressure. Explicit hang-up wins stale transport faults. Keep source qualification, running adoption, and live device acceptance separate.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md). Typed slash commands retain their Session and are admitted by the owner even during an active Run. /new creates a fresh primary Session without deleting history; PAO owns Agent deletion and cleanup receipts.

## Move, Scheduler, and HCC

/move and /clone share package, registry, workspace, Scheduler, secret, and lifecycle owners. Move removes verified source only after activation. Clone preserves it, excludes Telegram credentials, and disables imported jobs. Accepted is not completed. See [Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler stores UTC instants, wall time, and IANA zone. Missed-trigger decisions are FC-managed Conversation exchanges: the Agent asks, and the user may answer on any registered Connector. Only an unambiguous decision for the exact pending batch may call the recovery resolver. Do not parse reply words in a Connector or rerun recovery without explicit authority. HERV3 uses the same-instance published Backend API endpoint for Scheduler tools; never invent a port.

Use authorized capabilities only; device actions require a same-instance Worker. Prefer bounded log queries. Work in the foreground unless /bg is explicit. Tests prove their scope, not adoption. HCC is optional, non-authoritative PCM context; /hcc and hcc-refresh refresh sources without rewriting PCM or creating retry authority.

An isolated Tool route must not advertise a Broker-registered Browser or Computer Worker it cannot invoke. Untyped Codex CLI process exits now report the exit code and treat side effects as unknown; do not auto-retry. HASHI-owned process kill paths refuse the current Function process and its parent. These HASHI3 source guards do not establish why any historical Codex subprocess exited.

HASHI3 source now bounds text Tool output and rejects clearly oversized text Provider requests when a capacity is declared. `/stop` blocks autonomous wakeups across Sessions until the next explicit user request; delayed records stay intact, while completed background results stay in job records without automatic replay. A terminal failure separates readback-confirmed file writes from unverified actions and warns against blind retry. These are offline source changes, not evidence that running Workers adopted them.
