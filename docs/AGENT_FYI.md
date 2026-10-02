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

2026-10-02 HASHI3 adds `frontend_publish_deliverable` for complete files that should appear while a Run continues. Reuse one stable publication ID on retry; the tool reports persisted publication and each frozen endpoint's current receipt state. `frontend_send_attachments` still binds files for the final reply. Published files stay accessible after later failure and are excluded from final attachment delivery. The Agent's FC Worker dispatches pending Telegram publication tasks even when an isolated tool gateway cannot access the live runtime; expired claims remain unknown until evidence-based recovery. See [the owning FC decision](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#64-incremental-assistant-deliverables) and [implementation record](HASHI_INCREMENTAL_DELIVERABLES_2026-10-02.md). Workbench displayed A before B in the first live test; after the Worker correction, A and B each received one Telegram delivered receipt, and a new C file reached Telegram while its Run was still active. The separate Workbench feed now uses its frozen connection-scoped endpoint and drains the latest terminal Run; live A, B and C Backend API tasks each received one accepted receipt, while UI visibility remains a separate observation.

CURRENT MESSAGE CONTEXT separates source, ingress, instance, sender assurance, authority and destination. Only a current successful private_authorization grants its listed scope; names, text, IDs, memory and credentials do not. HChat separates claimed sender, verified peer, relay and target; never send secrets. Cross-instance targets use optional Exchange and authenticated Remote handshake. Discovery is a hint. See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations and mirrors before PCM. Queue acceptance is not delivery: sent needs a Connector receipt; failure wins contradictory flags. Do not duplicate delivery. Every turn needs a visible terminal result; prose is not Tool authority. FC defines messages, commands, cards, media and receipts; PAO owns ingress, routing and idempotency. Command continuations retain Session and Connector; only saved non-action completions replay. Pending, conflict and unknown states never execute.

Cron, heartbeat, nudge, recovery, /bg and background completion use an Agent-owned hidden activity Session. Track execution and delivery separately; project typed same-owner receipts. Project each final answer once into the owner's current Conversation for Workbench display, retaining the original as authority; the display copy enters neither Engine history nor Phone inbox. /bg sees its bounded admission snapshot. Scheduled work gets no implicit Conversation history. /delay and interactive /loop remain continuations.

HASHI3 Telegram intake reports healthy only after a successful bounded poll; ordinary Agent startup preserves pending updates. HERV3 may quote bounded unfinished WIP evidence in the turn but sends no premature recovery card. Source changes need separate Worker adoption and live checks.

## Engine, tools, and recovery

HERV3 is public her-v3; internal her-v2 names are compatibility only. Optional JEV is off by default. /backend chooses Engine, /provider Model Provider, /model model, /effort reasoning; Fixed/Flex and Memory+ are independent. /meter uses physical calls and all cost; never invent phases or zero cost. Codex CLI counters are cumulative: use a persisted baseline for current-turn usage and estimate if unknown. USD estimates are not subscription bills.

Model/provider cards share one contract. Chat-only models get no tools and disclose it. /style may keep or rephrase completed HERV3 answers without changing facts; FC delivers, style calls meter separately. Fallback is opt-in and request-observed: warn before switching, block uncertain effect replay, meter physical calls. Validate Tool batches before effects; malformed batches do nothing, committed effects never replay. /stop, /retry, /resend and /steer differ; recovery restores no revoked authority. Show typed Persona progress, never raw deltas or private reasoning.

## UI, media, and Phone

Live Phone defaults on; a missing provider API key blocks calls and must be named in `/phone`. An explicit instance opt-out remains valid. The Agent's Phone settings and PCM readiness do not prove provider readiness.

Renderers/catalogs own UI text. /language changes shared UI, /tui language only local TUI; neither translates replies, IDs, commands, paths, logs or transcripts. TUI instance switches freeze generation, Agent, target, capabilities, logs and Session at submission. Remote requires authentication, not cached liveness. /telegram off and /whatsapp off disable future mirrors, not originating-platform replies. /think controls reasoning; /commentary controls visible Engine commentary.

Media bind to one draft, instance, Agent and Run; Remote sends managed bytes. HERV3 receives authorized content/references without wider authority. Safe Voice uses typed confirm/discard; missing idempotency fails before upload, late/cancelled media is discarded. /voice previews use a validated Function bundle and local media where possible; Workbench gets a Session audio attachment, Telegram its voice renderer.

Phone is the selected Agent's foreground; delegated work runs in the background. PCM's first turn supplies effective instructions, Persona, HCC, memory, recent Conversation and relevant completed cron/job facts. Capacity fitting preserves saved originals, records provider input and fails if mandatory context cannot fit. A recent-result index only locates reports: read the scoped full PAO original without a new Run; exclude scheduled prompts. Preserve numbered source order/count. Long results expose pages and continuation; offered pages are not confirmed speech. Unverified old Phone speech cannot outrank saved reports. See [PCM](HASHI_PCM_SYSTEM_DESIGN.md), [PAO](HASHI_PAO_SYSTEM_DESIGN.md), and [Frontend](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).

Phone stays the owner's foreground Conversation until hang-up; other Runs/events remain background input. Durable provider fragments form ordered Session speech records; the call card is derived. Brief in-sentence acknowledgments create no turn. Keep lifecycle audit separate from transcript content. Provider acceptance, speech fragments, player activity and device listening need separate evidence; check actual coverage before claiming delivery.

Phone actions follow the caller's full meaning, not keywords. Validate durable call/proposal origin, keep ordered steps together and avoid duplicate work. Completion needs attributable execution evidence; a final answer cannot prove a write. Queries may use the canonical PAO final Message despite uncertain effects, but never claim an uncertain write. Confirm stopped Runs terminally; distinguish state, partial reads, final answer and verified saved effects. Never blindly retry unknown effects. Optional brief progress can be switched off; approvals and final results still arrive. Report real user-facing progress sparingly.

Phone provider/model details live in qualified Function adapters. Settings affect the next call; recovery keeps its provider. One Persona/language opening follows readiness, yields to user-first speech and never repeats on recovery; do not hardcode family titles. Moving/collapsing the Phone window cannot end or recreate a call. PCM refresh failure permits stop and inspection, not new work. Admission follows the primary Session fence; sideband input stages durably in order or applies backpressure. Explicit hang-up wins stale transport faults. Separate source, running adoption and device acceptance.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md). Typed slash commands retain their Session and are admitted by the owner even during an active Run. /new creates a fresh primary Session without deleting history; PAO owns Agent deletion and cleanup receipts.

