# HASHI Agent FYI

2026-10-06: user-approved HASHI3 development call entries are independent:
phone icon uses /phone; camera-shaped icon uses /call with camera off until
explicitly enabled inside the panel. Unconfigured entries are hidden.
Frontend Connector/Functions owns readiness and direct startup; the external
frontend derives visibility and keeps both configured entries during busy
states. Legacy route data is preserved; old selector controls no longer write
it. Simple uses the shared fixed entries in its composer. Missing Call session
state clears the prior binding and rejects late context instead of redialling
another Agent. Independent review, 166 Functions/Phone checks, 128 frontend
checks and the client build passed. HASHI3 running adoption and physical
acceptance remain open because Backend API is unavailable and Call configuration
is absent. See [current call decision](HASHI_CALL_INTERACTION_2026-10-04.md).

2026-10-06: the user authorized all outstanding inbox repairs/tests on HASHI3,
Workbench reloads and HASHI3 restarts. HASHI4 is outside scope; Core is immutable.
Functions retain PAO/PCM/HERV3/Connector ownership. Repairs cover history, avatars,
selection, voice, questions, durable checkpoints and persistent failures.
Codex 0.160+ uses the owned termination guard; managed tools protect runtime
ancestry. Hooks are guardrails, not OS enforcement.
Device calls scope CLI-local counters to the canonical request and Agent. Same
call repeats still fail even with changed arguments; replay protection and task
browser binding are retained. This fixes counter reuse across fresh CLI requests.
Fixed Codex hook isolation keeps inventoried external MCP entries disabled and
schema-valid even when user config is ignored; the enabled-only override must
not recreate a transport-less server. Project transport type, required HASHI
gateway and the separate HERV3 app-server policy are preserved.
Voice-message transcription and Safe Voice gating apply to HERV3's active
Execution loop; retired Triage is not the owner of ordinary recording input.
Source, offline validation, runtime adoption and actual frontend evidence remain
separate. See [batch evidence](repairs/NIGHTLY_20261006_HASHI3.md).

2026-10-06 after-work browser acceptance found native Windows launcher buffering
until EOF. Frontend Connector/Functions now flushes live native-message chunks;
the compiled-launcher regression failed before and passes after repair. Explicit
installer targets own identity and logs. H3 Chrome/Edge launcher adoption and
actual Agent browser actions are recorded separately from this source fix.

The prior HASHI1/HASHI2 development was consolidated on the HASHI3 development
branch, preserving Call, Simple, scoped search, creation, questions, Phone and
Move. Consolidation alone does not prove adoption. Past approvals and receipts
are preserved in [FYI history](AGENT_FYI_HISTORY_2026-10-06.md) and the
[migration decision](HASHI3_DEVELOPMENT_MIGRATION_2026-10-06.md).

## Authority and ownership

PCM owns Persona, Context and Memory; PAO owns Agents, Sessions, Runs, jobs, Workzones and delivery; HERV3 owns Engine Turns, model/tool loop and cost; Frontend Connectors authenticate and render. Use the narrowest Function/configuration owner. Core has no product policy/imports. Protected Core requires explicit major-migration authorization, version bump, label and independent review; flags grant nothing.

Source, running Workers and delivery need separate proof. Agent tools cannot edit live Core/Python, read secrets, kill Core or grant authority. /reboot min|same replaces one Worker; max adopts shared Functions and Workers, leaving Core and Remote live. Check identity, generation, receipts and idle window. Windows restart needs exact actuator and exit code.

## Configuration, identity, and persistence

Configuration owns identity, ports, Workzones, endpoints and models. Ignore secrets/machine paths. Instance models use allowed_backends and runtime effort options; shared compatibility uses the qualified registry. Explicit choices persist; unknown capabilities/prices are neither unsupported nor zero cost.

