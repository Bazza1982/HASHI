# HASHI Voice Bridge Plan

Status: **deferred research note** for `v3.2.0` and later.

This document records the research and paused plan for adding a real voice-call interface to HASHI.

The WhatsApp Desktop route was investigated as a low-cost local playground, not as an official WhatsApp integration. After Phase 0 experiments, the route is **deferred** because stable unattended call detection/answering through WhatsApp Desktop is too fragile for the current roadmap.

The original local desktop bridge idea was:

```text
Barry calls the WhatsApp account already logged in on HASHI1
 -> WhatsApp Desktop receives the real voice call
 -> HASHI1 captures WhatsApp Desktop audio at the OS audio layer
 -> local VAD/STT turns speech into text
 -> existing HASHI backend/agent routing produces a response
 -> local TTS produces speech
 -> HASHI1 injects TTS audio into WhatsApp Desktop as a virtual microphone
 -> the same WhatsApp call continues for the next turn
```

This is an unsupported local desktop integration, not an official WhatsApp integration. It was acceptable as a personal HASHI experiment using two already-owned WhatsApp numbers on a machine where WhatsApp Desktop was already logged in and configured. It is **not** currently accepted as a stable unattended production feature.

Use the spelling **HASHI** everywhere.

## Current Decision

Current status:

```text
deferred / revisit later
```

Deferred or future routes:

- WhatsApp Desktop local-call bridge.
- WhatsApp Web + Chrome/CDP call automation if browser-call controls become stable enough.
- Official WhatsApp Business Calling / Cloud API if account eligibility and media access fit the use case.
- Twilio WhatsApp Business Calling.
- Twilio normal phone number / Programmable Voice.
- Outgoing calls from HASHI to Barry.

Rejected routes:

- Telegram bot real-time voice calls: the official Telegram Bot API does not provide normal bot voice-call media access.
- WhatsApp text messages or voice messages: the target is a real call.
- Self-implementing the WhatsApp real-time call protocol in WSL2: message transport support does not imply call transport support. Calls require a separate real-time stack, including WebRTC/media/signaling/encryption behavior. This is not practical for HASHI's roadmap.
- WhatsApp Desktop automation as a production-grade unattended integration: this route is only suitable for a local personal playground unless major new background-capable signals become available.

## Research Summary

What was tested:

- Windows-native helper process launched from WSL2.
- WhatsApp Desktop process/window probing.
- Windows UI Automation scan of WhatsApp Desktop.
- Screenshot-based diagnostics.
- OCR fallback planning for visible call text.
- Real manual WhatsApp calls into the account logged in on HASHI1.

What worked:

- HASHI1 WhatsApp Desktop could receive a real incoming call.
- Windows helper could communicate with WSL-side HASHI over local HTTP.
- The helper could capture desktop screenshots and inspect WhatsApp Desktop window/process state.
- Phase 0 event logs were written under `logs/voice_sessions/`.

What blocked the route:

- UI Automation mostly exposed the WhatsApp WebView shell, not reliable call/chat content or answer buttons.
- Screenshot/OCR depends on an unlocked interactive Windows desktop.
- Locked-screen behavior hides the useful WhatsApp UI from screenshots and UI automation.
- Minimized/background/focused behavior may differ.
- WhatsApp Desktop can update and change UI structure without warning.
- Automatic answering would still require a reliable active-call detector plus either a stable button target or another call-control mechanism.

Decision:

- Pause WhatsApp Desktop call integration.
- Keep this document and the experimental helper code as future reference.
- Revisit only when the team has spare time or a more stable call-control surface becomes available.

## Experimental Files Preserved

The following Phase 0 files exist in the repository and are intentionally preserved as reference material:

- `apps/voice_whatsapp_desktop_runtime.py`
  - WSL-side Phase 0 runner.
  - Talks to the Windows helper over local HTTP.
  - Logs JSONL voice events under `logs/voice_sessions/`.
  - Supports detect-only mode, optional auto-answer, deeper UIA diagnostics, and screenshot OCR fallback.
