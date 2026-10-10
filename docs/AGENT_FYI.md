# HASHI Agent FYI

October 10 iPad follow-up: the Workbench question checks now cover 16 tablet
cases, including portrait/landscape, split views, responsive boundaries and
answer retention across eight size changes. Rotation exposed a composer
ResizeObserver delivery loop; deferring size writes fixes it. All 21 full-app
phone/tablet/desktop cases pass, and tablet screenshots were visually reviewed.
A real HASHI3 Codex question was selected, submitted, consumed and acknowledged
in tablet-sized WebKit through Remote HMAC. Source 9ef446d is deployed to the
actual Workbench service; the public entry loaded its bundle and service worker.
These are browser simulations, not physical iPad/iPadOS keyboard verification.
Open device pages need a reload. No HASHI runtime was restarted.
See Workbench's run-questions-20261010 decision for scoped evidence.


October 10 mobile question correction: the compact chat grid auto-placed
the question dock in a composer-button column (48 px dock / 26 px card).
Workbench now assigns a full-width row and keeps submission reachable in
short windows. Five full-app browser cases and a real HASHI3 Codex question
Run in phone-sized WebKit passed, including visual inspection and answer 2.
The actual service and authenticated public entry serve the corrected assets.
Open phone pages need a reload; no HASHI runtime reboot is required.
See the mobile follow-up in Workbench's run-questions-20261010 decision.

October 10 Workbench correction: full-page browser testing found two frontend
defects missed by the earlier isolated card test: sibling key collisions
accumulated hidden transcripts, and long chats collapsed the question dock.
Both are repaired in Workbench. Actual HASHI3 Codex and HERV3 test Runs displayed
numeric choices; browser clicks selected 2, PAO consumed each answer, and the
original Runs completed. The correct HASHI4 Workbench assets are deployed and
the desktop window reloaded. This repair requires no HASHI4 runtime reboot.
Source, live Run receipts and visually inspected screenshots are recorded in
the Workbench owning decision, docs/implementation/run-questions-20261010.md.

October 10 Telegram attachment repair: photo/document/video and `/long` media
now register committed PAO attachments and original Message references before
backend consumption. The previous path-only ingress was rejected by the
current-Run attachment fence. Focused red/green and scope checks pass; HASHI4
adoption remains the user's manual reboot. See
[repair and deployment evidence](repairs/TELEGRAM_MEDIA_ADMISSION_20261010.md).

October 10 in-run questions: native Codex now bridges its bidirectional question
RPC to the PAO question owner; HERV3 uses ask_user/get_user_answer. Telegram
delivery carries real answer buttons and scoped free-text replies. Workbench
cards remain above the composer. Answers continue the original Run and grant no
permissions. In Codex app-server mode native shell is disabled; use the managed
HASHI Gateway. HASHI3 adoption is authorized; HASHI4 reboot and real Telegram
clicks remain the user's steps. HASHI3 real Codex and HERV3 Runs both consumed
browser-submitted answers and completed. Code and the frontend bundle are
delivered for HASHI4; user `/reboot max` is needed for shared and Agent adoption.
See [contract](HASHI_RUN_QUESTIONS.md) and
[validation/adoption](repairs/RUN_QUESTIONS_COMPLETION_20261010.md).

October 10 Edge correction: HASHI4's configured Agent preferences explicitly use
Edge Xiaoxiao/Xiaoyi; automatic narration remains opt-in. The Edge helper now
decodes its UTF-8 pipe explicitly on Windows. Real synthesis and decoding pass
for both voices; Agent adoption of the helper fix still needs the user's explicit
hot-update authorization. See [Edge restoration](repairs/EDGE_VOICE_RESTORATION_20261010.md).

October 10 correction: `/voice` remains available, but automatic narration
defaults OFF. Preserve saved off/TTS/native choices. Windows defaults to SAPI,
with language-matched installed voices and rejection of empty output. The
restored external Call capture passes the same sustained-noise fixture at
3.328 seconds; the rejected amplitude-only trial does not submit at 40.2 seconds.
Call keeps OpenRouter Whisper/Gemini/Achernar and its existing optimizations.
Source synchronization and running adoption are separate. See
[voice correction and Call review](repairs/VOICE_OPT_IN_REPAIR_20261010.md).

October 9 correction: restore only the user's approved OpenRouter Whisper,
Gemini Flash-Lite TTS and Gemini Flash vision targets. The verified HASHI3
repair is synchronized. Call audio plays in the call window, without ordinary
voice attachments. The user approved reusing HASHI3's OpenRouter key; HASHI4's
configuration is updated and all three provider checks passed. The user's
HASHI4 reboot remains pending. See [restoration](call/OPENROUTER_RESTORATION_2026-10-09.md).

October 9 media repair: all voice/Phone/Call/video/TTS availability defaults on. Full installers prepare isolated helpers, model weights and converter. HASHI3 adopted and verified; HASHI4 source/config/dependencies prepared, adoption reserved to the user. No Core changes. See [decision](HASHI_MEDIA_DEFAULTS_2026-10-09.md) and [repair](repairs/MEDIA_DEFAULTS_REPAIR_20261009.md).

