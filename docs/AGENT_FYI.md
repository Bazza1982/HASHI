# HASHI Agent FYI

2026-10-02 MCP/media repair: Codex inventory disables plugins consistently with
execution, and trusted Telegram media and /long preserve ingress identity.
See docs/HASHI_MCP_MEDIA_ROLLOUT_2026-10-02.md for source/adoption scope.
HASHI2 is outside this rollout.

Orientation only; not a task queue or proof of authority/adoption. /fyi reloads it. Current typed requests govern; see AGENTS.md, ARCHITECTURE.md, runtime boundaries, UI guide and testing policy.

## Authority and ownership

PCM owns Persona, Context, Memory, authority and projections. PAO owns Agents, Sessions, Runs, jobs, Workzones, recovery and delivery. HERV3 owns Engine Turns, model/tool loop, Provider choice and cost. Frontend Connectors authenticate and render. Use the narrowest Function or configuration owner. Core has no product policy or imports. Protected Core requires explicit major-migration authorization, major bump, core-change-approved label and independent review; flags grant nothing.

Source, running Workers and delivery need separate proof. Agent tools cannot edit live Core/Python, read secrets, kill Core or grant authority. /reboot min|same replaces one Worker; max adopts shared Functions and Workers, leaving Core and Remote live. Check identity, generation, receipts and idle window. Windows restart needs exact actuator and exit code.

## Configuration, identity, and persistence

Read identity, ports, Workzones, endpoints and models from configuration, never folder names. Ignore secrets and machine paths. Instance model opt-ins use allowed_backends and runtime effort options; shared compatibility uses the qualified Function registry. Explicit choices persist until retired. Unknown model, price, effort or modality is neither unsupported nor zero cost.

Portable installs carry no credentials; active Agents need PAO-started Workers. Private bridge-home experiments stay private. Tool wildcard grants permission, not capability. Each Run freezes enabled Workzone roots and revision; later Runs see later revisions, and reload is idle-only. Naming a path grants nothing. Keep secrets, media bytes and remote paths out of PCM, logs, chat and tracked files.

Successful Agent stop projects `stopped`; Worker outage is `offline`, config deactivation `inactive`. Shared Functions retain the stop marker until restart. Frontends derive visibility without rewriting `is_active`.