- `tools/windows_helper/whatsapp_call_probe.py`
  - Windows-side WhatsApp Desktop call probe.
  - Scans WhatsApp windows/processes and UI Automation controls.
  - Separates active-call evidence from missed-call evidence.
- `tools/windows_helper/backends.py`
  - Exposes the `whatsapp_call_probe` helper action through the Windows helper server.
- `orchestrator/voice/events.py`
  - Minimal JSONL event logger used by the Phase 0 probe.
- `orchestrator/voice/windows_helper_client.py`
  - WSL-side client for calling Windows helper actions.
- `tests/test_voice_phase0.py`
  - Focused tests for helper-client parsing, voice event logging, active/missed call classification, and OCR fallback classification.

Relevant commits:

- `d8d748d Add WhatsApp desktop call probe`
- `bc9b1e3 Improve WhatsApp desktop process probing`
- `35c5898 Enhance WhatsApp call probe diagnostics`
- `5ba8a0b Document deferred WhatsApp voice research`

## Original Goals

- Prove that an incoming WhatsApp Desktop call on HASHI1 can be detected and answered.
- Keep one WhatsApp call open across multiple turns.
- Keep the voice runtime transport-agnostic so the same VAD/STT/TTS/agent logic can run against:
  - local microphone/speaker,
  - WhatsApp Desktop virtual audio,
  - future Twilio/official call streams.
- Reuse the existing HASHI backend and agent routing.
- Add a phone-optimized agent profile only where it improves latency and call behavior.
- Keep voice feature code modular, debuggable, and compatible with the slim-core/hot-reboot architecture.
- Log every state transition, turn, and audio/agent step clearly enough to debug failed calls.

These goals are preserved for future reference. They are not active implementation commitments while this feature remains deferred.

## Non-Goals From The First Experiments

- Do not start with OpenAI Realtime.
- Do not require WhatsApp Business.
- Do not require Twilio.
- Do not implement outgoing calls.
- Do not implement full barge-in/interruption.
- Do not scale to multiple simultaneous calls.
- Do not decode WhatsApp network traffic or bypass WhatsApp encryption.
- Do not put long-lived audio/call handles into `main.py`.

## Archived Fastest Proof

The first proof was intentionally smaller than voice AI:

```text
Incoming WhatsApp call to HASHI1 can be picked up.
```

The implementation must use a Windows-native helper because HASHI runs in WSL2 while WhatsApp Desktop, Windows UI Automation, and VB-CABLE audio devices live on the Windows side.

Acceptance for the first proof:

- HASHI1 has WhatsApp Desktop open and logged in.
- A second WhatsApp number calls the HASHI1 WhatsApp account.
- A Windows-native helper detects the incoming call UI.
- The probe clicks the answer button, or a human clicks it while the probe logs detection.
- The call stays connected for at least 60 seconds.
- The probe can log call state and optionally hang up.

No VAD, STT, TTS, or agent backend was required for this first proof.

Manual preflight before writing automation:

- WhatsApp Desktop is installed, logged in, and can receive calls.
- WhatsApp Desktop notifications are not blocked.
- Windows Do Not Disturb / Focus Assist is off for the test.
- WhatsApp Desktop can run in the background.
- Barry calls the HASHI WhatsApp account manually from the second WhatsApp number.
- The team records which incoming-call UI appears:
  - full app window,
  - floating overlay,
  - notification toast,
  - taskbar/tray only.
- The team confirms whether the same UI appears when WhatsApp Desktop is focused, minimized, and hidden behind another window.
- The team confirms the call rings long enough for automation to answer, target at least 30 seconds.

## Proposed High-Level Architecture

```text
Windows native helper process
  - WhatsApp Desktop UI probe
  - Windows UI Automation call control
  - VB-CABLE / VoiceMeeter audio capture
  - virtual microphone audio injection
  - exposes local socket/HTTP API
        |
        | call events + PCM frames + playback commands
        v
WSL2 HASHI process
  - DesktopWhatsAppTransport socket client
  - VoiceSessionRuntime
  - VADSegmenter
  - STTAdapter
  - HashiAgentClient
  - TTSAdapter
  - Voice event log / transcript / metrics
```

