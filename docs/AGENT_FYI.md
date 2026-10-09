# HASHI Agent FYI

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

October 9 correction: the user revoked the unauthorized direct-OpenAI Call
configuration. Call uses only the originally approved OpenRouter Whisper,
Gemini Flash-Lite TTS and Gemini Flash vision targets. Call audio belongs in
the call window; ordinary voice attachments are suppressed for sealed Call
inputs. The user approved copying HASHI3's OpenRouter key to HASHI4; fresh
HASHI4 configuration passed all three provider probes. HASHI4 adoption remains
the user's reboot. See [restoration](call/OPENROUTER_RESTORATION_2026-10-09.md).

October 9: media defaults cover recording, TTS and Call/video. Complete installs prepare isolated helpers and model weights. Explicit off/permissions remain binding. HASHI4 adoption is reserved to the user. See [decision](HASHI_MEDIA_DEFAULTS_2026-10-09.md) and [repair](repairs/MEDIA_DEFAULTS_REPAIR_20261009.md).

HASHI3 adopted the Phone capability repair through shared hot reboot. Call,
video observation, Phone WebRTC and default TTS/recording admission passed with
explicit test devices and real services. Human/physical-device acceptance is
separate. The unchanged older Phone action harness still has four recorded
baseline failures; it is not counted as passed.

October 8: production Workbench/HASHI3 delivered real unlocked mouse,
keyboard/Unicode and public-entry input; native touch emulation passed. Hidden
chat polling pauses, selected-display fencing and Agent/manual exclusion remain,
and control errors persist. Physical-phone/credential-login acceptance is pending.
See [repair](repairs/DESKTOP_INPUT_REPAIR_20261008.md).

Prior notes/receipts remain in [October 8 history](AGENT_FYI_HISTORY_DESKTOP_20261008.md)
and [earlier history](AGENT_FYI_HISTORY_2026-10-06.md). They grant no new authority.

## Authority and ownership

PCM owns Persona, Context and Memory; PAO owns Agents, Sessions, Runs, jobs, Workzones and delivery; HERV3 owns Engine Turns, model/tool loop and cost; Frontend Connectors authenticate and render. Use the narrowest Function/configuration owner. Core has no product policy/imports. Protected Core requires explicit major-migration authorization, version bump, label and independent review; flags grant nothing.

Source, running Workers and delivery need separate proof. Agent tools cannot edit live Core/Python, read secrets, kill Core or grant authority. /reboot min|same replaces one Worker; max adopts shared Functions and Workers, leaving Core and Remote live. Check identity, generation, receipts and idle window. Windows restart needs exact actuator and exit code.

## Configuration, identity, and persistence

Configuration owns identity, ports, Workzones, endpoints and models. Ignore secrets/machine paths. Instance models use allowed_backends and runtime effort options; shared compatibility uses the qualified registry. Explicit choices persist; unknown capabilities/prices are neither unsupported nor zero cost.