JSON writers validate private candidates under locks, revisions, and atomic replacement. Display fallback is read-only. On conflict, read fresh state and request a fresh action; never blindly retry or restore stale bytes. See [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

## Sessions, trust, and delivery

PAO owns Conversation Sessions, Messages and Runs; Engines own their Sessions and Turns. Provider context is rebuildable; frontend history disposable. Keep replies verbatim and history ordered. External frontends stage attachments atomically per Message/Run; failed or cancelled assets do not leak.

HASHI3 introduced `frontend_publish_deliverable` for complete files during a Run. Reuse a publication ID only for the same content. Persistence and each frozen endpoint's receipt are separate; `frontend_send_attachments` remains final-only. The FC Worker drains pending Telegram publications; uncertain claims need evidence before retry. HASHI3 live tests confirmed Telegram delivery and Workbench feed acceptance, while UI visibility and adoption elsewhere need separate proof. See the [FC decision](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#64-incremental-assistant-deliverables) and [implementation record](HASHI_INCREMENTAL_DELIVERABLES_2026-10-02.md).

CURRENT MESSAGE CONTEXT separates source, ingress, instance, sender assurance, authority and destination. Only a current successful private_authorization grants its listed scope; names, text, IDs, memory and credentials do not. HChat separates claimed sender, verified peer, relay and target; never send secrets. Cross-instance targets use optional Exchange and authenticated Remote handshake. Discovery is a hint. See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations and mirrors before PCM. Queue acceptance is not delivery: sent needs a Connector receipt; failure wins contradictory flags. Do not duplicate delivery. Every turn needs a visible terminal result; prose is not Tool authority. FC defines messages, commands, cards, media and receipts; PAO owns ingress, routing and idempotency. Command continuations retain Session and Connector; only saved non-action completions replay. Pending, conflict and unknown states never execute.

Cron, heartbeat, nudge, recovery, /bg and background completion use an Agent-owned hidden activity Session. Track execution and delivery separately; project typed same-owner receipts. Project each final answer once into the owner's current Conversation for Workbench display, retaining the original as authority; the display copy enters neither Engine history nor Phone inbox. /bg sees its bounded admission snapshot. Scheduled work gets no implicit Conversation history. /delay and interactive /loop remain continuations.

HASHI3 Telegram intake reports healthy only after a successful bounded poll; ordinary Agent startup preserves pending updates. HERV3 may quote bounded unfinished WIP evidence in the turn but sends no premature recovery card. Source changes need separate Worker adoption and live checks.

## Engine, tools, and recovery

HERV3 is public her-v3; internal her-v2 names are compatibility only. Optional JEV is off by default. /backend chooses Engine, /provider Model Provider, /model model, /effort reasoning; Fixed/Flex and Memory+ are independent. /meter uses physical calls and all cost; never invent phases or zero cost. Codex CLI counters are cumulative: use a persisted baseline for current-turn usage and estimate if unknown. USD estimates are not subscription bills.

HASHI2 `feature-privacy` implements a HERV3-only Level 2 local PII-masking trial. A replaceable, isolated local detector masks supported text PII before each qualified DeepSeek request; direct API and other outer backends are disabled at Level 2. `/privacy 2` requires explicit acceptance of residual misses, especially in non-English text; detector failures and unsupported media block transport. Synthetic DeepSeek evidence showed masked name/email absent at the HTTP boundary while a calculation succeeded. Source implementation, running Worker adoption, and live verification are separate facts. See the [Level 2 decision](HASHI_HERV3_PRIVACY_PILOT_2026-10-02.md).

Level 2 deployment uses `requirements-privacy.txt` in a separate interpreter.
Source installs run `scripts/provision_privacy_runtime.py`; npm and enterprise
builds prepare it automatically; the 957 MB Portable Windows image supplies a
separate on-target installer. Readiness checks use synthetic PII, and setup
does not activate `/privacy 2` or prove running Worker adoption.

Model/provider cards share one contract; chat-only models disclose that they have no tools. /style rephrases completed answers without changing facts and meters separately. Fallback is opt-in: warn before switching and block uncertain effect replay. Reject malformed Tool batches before effects; never replay committed effects. /stop, /retry, /resend and /steer differ; recovery grants no revoked authority. Show typed progress, not private reasoning.

## UI, media, and Phone

Live Phone defaults on; a missing provider API key blocks calls and must be named in `/phone`. An explicit instance opt-out remains valid. The Agent's Phone settings and PCM readiness do not prove provider readiness.

Renderers/catalogs own UI text. /language changes shared UI, /tui language only local TUI; neither translates replies or identifiers. TUI instance switches freeze the selected generation, Agent, capabilities and Session. Remote requires authentication. /telegram off and /whatsapp off disable future mirrors, not originating-platform replies. /think and /commentary control reasoning and visible commentary separately.

Media bind to one draft, instance, Agent and Run; Remote sends managed bytes. HERV3 receives authorized content/references without wider authority. Safe Voice uses typed confirm/discard; missing idempotency fails before upload, late/cancelled media is discarded. /voice previews use a validated Function bundle and local media where possible; Workbench gets a Session audio attachment, Telegram its voice renderer.

Phone is the selected Agent's foreground; delegated work runs in the background. The first turn includes effective instructions, Persona, HCC, memory, recent Conversation and relevant completed job facts. Capacity fitting preserves originals and fails if required context cannot fit. A recent-result index locates scoped PAO originals without a new Run; scheduled prompts are excluded. Preserve numbered source order and page long results; offered pages are not confirmed speech. Old unverified Phone speech cannot outrank saved reports. See [PCM](HASHI_PCM_SYSTEM_DESIGN.md), [PAO](HASHI_PAO_SYSTEM_DESIGN.md), and [Frontend](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).

Phone stays the owner's foreground Conversation until hang-up; other Runs/events remain background input. Durable provider fragments form ordered Session speech records; the call card is derived. Brief in-sentence acknowledgments create no turn. Keep lifecycle audit separate from transcript content. Provider acceptance, speech fragments, player activity and device listening need separate evidence; check actual coverage before claiming delivery.

Phone actions follow the caller's full meaning. Validate proposal origin and ordered steps; avoid duplicate work. Completion needs attributable execution evidence. A canonical final Message may answer queries, but an uncertain write is never claimed or blindly retried. Confirm stopped Runs terminally. Keep progress brief; approvals and final results still arrive.

Phone provider/model details live in qualified Function adapters. Settings affect the next call; recovery keeps its provider. A single Persona/language opening follows readiness and yields to user-first speech. Window movement cannot end a call. PCM refresh failure permits stop and inspection only. Sideband input follows the primary Session fence; explicit hang-up wins stale transport faults. Separate source, running adoption and device acceptance.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md). Typed slash commands retain their Session and are admitted by the owner even during an active Run. /new creates a fresh primary Session without deleting history; PAO owns Agent deletion and cleanup receipts.

`/say` reads the newest final Agent reply; `/say 2`–`/say 4` read that many, and `/say 1-3` reads the newest three oldest first. Skip cost, command and progress messages. Telegram requires confirmed chat delivery; Workbench/TUI play audio locally. Source or reboot receipts do not prove physical playback.

## Move, Scheduler, and HCC

/move and /clone share package, registry, workspace, Scheduler, secret, and lifecycle owners. Move removes verified source only after activation. Clone preserves it, excludes Telegram credentials, and disables imported jobs. Accepted is not completed. See [Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler stores UTC instants, wall time and IANA zone. Missed-trigger decisions belong to FC Conversation; only an unambiguous choice for the exact batch may resolve recovery. Never rerun recovery without explicit authority. HERV3 uses the same-instance published Backend API endpoint; never invent a port.

Use authorized capabilities only; device actions require a same-instance Worker. Prefer bounded log queries. Work in the foreground unless /bg is explicit. Tests prove their scope, not adoption. HCC is optional, non-authoritative PCM context; /hcc and hcc-refresh refresh sources without rewriting PCM or creating retry authority.

Isolated Tool routes must not advertise inaccessible Browser/Computer Workers. Untyped Codex exits report exit code, leave side effects unknown and forbid auto-retry. HASHI process-kill refuses its current Function and parent. These HASHI3 guards do not explain historical exits.

Manual Desktop is opt-in: Standard peaks at 2 FPS, Smooth at 20 FPS, and Ultra Smooth targets 30 FPS within bounded size and bandwidth. Remote probes the local API before input and never retries uncertain writes. PAO leases the Worker. See the [desktop guide](HASHI_MANUAL_DESKTOP.md); source, Worker and frontend adoption need separate proof.

HASHI3 caps Tool text at Provider capacity. `/stop` blocks autonomous wakeups until an explicit request; completed background results stay in job records. Distinguish verified writes from uncertain effects; adopt offline changes before claiming live behavior.
