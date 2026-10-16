# Phone and Call interaction decision

## 2026-10-17 current-turn raw live-frame bridge experiment

The user approved an immediate HASHI3 experiment that keeps VoiceLAN MiniCPM-V
as the continuous, free local visual narrator while also giving a capable main
answer model direct visual evidence. Camera-on capture now targets two JPEG
frames per second. Accepted frames feed both the existing latest-frame-wins
MiniCPM-V observer and a separate bounded in-memory ten-second ring.

At each caller message, the Call Function freezes the ring and evenly samples
at most ten frames across that window, oldest to newest with the newest frame
included. HERV3 resolves the exact stage provider/model capability after its
profile is applied. Only a primary answer stage whose adapter supports image
data URLs receives labelled structured images; a text-only model receives none.
This is capability routing, not intent detection. The user turns the visual
path off by turning the camera off.

The MiniCPM-V description remains useful auxiliary untrusted context, but does
not replace the raw images for a vision-capable answer model. A turn-specific
image observation is no longer overwritten by the continuous generic observer.
Raw frame bytes are removed from request metadata before PAO admission and
canonical audit, are held only in the selected Agent Worker for that Run, and
are cleared on completion or Worker shutdown. Camera-off or hangup clears the
live ring for later turns without revoking an already accepted turn snapshot.
Provider limits remain authoritative; DeepSeek keeps its documented 600-image,
32 MiB per-image, 64 MiB decoded-image aggregate, and 48 MiB serialized-body
checks. Protected Core remains unchanged. Source validation and physical Call
acceptance are separate; see
[experiment record](repairs/CALL_LIVE_FRAME_BRIDGE_EXPERIMENT_20261017.md).

## 2026-10-16 explicitly allow-listed private-LAN media targets

The instance-owned Call target registry may now declare a target with
`location: lan`. A LAN target uses the existing OpenAI-compatible media
adapter, but its URL host must be a literal IPv4 address inside an RFC 1918
private range. Hostnames, loopback, link-local, public addresses, embedded
credentials, query strings and redirects remain unavailable. This keeps the
endpoint an explicit operator choice and prevents provider or browser input
from turning Call into a general network client.

The first intended use is a VoiceLAN MiniCPM-V visual snapshot target. Target
selection remains revisioned per owner/Agent and applies to the next Call;
Gemini remains an available independent target. The Frontend Connector owns
the target validation and adapter call, while the endpoint/model stay in local
instance configuration. Protected Core, frame cadence, camera fallback, STT,
TTS, Session authority and model reasoning are unchanged. Source validation,
runtime adoption and a physical `/call` acceptance remain separate evidence.

## 2026-10-14 call-time context and multilingual filler variation

The Call opening remains a separate, tool-free Agent utterance built from the
typed PCM voice projection. That narrow projection intentionally does not pass
every general PCM section, and had omitted the ordinary PCM time section. The
opening now receives two explicit facts from the Call Function: the current
local date/time and the timestamp of the latest user message in the same HASHI
Conversation Session. The model is told to respond appropriately and naturally
with those facts and its existing Persona, relationship, language and recent
conversation context. This supplies context only: it does not force a greeting,
calculate a daypart, validate wording, or require the timestamps to be spoken.

Each supported Call locale (en, zh-CN, zh-TW, and ja) now has ten short, ten
medium and ten long conversational fillers. Slots 0–9, 10–19 and 20–29 are the
respective tiers. Workbench keeps the existing latency stages, rotates within
each tier without an immediate repeat, and varies the starting position by
Call. The server prepares only a filler the client actually asks for and caches
the resulting voice asset; opening a Call no longer synthesizes an unused
catalogue. Fillers remain presentation-only audio and never become a Message,
transcript row or Agent answer.

PCM and the PAO Call Function own the opening context and localized catalogue.
The Frontend Connector owns tier selection, rotation and playback. Protected
Core, selected models, reasoning, speech endpointing and Session authority are
unchanged.

## 2026-10-13 complete debug-call reconciliation

The user requested preservation of all correct HASHI3/HASHI4 repairs, then
explicitly directed all ongoing work back to `main`. The reviewed merge is
complete; HASHI3 and HASHI4 share GitHub `main`, and Workbench uses its own
GitHub `main`. Continue only on those existing `main` branches, without a new
branch or worktree. The [reconciliation record](repairs/DEBUG_CALL_RECONCILIATION_20261013.md)
retains both original histories, the complete 65-path inventory, and closeout
evidence. Earlier `debug-call` construction instructions below are superseded.

PAO Functions preserve the once-only canonical opening, all opening-segment TTS
ordering, terminal Phone attempt cleanup and durable safe provider errors. PCM
retains relevant spoken-turn timestamp guidance and main's independent memory
privacy/atomic-import/ACK implementation. Both desktop regression sets remain.
Historical records below retain their original scope: a selected-file match
was not proof of full-tree equality, and an earlier deployment receipt is not
current adoption evidence. Source synchronization in this repair authorizes no
reboot or new physical Call; Protected Core remains unchanged.

## 2026-10-13 multi-camera continuity and diagnostics correction

The user approved the multiple-camera implementation and then reported that its
switch action was visually poor and that a live camera repeatedly fell back to
voice. Correlated HASHI4 evidence established two causes: Call equated a slow or
stale completed vision observation with failed camera ingress, and a Workbench
snapshot started before camera enable could arrive afterward and cancel the new
preview. There was no browser track-ended event, frame-upload failure, or
bandwidth failure during the observed interruptions.

Camera liveness is now owned by accepted frame ingress, separately from vision
inference freshness. The startup window still falls back when no first frame
arrives. Once a frame is accepted, ringback may wait for the in-flight first
observation; later slow inference shows pending/delayed state while fresh frames
continue. The camera falls back only when frame ingress actually stops for the
freshness window or the first observation explicitly fails. Voice, Call
binding, frame cadence, and one-default-camera startup remain unchanged.