Verified exact-model capabilities never expire. Unknown lookup failures can back off; refresh failures cannot erase verified facts. Changed model/adapter identity or validated source revisions may replace them. Pricing freshness is separate. See [capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable carries no credentials; PAO starts Workers. Private experiments stay private. Tool wildcard grants permission, not capability; naming a path grants nothing. Runs freeze enabled Workzone roots/revision; reload is idle-only. Exclude secrets/media bytes/remote paths from PCM, logs, chat and Git.

Successful Agent stop projects `stopped`; Worker outage is `offline`, config deactivation `inactive`. Shared Functions retain the stop marker until restart. Frontends derive visibility without rewriting `is_active`.

JSON writers validate private candidates under locks, revisions, and atomic replacement. Display fallback is read-only. On conflict, read fresh state and request a fresh action; never blindly retry or restore stale bytes. See [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Endpoint publication may retry only a Windows sharing-blocked pre-commit
rename, bounded and with the same fsynced candidate. Permanent failure retains
durable/in-memory route and revision. Never advertise unwritten state, weaken
identity or replay startup/actions.

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines own Sessions/Turns. Provider context and frontend history are rebuildable. Preserve replies/order. Stage attachments atomically per Message/Run; failed/cancelled assets never leak.

Incremental deliverables bind exact files and stable publication IDs. Persistence, endpoint receipts and UI visibility are separate; uncertain delivery never auto-retries. Final attachments remain final-only. See [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#64-incremental-assistant-deliverables).

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

HASHI3 Telegram intake reports healthy only after a successful bounded poll; ordinary Agent startup preserves pending updates. HERV3 may quote bounded unfinished WIP evidence in the turn but sends no premature recovery card. Source changes need separate Worker adoption and live checks.

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
/style preserves completed facts and meters separately. Fallback is opt-in:
warn before switching, forbid uncertain/committed effect replay. Reject malformed
Tool batches before effects. /stop, /retry, /resend and /steer differ; recovery
cannot restore revoked authority. Show typed progress, not private reasoning.

## UI, media, and Phone

Local cascade speech is an opt-in Function sidecar. Synthetic evidence does not establish physical audio acceptance. See [local Phone](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Live Phone defaults on; a missing provider API key blocks calls and must be named in `/phone`. An explicit instance opt-out remains valid. The Agent's Phone settings and PCM readiness do not prove provider readiness.

Renderers/catalogs own UI text. /language changes shared UI, /tui language local TUI; neither translates replies/IDs. TUI switches freeze generation/Agent/capabilities/Session. Remote authenticates. /telegram off and /whatsapp off disable future mirrors, preserving origin replies. /think and /commentary are independent.

Media bind one draft/instance/Agent/Run; Remote sends managed bytes. HERV3 gains no wider authority. Safe Voice uses typed confirm/discard; missing idempotency blocks upload and stale media is discarded. /voice uses a validated Function bundle/local media; Workbench gets Session audio, Telegram its voice renderer.

Phone stays bound to the owner's foreground Conversation until hang-up.
Actions receive bounded original PCM/Conversation/job evidence, excluding
scheduled prompts; lookups create no Run. Required context must fit. See
[PCM](HASHI_PCM_SYSTEM_DESIGN.md) and [PAO](HASHI_PAO_SYSTEM_DESIGN.md).

Phone fragments remain ordered durable Session speech. Acceptance, execution, playback and listening need separate evidence. Validate action origin/order, deliver final results and never auto-retry uncertain writes.

Phone handoff preserves its frozen ID only after scoped snapshot/delegation
validation. Pre-model errors settle the durable Run and visible activity through
the result owner; caught errors are not success. Grouping for handoff byte limits
is lossless: retain roles, text, event IDs, order and timestamps and accept old
snapshots. It grants no authority/retry. See [handoff](HASHI_PHONE_CONTEXT_HANDOFF.md).

Qualified Function adapters own Phone settings: apply next call, retain provider
on recovery. Open once with Persona/language after readiness; yield to speech.
Window movement cannot hang up. Failed PCM refresh allows stop/inspection only.
Sideband follows the primary Session fence; explicit hang-up wins stale faults.
Keep source, adoption and device acceptance separate.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), retain Session and remain owner-admitted during active Runs. /new creates a fresh Session without deleting history; PAO owns Agent deletion/cleanup receipts.

`/say` reads the latest final reply, `/say 2`–`/say 4` that many, `/say 1-3` the latest three oldest first. Skip cost/command/progress. Telegram needs confirmed chat delivery; Workbench/TUI play locally. Reboot receipts never prove playback.

Historical call checkpoints are superseded by the current [call decision](HASHI_CALL_INTERACTION_2026-10-04.md); physical audio, camera, accuracy and latency acceptance remain separate.
The October 9 HASHI3 repair binds Call availability/start to the page's registered
connection instead of another device's selection. Daily frontend adoption and
file-microphone recognition/reply/playback/hangup pass; 52 focused checks pass.
Physical microphone/camera and hearing remain personal acceptance.


Remaining Move/Scheduler/HCC and prior deployment notes are preserved in [October 9 history](AGENT_FYI_HISTORY_MEDIA_20261009.md). Historical deployment scopes grant no current authority.