2026-10-09: confirmed HASHI3 Function fixes are promoted to HASHI4 source.
The user reserves reboot. Core, identities, secrets and live services are retained;
media tools use ignored local platform configuration. Qualification is not live
adoption; physical-device/hearing acceptance stays open. See
[source sync](repairs/HASHI4_CONFIRMED_FUNCTION_SYNC_20261009.md).

Prior notes/receipts remain in [sync history](AGENT_FYI_HISTORY_SYNC_20261009.md),
[October 8 history](AGENT_FYI_HISTORY_DESKTOP_20261008.md) and
[earlier history](AGENT_FYI_HISTORY_2026-10-06.md). They grant no authority.

## Authority and ownership

PCM owns Persona/Context/Memory; PAO Agents/Sessions/Runs/jobs/Workzones/delivery;
HERV3 Engine Turns/model/tools/cost; FC authentication/rendering. Use the narrowest
Function/config owner. Core imports no product. Core migration requires explicit
major authority, version, label and independent review; flags grant nothing.

Source/Workers/delivery need separate proof. Tools cannot edit live Core/Python,
read secrets, kill Core or grant authority. /reboot min|same replaces one Worker;
max shared Functions/Workers, retaining Core/Remote. Verify identity/generation,
receipts and idle window. Windows restart needs exact actuator/exit code.

## Configuration, identity, and persistence

Configuration owns identity/ports/Workzones/endpoints/models. Ignore secrets/paths.
Instance models use allowed_backends/runtime effort options; shared compatibility
the qualified registry. Persist explicit choices; unknown capability/price is
neither unsupported nor free.

