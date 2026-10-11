# HASHI Agent FYI

October 11 authoritative debug-call baseline: the current
[Call behavior contract](HASHI_CALL_CURRENT_BEHAVIOR.md) supersedes conflicting
historical notes. The fixed Phone control owns legacy `/phone`; the fixed video
control owns direct `/call`. They never cross-fallback. A video click immediately
requests microphone and camera; camera denial or failure degrades that direct
Call to audio-only. Camera and microphone toggles retain the same Call and
Session. Connected speech stops dial/ringback. Local video is mirrored in a
stable bounded preview while the Agent avatar and identity remain visible.
Captions use one canonical rolling PAO transcript. Public commentary and
complete answer chunks enter the same speech FIFO, with natural playable
chunking and no stage directions or artificial sentence gaps. If a known next
chunk is still synthesizing after the bounded playback grace period, the
approved prepared localized fillers may bridge the silence in order and stop as
soon as answer audio is ready; they never create another caption or Session row.
An unknown or unavailable Function has no entry. Call is a basic Function once
its selected STT/TTS models and credentials are ready; no Workbench per-instance
allowlist is required. Context actions sit outside the clipped flexible body, so
Stop speaking remains visible and clickable with video and rolling captions at
390×640. Phone and Call show a compact live voice-media cost: Phone uses the
qualified GPT Live per-minute rate; Call counts only STT/TTS media. Backend text
stays in `/metre`, Call vision stays excluded, and non-provider figures are
visibly marked as estimates.
Provider-declared Phone content-policy closure is terminal: show the explicit
content-filter reason, release the foreground line, and do not reconnect the
same rejected context as though the service were temporarily unavailable.
Terminal Phone calls also settle linked pending transport attempts. Startup
reconciles older terminal-call attempts, and Call/Phone admission ignores a
pending attempt whose linked logical call is already terminal, so a cancelled
resume cannot leave both entry icons permanently busy across a reboot.

The same candidate retains the recovered restart work: immutable qualified
generations are reused only after exact runtime/source/artifact checks;
duplicate warm-path verification is skipped without weakening the final
cutover check; the completion banner auto-dismisses and cannot be resurrected
by polling; receipt retry is limited to transient Windows pre-commit sharing
failures. Branch audit found no additional official Call or restart branch to
merge wholesale. Divergent historical branches contain superseded behavior.

October 11 Call hangup lifecycle correction: a failed 195-second HASHI3
replacement exposed a real Call-originated Provider request still running
after hangup. The old assertion preserved uncertain admission correctly but
wrongly detached the Run after PAO returned its identity. Hangup now waits only
until the exact request and Run IDs are known, then uses PAO's existing
targeted cancellation fence. A known Call Run is cancelled immediately. It
never becomes an Agent-wide stop and unrelated foreground/background work
remains protected.

Models, effort and endpointing remain unchanged. Source, artifact, running generation, renderer
adoption, restart timing and physical media acceptance are separate facts.
The Agent must not reboot HASHI4 for this recovery; the user owns the later
adoption reboot. Detailed October 10-11 notes remain in
[history](AGENT_FYI_HISTORY_2026-10-11.md).

HASHI4 source-load receipt: the final HASHI3 `debug-call` candidate was copied
from its previously matched baseline into this `debug-call` branch. At the
source-load checkpoint all 29 changed source, contract, repair, and regression
files matched the verified HASHI3 candidate byte for byte; this instance receipt
is the only later FYI addition. Two HASHI4-specific predecessor documents were
retained as explicitly superseded history. Independent validation passes 205
Call/restart checks and the Protected Core check. This is source qualification,
not running Function adoption; HASHI4 remained running and was not rebooted.
See [final source load](repairs/DEBUG_CALL_HASHI4_FINAL_SOURCE_LOAD_20261011.md).

Non-rebooted H4 live receipt: the new Workbench client passed physical media,
stable mirrored preview, retained avatar, one canonical transcript, natural
spoken projection, ringback termination, reversible controls, and hangup media
release. The old running Function did not publish commentary and released five
final speech segments together; even after the client played all three bridge
fillers, one synthesis wait left 7.367 seconds of silence. These four failed
gates are the exact behavior corrected in the loaded Function source. They are
not adopted until a separately authorized HASHI4 lifecycle action occurs.

October 11 Workbench entry-routing receipt: the video button had been wired to
a video-first launcher that silently fell back to legacy `/phone`, and the local
Workbench allow-list admitted direct Call only for HASHI2. Workbench commit
`d76fbc0949f21e19fb2c13eede70b0398dea77d6` restores strict ownership and
the scoped local configuration now admits HASHI2, HASHI3 and HASHI4. After a
managed Workbench reload, a physical HASHI4 video click opened only `/call`,
showed stable live camera, retained the Agent avatar, used one transcript, and
received an answer that correctly described the camera frame; no Phone audit
was created. A separate Phone click opened only the audio panel and created the
expected legacy attempt/call audit. Both calls ended normally. This correction
performed no HASHI4 lifecycle action.

October 11 final Call action and voice-cost receipt: Workbench behavior commit
`a396e12123a57a9b1576c45f728292f425518f57`, final Workbench source
`57c99bf97c01121463d820ff828a2e1fff94b2ad`, and HASHI4 Function commit
`912d9daf` are loaded on `debug-call`. Independent checks pass 53 focused
Workbench tests, its production build, 249 Call/Phone Function tests, and the
Protected Core check. The managed Workbench service reports the final source,
and the existing Electron window created a fresh renderer session after reload.
A physical direct Call opened a live 1024x576 camera track with mirror
transform, retained the Agent identity and one transcript, and kept all three
controls visible. During real playback, Stop speaking was fully inside the
viewport and the pointer hit-test selected the button; clicking it produced the
`user_stopped` playback receipt and returned the Call to listening. The Call
then ended and released camera and microphone. The Function source is staged for
the user-owned HASHI4 reboot; running-generation adoption and live cost display
remain pending that reboot. See
[source-load receipt](repairs/CALL_ENTRY_COST_HASHI4_SOURCE_LOAD_20261011.md).

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

Phone failures keep a safe durable cause. Content policy, provider authentication, quota, rate limit, rejected configuration, temporary provider outage, microphone permission, and uncertain network outcomes must not collapse into “line busy” or a generic disconnect. Failed/interrupted panels remain until user dismissal; normal hang-up may auto-dismiss.

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
