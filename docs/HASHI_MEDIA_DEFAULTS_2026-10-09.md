# Default media availability and complete installation

Owner: Frontend Connector. Layer: Functions, deployment and instance configuration.
User approved repair/adoption in HASHI3 and verified synchronization to HASHI4
on October 9. HASHI4 adoption is reserved to the user. No Core migration.

Recording, native-audio compatibility, Phone, Call/video and speech replies are
available by default. Availability does not start a microphone or camera;
capture still follows the user's action and browser permission. A saved explicit
off choice, privacy policy, authenticated routing and qualified model boundaries
remain binding. Automatic speech replies default OFF: availability is not
consent to narrate every reply. Only a saved TTS/native choice enables automatic
speech. Missing, empty or unreadable voice state never enables narration and
reading fallback state never persists it. When TTS is selected, the default
engine remains platform TTS; switching to native model audio still requires a
compatible declared target.

On Windows the platform engine is System.Speech/SAPI, not Edge. Without an
explicit voice it reuses the shared English/Chinese/Japanese text-language hint
to select an installed matching voice. An explicitly selected voice is retained;
missing voices/languages and empty synthesized WAVs fail instead of producing
wrong-voice or 0:00 attachments. See the
[October 10 correction](repairs/VOICE_OPT_IN_REPAIR_20261010.md).

An explicit instance/Agent preference takes priority over that platform fallback.
The user's October 10 HASHI4 correction restores its existing Agents to Edge
Xiaoxiao/Xiaoyi, with previously missing narration choices saved OFF. The isolated
Edge helper explicitly decodes its UTF-8 pipe, independent of Windows code page
or ignored Python environment variables. See
[Edge restoration and adoption boundary](repairs/EDGE_VOICE_RESTORATION_20261010.md).

An absent call declaration is initialized by its Functions owner from the
originally approved OpenRouter targets: `openai/whisper-large-v3`,
`google/gemini-3.8-flash-lite-tts` and `google/gemini-3.8-flash`. All use
`https://openrouter.ai/api/v1` and a server-side OpenRouter credential reference.
The default voice is Achernar. Example declarations are generated from
`frontend_call.defaults`, which owns this default catalogue. Existing declarations
and per-owner selections are retained. A bad
declaration fails the media surface, not ordinary chat. Other supported media
providers remain selectable through their instance declaration; optional
provider selection is distinct from disabling a feature.

Call synthesis and automatic playback belong to the active Call profile and
floating call window. A sealed Call input suppresses ordinary platform/native
voice attachments; its main Agent still supplies the text answer. OpenRouter
failure is an error, never permission to select another provider or model.
The unauthorized direct-OpenAI defaults and instance declarations introduced
earlier were revoked by the user on October 9; the correction and its separate
live evidence are recorded in [Call restoration](call/OPENROUTER_RESTORATION_2026-10-09.md).

Complete installations prepare isolated STT and TTS interpreters, default STT
weights and a real converter. Native Windows/WSL and npm perform preparation
and checks before reporting full media success; npm failures return nonzero.
Portable Windows/macOS bundle separate interpreters and model weights, with
launch paths derived from the current image so relocation does not retain
builder paths. Container preparation stays outside its Core environment.
Provider API keys and device permissions remain user configuration. Explicit
partial-install options must be described as partial, never full readiness.

Synchronization includes source, package files, dependency generation, model
readiness and enabled instance/frontend declarations. It retains instance
identity, endpoints and secret values. A source copy alone is not feature
delivery. Running adoption and live acceptance are separate evidence.

Validation: focused default/off persistence scenarios, real call/recording
boundaries, npm/full-install failure behavior, sidecar probes, curated Core
gate and immutable Worker lifecycle tests. Live evidence belongs in
`docs/repairs/MEDIA_DEFAULTS_REPAIR_20261009.md`; no simulated check is recorded
as human listening or physical-phone acceptance.

HASHI API's stateless adapter exports the qualified isolation/tool-disabling
capabilities of its shared transport owner. Phone auxiliary judgments use the
selected target without tools, reasoning or inherited conversation; no alternate
model is silently selected. Missing this declaration previously rejected Phone
before any model request. Its regression exercises the real adapter HTTP payload,
isolated prompt, usage receipt and connection cleanup.
