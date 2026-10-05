# Call interaction correction — HASHI2 experiment

## Ownership and approved scope

On 2026-10-04 the user approved the combined voice/video correction and the
Workbench main UI design, then instructed implementation to continue in HASHI2
until the next runnable user acceptance point. Frontend Connector owns the
replaceable Functions media service and Workbench presentation. PCM projects
sealed current-input call facts. PAO retains the existing Session/Run admission
and the same Agent, persona, history and work. Core is unchanged. No main merge
or other instance adoption is part of this experiment.

## Decisions

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
- Speech text stays verbatim in PAO. Sealed current-input media facts enter the
  actual model PCM context with oral-conversation guidance and the effective
  persona/history. User-shared camera data has no instruction, identity or
  authorization authority. Freshness is checked again during PCM assembly;
  immutable admission receipts retain their original snapshot.
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

Focused Python and both engine/frontend browser tests passed. The initial
curated gate passed 764 cases and rejected three qualification cases because
the candidate was not yet committed. Qualification must be rerun from the
committed checkpoint. An independent review, source qualification, running
Functions adoption, real transport/model canary and physical user acceptance
are separate gates. This document records implementation intent, not an
adoption or physical acceptance claim.

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
hangup, a new turn or a scope change. TTS failure leaves the completed text
answer available; only an explicit bounded speech retry repeats synthesis,
and it never resubmits the Agent Run.

Focused before/after proof, independent review, qualified Function adoption
and device latency acceptance are recorded separately on the owning call
task. This source decision alone is not evidence of live speed improvement.
