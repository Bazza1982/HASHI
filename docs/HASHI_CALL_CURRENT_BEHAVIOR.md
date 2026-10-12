# HASHI /call Current Behavior Contract

Status: **Authoritative current behavior**
Approved scope: user-directed `debug-call` recovery through 2026-10-12
Owners: Frontend Connector (Workbench presentation/media) and PAO Call Function
Engineering layers: external Workbench plus replaceable Layer 2 Functions
Protected Core: unchanged

This is the single current acceptance list for direct `/call`. Historical Call
design notes remain useful as evidence, but any conflicting behavior in them is
superseded by this contract. In particular, old fixed one-or-two-sentence and
numeric response-length requirements are retired.

## Entry, availability, and binding

- **CALL-ENTRY-01** — Workbench shows the established fixed Phone and video Call
  controls with their icons, focus treatment, and usable touch targets. Phone
  starts or returns only `/phone`; video starts or returns only `/call`. Neither
  control can fall back to, launch, or retarget the other transport.
- **CALL-ENTRY-02** — Direct Call binds to the currently selected Agent,
  connection, Session, and context generation. Changing the reading view during
  a call does not silently rebind the live call.
- **CALL-ENTRY-03** — Current `call_ready` is authoritative. A retired stored
  route cannot block direct Call. An unresolved route remains hidden until
  discovery confirms readiness. Unknown, unavailable, or failed Call never
  invokes Phone, and Phone startup never reads or selects the Call route.
- **CALL-ENTRY-04** — Busy state prevents a second call. Existing calls remain
  returnable and endable. Unconfigured or explicitly disabled Call stays hidden.
- **CALL-ENTRY-05** — Phone and Call are basic Functions. Phone is available
  when its selected live provider/model and credential are configured. Call is
  available when its selected STT/TTS targets and credentials are configured.
  A Workbench per-instance allowlist is not an availability owner. A global
  opt-out may still disable new Call starts.

## Immediate media startup and degradation

- **CALL-MEDIA-01** — Either fixed launch control immediately displays
  Connecting and begins its own media acquisition in that same user action.
  Phone acquires its WebRTC microphone; video Call acquires microphone and
  camera. Camera is part of video Call startup, not Phone. Context and server
  startup may proceed concurrently; they must not postpone the applicable
  device permission request.
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
- **CALL-OPEN-02** — The opening begins before ordinary caller capture. Once
  playback begins, the same confirmed barge-in rule as other Agent speech may
  stop its presentation and admit the caller's new input. If the opening fails,
  the call recovers to voice listening without inventing a successful opening.

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
  defined thresholds. This includes a bounded inter-chunk grace period when the
  next public answer chunk is already known but its TTS asset is not ready. The
  approved prepared fillers may play in order to bridge an unusually long silent
  gap, and the active filler is cut off as soon as answer audio is ready. Fillers
  never enter the canonical answer, Session history, or transcript as Agent
  content.
- **CALL-SPEECH-06** — TTS and visible Call captions use the same cleaned
  conversational text. The canonical PAO Session answer remains intact outside
  the Call presentation.
- **CALL-SPEECH-07** — Direct Call admission declares Workbench as its
  presentation surface while retaining Session API as the physical frontend
  transport. Those facts are not interchangeable: public complete-sentence
  answer previews and commentary must reach the Call speech queue before the
  terminal answer whenever they become available.
- **CALL-SPEECH-08** — The selected Agent Worker is authoritative for live
  request activity. When its activity poll is available, Call must not prefer a
  shared-process compatibility store, because that store can be present but
  stale in the isolated-Worker runtime. Call consumes the Worker stream with a
  monotonic cursor so commentary and answer previews are neither missed nor
  replayed.
- **CALL-SPEECH-09** — Complete-sentence streaming uses bounded natural speech
  chunks. A sufficiently substantial complete sentence may start before the run
  finishes; adjacent short sentences are coalesced so repeated local TTS startup
  does not create avoidable sentence gaps. Chunk readiness is based on estimated
  playable duration across compact scripts, spoken words, and punctuation pauses;
  raw character count is not a valid cross-language duration proxy. A short
  terminal tail is always flushed, and every streamed chunk remains an exact
  prefix of the final chunk sequence.