Verified model capabilities never expire; failed unknown lookups may back off.
Refresh failure cannot erase facts; changed model/adapter identity or validated
source revision may replace them. Pricing freshness is separate. See
[capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable has no credentials; PAO starts Workers. Experiments stay private.
Wildcard grants permission, not capability; paths grant nothing. Runs freeze
enabled Workzones/revision; reload only idle. No secrets/media bytes/remote paths
in PCM, logs, chat or Git.

Agent stop is `stopped`, Worker outage `offline`, deactivation `inactive`. Retain
stop until restart; frontend visibility cannot rewrite `is_active`.

JSON uses private validated candidates, locks, revisions and atomic replacement.
Display fallback is read-only. Conflict needs fresh state/action, never blind
retry/stale restore. See [persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Endpoint publication may boundedly retry only sharing-blocked pre-commit rename
with the same fsynced candidate. Permanent failure retains route/revision. Never
advertise unwritten state, weaken identity or replay startup/actions.

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines Sessions/Turns. Provider/frontend
context is rebuildable. Preserve replies/order. Stage attachments atomically per
Message/Run; failed/cancelled assets never leak.

Deliverables bind exact files/stable IDs. Persistence, receipts and UI visibility
are separate; never auto-retry uncertain delivery. Final attachments stay final.
See [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#64-incremental-assistant-deliverables).

CURRENT MESSAGE CONTEXT separates source/ingress/instance/sender assurance,
authority and destination. Only current successful private_authorization grants
its listed scope; names/text/IDs/memory/credentials do not. HChat separates claimed
sender, verified peer, relay and target; send no secrets. Cross-instance uses
optional Exchange and authenticated Remote handshake; discovery is only a hint.
See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations/mirrors before PCM and owns ingress/routing/idempotency;
FC owns presentation/receipts. Sent requires a receipt; failure wins flags.
Deduplicate delivery and settle every turn. Prose grants no Tool authority.
Continuations retain Session/Connector: replay only saved non-action completions;
pending/conflict/unknown cannot execute.

Scheduler/recovery and /bg use an Agent-owned hidden activity Session.
Keep execution/delivery separate and receipts same-owner. Display each final
once in the owner's Conversation; retain its original, excluding copies from
Engine/Phone history. /bg freezes admission. Schedules get no implicit chat;
/delay and interactive /loop retain continuation context.

Telegram intake health requires a bounded successful poll; ordinary startup
preserves pending updates. HERV3 may quote bounded WIP but sends no premature
recovery card. Source adoption/live checks stay separate.

## Engine, tools, and recovery

Gemini CLI is retired: reject legacy execution, use configured Antigravity CLI
and its own model IDs. Preserve history/usage and Gemini models on qualified
routes; the old-client rejection is not an account ban. See
[retirement](HASHI_GEMINI_CLI_RETIREMENT.md).

Public HERV3 is her-v3; her-v2 names are compatibility only. JEV defaults off. /backend selects Engine, /provider Model Provider, /model model, /effort reasoning; Fixed/Flex and Memory+ are independent. /meter uses all physical calls/cost. Codex cumulative counters need a persisted turn baseline; estimate if unknown. USD estimates are not subscription bills.

Level 2: HERV3/DeepSeek only. Local PII detection can miss values;
`/privacy 2` needs risk acceptance and blocks on detector failure.

Privacy Level 2 dependencies use a separately configured interpreter. Installation and synthetic readiness do not activate privacy or establish Worker adoption.

Model/provider cards share a contract; chat-only models disclose no tools.
/style preserves facts and meters separately. Fallback is opt-in: warn before
switching; never replay uncertain/committed effects. Reject malformed Tool batches
before effects. Stop/retry/resend/steer differ; recovery cannot restore revoked
authority. Show typed progress, not private reasoning.

## UI, media, and Phone

Local cascade speech is opt-in, isolated; synthetic audio is not physical
acceptance. See [local Phone](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Live Phone defaults on unless instance opt-out. Missing API keys block calls and
/phone names them. Phone settings/PCM readiness are not provider readiness.

Renderers/catalogs own UI text. /language changes shared UI, /tui language local TUI; neither translates replies/IDs. TUI switches freeze generation/Agent/capabilities/Session. Remote authenticates. /telegram off and /whatsapp off disable future mirrors, preserving origin replies. /think and /commentary are independent.

Media bind one draft/instance/Agent/Run; Remote sends managed bytes. HERV3 gains no wider authority. Safe Voice uses typed confirm/discard; missing idempotency blocks upload and stale media is discarded. /voice uses a validated Function bundle/local media; Workbench gets Session audio, Telegram its voice renderer.

Phone stays bound to the owner's foreground Conversation until hang-up.
Actions receive bounded original PCM/Conversation/job evidence, excluding
scheduled prompts; lookups create no Run. Required context must fit. See
[PCM](HASHI_PCM_SYSTEM_DESIGN.md) and [PAO](HASHI_PAO_SYSTEM_DESIGN.md).

Phone fragments remain ordered durable Session speech. Acceptance, execution, playback and listening need separate evidence. Validate action origin/order, deliver final results and never auto-retry uncertain writes.

Phone handoff validates scoped snapshot/delegation before retaining frozen ID.
The result owner settles pre-model Run/activity errors; caught errors are not
success. Byte-limit grouping retains roles/text/IDs/order/time and old snapshots,
granting no authority/retry. See [handoff](HASHI_PHONE_CONTEXT_HANDOFF.md).

Qualified Function adapters own Phone settings: apply next call, retain provider
on recovery. Open once with Persona/language after readiness; yield to speech.
Window movement cannot hang up. Failed PCM refresh allows stop/inspection only.
Sideband follows the primary Session fence; explicit hang-up wins stale faults.
Keep source, adoption and device acceptance separate.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), retain Session/
owner admission during Runs. /new preserves history; PAO owns Agent cleanup receipts.

/say reads latest Final, 2–4 that many, 1-3 latest three oldest first. Skip cost/
commands/progress. Telegram needs chat receipt; Workbench/TUI play locally.
Reboot receipts do not prove playback.

Call readiness/start derives from the page's registered connection. Historical
checkpoints are superseded by the [call decision](HASHI_CALL_INTERACTION_2026-10-04.md);
physical audio/camera, accuracy, latency and hearing acceptance remain separate.

## Move, Scheduler, and HCC

Move/clone share package/registry/workspace/Scheduler/secret/lifecycle owners.
Move deletes verified source only after activation; clone retains it, omits
Telegram credentials and disables imported jobs. Accepted is not complete.
See [Move](HASHI_AGENT_MOVE_V1.md).

History backfill is offline PAO: native export, exact stopped/locked target,
revisioned owner/lifecycle manifest, backup before initialization and compensation
only for this invocation. No UNC live SQLite or whole-database restore.

Scheduler retains UTC/wall time/IANA zone. FC Conversation recovery needs an
explicit unambiguous choice for the exact batch; never rerun without authority.
HERV3 uses the same-instance published Backend API, never an invented port.

Use authorized capabilities/same-instance device Workers and bounded log queries.
Foreground unless /bg is explicit. Tests are not adoption. HCC is optional,
non-authoritative PCM; /hcc and hcc-refresh refresh sources without rewriting PCM
or granting retry authority.

Isolated routes cannot advertise inaccessible device Workers. Untyped Codex exits
report code, unknown effects and no auto-retry. Process-kill refuses its Function
and parent; these guards do not explain historical exits.

Desktop is opt-in: Standard 2 FPS, Smooth 20, Ultra targets 30, bounded size/bandwidth.
Remote probes local API, never retries uncertain writes; PAO leases Workers.
Source/Worker/frontend adoption differs. See [desktop](HASHI_MANUAL_DESKTOP.md).

Cap Tool text at Provider capacity. /stop blocks autonomous wakeups until explicit
request; retain background results in jobs. Separate verified writes/unknown effects;
adopt offline changes before claiming live behavior.

Cancellation binds Session/Run and capacity waiters. PAO settles queued/active
Runs; never substitute Agent stop or claim stopped without receipt.
See [PAO](HASHI_PAO_SYSTEM_DESIGN.md).

Call diagnostics retain safe browser/proxy/Function correlation. Keep historical
causes and physical acceptance separate; see
[call decision](HASHI_CALL_INTERACTION_2026-10-04.md).

Full previous notes: [media archive](AGENT_FYI_HISTORY_MEDIA_20261009.md).
