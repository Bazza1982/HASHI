# HASHI Agent FYI

October 11 Call preview/streaming correction: the consolidated `debug-call`
line had reintroduced four errors even though the older design decisions were
still present. Local video replaced the Agent avatar, its flexible sizing let
captions collapse the preview, playback state created a second caption track,
and direct Call labelled the presentation surface as `session-api`. The last
error disabled Workbench answer previews and made speech wait for the terminal
answer.

The current candidate keeps a stable mirrored preview together with the Agent
identity, renders one canonical rolling transcript, and distinguishes Workbench
presentation from Session API transport. Public commentary and complete answer
sentences enter the existing FIFO immediately. No additional historical branch
is a merge input; wholesale merging would restore explicitly retired behavior.
HASHI3 Frontend Call passes 144/144, Workbench Call passes 89/89, and the
production client build passes. Live adoption remains a separate fact.

October 11 restart-lifecycle correction: the expanded `debug-call` audit found
two formal performance commits that the earlier audit had incorrectly called
equivalent. They survive only under the retained
`migration-hashi2/preserve/h2-runtime-cache-20260917` remote-tracking ref and
were never ancestors or patch-equivalent. Their runtime-fingerprint cache and
tests are now ported onto the current Function-generation API without merging
the branch's superseded dependency-drift implementation. The older worker-start
optimization is already an ancestor. Recent Workbench branches remain ancestors,
patch-equivalent, or semantically superseded; the divergent
`fix/nightly-nonvoice-20261003` line must not be merged wholesale.

The later 37-second live regression was not another missing branch. Two newer
API paths bypassed the recovered cache: a generation returned by
`qualified_generation()` was immediately source-verified again by
`prepare_generation()`, and Worker preparation called the uncached dependency
digest directly. The trusted immediate handoff now skips only that duplicate
first check; default and untrusted callers still verify, and the final
pre-cutover source verification remains. Workers now use the authoritative
current runtime-fingerprint cache.

The same correction hardens two observed presentation and persistence
boundaries. A late success-banner timer remembers the dismissed operation even
after a newer reboot becomes the active target, so polling cannot resurrect it
or clear the newer result. Receipt persistence boundedly retries only transient
Windows pre-commit replace denials; permanent and post-commit failures remain
visible. Three focused red/green checks, the 129-case expanded restart suite,
and the protected-Core gate pass.

The exact HASHI3 `debug-call` candidate then completed three real Lumi restarts
in 17.4 seconds after the cold instance start, 14.3 seconds, and 9.8 seconds.
Canonical start preceded success; the completion banner remained for about four
seconds, dismissed automatically, and did not return on later polling. A
post-restart physical Electron Phone-entry test opened video-first, preserved
camera off/on and microphone mute/unmute, used natural telephone speech, and
released media on hangup. See
`docs/repairs/DEBUG_CALL_RESTART_RECONCILIATION_20261011.md`.

October 11 authoritative `debug-call` recovery: the clean candidate is defined
by [the current behavior contract](HASHI_CALL_CURRENT_BEHAVIOR.md), which
supersedes conflicting historical Call notes. Both fixed Phone and video
controls start the same video-first direct Call. Either click immediately starts
microphone and camera acquisition; camera denial or device failure degrades the
same call to audio-only. An unresolved route is refreshed on foreground and
rechecked on click, and legacy Phone is used only after direct Call is confirmed
unavailable. Camera off/on, microphone mute/unmute, captions, speech, and hangup
retain the same Call and Session binding. Dial/ringback stops before connected
speech or microphone admission.
Call presentation uses natural spoken language and removes stage directions
from Call captions and TTS while preserving the canonical PAO Session answer.
Models, reasoning, endpointing, default availability, and installation-template
behavior are unchanged.

The exact HASHI3 candidate is on `debug-call`. Focused Workbench Call and
Phone/live checks pass, HASHI3 Frontend Call passes 143/143, the complete
Workbench server suite and production build pass, and the protected-Core check
is clean. Real HASHI3 acceptance used the physical LifeCam microphone/camera and
speaker. It verified both fixed launch entries, immediate video-first startup,
same-call camera off/on and microphone mute/unmute, caller/Agent captions,
completed TTS playback, ringback termination, and media release on hangup. The
established controls, rolling captions, natural telephone speech, and once-only
opening remained present.