The voice runtime owns the state machine. The Windows helper owns Windows-only UI and audio handles. The WSL2 HASHI process owns VAD/STT/agent/TTS/session logic. WhatsApp Desktop UI automation must stay outside the core runtime.

The helper boundary is mandatory, not optional:

- WSL2 Python cannot reliably control Windows app UI with `pywinauto`, `uiautomation`, or Win32 APIs.
- WSL2 Python cannot directly open Windows VB-CABLE/VoiceMeeter devices with `sounddevice`.
- A Windows-native helper can do both, then stream normalized events/audio to HASHI over localhost.

Preferred first bridge protocol:

```text
127.0.0.1 TCP socket or HTTP/WebSocket
```

Keep the protocol simple:

- JSONL control/events channel.
- Binary or base64 PCM frames for audio.
- Explicit sequence numbers and timestamps for audio frames.
- Heartbeat from helper to HASHI.
- Clear shutdown event when WhatsApp call ends.

## Proposed Module Layout

```text
orchestrator/voice/
  __init__.py
  runtime.py
  state.py
  types.py
  logging.py

orchestrator/voice/transports/
  __init__.py
  base.py
  local.py
  desktop_whatsapp.py
  twilio_voice.py

orchestrator/voice/audio/
  __init__.py
  vad.py
  codec.py
  resample.py
  jitter.py
  virtual_devices.py

orchestrator/voice/stt/
  __init__.py
  base.py
  faster_whisper.py

orchestrator/voice/tts/
  __init__.py
  base.py
  piper.py

orchestrator/voice/agent/
  __init__.py
  hashi_client.py
  phone_operator.py

apps/
  voice_local.py
  voice_whatsapp_desktop_runtime.py

tools/windows_helper/
  server.py
  whatsapp_call_probe.py

tests/
  test_voice_phase0.py
```

`orchestrator/voice/` contains reusable WSL-side voice logic. `tools/windows_helper/` contains the existing Windows-native helper service and WhatsApp call-probe actions; it must be run with native Windows Python, for example `py.exe`, not WSL Python. `apps/` contains WSL-side entrypoints and may require cold restart unless a dedicated service restart is added.

## Hot-Reboot Boundary

Current slim-core hot reload primarily covers project modules such as `orchestrator/`.

Therefore:

- Changes under `orchestrator/voice/` should be designed to be adopted by `/reboot` where possible.
- Changes under `apps/` are entrypoint/service changes and should be treated as requiring cold restart or a dedicated voice service restart.
- Changes under `tools/windows_helper/` require restarting the Windows helper process.
- Active WhatsApp Desktop calls cannot be assumed to survive runtime code replacement.
- UI automation handles, audio device handles, and active call state should not be stored in module-level globals.

If this feature becomes a long-running built-in service, add a `VoiceManager` that owns:

- voice service lifecycle,
- active call registry,
- WSL-side helper connection state,
- helper heartbeat/watchdog,
- graceful shutdown/restart.

The Windows helper owns:

- WhatsApp Desktop UI automation session,
- audio device handles,
- active Windows call-control state,
- capture/playback streams.

Until then, the WhatsApp Desktop bridge can run as explicit paired processes:

```text
Windows: py.exe -m tools.windows_helper.server
WSL2:    python -m apps.voice_whatsapp_desktop_runtime
```

Phase 0 starter commands:

```text
# Windows side, from the HASHI repo checkout:
uv run --no-project --with fastapi --with uvicorn --with fastmcp --with windows-mcp --with pillow --with uiautomation python -m tools.windows_helper.server

# WSL2 side, detect only:
python -m apps.voice_whatsapp_desktop_runtime --duration 60 --exit-on-detect

# WSL2 side, detect only with UIA tree diagnostics and screenshot OCR fallback:
python -m apps.voice_whatsapp_desktop_runtime --duration 60 --exit-on-detect --include-uia-tree --ocr-fallback

# WSL2 side, allow automatic answering when an answer control is detected:
python -m apps.voice_whatsapp_desktop_runtime --duration 60 --exit-on-detect --auto-answer
```

If the default Windows helper port is occupied or unhealthy, use an explicit port on both sides:

```text
Windows: uv run --no-project --with fastapi --with uvicorn --with fastmcp --with windows-mcp --with pillow --with uiautomation python -m tools.windows_helper.server --port 48999
WSL2:    python -m apps.voice_whatsapp_desktop_runtime --helper-url http://127.0.0.1:48999 --duration 60 --exit-on-detect
```

Use detect-only mode first. Enable `--auto-answer` only after manual preflight confirms the incoming-call UI and false-positive behavior.

## Audio Type Contracts

Define these before Phase 1 code.

Internal audio should use one canonical format:

```text
encoding: pcm_s16le
channels: 1
sample_rate_hz: 16000
frame_duration_ms: 20 preferred for streaming frames
```

Example type shape:

```python
@dataclass(frozen=True)
class AudioFrame:
    pcm: bytes
    sample_rate_hz: int
    channels: int
    encoding: Literal["pcm_s16le"]
    duration_ms: int
    timestamp_ms: int
    source: str


@dataclass(frozen=True)
class AudioBuffer:
    pcm: bytes
    sample_rate_hz: int
    channels: int
    encoding: Literal["pcm_s16le"]
    duration_ms: int
```

All transports must convert into this internal format before handing frames to `VoiceSessionRuntime`.

Provider/desktop-specific formats live at the edge:

- Windows virtual devices may expose 44.1 kHz or 48 kHz audio.
- WhatsApp Desktop output may be stereo.
- Twilio Media Streams use `audio/x-mulaw` at 8000 Hz.
- TTS engines may output 16 kHz, 22.05 kHz, 24 kHz, or 48 kHz audio.

The runtime should reject unknown formats early with clear errors rather than passing garbled audio to VAD/STT.

## VAD Frame Contract

`VADSegmenter` must own buffering and rechunking.

Reason:

- `webrtcvad` requires fixed 10/20/30 ms frames at supported sample rates.
- `silero-vad` has different window expectations.
- Desktop and provider transports may deliver variable frame sizes.

The transport should not need to know VAD-specific frame requirements.

## Core Interfaces

```python
class AudioTransport:
    async def start(self) -> None: ...
    async def recv_frame(self) -> AudioFrame: ...
    async def send_audio(self, audio: AudioBuffer) -> None: ...
    async def is_connected(self) -> bool: ...
    async def close(self) -> None: ...
```

`recv_frame()` may raise a transport-specific disconnect exception, but `is_connected()` gives the state machine a clean signal for call-ended handling.

For WhatsApp Desktop, `DesktopWhatsAppTransport` is a WSL-side socket client for the Windows helper. It must not directly import or call Windows UI/audio libraries.

```python
class CallControl:
    async def detect_incoming_call(self) -> IncomingCall | None: ...
    async def answer(self, call: IncomingCall) -> None: ...
    async def hangup(self) -> None: ...
```

`CallControl` is implemented in the Windows helper for WhatsApp Desktop automation. The WSL side receives call-control events through the helper protocol.

```python
class VADSegmenter:
    async def accept_frame(self, frame: AudioFrame) -> VADResult: ...
```

```python
class STTAdapter:
    async def transcribe(self, utterance: AudioBuffer) -> Transcript: ...
```

```python
class HashiAgentClient:
    async def ask(self, session_id: str, text: str, metadata: dict) -> str: ...
```

```python
class TTSAdapter:
    async def synthesize(self, text: str, voice: str | None = None) -> AudioBuffer: ...
```

## WhatsApp Desktop Data Flow

```text
Barry calls HASHI's WhatsApp number
 -> WhatsApp Desktop on HASHI1 rings
 -> CallControl detects incoming call UI
 -> CallControl answers the call
 -> WhatsApp Desktop plays caller audio through a selected output device
 -> DesktopWhatsAppTransport captures that output from a virtual/loopback device
 -> audio is converted to internal mono pcm_s16le 16 kHz
 -> VAD detects utterance boundary
 -> STT turns speech into text
 -> HashiAgentClient sends text to the selected HASHI agent/profile
 -> agent response is constrained for phone speech
 -> TTS synthesizes local speech
 -> TTS output is converted to the format expected by the virtual microphone
 -> WhatsApp Desktop uses the virtual microphone as its mic input
 -> Barry hears HASHI in the same call
```

