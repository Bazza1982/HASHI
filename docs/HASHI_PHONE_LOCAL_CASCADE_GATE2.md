# HASHI Phone optional local cascade: Gate 2 source checkpoint

Status: experimental source checkpoint, originally developed on HASHI2 `feat/phone-local-cascade`. This checkpoint has
offline source and media tests; the running HASHI2 instance and a real Workbench
microphone/speaker call have not adopted or qualified it.

Owner: Frontend Connector Functions and the isolated local speech Worker. The
protected Core and the default OpenAI Phone provider are unchanged.

## Decision and flow

The sidecar now uses cached faster-whisper weights for final utterance recognition
and a locally configured Piper model for speech. Its audio stays in memory. The
existing selected Agent's tool-free Phone inference judges spoken requests and
routes actions through PAO. It also generates a short opening and renders every
foreground answer, including direct answers and saved result pages, into spoken
words using the effective PCM instructions. The Worker
never grants its speech model tools or directly executes a task.

`session.speech.enqueue` carries only foreground words selected by PAO. The
existing `thinking` and `instructions` append channels remain context only;
scheduled results arriving from another conversation use `thinking` and cannot
start playback. Assistant transcript events are emitted after synthesis succeeds.
When the caller starts speaking, the Worker increments the output generation,
clears queued audio and declines late synthesis from the old generation. Each
foreground speech request carries the generation observed before rendering, so
a delayed model answer cannot speak after the caller interrupts it.

The Worker refuses calls when a selected voice has no prepared model. A clean
sidecar starts only after the offline recognition and synthesis models load.
Only `default` is available with the single local Piper model used in the
current HASHI2 experiment. Other catalogue voices need distinct local model
paths before they are usable; no missing voice silently aliases to `default`.

## Isolated setup

The Worker uses its own Python environment. Install
`tools/voice_cascade_requirements.txt` there. Set a private
`CASCADE_WORKER_TOKEN`, `CASCADE_TTS_MODEL` to the local Piper `.onnx` file, and
optionally `CASCADE_STT_MODEL` (default `small`) and `CASCADE_STT_LANGUAGE`
(default automatic). Precache the recognition model: the Worker loads it with
`local_files_only=True` and never downloads during a call. Additional voice
models can use `CASCADE_TTS_MODEL_<VOICE_ID>` environment variables. Keep
credentials and machine paths in instance configuration, never this repository.

The HASHI-side adapter still needs an explicit cascade opt-in and the matching
Worker token. No packages were installed into HASHI's protected interpreter.

## Verification and remaining qualification

The focused tests cover real Piper audio, recognition of those spoken words,
WebRTC transport, sideband transcript, typed foreground speech, background
context isolation, and cancellation of queued generated audio. They also check
that Phone speech inference receives effective PCM instructions with tools
disabled, and that a recognized turn passes through PAO judgment, PCM speech
rendering, synthesis and durable assistant transcript. This proves a source-level
spoken turn through the Worker and Manager.

Production qualification still requires a real Workbench microphone and
speaker call, tuning voice activity on real microphones, durable acknowledged
sideband recovery across Worker crashes, and a decision on the final voice
catalogue and multilingual synthesis quality. The current in-memory replay
limit and 60-second queued output cap are deliberate experimental bounds.