Workbench fences camera state from requests that predate a local camera
transition. It records ignored stale responses and authoritative fallback
state/reason/epoch. The Function journal now preserves startup, observation,
and frame-unavailable reasons plus last-frame age; no media content is logged.
The switch action keeps its 44×44 accessible target but uses a recognizable
camera-and-swap glyph on a quieter translucent surface. Protected Core is
unchanged.

## 2026-10-13 Agent-visible video readiness and conversational use

The user approved making Agent-usable vision, rather than a successful local
camera preview, the source of truth. Video Call now opens the camera during
ringback, waits for local readiness before starting the Agent opening, and sends
the first frame while ringback continues. The startup window requires an
accepted frame, not a completed provider observation. If no frame arrives, the
same Call continues by voice, local preview turns off, and the camera control
remains an explicit retry. If a frame is accepted, ringback can continue while
the first observation completes. This is not a cross-transport fallback to
Phone.

The visible state machine is preparing, current, delayed, or voice-only.
Observations are current through 12 seconds and then carry a visible delay
warning. Observation age does not disable a camera while new frames continue to
reach HASHI. A stopped frame stream or confirmed first-observation failure
disables the camera until the caller retries. One transient upload/provider
failure retains the last usable observation; increasing frame cadence is
explicitly not the remedy.

The first observation is untrusted silent context for the natural once-only
greeting. The Agent must not default to “video connected”/“I can see you” or
describe ordinary appearance, clothing, expression, or surroundings. It may
volunteer a visual only when exceptionally noticeable, directly relevant, and
natural for a polite human; otherwise visuals are discussed when the caller
asks or refers to them. The same conservative rule applies to later turns.

PCM owns that conversational policy, PAO Call Function owns readiness and the
frozen observation, and Frontend Connector owns preflight, overlay, fallback,
and retry presentation. Protected Core, models, reasoning, frame cadence, STT,
TTS, Session ownership, and Phone remain unchanged. Focused red evidence
captured the former startup race, local/Agent visibility mismatch, missing
proxy field and missing overlays. Offline validation passes 224 HASHI3 Call
cases, 113 Workbench Call cases, and the production Workbench build. Runtime
adoption and physical acceptance remain separate.

## 2026-10-12 caller barge-in and transport evidence

The user approved full-duplex-style caller interruption for direct Call without
turning it into background-work control. During Agent/filler playback the
Frontend Connector keeps the microphone path alive with browser echo
cancellation/noise suppression, a frozen same-call noise baseline, conservative
local speech confirmation, and short pre-roll. Sustained caller speech stops
only the audible presentation and is submitted as a normal new input. It may
later mean chat, queued work, or explicit cancellation, but barge-in itself does
not infer or perform any of those actions. PAO continues older admitted work;
the latest caller turn owns presentation, while stale turns cannot take it back.

The same decision closes the known `fetch failed`/502 evidence gap. One bounded
correlation ID now crosses renderer, Workbench proxy, Remote loopback relay and
Call Function. Each boundary records headers/body completion separately, names
whether Workbench or a proxy produced the response, records client cancellation
and final server send, and preserves safe nested socket type/code/errno/syscall.
No media, conversation text, full query, credentials, endpoint-bearing native
message, or automatic retry is added. This makes a future failure attributable;
it does not retroactively identify which layer closed an earlier connection.

## 2026-10-11 authoritative debug-call recovery

The user-approved current behavior is
[HASHI /call Current Behavior Contract](HASHI_CALL_CURRENT_BEHAVIOR.md).
It is the release checklist for the clean `debug-call` branch and supersedes
every conflicting behavior below. The two fixed controls keep independent
ownership: Phone starts or returns legacy `/phone`, while video starts or returns
direct `/call`. A video click requests microphone and camera together, and
camera failure degrades that direct Call to audio-only. An unresolved direct
route is refreshed on foreground and rechecked only from the video click.
Neither route may fall back to the other. The former camera-off direct startup
rule and fixed sentence/character answer limits are retired; separate
entry-route assertions are required.

Frontend Connector owns Workbench media, controls, captions, and connection
sounds. PAO Call Function owns the bound Call, opening, typed speech projection,
streaming order, and canonical Session boundary. Protected Core is unchanged.
Models, reasoning, endpointing, default availability, and install-time
deployment defaults are unchanged.

## 2026-10-11 preview, transcript, and streaming correction

The follow-up regression was an integration error in the consolidated
`debug-call` implementation, not a missing branch that should be merged
wholesale. The Call renderer replaced the Agent avatar with local video, let
the preview participate in caption flex sizing, and appended playback state to
the already canonical PAO transcript. Separately, direct Call admission marked
its presentation surface as `session-api`; the request pipeline therefore did
not enable Workbench answer previews even though Session API correctly remained
the physical transport.

The corrected contract keeps a stable mirrored local preview, retains the Agent
identity below it, and renders only canonical Call rows. Direct Call now declares
the Workbench presentation surface while keeping the Session API transport
identity. Public commentary and complete answer sentences enter the existing
single speech FIFO as soon as observed; content-free diagnostics record activity
and queued-segment timing without recording speech content.

Focused red/green evidence covers the incorrect admission surface, duplicate
caption source, stable mirrored preview with retained avatar, commentary speech
before terminal completion, and a newly streamed sentence being prepared while
current audio is still playing. The complete current Call suites pass 144/144
on HASHI3 and 89/89 in Workbench; the production Workbench build also passes.
Physical HASHI3 acceptance and later non-rebooted HASHI4 loading are separate
release facts.

The candidate passes the focused Workbench Call and Phone/live checks,
143/143 HASHI3 Frontend Call checks, the complete Workbench server suite,
production build, and protected-Core check. Physical HASHI3 acceptance verified
direct Call's immediate microphone/camera startup, same-call camera off/on,
microphone mute/unmute, once-only opening, visible two-sided captions, natural
Agent speech without stage directions, completed TTS playback, ringback
termination after connection, hangup, and ended camera tracks. The tests that
opened video-first direct Call from the Phone control are now classified as the
entry-routing regression. They remain direct Call evidence but do not prove the
required Phone-to-`/phone` mapping.

