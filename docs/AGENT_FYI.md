# HASHI Agent FYI

2026-10-04 HASHI1 scoped-search source work adds `file_search`, own-Agent home
read projection, bounded foreground scans/Shell drains and technical activity
independent of commentary. Shell remains directly usable; wide scope is advisory,
not a new denial. That source checkpoint did not authorize production adoption.
The user subsequently approved HASHI1 reboot; broad Function adoption succeeded
with the same Core/fingerprint and exact five-Agent set online/connected. sunny
passed live scoped search/read, commentary-off activity, verbose live-toggle and
cancel/cleanup checks through the authenticated API; original display settings
were restored. External client screen rendering remains unverified because its
browser was leased by another task. No external client/Core change or other
instance reboot occurred. See [owning decision](HERV3_SCOPED_SEARCH.md).

2026-10-03 Lily correction supersedes Windows automation: six Windows tasks and
eleven old HASHI jobs were removed. Four Agent crons run import 01:00, offline
embedding 03:00, Wiki 04:05 and report 05:00 Sydney time. Wiki requires a
SHA256-bound foreground answer, otherwise blocks; it calls no model API/CLI.
The Agent supervises tools. A 50-record batch passed; scheduled workflow,
delivery and live HERV3 remain open. Do not reactivate old automation; see the
standalone centre's operations/migration decision.