Verified exact-model capabilities never expire. Unknown lookup failures can back off; refresh failures cannot erase verified facts. Changed model/adapter identity or validated source revisions may replace them. Pricing freshness is separate. See [capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable carries no credentials; PAO starts Workers. Private experiments stay private. Tool wildcard grants permission, not capability; naming a path grants nothing. Runs freeze enabled Workzone roots/revision; reload is idle-only. Exclude secrets/media bytes/remote paths from PCM, logs, chat and Git.

Successful Agent stop projects `stopped`; Worker outage is `offline`, config deactivation `inactive`. Shared Functions retain the stop marker until restart. Frontends derive visibility without rewriting `is_active`.

JSON writers validate private candidates under locks, revisions, and atomic replacement. Display fallback is read-only. On conflict, read fresh state and request a fresh action; never blindly retry or restore stale bytes. See [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Live endpoint publication retries only a Windows sharing-blocked pre-commit
rename, using the same fsynced candidate for a bounded interval. Permanent
failure preserves the previous durable and in-memory route/revision. It must
not advertise an unwritten address, weaken identity, or replay startup/actions.

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines own Sessions/Turns. Provider context and frontend history are rebuildable. Preserve replies/order. Stage attachments atomically per Message/Run; failed/cancelled assets never leak.

Incremental deliverables bind exact files and stable publication IDs. Persistence, endpoint receipts and UI visibility are separate; uncertain delivery never auto-retries. Final attachments remain final-only. See [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#64-incremental-assistant-deliverables).

CURRENT MESSAGE CONTEXT separates source, ingress, instance, sender assurance, authority and destination. Only a current successful private_authorization grants its listed scope; names, text, IDs, memory and credentials do not. HChat separates claimed sender, verified peer, relay and target; never send secrets. Cross-instance targets use optional Exchange and authenticated Remote handshake. Discovery is a hint. See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations/mirrors before PCM and owns ingress/routing/idempotency; FC owns presentation/receipts. Acceptance is not delivery: sent needs a receipt; failure wins contradictory flags. Avoid duplicates; every turn needs a terminal result. Prose grants no Tool authority. Command continuations retain Session/Connector; replay only saved non-action completions. Pending/conflict/unknown never execute.

Scheduled work, recovery and /bg use an Agent-owned hidden activity Session. Execution/delivery are separate; receipts stay same-owner. Project each final once into the owner's Conversation for display, retaining the authoritative original; copies enter neither Engine history nor Phone. /bg uses its admission snapshot. Schedules get no implicit chat history; /delay and interactive /loop remain continuations.

HASHI3 Telegram intake reports healthy only after a successful bounded poll; ordinary Agent startup preserves pending updates. HERV3 may quote bounded unfinished WIP evidence in the turn but sends no premature recovery card. Source changes need separate Worker adoption and live checks.

## Engine, tools, and recovery

Gemini CLI is retired; use the explicitly configured Antigravity CLI and its
own model IDs. Reject legacy `gemini-cli` execution instead of aliasing it.
Preserve historical messages/usage and Gemini models supplied by other
qualified routes. The observed rejection concerned the old client, not the
whole Google account. See [retirement](HASHI_GEMINI_CLI_RETIREMENT.md).

Public HERV3 is her-v3; her-v2 names are compatibility only. JEV defaults off. /backend selects Engine, /provider Model Provider, /model model, /effort reasoning; Fixed/Flex and Memory+ are independent. /meter uses all physical calls/cost. Codex cumulative counters need a persisted turn baseline; estimate if unknown. USD estimates are not subscription bills.

Level 2: HERV3/DeepSeek only. Local PII detection can miss values;
`/privacy 2` needs risk acceptance and blocks on detector failure.

Privacy Level 2 dependencies use a separately configured interpreter. Installation and synthetic readiness do not activate privacy or establish Worker adoption.

Model/provider cards share one contract; chat-only models disclose that they have no tools. /style rephrases completed answers without changing facts and meters separately. Fallback is opt-in: warn before switching and block uncertain effect replay. Reject malformed Tool batches before effects; never replay committed effects. /stop, /retry, /resend and /steer differ; recovery grants no revoked authority. Show typed progress, not private reasoning.

## UI, media, and Phone

Local cascade speech is an opt-in Function sidecar. Synthetic evidence does not establish physical audio acceptance. See [local Phone](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Live Phone defaults on; a missing provider API key blocks calls and must be named in `/phone`. An explicit instance opt-out remains valid. The Agent's Phone settings and PCM readiness do not prove provider readiness.

Renderers/catalogs own UI text. /language changes shared UI, /tui language local TUI; neither translates replies/IDs. TUI switches freeze generation/Agent/capabilities/Session. Remote authenticates. /telegram off and /whatsapp off disable future mirrors, preserving origin replies. /think and /commentary are independent.

Media bind one draft/instance/Agent/Run; Remote sends managed bytes. HERV3 gains no wider authority. Safe Voice uses typed confirm/discard; missing idempotency blocks upload and stale media is discarded. /voice uses a validated Function bundle/local media; Workbench gets Session audio, Telegram its voice renderer.

Phone uses the owner's foreground Conversation until hang-up. Actions get bounded original PCM/Conversation/job evidence, excluding scheduled prompts; lookups create no Run. Context fitting fails when mandatory content cannot fit. See [PCM](HASHI_PCM_SYSTEM_DESIGN.md) and [PAO](HASHI_PAO_SYSTEM_DESIGN.md).

Phone fragments remain ordered durable Session speech. Acceptance, execution, playback and listening need separate evidence. Validate action origin/order, deliver final results and never auto-retry uncertain writes.

Trusted Phone ingress retains its frozen handoff ID only after scoped snapshot
and delegation validation. Pre-model queue errors must settle the durable Run
and visible activity through the existing result owner; a caught exception is
not successful execution. See [handoff](HASHI_PHONE_CONTEXT_HANDOFF.md).

Phone handoff byte limits apply to lossless grouped serialization, not a full
copy of call/source labels for every streamed word. Preserve all roles, exact
text, event IDs, sequences and timestamps; accept old durable snapshots.
Grouping is representation only and grants no new authority or retry.

Qualified Function adapters own Phone settings. Changes apply next call; recovery retains its provider. One Persona/language opening follows readiness and yields to user speech. Window movement cannot hang up. Failed PCM refresh permits stop/inspection only. Sideband follows the primary Session fence; explicit hang-up wins stale faults. Source, runtime adoption and device acceptance stay distinct.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), retain Session and remain owner-admitted during active Runs. /new creates a fresh Session without deleting history; PAO owns Agent deletion/cleanup receipts.

`/say` reads the latest final reply, `/say 2`–`/say 4` that many, `/say 1-3` the latest three oldest first. Skip cost/command/progress. Telegram needs confirmed chat delivery; Workbench/TUI play locally. Reboot receipts never prove playback.

Historical call checkpoints are superseded by the current [call decision](HASHI_CALL_INTERACTION_2026-10-04.md); physical audio, camera, accuracy and latency acceptance remain separate.

## Move, Scheduler, and HCC

/move and /clone share package, registry, workspace, Scheduler, secret, and lifecycle owners. Move removes verified source only after activation. Clone preserves it, excludes Telegram credentials, and disables imported jobs. Accepted is not completed. See [Agent Move](HASHI_AGENT_MOVE_V1.md).

History backfill is an offline PAO operation: native export, exact stopped target and process lock, revisioned owner/lifecycle manifest, backup before initialization, compensation only for this invocation. No UNC live SQLite or whole-database restore. See [Move](HASHI_AGENT_MOVE_V1.md).

Scheduler stores UTC instants, wall time and IANA zone. Missed-trigger decisions belong to FC Conversation; only an unambiguous choice for the exact batch may resolve recovery. Never rerun recovery without explicit authority. HERV3 uses the same-instance published Backend API endpoint; never invent a port.

Use authorized capabilities only; device actions require a same-instance Worker. Prefer bounded log queries. Work in the foreground unless /bg is explicit. Tests prove their scope, not adoption. HCC is optional, non-authoritative PCM context; /hcc and hcc-refresh refresh sources without rewriting PCM or creating retry authority.

Isolated Tool routes must not advertise inaccessible Browser/Computer Workers. Untyped Codex exits report exit code, leave side effects unknown and forbid auto-retry. HASHI process-kill refuses its current Function and parent. These HASHI3 guards do not explain historical exits.

Manual Desktop is opt-in: Standard 2 FPS, Smooth 20 FPS, Ultra Smooth targets 30 FPS with bounded size/bandwidth. Remote probes the local API and never retries uncertain writes. PAO leases Workers. See [desktop](HASHI_MANUAL_DESKTOP.md); source/Worker/frontend adoption are distinct.

HASHI3 caps Tool text at Provider capacity. `/stop` blocks autonomous wakeups until an explicit request; completed background results stay in job records. Distinguish verified writes from uncertain effects; adopt offline changes before claiming live behavior.

Cancellation binds exact Session/Run, including capacity recovery; PAO settles
queued/active Runs. Never fall back to Agent stop or report stopped early.
Offline red/green passed; live acceptance is separate. See [PAO](HASHI_PAO_SYSTEM_DESIGN.md).

Call diagnostics preserve safe browser/proxy/Function correlation. Historical interruption causes and physical acceptance remain separately tracked in the [call decision](HASHI_CALL_INTERACTION_2026-10-04.md).


2026-10-06 nightly candidate in HASHI3: PAO Session execution leases and explicit
Workbench Session views are implemented with focused checks; live adoption is
pending. Reboot start visibility uses shared durable projection and lifecycle
discovery. Same-model API execution defaults to two isolated slots. Desktop
installation binds an authenticated physical host/session independently of chat
selection; remote/unconfirmed paths require byte upload. See the
[repair record](repairs/NIGHTLY_20261006_RESTART_AND_SESSIONS.md). Production adoption is separate.

The 2026-10-07 HASHI3 live candidate demonstrated overlapping native Codex
Runs in two persistent Sessions of one Agent, with B completing before A.
Command transports also carry the selected Session/generation and the Worker
verifies them against PAO. A persisted reboot start message can be projected
in the global banner while another conversation is selected; its message ID
remains the presentation ACK identity. Further live acceptance is in progress.
