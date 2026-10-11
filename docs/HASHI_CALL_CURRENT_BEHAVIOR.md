# HASHI /call Current Behavior Contract

Status: **Authoritative current behavior**
Approved scope: user-directed `debug-call` recovery on 2026-10-11
Owners: Frontend Connector (Workbench presentation/media) and PAO Call Function
Engineering layers: external Workbench plus replaceable Layer 2 Functions
Protected Core: unchanged

This is the single current acceptance list for direct `/call`. Historical Call
design notes remain useful as evidence, but any conflicting behavior in them is
superseded by this contract. In particular, old fixed one-or-two-sentence and
numeric response-length requirements are retired.

## Entry, availability, and binding

- **CALL-ENTRY-01** — Workbench shows the established fixed Phone and video Call
  controls with their icons, focus treatment, and usable touch targets. Either
  control starts or returns to the same direct, video-first Call. The legacy
  Phone route is only a compatibility fallback after direct Call is confirmed
  unavailable.
- **CALL-ENTRY-02** — Direct Call binds to the currently selected Agent,
  connection, Session, and context generation. Changing the reading view during
  a call does not silently rebind the live call.
- **CALL-ENTRY-03** — Current `call_ready` is authoritative. A retired stored
  route cannot block direct Call. An unresolved route is refreshed when the
  Workbench returns to the foreground and rechecked from either launch click; it
  must not be treated as unavailable. Phone fallback is allowed only after a
  current check confirms direct Call is unavailable.
- **CALL-ENTRY-04** — Busy state prevents a second call. Existing calls remain
  returnable and endable. Unconfigured or explicitly disabled Call stays hidden.
- **CALL-ENTRY-05** — Call remains available by default in the installed
  Workbench templates. This recovery does not change any model, reasoning level,
  default enablement, or deployment-template promise.

## Immediate media startup and degradation

- **CALL-MEDIA-01** — Either fixed launch control immediately displays
  Connecting and begins microphone and camera acquisition in that same user
  action. Camera is part of Call startup, not a later optional mode. Context and
  server startup may proceed concurrently; they must not postpone the device
  permission request.
- **CALL-MEDIA-02** — Media acquired while the Agent opening is pending is held
  for that call and does not admit caller speech until the once-only opening has
  completed.
- **CALL-MEDIA-03** — Camera denial, absence, preview failure, or local device
  failure degrades the same call to audio-only. It does not end, duplicate,
  rebind, or pause the voice call.
- **CALL-MEDIA-04** — A server-side camera-enable failure is reconciled with a
  confirmed camera-off transaction before ordinary voice admission resumes.
- **CALL-MEDIA-05** — The caller can turn the camera off and on at any time in
  the same call. Camera changes preserve the call ID, Session binding, microphone
  state, and audio conversation.
- **CALL-MEDIA-06** — Muting, unmuting, camera changes, backgrounding, network
  recovery, and hangup cannot resurrect stale tracks or admit audio from an old
  call.

## Connection sound and opening

- **CALL-AUDIO-01** — Dial/ringback belongs only to Connecting. It remains
  audible long enough to be perceptible, then stops before the connected cue,
  Agent speech, or caller microphone admission. It never continues after the
  call has connected.
- **CALL-AUDIO-02** — Busy, failure, connected, and confirmed-hangup sounds keep
  their existing meanings. An uncertain hangup does not play a false success
  sound.
- **CALL-OPEN-01** — The selected Agent answers once, in its existing persona,
  language, Session, and conversation context. Polling, reconnects, or delayed
  responses cannot repeat that opening.
- **CALL-OPEN-02** — The opening is spoken before new caller speech is admitted.
  If the opening fails, the call recovers to voice listening without inventing a
  successful opening.

## Spoken response and streaming

- **CALL-SPEECH-01** — Call output is natural telephone conversation: plain
  spoken sentences with no headings, tables, bullets, Markdown, report preface,
  or spoken stage/action directions such as “(小声)” or “（轻声说）”.
- **CALL-SPEECH-02** — Content and length follow the caller's request and the
  Agent's normal answer. Concise questions can receive concise answers; detailed
  explanations and long stories remain complete. Call presentation must not
  impose fixed sentence or character limits.
- **CALL-SPEECH-03** — Stable complete sentences from commentary and the final
  answer can be spoken incrementally. Private reasoning, tool activity, partial
  fragments, markup, code blocks, URLs, and duplicate final text are not spoken.
- **CALL-SPEECH-04** — Speech is FIFO, uses at most one-segment lookahead, keeps
  bounded polling/retry behavior, and stops cleanly on user stop, mute,
  background, network suspension, or hangup.
- **CALL-SPEECH-05** — Localized short fillers may acknowledge a long wait at
  defined thresholds. They stop when answer speech arrives and never enter the
  canonical answer, Session history, or transcript as Agent content.
- **CALL-SPEECH-06** — TTS and visible Call captions use the same cleaned
  conversational text. The canonical PAO Session answer remains intact outside
  the Call presentation.