Browser route 4 relays CLI tools through Worker/Capability Broker, reusing login
state and task-bound Chrome/Edge endpoints. Unspecified browsers may use any
connected one. HASHI4 read a real tab on 2026-10-03; its old Browser Worker needs
replacement for names. Dual-browser/HERV3 live checks remain open. See
[device control](HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md#hashi4-browser-route-repair--2026-10-02).

2026-10-02 MCP/media repair: Codex inventory disables plugins consistently with
execution, and trusted Telegram media and /long preserve ingress identity.
See docs/HASHI_MCP_MEDIA_ROLLOUT_2026-10-02.md for source/adoption scope.
HASHI2 is outside this rollout.

Orientation only; /fyi reloads it. Typed requests govern authority; see AGENTS.md, architecture, runtime boundaries and testing policy.

## Authority and ownership

PCM owns Persona, Context and Memory; PAO owns Agents, Sessions, Runs, jobs, Workzones and delivery; HERV3 owns Engine Turns, model/tool loop and cost; Frontend Connectors authenticate and render. Use the narrowest Function/configuration owner. Core has no product policy/imports. Protected Core requires explicit major-migration authorization, version bump, label and independent review; flags grant nothing.

Source, running Workers and delivery need separate proof. Agent tools cannot edit live Core/Python, read secrets, kill Core or grant authority. /reboot min|same replaces one Worker; max adopts shared Functions and Workers, leaving Core and Remote live. Check identity, generation, receipts and idle window. Windows restart needs exact actuator and exit code.

## Configuration, identity, and persistence

Configuration owns identity, ports, Workzones, endpoints and models. Ignore secrets/machine paths. Instance models use allowed_backends and runtime effort options; shared compatibility uses the qualified registry. Explicit choices persist; unknown capabilities/prices are neither unsupported nor zero cost.

Portable carries no credentials; PAO starts Workers. Private experiments stay private. Tool wildcard grants permission, not capability; naming a path grants nothing. Runs freeze enabled Workzone roots/revision; reload is idle-only. Exclude secrets/media bytes/remote paths from PCM, logs, chat and Git.

Successful Agent stop projects `stopped`; Worker outage is `offline`, config deactivation `inactive`. Shared Functions retain the stop marker until restart. Frontends derive visibility without rewriting `is_active`.

JSON writers validate private candidates under locks, revisions, and atomic replacement. Display fallback is read-only. On conflict, read fresh state and request a fresh action; never blindly retry or restore stale bytes. See [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines own Sessions/Turns. Provider context and frontend history are rebuildable. Preserve replies/order. Stage attachments atomically per Message/Run; failed/cancelled assets never leak.

`frontend_publish_deliverable` publishes complete files during a Run; IDs bind exact content. Persistence and endpoint receipts are separate; `frontend_send_attachments` remains final-only. FC drains Telegram publications; uncertain delivery needs evidence before retry. HASHI3 verified Telegram and Workbench feed acceptance; UI visibility and other adoption remain separate. See [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#64-incremental-assistant-deliverables) and [evidence](HASHI_INCREMENTAL_DELIVERABLES_2026-10-02.md).

CURRENT MESSAGE CONTEXT separates source, ingress, instance, sender assurance, authority and destination. Only a current successful private_authorization grants its listed scope; names, text, IDs, memory and credentials do not. HChat separates claimed sender, verified peer, relay and target; never send secrets. Cross-instance targets use optional Exchange and authenticated Remote handshake. Discovery is a hint. See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations/mirrors before PCM and owns ingress/routing/idempotency; FC owns presentation/receipts. Acceptance is not delivery: sent needs a receipt; failure wins contradictory flags. Avoid duplicates; every turn needs a terminal result. Prose grants no Tool authority. Command continuations retain Session/Connector; replay only saved non-action completions. Pending/conflict/unknown never execute.

Scheduled work, recovery and /bg use an Agent-owned hidden activity Session. Execution/delivery are separate; receipts stay same-owner. Project each final once into the owner's Conversation for display, retaining the authoritative original; copies enter neither Engine history nor Phone. /bg uses its admission snapshot. Schedules get no implicit chat history; /delay and interactive /loop remain continuations.

HASHI3 Telegram intake reports healthy only after a successful bounded poll; ordinary Agent startup preserves pending updates. HERV3 may quote bounded unfinished WIP evidence in the turn but sends no premature recovery card. Source changes need separate Worker adoption and live checks.

## Engine, tools, and recovery

Public HERV3 is her-v3; her-v2 names are compatibility only. JEV defaults off. /backend selects Engine, /provider Model Provider, /model model, /effort reasoning; Fixed/Flex and Memory+ are independent. /meter uses all physical calls/cost. Codex cumulative counters need a persisted turn baseline; estimate if unknown. USD estimates are not subscription bills.

Level 2: HERV3/DeepSeek only. Local PII detection can miss values;
`/privacy 2` needs risk acceptance and blocks on detector failure.

Model/provider cards share one contract; chat-only models disclose that they have no tools. /style rephrases completed answers without changing facts and meters separately. Fallback is opt-in: warn before switching and block uncertain effect replay. Reject malformed Tool batches before effects; never replay committed effects. /stop, /retry, /resend and /steer differ; recovery grants no revoked authority. Show typed progress, not private reasoning.

## UI, media, and Phone

Experimental `local-cascade` connects local speech to Agent Phone actions, with
opt-in/offline models outside the default registry. HASHI1's loopback Worker
needs no token; adaptive noise calibration passed two synthetic WebRTC turns
on 2026-10-03. Speech disables reasoning; reconnect replay is bounded. Physical
audio acceptance remains open. See [Gate 2](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Live Phone defaults on; a missing provider API key blocks calls and must be named in `/phone`. An explicit instance opt-out remains valid. The Agent's Phone settings and PCM readiness do not prove provider readiness.

Renderers/catalogs own UI text. /language changes shared UI, /tui language local TUI; neither translates replies/IDs. TUI switches freeze generation/Agent/capabilities/Session. Remote authenticates. /telegram off and /whatsapp off disable future mirrors, preserving origin replies. /think and /commentary are independent.

Media bind one draft/instance/Agent/Run; Remote sends managed bytes. HERV3 gains no wider authority. Safe Voice uses typed confirm/discard; missing idempotency blocks upload and stale media is discarded. /voice uses a validated Function bundle/local media; Workbench gets Session audio, Telegram its voice renderer.

Phone stays the owner's foreground Conversation until hang-up; delegated Runs/events are background. First-turn PCM includes instructions, Persona, HCC, memory, recent Conversation and scoped completed jobs. Capacity fitting preserves originals and fails if required context cannot fit. Result lookup uses PAO originals, excludes scheduled prompts and creates no Run. Preserve source order and page long results; offered pages and old unverified speech cannot outrank saved reports. See [PCM](HASHI_PCM_SYSTEM_DESIGN.md), [PAO](HASHI_PAO_SYSTEM_DESIGN.md) and [Frontend](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).

Durable provider fragments form ordered Session speech; call cards are derived. Brief acknowledgments create no turn. Lifecycle audit, provider acceptance, fragments, playback and physical listening are separate facts. Phone actions preserve caller meaning, validate origin/order and avoid duplicate effects; completion requires execution evidence, uncertain writes never auto-retry. Confirm stopped Runs terminally; deliver approvals and final results.

Qualified Function adapters own Phone settings. Changes apply next call; recovery retains its provider. One Persona/language opening follows readiness and yields to user speech. Window movement cannot hang up. Failed PCM refresh permits stop/inspection only. Sideband follows the primary Session fence; explicit hang-up wins stale faults. Source, runtime adoption and device acceptance stay distinct.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), retain Session and remain owner-admitted during active Runs. /new creates a fresh Session without deleting history; PAO owns Agent deletion/cleanup receipts.

`/say` reads the latest final reply, `/say 2`–`/say 4` that many, `/say 1-3` the latest three oldest first. Skip cost/command/progress. Telegram needs confirmed chat delivery; Workbench/TUI play locally. Reboot receipts never prove playback.

## Move, Scheduler, and HCC

/move and /clone share package, registry, workspace, Scheduler, secret, and lifecycle owners. Move removes verified source only after activation. Clone preserves it, excludes Telegram credentials, and disables imported jobs. Accepted is not completed. See [Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler stores UTC instants, wall time and IANA zone. Missed-trigger decisions belong to FC Conversation; only an unambiguous choice for the exact batch may resolve recovery. Never rerun recovery without explicit authority. HERV3 uses the same-instance published Backend API endpoint; never invent a port.

Use authorized capabilities only; device actions require a same-instance Worker. Prefer bounded log queries. Work in the foreground unless /bg is explicit. Tests prove their scope, not adoption. HCC is optional, non-authoritative PCM context; /hcc and hcc-refresh refresh sources without rewriting PCM or creating retry authority.

Isolated Tool routes must not advertise inaccessible Browser/Computer Workers. Untyped Codex exits report exit code, leave side effects unknown and forbid auto-retry. HASHI process-kill refuses its current Function and parent. These HASHI3 guards do not explain historical exits.

Manual Desktop is opt-in: Standard 2 FPS, Smooth 20 FPS, Ultra Smooth targets 30 FPS with bounded size/bandwidth. Remote probes the local API and never retries uncertain writes. PAO leases Workers. See [desktop](HASHI_MANUAL_DESKTOP.md); source/Worker/frontend adoption are distinct.

HASHI3 caps Tool text at Provider capacity. `/stop` blocks autonomous wakeups until an explicit request; completed background results stay in job records. Distinguish verified writes from uncertain effects; adopt offline changes before claiming live behavior.

Cancellation binds exact Session/Run, including capacity recovery; PAO settles
queued/active Runs. Never fall back to Agent stop or report stopped early.
Offline red/green passed; live acceptance is separate. See [PAO](HASHI_PAO_SYSTEM_DESIGN.md).