The shared Workbench service is running the verified `debug-call` client.
Service identity alone is not enough: the final acceptance relaunched Workbench
through its official launcher, verified the newly created renderer emitted the
expected direct Call route request, and then ran the physical Phone-entry test.
Source, build, service, shared Function generation, and loaded renderer are
separate release facts.
HASHI4 source is loaded on its own `debug-call` branch. Its product, contract,
and test blobs match the HASHI3 candidate; the protected-Core check is clean,
and the exact HASHI4 tree passes 143/143 Frontend Call checks in an isolated
checkout. Without rebooting HASHI4, live Workbench acceptance verified the
physical LifeCam, same-call camera off/on, microphone mute/unmute, captions,
ringback termination after readiness, and hangup media release. An external
Windows speaker phrase produced no caller transcript and was not admitted as
caller speech, so it is not counted as a second H4 speech-loop proof.
The running HASHI4 Function has not adopted the new generation, and no HASHI4
reboot is authorized. See Workbench
`docs/call/CALL_DEBUG_BASELINE_2026-10-11.md`.

October 10 Workbench Call-control visual repair: the shared button design
system painted camera-on and keyboard-focus state on the full icon-and-label
wrapper, producing the reported oversized oval and outer ring. Workbench now
keeps that wrapper transparent and paints pressed, hover and focus state on the
circular icon. A real-browser red/green check passes at PC (1440 x 900), pad
(820 x 1180) and phone (390 x 844) sizes; all three screenshots were visually
reviewed, 47 selected Call checks pass, and the production build succeeds. The
new assets are served by the current Workbench without a process restart.
Open pages need one reload. No HASHI Core or runtime code changed. See Workbench
`docs/call/CALL_CONTROL_VISUAL_2026-10-10.md`.

October 10 Workbench history-order repair: the earlier ordering fix remained
deployed, but delayed durable Frontend Connector errors were an uncovered event
type. The canonical transcript stayed ordered while an old provider failure
arrived through the answer feed at the live edge; snapshot refresh then retained
that wrong local position. Workbench now merges non-transient durable feed
errors by original event time and preserves their position across refresh.
A red/green regression changed the reproduced 5-15-10 order to 5-10-15; 75
focused checks, a clean production build, a replay of the reported conversation
data, baseline bundle comparison, and public static asset hash verification
pass. The fixed bundle is deployed to the current H4-connected Workbench
service. Already-open pages need a reload. No Core code changed and no HASHI
runtime was restarted. See Workbench
docs/implementation/chat-history-order-20261010.md.

October 10 Call pause repair: content-free diagnostics from the reported call
showed that one-segment lookahead was working, but a 27-character first phrase
was followed by a 272-character TTS request. The first audio lasted about 5.4
seconds while the next synthesis took 15.7 seconds, exposing roughly 10 seconds
of silence; the next turn exposed an even larger mismatch. Call segmentation
now grows from the preceding playable segment instead of jumping directly to
the 600-character ceiling, while retaining the 12-segment/600-character bounds
for long answers. Workbench's bounded one-segment lookahead and its three
failure/cancellation checks are restored in source. HASHI3 and synchronized
HASHI4 source each pass 132 Call checks; 43 Workbench Call checks and the
production build pass. The public client used for the report already carried
the lookahead, so no static republish was needed. HASHI4 running adoption still
requires the user's lifecycle action. No Core or runtime was restarted. See
repairs/CALL_VOICE_PREVIEWS_20261010.md.

October 10 direct Call answering repair: the user's Chinese/gender voice menu
and 120 prerecorded samples were present in HASHI3 but missing from HASHI4;
the complete verified source and assets are now synchronized to HASHI4. Direct
Call keeps symbolic dial/ringback audible for at least 1.4 seconds, plays
connected/failure/hangup cues, and uses a once-only Agent opening turn before
opening the microphone. Call guidance now controls spoken presentation rather
than length: natural sentences and pacing, no written report formatting, with
long stories and detailed explanations allowed for as long as needed. Call-only
presentation clears after hangup. Confirmed hangup also boundedly rechecks a
briefly stale busy route, so the entrance clears without a reload. HASHI3 and
synchronized HASHI4 each pass 131 Call checks; the clean Workbench suite passes
69 checks and its public bundle and active service worker match build
f9c6104ba662. HASHI4 source commit e6a06ae9 is
not running adoption: shared Functions still need separately authorized
`/reboot max`. No Core or HASHI runtime was restarted. See
repairs/CALL_ANSWERING_EXPERIENCE_20261010.md.