- **CALL-SPEECH-07** — Direct Call admission declares Workbench as its
  presentation surface while retaining Session API as the physical frontend
  transport. Those facts are not interchangeable: public complete-sentence
  answer previews and commentary must reach the Call speech queue before the
  terminal answer whenever they become available.

## Captions and controls

- **CALL-UI-01** — The established three in-call controls remain microphone,
  camera, and end call. Camera state can change independently of audio.
- **CALL-UI-02** — Captions show a rolling recent transcript (up to 24 rows),
  follow new content while the user is at the bottom, preserve manual reading
  position, and provide a return-to-latest action.
- **CALL-UI-03** — Notices remain distinct from transcript content. Captions,
  controls, focus, and responsive layout keep the corrected desktop and mobile
  appearance.
- **CALL-UI-04** — Preconnection shows Connecting. It must not claim that the
  microphone is paused or redirect to settings while normal connection work is
  still in progress.
- **CALL-UI-05** — The local camera preview has stable geometry, fills its
  bounded frame without pale side bars, and is mirrored horizontally. Caption
  growth cannot resize or remove it. The selected Agent's avatar and identity
  remain visible while local video is on.
- **CALL-UI-06** — The Call panel renders one canonical rolling transcript from
  PAO Call rows. Playback-only current-sentence state must not create a second
  caption track or duplicate the same Agent answer.

## Turn safety, recovery, and termination

- **CALL-SAFE-01** — Existing endpointing, low-noise rejection, cloud-confirmed
  noise learning, capture IDs, uncertain-upload deduplication, and cold-start
  detector protections remain in force.
- **CALL-SAFE-02** — Brief snapshot/network failures do not destroy live media.
  Sustained failure suspends safely and the next authoritative snapshot resumes
  the correct opening, speech, or listening phase without replaying mutations.
- **CALL-SAFE-03** — Background/foreground recovery, camera permission races,
  power/session changes, and stale async work cannot cross call generations.
- **CALL-SAFE-04** — Hangup settles the exact call ID. A pending start is ended
  after it resolves; an unconfirmed end remains retryable and cannot start a new
  call over the old binding.

## Release and evidence gate

- **CALL-REL-01** — Workbench static assets, Workbench Call proxy, and PAO Call
  Function changes are versioned and adopted together. Source, built artifact,
  running service/generation, and the code loaded by an already-open Electron
  renderer are reported as separate facts. A service restart alone does not
  prove that an existing renderer adopted the new client.
- **CALL-REL-02** — Every changed behavior has a focused regression test.
  Previously correct Call tests remain present; obsolete tests that assert
  retired behavior are removed or rewritten against this contract.
- **CALL-REL-03** — HASHI3 must pass protected-Core checks, focused Call tests,
  relevant broader checks, a production build, and real Workbench acceptance
  with microphone, camera, speaker/ring, captions, toggles, and hangup.
- **CALL-REL-04** — Only that exact verified candidate may be loaded into
  HASHI4. HASHI4 adoption and live observations must be recorded separately;
  no HASHI4 reboot is authorized for this recovery.
- **CALL-REL-05** — All recovery work stays on the clean `debug-call` branch
  until the complete candidate is verified and deliberately merged back to
  main.

## 2026-10-11 verified checkpoint

- HASHI3: exact `debug-call` Function candidate at `d1a872c8` is running.
  The 129-case expanded restart suite, 143/143 focused Call checks, three live
  restarts (17.4 seconds cold, 14.3 seconds, 9.8 seconds), and the complete
  physical microphone/camera/speaker Call path passed.
- Workbench: exact `debug-call` client/proxy candidate; focused Call/Phone/live
  checks, complete server suite, and production build passed. A newly launched
  Electron renderer verified that either fixed entry starts video-first,
  camera and microphone controls remain reversible, and hangup releases media.
  The Phone-entry physical smoke test was repeated after the final HASHI3
  restart integration and passed.
- HASHI4 source: `debug-call`; product/contract/test blobs match HASHI3 and the
  isolated exact tree passes 143/143 focused checks.
- HASHI4 runtime: no reboot and no new Function-generation adoption. Live
  observation covers the existing runtime with the verified Workbench client;
  it does not prove the new HASHI4 Function source is running.
- Branches remain unmerged pending deliberate review and user acceptance.

## Required release matrix

| Area | Mandatory evidence |
| --- | --- |
| Entry/binding | launcher and route-policy tests |
| Immediate devices | controller/media tests with delayed context/start and denied camera |
| Camera lifecycle | enable, disable, re-enable, denial, server failure, background race |
| Ring/opening | timing/order tests and live speaker observation |
| Spoken style | PCM guidance and deterministic Call-cleaning tests |
| Streaming/fillers | FIFO, complete-sentence, cancellation, no-duplicate tests |
| Captions/UI | single rolling transcript, stable mirrored preview, retained Agent identity, smart-follow, visual/browser tests |
| Recovery/hangup | existing noise, network, lifecycle, and settlement regressions |
| H3 live | versioned frontend acceptance receipt plus real media observations |
| H4 live | exact deployed artifact/source identity plus live observations, without reboot |
