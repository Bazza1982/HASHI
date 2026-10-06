# Call interaction correction — HASHI2 experiment

## Current 2026-10-06 decision — independent entries on HASHI3

The user approved replacing the shared next-call selector with two fixed
entries. Frontend Connector owns the Functions readiness/start/settings change
and the matching external frontend presentation on `development/hashi3-20261006`.

- The phone icon always starts or returns to `/phone`; the camera-shaped icon
  always starts or returns to `/call`. Neither entry writes a route selection.
- `/call` starts with its camera off, including when vision is configured.
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
HASHI3's Backend API was unreachable and its active Call configuration was
absent at closeout. Running adoption and physical-device acceptance remain open.
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