October 10 Call/Workbench live recovery: the public blank screen came from an
inactive-Agent controller effect referencing an undefined identifier. Direct
`/Call` also treated one transient Cloudflare 502 during audio submission as a
permanent pause, and low variable microphone background was repeatedly uploaded
after cloud STT rejected it because only the steady floor, not the louder peak,
was retained. The Workbench now holds uncertain submissions for authoritative
snapshots without replay, tolerates two transient snapshot-read failures,
recovers automatically on the next successful read, and learns rejected peak
background so it is not sent again. Foreground audio resumes once, manual mute is
preserved, and both direct Call and native Phone use the restored 640-pixel
phone-like height.

The exact public final bundle and active service worker carry revision
8a1a9b9bc9e7, with no waiting generation or false update banner. A one-click
public direct Call reached `可以说话` within five seconds and stayed ready for 24
seconds; the immediately preceding identical logic stayed ready for 47 seconds.
Public native Phone received a real Agent greeting and passed hidden/foreground
auto-mute, auto-resume and manual-mute retention. Sixty-seven Call checks, five
voice-preview checks, 20 OpenRouter checks and the production build pass. HASHI4
still selects `x-ai/grok-stt-1.0`; a real provider-chain fixture transcribed the
known Chinese sentence exactly in 3.284 seconds. No HASHI runtime or Core process
was restarted or modified. Physical iPhone/iPad microphone, battery and hearing
acceptance remain open. See Workbench
docs/call/CALL_WORKBENCH_LIVE_RECOVERY_2026-10-10.md.

October 10 Call follow-up: user approved Grok STT 1.0 with automatic language
detection. HASHI3/HASHI4 instance Call configuration selects it; the live HASHI4
context read the matching revision. The four synthetic probes returned correct
Chinese “谢谢” and the full topic sentence, correct English, but “嗯” became “文”.
The earlier Scribe suggestion is superseded by this explicit trial choice.

HASHI3 now contains Chinese/gender Call voice labels, 30 voices with English and
Chinese prerecorded samples, and selection/replay through the existing FC media
notification path. Workbench receives MP3 (OGG is unsupported in the tested
WebKit build); Telegram retains OGG. All 120 packaged files decode. The first
spoken final-answer phrase is shorter, and the external client prepares one
segment ahead. Synthetic first-audio synthesis dropped from 5.3–5.8 s to 3.2 s.
143 Python cases, 30 controller cases, build and four browser layout/playback
fixtures pass. These do not constitute physical iPhone latency acceptance.
HASHI4 backend adoption is still pending explicit production/lifecycle scope;
Grok configuration can apply to a new call without that adoption.
See repairs/CALL_VOICE_PREVIEWS_20261010.md for separate source/live evidence.

October 10 Call audit: mobile Phone and Call entries overlapped in one grid cell;
Workbench patch 140f4ff is published and reserves independent reachable targets.
All 21 layout cases pass, including eight rotation/split changes; the public
entry returns the new assets. Open device pages need a reload. HASHI3's Call
service now wakes on a published camera observation instead of waiting for the
continuous observer task to finish. Focused red/green evidence and 52 tests pass.
The backend correction is not yet adopted by HASHI4; no runtime was restarted.
A four-sample provider comparison supports evaluating Scribe v2 with automatic
language choice, but no model was switched and no real-user accuracy is claimed.
Actual reported TTS stages took 5.1–7.6 seconds, so STT alone is not the main
latency remedy. See repairs/CALL_LANGUAGE_LATENCY_20261010.md for boundaries.

October 10 mobile power repair: Workbench now suspends question, reboot-discovery
and overview polling while hidden, serializes slow reads, and backs off when
idle. A 15-second fixture fell from 22 question/reboot reads to 2 foreground
reads and zero hidden reads. Audio URLs are attached only on play activation;
WebKit's preload hint alone was insufficient. History completion now clears
the loading spinner while preserving explicit Session isolation.
Source patch 716fae7 is deployed; 44 focused checks and browser media/visibility
checks passed. Actual service phone/iPad screenshots were reviewed, and the
public hidden browser completed zero API reads in a bounded 15-second sample.
This is not a physical battery or temperature measurement. Open device pages
need a reload; no HASHI runtime was restarted and no Core code was changed.
See Workbench docs/implementation/mobile-idle-media-20261010.md for evidence
and the independently reproduced Playwright WebKit native-controls warning.

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