## Local Simulation Data Flow

```text
Local microphone
 -> LocalAudioTransport
 -> VAD
 -> STT
 -> HASHI backend
 -> TTS
 -> local speaker
```

Local simulation remains useful for developing VAD/STT/TTS without touching WhatsApp Desktop.

## State Machine

```text
IDLE
  -> WAITING_FOR_CALL
  -> CALL_CONNECTED for local/manual runs

WAITING_FOR_CALL
  -> INCOMING_CALL_DETECTED
  -> STOPPED

INCOMING_CALL_DETECTED
  -> ANSWERING
  -> WAITING_FOR_CALL if false alarm

ANSWERING
  -> CALL_CONNECTED
  -> ERROR_RECOVERY

CALL_CONNECTED
  -> LISTENING
  -> CALL_ENDED

LISTENING
  -> USER_SPEAKING
  -> CALL_ENDED

USER_SPEAKING
  -> ENDPOINTING

ENDPOINTING
  -> TRANSCRIBING
  -> LISTENING if false alarm

TRANSCRIBING
  -> THINKING
  -> ERROR_RECOVERY

THINKING
  -> SYNTHESIZING
  -> ERROR_RECOVERY

SYNTHESIZING
  -> SPEAKING

SPEAKING
  -> LISTENING
  -> CALL_ENDED

ERROR_RECOVERY
  -> LISTENING if recoverable
  -> CALL_ENDED if fatal
```

Half-duplex MVP rules:

- Listen only while in `LISTENING`, `USER_SPEAKING`, and `ENDPOINTING`.
- Do not attempt barge-in while `THINKING`, `SYNTHESIZING`, or `SPEAKING`.
- If speech is detected during `SPEAKING`, log it but do not interrupt in v1.
- Return to `LISTENING` after playback completes.

## Logging And Audit Requirements

Every call has:

- `voice_session_id`
- `call_id`
- `transport`
- `started_at`
- `ended_at`
- `ended_reason`

Every turn has:

- `turn_id`
- `started_at`
- `completed_at`
- latency metrics for VAD/STT/agent/TTS/playback
- user transcript
- agent response text
- recoverable error state if applicable

Write JSONL logs under:

```text
logs/voice_sessions/{voice_session_id}.jsonl
```

Minimum event types:

- `call.waiting`
- `call.incoming_detected`
- `call.answering`
- `call.answered`
- `call.ended`
- `transport.connected`
- `transport.disconnected`
- `audio.device_selected`
- `audio.capture_started`
- `audio.capture_stalled`
- `audio.capture_level`
- `audio.playback_started`
- `audio.playback_completed`
- `helper.connected`
- `helper.disconnected`
- `helper.heartbeat`
- `state_transition`
- `turn.started`
- `vad.speech_started`
- `vad.speech_ended`
- `stt.started`
- `stt.completed`
- `agent.started`
- `agent.completed`
- `tts.started`
- `tts.completed`
- `turn.completed`
- `error.recoverable`
- `error.fatal`

`state_transition` schema:

```json
{
  "event": "state_transition",
  "from_state": "LISTENING",
  "to_state": "USER_SPEAKING",
  "reason": "vad_speech_started"
}
```

`call.incoming_detected` schema:

```json
{
  "event": "call.incoming_detected",
  "detection_method": "uia",
  "detection_latency_ms": 2300,
  "window_state": "minimized"
}
```

`audio.device_selected` schema:

```json
{
  "event": "audio.device_selected",
  "role": "capture",
  "device_name": "CABLE Output (VB-Audio Virtual Cable)",
  "sample_rate_hz": 48000,
  "channels": 2
}
```

Periodic `audio.capture_level` schema:

```json
{
  "event": "audio.capture_level",
  "peak_db": -18.4,
  "rms_db": -24.1,
  "frame_count": 50
}
```

`error.*` schema:

```json
{
  "event": "error.recoverable",
  "state": "TRANSCRIBING",
  "component": "stt",
  "exception_type": "TimeoutError",
  "message": "STT timed out after 10s"
}
```

Do not record raw call audio by default. Audio capture for debugging must be an explicit config flag because calls may have consent requirements.

## Phone Operator Agent

The current HASHI backend is sufficient for the first pickup and audio-routing proofs.

For real conversation, add a phone-optimized prompt overlay rather than a separate agent identity or separate Telegram/WhatsApp bot:

```text
phone_operator
```

Purpose:

- fast responses,
- short spoken answers,
- lower max tokens,
- lighter/faster model,
- limited tool use during live calls,
- explicit timeouts,
- graceful filler when an operation takes too long.

Implementation decision:

```text
phone_operator = prompt overlay injected by HashiAgentClient for voice sessions
```

This means the call can still route to the current selected HASHI agent/profile. The overlay only changes speaking style and runtime policy for the voice call.

Suggested behavior:

- Prefer a one- or two-sentence answer.
- Ask whether Barry wants more detail before long explanations.
- Do not read large code blocks aloud.
- If work takes more than a configured threshold, say a short status line.
- If a task is long-running, create a follow-up task for normal chat instead of blocking the call.

Suggested first overlay:

```text
You are on a live voice call. Answer in one or two short spoken sentences.
Do not read code blocks, long URLs, or long lists aloud.
If a task will take more than 10 seconds, say so briefly and offer to continue in chat.
```

Initial tool policy:

- Allow: memory recall, simple arithmetic, date/time.
- Disallow during live call: web search, file I/O, shell execution, long-running workflows.
- Timeout: any tool call taking more than 5 seconds should trigger a short filler phrase and avoid blocking the call.

Suggested defaults:

- `max_tokens`: 150.
- model: fastest reliable backend available, Haiku-class or equivalent.
- `temperature`: 0.3.

The first implementation can route to the current selected agent. The phone overlay becomes important once STT/TTS is working and latency matters.

## Agent Integration Decision

Resolve before full conversation Phase 3.

Preferred first option:

```text
HashiAgentClient -> internal HASHI HTTP/API gateway -> selected agent/profile
```

Reason:

- keeps the voice runtime decoupled from internal runtime queues,
- works even if voice is run as a separate process,
- avoids importing large runtime internals into audio code.

Fallback:

```text
HashiAgentClient -> enqueue prompt into running agent runtime
```

Only use the fallback if the internal API path is not available or too slow.

## MVP Phases

### Phase 0: Incoming Call Pickup Probe

Deliverables:

- `tools.windows_helper.server` with the `whatsapp_call_probe` action.
- WSL-side helper client stub for logging helper events.
- UI detection for incoming WhatsApp Desktop call.
- Manual or automated answer path.
- Basic event log.
- UIA as primary automation method.
- Screenshot matching as fallback only if WhatsApp Desktop does not expose usable accessibility elements.

Acceptance:

- HASHI1 WhatsApp Desktop is open and logged in.
- Barry calls HASHI's WhatsApp number from the second WhatsApp account.
- Windows helper logs `call.incoming_detected`.
- Probe clicks answer, or operator manually clicks while probe logs the state.
- Call remains connected for at least 60 seconds.
- Optional hangup can be triggered.
- `call.pickup_latency_ms` is logged.
- Target detection latency is under 5 seconds.
- If detection latency exceeds 15 seconds, treat it as a failure for automation reliability.
- False-positive test: a normal WhatsApp text message must not log `call.incoming_detected`.
- Background state test: repeat the call with WhatsApp Desktop minimized and detection must still fire.

Manual preflight is mandatory before automation:

- WhatsApp Desktop rings visibly for an incoming call.
- The exact incoming-call UI shape is recorded.
- Minimized/focused/hidden behavior is recorded.
- The call rings for at least 30 seconds before timing out or becoming unavailable.

### Phase 1: Desktop Audio Route Proof

Deliverables:

- Virtual audio device setup notes for HASHI1.
- Capture WhatsApp Desktop call output from loopback/virtual sink.
- Inject fixed audio into WhatsApp Desktop as virtual microphone.
- No STT/TTS/agent required.
- Windows helper captures and injects audio.
- WSL side receives helper audio level events.