- **CALL-SPEECH-10** — While Agent audio or a filler is playing, browser echo
  cancellation/noise suppression and a local detector monitor for sustained
  caller speech. Playback audio cannot retrain the noise baseline. A short
  pre-roll is retained so confirmation does not cut off the first syllable;
  brief echo, clicks, and ordinary background noise do not stop playback.
- **CALL-SPEECH-11** — Confirmed caller speech stops only the current audio
  presentation, fences late playback completion, and becomes an ordinary new
  Call input immediately. It may be conversation, newly queued work, an explicit
  cancellation instruction, or another request. Barge-in itself never guesses
  that intent and never cancels, pauses, or rewrites existing PAO work.
- **CALL-SPEECH-12** — New caller input has admission priority over unfinished
  older Call turns. Older admitted Runs may continue concurrently within a
  bounded Call-owned task set, but cannot reclaim current speech presentation.
  Existing PAO semantics remain authoritative for later queueing or explicit
  cancellation. After playback, ordinary listening may update the same-call
  background-noise estimate before the next utterance.

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
- **CALL-UI-07** — Context actions sit outside the clipped flexible media/body
  region. At 390×640 with video and rolling captions, Stop speaking remains
  fully visible and its complete hit target remains clickable.

## Voice-media billing

- **CALL-COST-01** — Phone projects a live voice-media amount from the qualified
  GPT Live per-minute rate and provider-session seconds. It does not include a
  separate backend text-model amount.
- **CALL-COST-02** — Call projects only STT and TTS media cost. Provider-returned
  cost wins; otherwise the qualified model catalogue and measured audio duration
  produce a visibly marked estimate. Backend text-model usage remains in
  `/metre`, and vision cost is excluded from the voice-media total.
- **CALL-COST-03** — Unknown catalogue price or missing usage is incomplete,
  never silently zero. The display freezes the selected qualified rate/model
  for the call so a mid-call configuration change cannot rewrite prior cost.

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
- **CALL-SAFE-05** — Hangup also stops the exact PAO Run admitted by that Call.
  If admission identity is still uncertain, Call waits only until the accepted
  request and Run IDs are known, then uses PAO's targeted cancellation fence.
  It never converts hangup into an Agent-wide stop and never cancels unrelated
  foreground or background work.
- **CALL-SAFE-06** — Browser, Workbench proxy, Remote loopback gateway, and Call
  Function records carry the same bounded correlation/trace identity. Logs
  distinguish request start, response headers, body completion, producer-made
  502, caller abort, socket cause, TTS completion, speech-cache publication and
  client retrieval. They never contain audio, transcript, answer, credentials,
  full query strings, or native exception messages.

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

## Release receipts

This contract is the stable acceptance source, not a claim that a checkout or
running process has adopted it. Exact source, artifact, running generation,
loaded renderer, restart timing, and physical media observations are recorded
as separate versioned receipts under the authorized live-evidence directory.
Every new candidate must satisfy the complete matrix below; results from an
older commit or renderer cannot be carried forward.

## Required release matrix

| Area | Mandatory evidence |
| --- | --- |
| Entry/binding | launcher and route-policy tests |
| Immediate devices | controller/media tests with delayed context/start and denied camera |
| Camera lifecycle | enable, disable, re-enable, denial, server failure, background race |
| Ring/opening | timing/order tests and live speaker observation |
| Spoken style | PCM guidance and deterministic Call-cleaning tests |
| Streaming/fillers | FIFO, complete-sentence, cancellation, no-duplicate tests |
| Captions/UI | single rolling transcript, stable mirrored preview, retained Agent identity, smart-follow, visible/clickable contextual actions, visual/browser tests |
| Voice cost | Phone per-minute and Call STT/TTS projections, exact/estimate boundary, backend text and vision exclusions |
| Recovery/hangup | existing noise, network, lifecycle, and settlement regressions |
| H3 live | versioned frontend acceptance receipt plus real media observations |
| H4 live | exact deployed artifact/source identity plus live observations, without reboot |