HASHI4 `debug-call` carries the exact candidate product blobs and its isolated
tree passes 143/143 Frontend Call checks. Live acceptance against the existing
non-rebooted HASHI4 runtime verified the physical camera, controls, captions,
ringback termination, and media release. An external speaker phrase produced no
caller transcript and was not admitted as caller speech, so HASHI3
remains the end-to-end physical speech-loop evidence. The running HASHI4
Function has not adopted the new generation; this authorization excludes a
HASHI4 reboot.

The final Workbench acceptance also distinguishes source, production build,
running service identity, and the code loaded by the Electron renderer. The
service reported the exact candidate, but an already-open renderer still held
the old launcher until Workbench was relaunched through its official launcher.
The newly created renderer emitted the expected direct Call route request and
then reproduced the incorrect Phone-to-direct mapping. That observation proves
renderer adoption while simultaneously failing entry routing. A service restart
or source hash alone is therefore not renderer-adoption evidence; current
acceptance also needs separate Phone and video route receipts.

That entry-routing gate passed after the Workbench repair at
`d76fbc0949f21e19fb2c13eede70b0398dea77d6`. The remaining deployment
fault was an ignored local Workbench allow-list that admitted direct Call only
for HASHI2. Its scoped value now names HASHI2, HASHI3, and HASHI4, and the
managed Workbench reload reports direct Call available for HASHI4. This local
configuration correction did not restart HASHI4.

Physical HASHI4 checks then distinguished the two fixed controls. Video opened
direct `/call`, displayed stable live camera, retained the Agent avatar, and
used one rolling transcript. A spoken question received a visible answer while
the panel showed active speech. It identified the session as video and correctly
described the chair in the camera frame. No
legacy Phone audit appeared. Phone then opened the audio-only
`/phone` panel, showed that video was unsupported, and created a new legacy
attempt/call audit pair. Both calls hung up and released their active media.
These are separate route receipts and close the Workbench entry-routing gate.

## 2026-10-11 sentence-streamed speech and rolling captions

PAO Call Function owns typed activity filtering, sentence/TTS ordering and
prepared filler assets; Frontend Connector owns playback and responsive caption
presentation. Only public `answer_preview` and `user_commentary` activity may
enter Call speech. Reasoning and technical activity remain excluded.

Stable complete answer sentences now synthesize immediately and play through a
single FIFO queue, with bounded one-sentence preparation ahead. Final answer
reconciliation avoids replaying preview text. During a silent long wait, the
client may play increasingly spaced, localized fillers prepared in the selected
Agent voice. A real answer cancels pending filler, and filler is never stored as
an answer, transcript row or Session history.

Workbench retains the latest 24 caption rows, follows the bottom until the user
scrolls away, and offers a localized return-to-latest action. Caption sizing is
16 px on desktop/tablet and 18 px on phone; Call controls remain reachable on PC,
tablet, portrait phone and landscape phone. Existing speech-end detection,
camera cadence, selected text model/reasoning level and STT admission remain
unchanged.

All 140 HASHI3 Frontend Call tests, 69 focused Workbench checks, 10 Chromium
Call/window checks and the production client build pass. Screenshots at four
responsive sizes were visually reviewed. HASHI4 backend source and running
Functions were not changed or restarted. See Workbench
`docs/call/CALL_STREAMING_SPEECH_CAPTIONS_2026-10-11.md`.

## 2026-10-10 approved Grok trial, voice previews and speech latency

Frontend Connector owns this Functions and external Workbench correction.
The user approved fixing Call defects/latency and evaluating x-ai/grok-stt-1.0,
without forcing recognition language. The instance Call declarations now select
Grok with empty STT options; the running HASHI4 context returned the matching
configuration revision. Active calls retain their frozen configuration.

Call voice labels derive from one Gemini catalog, with localized descriptions
and documented gender. Selection publishes a verified prerecorded sample through
the existing /voice FC notification path, outside conversation Turns. Chinese
and English samples cover all 30 voices. Workbench uses MP3; Telegram uses OGG.
A preview button replays the selected sample, and a repeated callback stays
idempotent. No provider synthesis runs in menu callbacks.

The first final-answer audio segment uses a short natural phrase (at most 120
characters); the remaining segments retain the existing bounded policy.
Workbench requests exactly one segment ahead while current audio plays, without
raising its polling frequency. Hangup fencing and explicit retry remain intact.
This complements the earlier observation-ready join correction.

143 focused Python cases, 30 controller cases and the client build pass.
All 120 media files validate and decode. Four phone/iPad full-app fixture cases
verify readable labels and advancing audio; the known Playwright WebKit native
controls warning remains distinct from playback and product errors.
Synthetic TTS first-audio wait fell from 5.34/5.78 s to 3.19/3.19 s; this is not
a whole-call benchmark. Grok still misrecognized the isolated Chinese “嗯”.
See [evidence and adoption scope](repairs/CALL_VOICE_PREVIEWS_20261010.md).

The source changes are prepared in HASHI3. Workbench client publication is a
separate static-asset operation. HASHI4 backend code and shared Functions
adoption remain unperformed: the standing restriction requires explicit
production/lifecycle authorization. No Core changes or restarts are included.

## 2026-10-10 mobile entries and latency audit

Frontend Connector owns the external mobile entry layout and a HASHI3 Functions
correction that wakes a speech turn on a fresh camera observation. The observer
continues processing later frames; its existing 2.5-second join limit remains.
The latency, correction and service tests pass (52), with a focused regression
that failed before repair. HASHI4 has not adopted this backend change.

