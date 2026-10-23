# Call external AV output and AgentPresence

Decision: user-approved integration on HASHI3, 2026-10-23.
Functional owner: Frontend Connector Call Function; PAO retains the Compute
Service Directory and Conversation/Run authority. Layer 2 Functions and
Layer 4 instance configuration. Protected Core is unchanged.

## Responsibilities

- Without an enabled, opted-in, authenticated and instance-bound AV connector,
  Call keeps its existing STT, camera, PAO, audio TTS, fallback and window playback.
- In negotiated AV mode, HASHI Call selects and invokes VideoTTS. AP supplies
  the active pack/day-or-night PNG and owns enter/idle/exit clips and synchronized
  reply audio/video. HASHI keeps reference/media bytes transiently for transport.
- A service must return one complete H.264/AAC MP4 containing both video and
  audio. AP validates the tracks and independently confirms decode/presentation.
  Separate, unsynchronized audio is not treated as a successful AV response.
- AP is the sole reply-audio presenter in this mode. Call returns no browser
  audio bytes, does not mirror ordinary call speech to AP, and skips audio fillers.
  Microphone/camera admission and PAO Run ownership stay with their existing owners.

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
    "job_timeout_seconds": 900
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
cannot silently redirect an existing call. There is no automatic provider fallback.

An accepted job ID is projected to the current turn and content-free diagnostic
journal before polling. An uncertain submission or job outcome is never
automatically resubmitted; explicit speech retry cannot replay an uncertain job.
Cancellation fences local output immediately and optionally cancels the known
remote job. Late results cannot upload or resume old presentation.

## Window contract: call-output/1

The existing authenticated/scoped Call endpoint remains authoritative. New
context responses advertise `output_protocols: ["call-output/1"]`.

1. A capable window sends `output_protocol: "call-output/1"` on `start`.
   If an enabled AP AV sink is available, `output.mode` is `external_av` and
   `output.audio_owner` is `agentpresence`. Duplicate starts join the same
   in-flight negotiation. An old window cannot silently enter this mode.
2. The usual `speech` operation waits for generation and AP preload. A ready
   response contains `output` and a `presentation` receipt, with no `content_b64`.
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
6. `end`, lease expiry and service shutdown fence output and end the scoped AP
   context. Snapshot polling observes AP hangup/disconnection and ends the same
   HASHI Call. No mid-utterance audio-only fallback or replay is inferred.

Control operations preserve owner/client/Agent/Conversation/context-generation/
Call-generation binding. Missing or stale IDs are rejected. Camera changes
remain independent of avatar output. A fresh service failure is exposed through
speech/turn receipts and a stable error code; readiness alone is not generation.

## Implementation and evidence scope

The implementation is in `frontend_call/output.py`, `video_tts.py`, the existing
Call service/config/routes/port, and the AP Connector port. The connector owns
its existing token and local transport; Call business code imports no AP client.

Focused checks cover legacy audio, AV prepare/commit, late-result fencing,
duplicate start, unused audio-TTS credentials, configured third-party manifests,
privacy/origin/download bounds, cancellation and AP generation races.
Candidate CallService + real service + real AP has produced fresh day/night
video, actual frame/audio progress, complete playback and night hangup. Native
interruption paused/muted the actual player and repeated interruption stayed
on the same generation. These probes used a controlled utterance and no live
PAO Conversation admission or Call Window; they are not complete user acceptance.

AP 1.5.1 fixes late call entry after the startup overlay has been removed, and
reports JavaScript dispatch failure instead of waiting forever in entering.
The regression was observed red before the DOM guard and green afterward.

The external Call Window repository is outside this Run’s access roots. Its
handshake/playback/VAD changes and a real `/call` remain pending. The instance
VideoTTS selection stays disabled until that client is connected. Source commit,
Function-generation qualification, live hot adoption and physical-user acceptance
must be recorded separately; no source test alone claims runtime adoption.