`/say` reads the newest final Agent reply; `/say 2`–`/say 4` read that many, and `/say 1-3` reads the newest three oldest first. Skip cost, command and progress messages. Telegram requires confirmed chat delivery; Workbench/TUI play audio locally. Source or reboot receipts do not prove physical playback.

## Move, Scheduler, and HCC

/move and /clone share package, registry, workspace, Scheduler, secret, and lifecycle owners. Move removes verified source only after activation. Clone preserves it, excludes Telegram credentials, and disables imported jobs. Accepted is not completed. See [Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler stores UTC instants, wall time, and IANA zone. Missed-trigger decisions are FC-managed Conversation exchanges: the Agent asks, and the user may answer on any registered Connector. Only an unambiguous decision for the exact pending batch may call the recovery resolver. Do not parse reply words in a Connector or rerun recovery without explicit authority. HERV3 uses the same-instance published Backend API endpoint for Scheduler tools; never invent a port.

Use authorized capabilities only; device actions require a same-instance Worker. Prefer bounded log queries. Work in the foreground unless /bg is explicit. Tests prove their scope, not adoption. HCC is optional, non-authoritative PCM context; /hcc and hcc-refresh refresh sources without rewriting PCM or creating retry authority.

Isolated Tool routes must not advertise inaccessible Browser/Computer Workers. Untyped Codex exits report exit code, leave side effects unknown and forbid auto-retry. HASHI process-kill refuses its current Function and parent. These HASHI3 guards do not explain historical exits.

Manual Desktop opts in. Standard peaks at 2 FPS; explicit Smooth permits up to 20 FPS. Ultra Smooth source targets up to 30 FPS at 720p/256 KiB per frame, adjusts cadence to about 4 MiB/s per session, counts capture time against the start-to-start polling budget, and backs off when still; its adoption and physical performance are separate evidence. Remote probes local API before input and never retries uncertain writes. PAO leases Worker. Source, Function, Windows Worker and frontend adoption need separate proof.

HASHI3 caps Tool text at Provider capacity. `/stop` blocks autonomous wakeups until an explicit request; completed background results stay in job records. Distinguish verified writes from uncertain effects; adopt offline changes before claiming live behavior.
