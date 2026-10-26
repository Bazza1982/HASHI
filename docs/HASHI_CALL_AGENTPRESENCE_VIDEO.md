# Call external AV output and AgentPresence

Decision: user-approved integration on HASHI3, 2026-10-23.
Functional owner: Frontend Connector Call Function; PAO retains the Compute
Service Directory and Conversation/Run authority. Layer 2 Functions and
Layer 4 instance configuration. Protected Core is unchanged.

## Command policy (2026-10-23)

- AP is authoritative for whether its active character supports human-face
  VideoTTS. When it explicitly advertises `video_tts_supported: false`,
  `/call` keeps ordinary TTS from the start, creates no AP video context and
  submits no VideoTTS job. This is the character's normal mode, not degradation.
- When the opted-in AP connector is connected, `/call` prefers VideoTTS and
  negotiates `call-output/1`. If VideoTTS is missing, unavailable, or fails for
  one reply, the same Call visibly hands reply audio back to the Call Window and
  uses the existing ordinary TTS chain. A runtime failure affects that speech
  segment only; later segments probe VideoTTS again. It does not start Phone or
  end the Call. AP-free Call retains its existing behavior.
- `/phone` keeps the existing live-audio provider, microphone and audio owner.
  The companion leaves for the phone call, stays away, then returns on hangup.
  The two media services share their existing foreground admission interlock.
- Phone lifecycle events carry `mode: phone`, instance and logical call IDs.
  Only these events reach `/v1/phone/start` and `/v1/phone/end`. Generic Call
  events carry `mode: call` and cannot trigger departure.
- Phone notifications follow successful state persistence and are serialized
  for each owner, with subscribers frozen at start. Replays, Function recovery,
  failure and terminal cleanup retain logical call scope. Audio is never
  mirrored into the ordinary voice-reply animation.
- AP advertises `ap-phone/1`; its legacy `/v1/call/incoming` and
  `/v1/call/ended` endpoints remain compatibility-only animation controls.

## Responsibilities

- Without an opted-in, authenticated and instance-bound AV connector,
  Call keeps its existing STT, camera, PAO, audio TTS, fallback and window playback.
- AP owns the current character capability. HASHI consumes the advertised
  boolean and does not classify characters or inspect their images. Older AP
  versions without the field retain the previous VideoTTS-compatible behavior.
- While negotiated AV remains healthy, HASHI Call selects and invokes VideoTTS. AP supplies
  the active pack/day-or-night PNG and owns enter/idle/exit clips and synchronized
  reply audio/video. HASHI keeps reference/media bytes transiently for transport.
- A service must return one complete H.264/AAC MP4 containing both video and
  audio. AP validates the tracks and independently confirms decode/presentation.
  Separate, unsynchronized audio is not treated as a successful AV response.
- AP is the sole reply-audio presenter only while a segment uses AV. Call returns
  no browser audio bytes for that segment and does not mirror it to AP. A
  transient failure hands only the affected segment to the Call Window.
  Structural inability (for example, no supported face/reference or an
  incompatible capability) keeps ordinary TTS for the Call. Microphone/camera
  admission and PAO Run ownership stay with their existing owners.

## Exact service selection

The existing revisioned, ignored `call_profiles.json` owns an optional top-level
`video_tts` selection. It is disabled/absent by default. Existing audio TTS
selections are preserved. A call freezes this selection at its Call revision.

```json
{
  "video_tts": {
    "enabled": true,
    "reference": {"node_id": "studio", "service_id": "avatar", "model_id": "talker"},
    "protocol": "video.speech.async.v1",
    "voice": "warm",
    "speed": 1.0,
    "max_input_chars": 400,
    "job_path": "/avatar/jobs/{job_id}",
    "cancel_path": "/avatar/jobs/{job_id}/cancel",
    "request_timeout_seconds": 120,
    "job_timeout_seconds": 180,
    "wait_base_seconds": 18.5,
    "wait_per_char_seconds": 0.17,
    "wait_min_seconds": 20,
    "wait_max_seconds": 90
  }
}
```

The PAO directory resolves the exact configured node/service/model with
text+image input and audio+video output. Formal manifests may use arbitrary
relative paths for these protocols; the compatible `/v1/video/speech` path maps
to `video.speech.async.v1`. This does not require a Hashi Compute vendor.
`video.speech.mp4.v1` supports a direct complete MP4 POST response instead.

Async input is `{model,input,voice,speed,image}`; image is an AP PNG data URI.
The service returns `{id,status}` and the job path returns the same identity,
status and `video_url` on completion. Terminal states are completed/failed/
cancelled. Cancel is optional and declared by the configured adapter path.
The result URL must remain on the configured origin; redirects and environment
proxies are disabled. Responses are bounded. Privacy is checked before every
generation and again before presentation. A changed configured node origin
cannot silently redirect an existing call. A failed or uncertain VideoTTS job
is never resubmitted to another video provider; Call instead uses its already
configured ordinary TTS chain.

An accepted job ID is projected to the current turn and content-free diagnostic
journal before polling. The foreground wait is calculated from text length and
is separate from the longer terminal-state tracking limit. With the calibrated
defaults it is `18.5 + 0.17 * characters` seconds, clamped to 20–90 seconds.
When that wait expires, ordinary TTS races the still-running job. Video wins if
it becomes ready before audio is published; otherwise audio owns only that
segment. A later successful video is retained as a health/recovery signal for
the next segment and is never replayed over published audio. An uncertain
submission is never resubmitted. Cancellation fences local output immediately;
late results from an old turn cannot upload or resume presentation.