Acceptance:

- Barry speaks in the WhatsApp call.
- HASHI1 captures measurable audio frames.
- HASHI1 plays a fixed pre-recorded WAV/PCM/tone buffer into the virtual microphone.
- Barry hears the fixed audio in the call.
- Captured frame count and peak/RMS dB are logged at least once per second.
- Captured audio is not silence while Barry is speaking.
- Injected audio reaches Barry without obvious clipping.
- Captured sample rate and channel count are logged.
- Resampling from the Windows device format to internal 16 kHz mono is confirmed in logs.
- Echo check passes: physical speaker/microphone are muted or disconnected, and captured frames do not contain TTS output echo.

Phase 1 must already use the `AudioTransport` interface. `VoiceSessionRuntime` must not depend directly on `DesktopWhatsAppTransport`.

### Phase 2: Local Voice Runtime

Deliverables:

- `LocalAudioTransport`.
- VAD segmenter.
- Programmatically generated audio fixtures for tests.
- Local mic/speaker manual test.

Acceptance:

- Local mic input triggers VAD start/end.
- Runtime plays a pre-recorded fixture through `LocalAudioTransport.send_audio()`.
- Five local turns can complete without process restart.

### Phase 3: STT Integration

Deliverables:

- `STTAdapter`.
- First local STT implementation, likely `faster-whisper`.
- Empty transcript handling.
- STT latency logging.

Acceptance:

- Captured speech turns into text.
- Empty/uncertain speech returns to listening.
- STT failure is recoverable.

### Phase 4: HASHI Agent Integration

Deliverables:

- `HashiAgentClient`.
- Voice session to HASHI agent/profile mapping.
- Timeout and fallback response.
- Optional `phone_operator` profile.

Acceptance:

- Spoken input produces a HASHI text response.
- Backend timeouts are logged and recoverable.
- A lighter/faster phone profile can be selected when configured.

### Phase 5: TTS Integration

Deliverables:

- `TTSAdapter`.
- First local TTS implementation, likely `piper` or another lightweight local TTS.
- Response shortening/chunking policy for phone speech.

Acceptance:

- HASHI response is synthesized and played locally.
- Synthesized audio can be routed into WhatsApp Desktop through virtual microphone.
- TTS failures are recoverable.

### Phase 6: End-To-End WhatsApp Desktop Conversation

Deliverables:

- `DesktopWhatsAppTransport`.
- Windows helper call-control events integrated with runtime.
- Windows helper audio capture/input injection integrated through the helper protocol.
- Full event log.

Acceptance:

- Barry calls HASHI on WhatsApp.
- HASHI answers.
- Barry speaks one utterance.
- HASHI transcribes it, routes to agent, synthesizes response, and speaks back.
- At least five turns complete in one call.

### Phase 7: Hardening

Deliverables:

- Watchdog for WhatsApp Desktop availability.
- Audio-device health checks.
- Manual recovery commands.
- Clear troubleshooting documentation.
- Optional outgoing call automation investigation.

Acceptance:

- Failures identify whether the fault is UI detection, audio capture, audio injection, VAD, STT, agent, or TTS.
- A failed call does not corrupt the next call session.

## Windows Desktop Audio Plan

For HASHI1, start with Windows-friendly tooling:

- VB-CABLE for the fastest proof.
- VoiceMeeter if routing needs more control.
- Windows-native Python helper launched with `py.exe`.
- UIA through `uiautomation` or `pywinauto` as the first call-control method.

Intended routing:

```text
WhatsApp Desktop speaker/output device
 -> virtual output / loopback
 -> Windows helper capture
 -> HASHI over localhost helper protocol

HASHI TTS output
 -> Windows helper playback
 -> virtual input device
 -> WhatsApp Desktop microphone/input device
```

The exact device names should be logged at startup.

Manual Phase 1 audio preflight:

- VB-CABLE is installed on HASHI1.
- Windows audio settings show:
  - `CABLE Output (VB-Audio Virtual Cable)` as a capture source for HASHI/helper.
  - `CABLE Input (VB-Audio Virtual Cable)` as a playback sink for helper/TTS injection.