The current Whisper route reproduced short Chinese language/script mistakes in
a four-sample synthetic comparison. Scribe v2 is a candidate for real-user A/B
testing with automatic language detection, not an authorized production switch.
Observed TTS generation (5.1–7.6 seconds) dominates two reported turns; a model
change to recognition alone cannot remove that wait.
See [scoped evidence and decisions](repairs/CALL_LANGUAGE_LATENCY_20261010.md).
Existing model configuration, capture restoration and camera opt-in are retained.

## Historical 2026-10-06 decision — superseded startup behavior

This section records the earlier independent-entry design. Its camera-off
startup requirement is obsolete and must not be used as an assertion or release
test after the 2026-10-11 contract above.

The user approved replacing the shared next-call selector with two fixed
entries. Frontend Connector owns the Functions readiness/start/settings change
and the matching external frontend presentation on `development/hashi3-20261006`.

- The phone icon always starts or returns to `/phone`; the camera-shaped icon
  always starts or returns to `/call`. Neither entry writes a route selection.
- Historical behavior: `/call` started with its camera off, including when vision was configured.
  Returning to an existing call does not enable its camera. The user enables
  camera sharing explicitly inside the call panel.
- Unconfigured entries are absent. Readiness comes from the existing Phone
  capability and Call readiness projections; camera support is independent of
  Call availability. Configured entries remain visible during a temporary busy
  condition, with start disabled while the other engine is active.
- The settings menu has no activate/restore switch. Old route callbacks and
  `/call activate|deactivate` refresh settings without writing configuration;
  revision checks remain mandatory. Legacy saved route data is preserved for
  older clients and does not gate direct Call startup or camera capability.
- An active call retains its bound Agent/Session. Existing mutual exclusion,
  privacy, revision checks, media leases and connection/session fencing remain
  enforced by their current owners.

Focused failure evidence covers legacy routing, camera readiness, direct start,
unchanged configuration bytes, retired menu switches, hidden entry buttons,
actual Simple panel rendering, remote busy state and missing-session binding
invalidation. Real Phone startup now succeeds even with a saved Call selection.
Both Phone registration and startup no longer consume that selection; their
shared busy interlock remains. A rejected/missing Call context clears the prior
binding, and obsolete context cannot start another Agent's call.

Functions/Phone integration checks passed (166); frontend call/media/Phone and
Simple composer checks passed (128, Node 22). Production client build and
independent review passed. Commands, failure receipts and build facts are saved
separately in the HASHI3 ignored `state/call-entries-20261006` receipts.
At the initial 2026-10-06 closeout, HASHI3's Backend API was unreachable and its
active Call configuration was absent. The 2026-10-07 adoption and design
acceptance below supersede that readiness checkpoint; physical-device
acceptance remains separate.
Earlier decisions below remain experiment history.

## Historical 2026-10-05 local input and audio follow-up

This checkpoint predates the later approved volume-based capture plus cloud
speech-judgment trial recorded below. Preserve it as experiment history;
the consolidated development client uses the later cloud-judgment path.

The user approved implementing Aptenra-inspired local acoustic admission and
restoring call system sounds, while preserving call responsiveness. Frontend
Connector owns this browser media change; the existing Functions call contract,
PAO admission and Core remain unchanged. Speech probability now gates WAV
production before STT, with browser suppression/echo cancellation, local warmed
Silero processing, onset audio and the existing 800 ms silence wait. Failure
pauses capture instead of falling back to amplitude-only admission. Speech in
television/other voices remains an acoustic limitation, not caller identity.

The existing Phone sound catalogue supplies dial/ringback, connected, busy,
failure and confirmed hangup cues. Connected feedback waits for actual input
readiness, and cancellation fences obsolete sound and detector completions.
Implementation and focused offline/browser acoustic checks are complete;
ordinary Workbench deployment and physical acceptance are recorded separately
on the original call task. Normal/short samples add 32–96 ms at the endpoint in
the bounded audio comparison; quiet syllables retained by the new detector are
not judged against the old prematurely cut endpoint. Full call latency and the
proposed <=100 ms incremental physical target remain user-device acceptance.
The detailed client decision is `docs/call/CALL_INPUT_REPAIR_2026-10-05.md` in
the authorized external frontend checkout. No HASHI restart is part of this
frontend-only repair.

## Ownership and approved scope

On 2026-10-04 the user approved the combined voice/video correction and the
Workbench main UI design, then instructed implementation to continue in HASHI2
until the next runnable user acceptance point. Frontend Connector owns the
replaceable Functions media service and Workbench presentation. PCM projects
sealed current-input call facts. PAO retains the existing Session/Run admission
and the same Agent, persona, history and work. Core is unchanged. No main merge
or other instance adoption is part of this experiment.

## Historical 2026-10-04 decisions

- The original call button reads the backend-owned route for its owner/Agent.
  Default is the independent `/phone` engine. `/call activate` and deactivate
  select the next call. Configuration alone never activates a route. Calls
  freeze route and media profile for their lifetime.
- The adjacent video button starts or upgrades the same bound call. Selecting
  another chat returns to the existing owner. Same-instance navigation neither
  retargets nor ends the call; Session/connection generation changes close it.
- `/call` settings use existing command cards, catalogue, callback contract,
  locale and revision-aware config writer. Browser configuration forms and
  local command interception are retired. The media context API returns only
  availability and call facts, never provider/model/voice profiles or secrets.
- Click starts directly subject to native device permission and existing
  authority/privacy policy. There is no application consent checkbox. Cloud
  access still requires the existing privacy level and configured service.
- Auto VAD produces ordinary speech turns, continuing listening after playback.
  Speaking begins only after actual playback. Stopping speech preserves the
  accepted task; hangup stops media and invalidates camera observations.
- Video observation is independent of utterances, including silence. There is
  at most one inference in flight and one latest pending frame; backend rate,
  minute budget, JPEG validation, deduplication and capture-time freshness apply.
  A newer pending frame does not erase a completed fresh snapshot. Camera
  epoch changes discard delayed results. Frames do not create Agent Runs.
  Continuous observations ask for one or two brief sentences of current
  salient facts rather than background inventories, timecodes or lists of
  absent things. Question-specific inspection keeps the user's question;
  this does not truncate the Agent's task answer or infer motion from one frame.