External AV speech is segmented separately from ordinary audio Call. Streaming
provider answer deltas, including Codex app-server agent-message deltas, enter
the Workbench-only answer-preview lane; commentary deltas remain excluded. The
first complete sentence is released immediately as a short VideoTTS request,
without waiting for the PAO Run to finish. All remaining cleaned text is then
submitted as one second request. Call keeps up to two VideoTTS renders in flight,
so the body enters Compute as soon as it is known instead of waiting for the
first complete MP4. AP preload and playback remain strictly ordered: a later
render may finish first but cannot overtake the opener at the presentation gate.
A one-sentence answer remains one request, and ordinary audio Call remains
serial. The configured input-character guard still applies. Preview,
commentary, final, and opening speech share this rule. A failed async job
remains terminal and is not retried; diagnostics retain a validated provider
error code. Non-success HTTP responses retain only their numeric status, never
their body.

## Window contract: call-output/1

The existing authenticated/scoped Call endpoint remains authoritative. New
context responses advertise `output_protocols: ["call-output/1"]`.

Before AV negotiation, the connector probes AP's active-character capability.
An explicit `video_tts_supported: false` returns no AV sink, so the existing
window-audio/ordinary-TTS route proceeds without opening an AP video session or
reporting a VideoTTS failure.

1. A capable window sends `output_protocol: "call-output/1"` on `start`.
   If an enabled AP AV sink and VideoTTS are available, `output.mode` is
   `external_av` and `output.audio_owner` is `agentpresence`. If VideoTTS is
   unavailable, `output.mode` is `window_audio`, the stable reason is visible,
   and the same Call starts on ordinary TTS. Duplicate starts join the same
   in-flight negotiation. An old window cannot silently enter AV mode.
2. The usual `speech` operation waits for generation and AP preload. A ready
   response contains `output` and a `presentation` receipt, with no `content_b64`.
   The receipt projects the logical Call ID supplied by the window. A connector
   may use a different private ID to isolate its local playback context, but that
   ID remains inside the connector and is never exposed as the window Call ID.
3. The window stops ringback/local speech/fillers and yields audio before sending
   `speech_commit` with the usual scope, turn ID, segment and output protocol.
   Commit is idempotent; acceptance does not claim a frame has been presented.
4. `speech_status` (same scope, turn ID and segment) returns AP receipt states
   and actual media time/audio pause/mute/signal facts. The window maintains its
   presentation/VAD/caption state until completed, interrupted, cancelled or failed.
5. Confirmed barge-in sends `interrupt` with the current turn ID immediately.
   It stops presentation and clears the speech queue without cancelling PAO work.
   Duplicate interruption of the same turn does not advance AP twice. A newly
   accepted caller turn also fences prior AV before generating new output.
6. A transient VideoTTS failure returns only that segment to ordinary TTS and
   leaves the scoped AP presentation available for recovery. Workbench follows
   HASHI3's per-segment remaining-time hint instead of counting a fixed number
   of polls. Structural incompatibility still ends AV ownership for the Call.
   `end`, lease expiry and service shutdown end the scoped AP context.

Control operations preserve owner/client/Agent/Conversation/context-generation/
Call-generation binding. Missing or stale IDs are rejected. Camera changes
remain independent of avatar output. A fresh service failure is exposed through
speech/turn receipts and a stable error code; readiness alone is not generation.

## Implementation and evidence scope

The implementation is in `frontend_call/output.py`, `video_tts.py`, the existing
Call service/config/routes/port, and the AP Connector port. The connector owns
its existing token and local transport; Call business code imports no AP client.

Focused checks cover legacy audio, visible VideoTTS-to-audio degradation,
provider-delta preview, concurrent bounded
first-sentence/remainder generation, ordered AV prepare/commit, late-result
fencing, duplicate start, unused audio-TTS
credentials, configured third-party manifests, privacy/origin/download bounds,
cancellation and AP generation races.
Candidate CallService + real service + real AP has produced fresh day/night
video, actual frame/audio progress, complete playback and night hangup. Native
interruption paused/muted the actual player and repeated interruption stayed
on the same generation. These probes used a controlled utterance and no live
PAO Conversation admission or Call Window; they are not complete user acceptance.

AP 1.5.1 fixes late call entry after the startup overlay has been removed, and
reports JavaScript dispatch failure instead of waiting forever in entering.
The regression was observed red before the DOM guard and green afterward.

## Integration FYI — 2026-10-24

The Workbench Call Window candidate now negotiates `call-output/1`, yields
browser reply audio, follows AP presentation receipts, and pauses microphone
capture during external playback. Its independent native Windows entry has
authenticated to HASHI3 through the existing Remote HMAC. The instance VideoTTS
selection was enabled with revision-checked atomic publication, retaining the
configured voice.

A real newly created Conversation reached PAO opening completion and a new
VideoTTS job. Camera-on requests exposed a Hashi Compute gateway-capacity
translation defect: activity was treated as saturation. The directory fix has
focused red/green evidence, including retained full-capacity rejection; it is
not yet adopted by the running shared Function generation.

With the test camera denied, the provider accepted a job, reported partial
rendering, then terminated with `video_render_failed` and no complete MP4.
That is a separate provider failure; its public receipt does not establish the
underlying rendering cause. No incomplete media was promoted to AP playback.
Full STT/reply/presentation and physical-user acceptance remain unverified.
The current campaign has not tested `/phone`.

Source commit, Function-generation qualification, live hot adoption and
physical-user acceptance remain separate facts. Neither component checks nor
the successful PAO opening establish a passing end-to-end `/call`.