- WhatsApp Desktop speaker/output device is set to the virtual cable path that the helper captures.
- WhatsApp Desktop microphone/input device is set to the virtual cable path that receives helper/TTS playback.
- Windows privacy settings allow microphone access for the Windows helper.
- Physical speakers and physical microphones are muted or disconnected during tests.

Expected first conversion path:

```text
Windows virtual device audio: 48000 Hz stereo or 44100 Hz stereo
 -> helper captures frames
 -> downmix to mono
 -> resample to 16000 Hz
 -> encode as internal pcm_s16le AudioFrame for HASHI
```

If the Windows device exposes 44.1 kHz instead of 48 kHz, the helper must log that and still resample to the same internal format.

## Risks

- WhatsApp Desktop has no supported call automation API.
- UI layout and buttons can change after app updates.
- WhatsApp Desktop may auto-update silently; the helper should log the detected app version where possible, and UI automation should be revalidated after updates.
- Incoming call UI may differ between minimized, focused, background, and locked-screen states.
- HASHI runs in WSL2. Windows UI automation and Windows audio devices require a Windows-native helper process.
- If the Windows helper exits, WSL-side HASHI voice runtime loses call-control and audio access.
- Windows privacy settings may block microphone/audio capture for the helper.
- Audio device routing may break after Windows updates or device changes.
- Echo and feedback can occur if physical speakers/microphone are left enabled.
- One machine can realistically handle only one WhatsApp Desktop call/account.
- The approach is not suitable for production or multi-user service without major hardening.
- Call recording/transcription may require consent depending on jurisdiction.
- If WhatsApp Desktop logs out or asks for re-linking, automation cannot proceed.

## Future Provider Adapters

The same runtime should later support:

- `FutureTwilioVoiceTransport` for ordinary phone numbers via Twilio Programmable Voice.
- `FutureTwilioWhatsAppTransport` for WhatsApp Business Calling if account eligibility exists.
- `FutureOfficialWhatsAppTransport` if a suitable official call media API becomes available.

These are future transport adapters, not the current main path.

## Archived Initial Development Checklist

This checklist is paused with the feature. Items completed or partially completed during the research phase remain useful evidence for a future revisit.

1. Confirm HASHI1 WhatsApp Desktop can receive manual incoming calls. `partial`: confirmed once via missed-call screenshot evidence.
2. Record the incoming-call UI shape for focused/minimized/hidden WhatsApp Desktop.
3. Extend `tools.windows_helper.server` with UIA-based incoming-call detection. `partial`: helper action and diagnostics exist, but active-call detection was not reliable.
4. Add WSL-side helper client and minimal voice event logger. `done`: Phase 0 helper client and JSONL event logger exist.
5. Log `call.incoming_detected`, detection method, pickup latency, and false-positive checks. `partial`: event structure exists; reliable active-call detection is still blocked.
6. Define `AudioFrame`, `AudioBuffer`, and transport interface.
7. Configure VB-CABLE or VoiceMeeter on HASHI1.
8. Add helper audio device enumeration and `audio.device_selected` logs.
9. Prove audio capture from WhatsApp Desktop with `audio.capture_level`.
10. Prove fixed audio injection into WhatsApp Desktop mic.
11. Add local VAD.
12. Add STT.
13. Add HASHI agent routing.
14. Add TTS.
15. Add `phone_operator` prompt overlay if latency or verbosity hurts the call experience.
16. Run five-turn WhatsApp Desktop call test.

## Open Questions For Future Revisit

- Does WhatsApp Desktop expose answer/hangup controls through UIA reliably, or do we need screenshot fallback for those buttons?
- Which virtual audio tool is already installed or easiest to install on HASHI1?
- Should the first pickup probe be allowed to click automatically, or should it only detect and log until manual approval?
- Which current HASHI agent should the first phone conversation route to?
- Which faster backend/model should be used for `phone_operator`?
- Should phone sessions be transcripted into normal HASHI conversation history, a separate voice log, or both?