- Speech text stays verbatim in PAO. Sealed current-input media facts enter the
  actual model PCM context with oral-conversation guidance and the effective
  persona/history. User-shared camera data has no instruction, identity or
  authorization authority. Freshness is checked again during PCM assembly;
  immutable admission receipts retain their original snapshot.
  A narrow in-process marker adds only the application-owned interaction
  policy to `local_system`; generic tuple metadata cannot promote text. Fixed
  Sessions explicitly revoke this policy on the next ordinary input. HERV3
  renders this existing trusted policy directly as current-input instructions,
  with its `local_system` authority unchanged. Other instructions keep their
  existing projection; same-key untrusted data and camera facts remain user
  reference material. The
  default spoken target is one brief paragraph of one or two short sentences,
  about 20–60 Chinese characters or 10–30 words including any greeting or
  follow-up. Answer first, omit analysis announcements and repeated greetings,
  and name the relevant visual object rather than inventorying secondary
  details. Essential information and
  explicitly requested detailed answers retain their complete meaning.
- Both independent engines share the existing themed presentation shell.
  Desktop voice is about 390×360 and video 390×500; mobile expands to the
  viewport and minimizes above the composer. Three fixed control positions
  are microphone, camera and end. Extra recovery/stop actions are contextual.
  Latest captions use up to three lines. No second launcher or settings gear.

## Focused verification and failure proof

The initial correction tests failed on missing saved route, cloud-start checkbox,
and unsupported camera observation operations before implementation. Additional
red/green checks covered queued PCM observations expiring, negative menu callback
indices, audio unlock before asynchronous route lookup, visible route failures,
and slow vision starving a continuously uploading camera. Failure and green
receipts are held in the experiment evidence; none of the temporary defects is
retained. Retired tests for frontend configuration writers and source-text
disclosure checks were removed; backend persistence and rendered behavior are
the acceptance boundaries.

The committed correction passed 767 curated checks. The final disabled-readiness
fix passed 35 owning checks; the paired Workbench passed 115 owning and direct
consumer checks and its production build. The model-wire and persisted-Session
revocation correction passed 66 owning/direct consumer checks and 767 curated
checks. The final spoken-target refinement passed 77 owning/direct consumer
checks, with 16 final focused checks. These are scoped results, not a full-suite or
physical acceptance claim. Existing source qualification and Core guards passed.

Independent review found unknown camera-off/hangup outcomes and the disabled
route restoration edge. Those were fixed before adoption. A further cross-call
permission completion defect was demonstrated red, fixed, and independently
reviewed. Rika also found that omitting the call policy did not revoke it from
a Fixed Session. The explicit-revocation test failed before the fix and passed
after a persisted coordinator was recreated. Rika's primary, disabled-context,
spoken-guidance, model-wire/revocation and spoken-target reviews passed; the
blocked initial model-wire review and incomplete Claude/Codex CLI reviews remain
preserved rather than rewritten as approvals.

Real headless integration used synthetic microphone/camera inputs with the
actual HASHI2 services and Arale model. Voice and video kept the same PAO
Session and effective persona. Persisted typed PCM contained current-call facts,
fresh untrusted camera observations and no added private authorization. Actual
non-silent WebAudio playback, automatic listening, confirmed camera-off/hangup,
mobile fullscreen, fixed controls and minimized presentation passed. The first
voice harness had a wrong exact status-label matcher; its failed receipt remains
preserved alongside observed speaking-to-listening transitions, not rewritten
as a successful harness run.

The first actual video reply was a 327-character report with 60.2 seconds of
speech. That passed transport but failed conversational quality. PCM guidance
now requests one or two short spoken sentences by default, a relevant visual
summary rather than a report, and no routine processing/snapshot disclaimer.
Explicit requests for detail and truthful unavailable/stale vision remain valid.
The before-fix receipt fails the spoken-quality check. A later 43.24-second
reply still failed despite a confirmed trusted call section in the actual
Fixed Session; this was a model expression defect, not evidence of a missing
transport. The ordinary visual-question target is now explicit and contains
no example or prefilled answer from the synthetic fixture. An intermediate
real reply improved to 108 characters and 25.24 seconds but still failed the
unchanged 25-second speech gate. The final target includes greeting/follow-up
length and removes visual prefaces and secondary inventories. That actual
generation still produced a 153-character / 34.12-second reply and failed.
Its failed receipt and confirmed cleanup are preserved. A native provider-wire
test then failed on escaped/quoted current-call instructions and passed after
the HERV3 consumer rendered the existing trusted policy directly. It also
checks the effective persona and the separation of hostile camera text and
forged same-key data. The native adapter module passed 96 checks; the focused
consumer/Fixed Session scope passed 31. This establishes input projection,
not model obedience or conversational feel. Its curated gate passed 766 cases;
two source-qualification checks initially rejected the uncommitted compiler
and passed after the coherent source checkpoint was committed. Independent
consumer review passed and the candidate was adopted with unchanged Core.
The actual reply remained 155 characters / 35.76 seconds and failed; cleanup
was confirmed. An offline check using the real DeepSeekAdapter up to its
stubbed HTTP call retained the complete system policy and separated camera
data, with no external model call. Continuous background facts are now
requested briefly at their source; 37 owning checks passed. Rika independently
reviewed the exact consumer and observation commits. Her observation receipt
separates 37 media checks and 11 HERV3 consumer-contract checks from the actual
protected-Core script; all passed. The consumer contract is not a Core guard.
The observation correction was adopted through the approved HASHI2-only hot
operation, retaining Core PID 838 and its original source digest. All five
Workers and shared Functions adopted the qualified observation generation.

The final current-generation synthetic canaries passed against the actual
services and model. Video produced an 82-character relevant reply with 19.24
seconds of non-silent playback; voice produced 5.68 seconds of playback. Both
retained Arale and the same PAO Session. The matched persisted current-input
receipts confirmed fresh camera facts for video, no camera observation for
voice, and no added private authorization. Automatic listening, backend
camera-off confirmation, mobile layout/minimization, fixed controls and
confirmed hangup passed. The initial context-body capture failure remains
preserved as a harness failure. A read-only context operation bound to the
same start request verified the original context/privacy gate; no product
gate or spoken-duration limit was weakened to obtain a passing receipt.

This is a runnable experimental acceptance checkpoint, not complete call-feel
acceptance. Preparing-to-speaking took 17.413 seconds for video and 28.610
seconds for voice; those end-to-end observations include speech recognition,
Agent generation and speech synthesis. They do not meet the proposed normal
3-second / slow 6-second response target. The actual video answer still used
Markdown emphasis and a greeting paragraph and exceeded the soft 20–60-character
target, despite passing the unchanged no-list / 25-second speech gate. Physical
devices, sustained conversational presence, interruption and independent
`/phone` physical regression remain open. All failed model and harness
receipts are retained; none was relabelled as successful.

## Delivery gate

Preserve Core PID/digest and recovery commit. Immediately before the approved
HASHI2-only hot adoption, read all Agents/queues/background work, authoritative
scheduled work for the next 30 minutes and transfer evidence. Historical QA
fences must be preserved, never replayed or deleted for maintenance. A transfer
notice is not an execution receipt; inspect the actual remaining task and target
Run state. Replace only the approved experiment source and paired preview.
After adoption verify Core identity, new Function generation, API, normal and
video turn context, camera-off/hangup cleanup and actual playback evidence.
Invite user testing only when that next point is runnable; physical microphone,
speaker, camera and conversational feel remain user acceptance until observed.

## 2026-10-05 ordinary frontend deployment

The user explicitly replaced the separate-preview acceptance approach with
deployment to HASHI2 through their ordinary Workbench. Existing branch progress,
reviews and failed/successful canary receipts remain preserved. Main promotion
and other-instance adoption are not implied.

HASHI2 already runs the reviewed call Functions generation. The ordinary
Workbench source now includes the reviewed client implementation and retains
its newer model-metadata behavior and five unrelated local edits. Its existing
Windows service, connection registry, user data and URL own deployment. An
instance-local opt-in limits the new call path to the HASHI2 connection; other
connections retain their existing Phone path.

Focused frontend checks passed (75), including a red/green scoped-admission
case, and the production build passed. Through the ordinary frontend, Arale's
original phone button reached `call`, returned a successful start, displayed
listening readiness, exposed camera availability and confirmed hangup. This
deployment handshake submitted no media/model turn and is not physical
microphone, speaker, camera or conversation acceptance. Those remain open.

HASHI2 Core identity and its shared Function generation stayed unchanged during
the frontend deployment. The ordinary frontend connection was restored after
verification. The existing call task owns user feedback; the preview is retained
as development evidence, not the user acceptance entrance.

## 2026-10-05 correlated diagnostic repair

The user reports interruption while changing cameras. HTTP success alone did
not prove native-media readiness, continued microphone input or why the call
ended. Frontend Connector adds content-free, correlated diagnostics in the
Workbench browser/transport and replaceable call Functions. Core is unchanged.
Camera-off microphone reacquisition and page-background suspension are
observable candidates; the historical interruption's cause is not established.

Evidence now distinguishes native track/AudioContext failures, camera
requests/confirmation/cancellation, aggregate input quality and detector
failures, terminal source, proxy rejections, STT/PAO/TTS stages and provider
response versus existing receipt-lookup time. Late callbacks retain their
original call identity. No media, transcript, credentials or native exception
message is logged; diagnostic failures cannot fail the call. No extra provider
request, configured delay, retry or state owner was introduced.

Backend checkpoints `155890ca` and `da41ee3e` passed 52 focused cases; 15 new
boundary cases failed against isolated original code. Independent parent review
checked logging failure, safe provider facts and shared-endpoint modality.
Frontend coverage/build and deployment receipts are recorded in the owning
Workbench call logging decision and ignored `state/call-logging-20261005/`.
Source validation, browser/server adoption and Function adoption are distinct;
hot replacement still requires current scoped approval. Historical gaps cannot
be recovered, and physical camera continuity, recognition and speed acceptance
remain open under the existing call task.

## Shared diagnostic journal follow-up

The scoped operational approval was received on 2026-10-05. Initial hot
adoption kept the Core identity and qualified all five Workers, but an actual
ordinary-frontend rejected-camera probe exposed a persistence gap: the call
logger inherited filtered console output rather than the existing bridge file
handler. Diagnostics now use a child of the existing bridge logger; no extra
writer, provider request, media wait, retry or lifecycle policy is introduced.

A real configured bridge-file regression failed before the fix with no call
start/end records and passed afterwards. The focused `tests/frontend_call`
component passed 53 cases, including that persistence boundary; the existing
aiohttp deprecation warning remains. Current-source hot adoption and a fresh
ordinary-frontend journal probe are tracked separately in ignored receipts.

The approved deployment is now verified through the ordinary frontend: its
context route reaches HASHI2 and a deliberately nonexistent camera call returns
the expected rejection without opening devices or invoking a model. That
request appears with the same call identity and generation in the Workbench
proxy journal and HASHI2's existing `logs/bridge.log`; browser numeric metrics
also survive the durable default-content-off writer. The qualified corrected
generation runs on all five Workers, service health is ready, and Core PID,
runtime and protected source are unchanged. Actual camera continuity,
recognition accuracy and conversation latency remain open user acceptance.

## 2026-10-11 Call-owned Run termination correction

A live HASHI3 replacement exposed an admitted Call Run that survived hangup
and continued as real Provider work. Shared Function quiescence correctly
protected it, then timed out after 120 seconds. Branch history confirmed that
the restart and Call repair lines were already present; this was a missing
lifecycle requirement rather than another merge omission.

Call keeps the admission-race safety boundary: it does not cancel while PAO's
acceptance identity is unknown. Once the exact request and Run IDs resolve,
hangup uses PAO's existing targeted Run fence and records the control outcome.
An already-known Call Run is cancelled at once. No session-wide or Agent-wide
stop is introduced, and unrelated foreground or background work remains
protected. The former test that required the admitted request to detach after
hangup is retired in favor of exact-scope red/green coverage.

## 2026-10-05 approved call latency repair

The user approved eliminating unnecessary waits, preserving the selected
Agent's model, reasoning setting, PCM, tools and execution path. Frontend
Connector owns the repair in Functions; Core and the separate Phone route
remain unchanged. Scope stays on the isolated HASHI2 call experiment.

Media adapters now return the original STT/TTS/vision response immediately.
The OpenRouter generation GET, retries and provider-name verification are
removed entirely, including any background lookup. Existing response IDs and
bounded timing diagnostics remain available directly from that response.
The optional provider receipt stays null; no serving-provider claim is made.

PAO's frozen, server-built call context suppresses Telegram ephemeral progress
for call Runs. Placeholder, typing, thinking and verbose display tasks are not
created, so their network setup and cleanup cannot delay the call. Original
caller hints and normal Session API/text requests do not suppress progress.
Local activity and canonical evidence remain available for both CLI and HER;
the user's existing final mirror preference remains separately owned.

When CallService observes a successfully completed Run and has speech-ready
text, it immediately schedules the first TTS segment without waiting for a
client speech request. Polling joins that same task or returns its cache;
later segments retain bounded sequential admission. Synthesis rechecks scope
and privacy, captures its exact originating turn, and cannot publish after
hangup, lease expiry, a new turn or a scope change. Privacy is checked again
before caching and before returning cached audio; revoked audio is discarded
across all cached segments and can be regenerated only through an explicit
retry after permission returns. A retry rejected by current privacy settings
preserves its error and retry allowance, so restoring permission still permits
that explicit retry.
TTS failure leaves the completed text
answer available; only an explicit bounded speech retry repeats synthesis,
and it never resubmits the Agent Run.

Focused before/after proof, independent review, qualified Function adoption
and device latency acceptance are recorded separately on the owning call
task. This source decision alone is not evidence of live speed improvement.

## 2026-10-05 cloud speech judgment experiment

The user approved volume-based capture followed directly by cloud speech
judgment and transcription, without specifying a language. Frontend Connector
owns the external browser input and replaceable call Functions. This supersedes
the local Silero admission decision for the isolated HASHI2 call experiment;
the Agent's model, reasoning, PCM, tools, Core and separate Phone route retain
their existing owners and behavior.

The client performs bounded PCM downsampling and amplitude segmentation only.
It retains 256 ms of onset audio, the 800 ms quiet endpoint and the 59.8-second
utterance bound, and neither loads nor runs a local speech model. Low steady
background estimates become a cross-turn baseline only after the cloud rejects
that exact sealed recording. A verdict is bound to its capture and turn;
late or duplicate results cannot alter a newer recording or reopen its device.

An instance-owned STT target may opt into `stt_protocol: audio_chat` on the
existing OpenAI-compatible adapter. A single request sends the WAV and asks for
`has_speech` plus verbatim original-language `text` as a strict JSON object.
There is no second classifier, language hint, Agent action or tool in that
request. The proposed HASHI2 Arale target is Gemini 2.5 Flash Lite through the
already configured gateway; selection and credentials remain ignored instance
configuration, not a shared model catalogue or browser-supplied endpoint.

Explicit no-speech and valid empty transcription results settle as `ignored`:
no user row, Agent Run, response or TTS is produced and listening resumes.
Malformed or contradictory structured results remain service failures instead
of learning speech as background. For transcription-only routes, available
segment speech evidence is conservative; known marked sound annotations may be
removed while retaining mixed spoken text and unknown formatted words. These
fallbacks cannot distinguish a noise hallucination written as an ordinary
sentence and are not evidence that all non-speech has been eliminated.

Actual configured-gateway probes disproved the old assumption that every noise
result is marked: Whisper transcribed synthetic silence as `Thank you.` and a
pure tone as `you`, without a usable no-speech probability. Both tested Gemini
audio targets classified silence/tone and speech correctly in one request and
retained the Chinese number sentence with and without noise. A synthetic quiet
interjection remained classified as speech but was mistranscribed; this is an
open accuracy boundary. These probes did not submit an Agent Run or use physical
devices and do not prove whole-call latency or real short-word accuracy.

Implementation approval, focused red/green checks, independent review, built
artifacts, Function adoption and physical acceptance stay separate on the
original call task. New HASHI2 hot adoption requires its own scoped approval;
the previous generation's reboot approval is not carried forward. Source
qualification alone does not make this experiment available in the running
ordinary Workbench.

## 2026-10-07 HASHI3 fixed entries and current conversation

The user authorized completing Phone/Call on HASHI3 and the associated test
Workbench, including hot Function adoption. Frontend Connector owns this
Function-layer repair; protected Core and production Workbench are excluded.
Phone and camera-shaped Call entries have fixed independent engines. Call
requires STT/TTS, with optional vision, and starts with camera off. Busy entries
remain visible and say the line is busy; unconfigured entries are hidden.

Both engines accept the selected owned active conversation, including Simple's
fresh nonprimary conversation. The primary binding is not mutated. Canonical
SessionStore ownership, Agent, conversation kind, active state and context
generation remain admission fences. Completed Call questions and answers stay
in that exact conversation. Archived/deleted/activity Sessions remain rejected.

The native Simple failure was a 409 primary-only fence in both media adapters.
Two fresh-conversation checks failed before the change; the negative ownership
and stale-state checks passed. Focused red/green and live adoption are recorded
in the nightly repair record. Provider selection is ignored instance
configuration with existing secret references, not shared catalogue metadata.

HASHI3 live adoption now proves those Function fences and selected-conversation
results. The paired Workbench is built/served at 5801ff7 on the scoped test
entry 5179. Simple's fixed pair uses its own flow row above the composer because
the former absolute attachment control obscured Phone. Actual native hit tests
passed three Simple themes and desktop/narrow sizes; both engines were started
and ended in the fresh Session without moving primary. Real external media
responses and fixture microphone/camera checks passed. Physical hearing,
microphone, camera content and real mobile remain user acceptance, not inferred
from the synthetic device evidence. See the nightly repair record for receipts.

## Current 2026-10-07 follow-up — HASHI3 ready for user testing

The user's continuation limits construction and testing to HASHI3 and its
existing isolated frontend. Frontend Connector owns the replaceable Functions
and external client; the current Function bytes are unchanged by this follow-up.
The scoped client is built and served at 9edaa03. Its fixed entries retain the
2026-10-06 design, including independently derived readiness and optional vision.

Switching the selected conversation or Agent is background reading: it must
not hang up or retarget an active Call. The same bound Session's observed
generation reset, connection replacement, read-only state and competing engine
still invalidate it. The controller defect failed both an actual React hook
check and native Simple navigation before 9fd254e; the repaired native call
remained bound and returning did not redial.

Camera permission refusal preserves a listening Call after confirmed
camera-off, with localized guidance. Unknown confirmation cannot reopen input.
The initial recovery repair passed its owning checks but its notice was clipped
below the scrollable body. Native geometry and screenshot evidence failed on
that rendered defect. Permission/recovery notices now occupy a fixed area above
the controls, outside the scrollable body. The final native check proves resumed
microphone capture, visible guidance, actual permission grant and manual retry,
fresh vision, confirmed camera-off, refresh without redial and final hangup.
All twelve themes at desktop, 390 and 320 widths keep the notice and three
controls visible and hit-testable.

Separate native canaries now prove browser microphone capture through actual
Call STT/PAO/TTS and completed WebAudio playback, and Phone's microphone through
real WebRTC, provider transcripts and advancing non-silent remote playback.
The configuration matrix, vision-free Call and other-client busy presentation
also passed with configuration restored. These fixtures qualify the software
path, not physical hearing, camera content, real mobile or acoustic/latency
quality. The older generic-script query's unknown receipt is not reclassified.
The design-to-evidence map is
[Phone/Call design acceptance](repairs/PHONE_CALL_DESIGN_ACCEPTANCE_20261007.md).

## 2026-10-11 independent availability and voice-media billing

Phone and Call remain independent basic Functions. Phone readiness comes from
its configured live provider and credential. Call readiness requires an enabled
configuration plus available selected STT and TTS credentials; vision is
optional and its own credential controls camera availability. The Frontend
Connector shows only entries confirmed ready. Unknown, unavailable or failed
Call never launches Phone, and Phone never selects Call. The old Workbench
per-instance Call allowlist is retired; instance Function configuration is the
single availability owner.

The call surfaces project a separate `voice_media` billing view. Phone uses the
qualified GPT Live session-second meter at its published per-minute rate. Call
records actual input/output audio duration, provider-reported STT cost when
present, and a qualified STT/TTS catalogue estimate otherwise. Provider cost
receipts take precedence over estimates. Backend text-model usage remains in
`/metre`; vision is excluded from the Call voice-media total. Estimates are
labelled and missing catalogue prices remain incomplete rather than zero.

## 2026-10-12 HASHI4 listening endpoint, opening record, and time continuity

The user's physical HASHI4 report explicitly authorizes this Call defect repair
on the matching `debug-call` HASHI and Workbench branches. It does not authorize
a HASHI4 lifecycle action. PAO Call Function owns opening persistence, spoken
context, and TTS scheduling; Frontend Connector owns browser capture endpointing.
Protected Core remains unchanged.

The correlated Feiyan trace separates the regression from backend reasoning.
The two Agent Runs completed in about 2.8 and 4.4 seconds. STT completed in
about 1.8 and 2.4 seconds, but the second browser recording stayed open for the
59.8-second hard limit. A history audit found that the approved
Aptenra-inspired local detector path had been replaced by the later HASHI2-only
amplitude/cloud experiment and had not actually been reconnected on the
ordinary branch despite the restoration record. A separate opening trace
showed low-priority filler preparation starting while a later opening segment
still used the same TTS provider. The audible opening had no canonical
presentation row, and Call guidance exposed timestamps without telling the
Agent to compare them when relative dates or elapsed time mattered.

The earlier peak-and-stable-tail repair was a separate symptom heuristic and is
superseded; it is not considered better than the established design. The
browser now again warms and reuses local speech detection with the moving
idle-noise baseline, onset pre-roll, the ordinary 800 ms quiet endpoint, and the
59.8-second continuous-speech safety bound. Speech never trains its own
rejection floor, and detector failure is explicit rather than silently
degrading to amplitude-only capture.

The Function persists the generated opening once through SessionStore with a
Call-scoped idempotency key before exposing its audio. It is visible in the
conversation record and explicitly excluded from model history because no user
Turn created it. Filler preparation waits for every opening speech segment.
Spoken-turn guidance uses `call.valid_at` and existing exchange timestamps to
compare relative dates and real gaps only when relevant.

Red checks reproduced the one-minute capture, missing opening row, absent time
guidance, and TTS contention. The restored browser gate also passes the retained
acoustic replay: noise and Call tones produce no turn, normal/quiet speech retain
the same segments, and detector frame p95 is 1.1 ms. The complete Workbench Call
suite passes 95/95, its production build passes, the matching HASHI Function
suite passes 158/158, and Protected Core remains clean. These are source and
deterministic-browser checks. Post-fix HASHI4 Function adoption, a fresh
renderer, and physical acoustic acceptance remain separate and pending the
user's lifecycle action.
