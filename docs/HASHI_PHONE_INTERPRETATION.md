# Phone simultaneous interpretation

Status: experimental implementation, qualified in an isolated real-model canary.
No live instance or external frontend installation has been switched by this work.

## Ownership

PAO Functions retain the logical call, selection, persistence, epochs, controls
and recovery. The Frontend Function provider adapts the Compute service. Model
execution, audio queues and CPU speech belong to the independent Compute sidecar.
Protected Core and the browser's microphone/playback implementation are unchanged.

## Selection and behavior

The existing Phone provider/model menus expose HY-SimulMT and two directions:
`hy-simulmt-en-zh` and `hy-simulmt-zh-en`. Selecting the language also changes
the direction; unsupported languages and conversational speaking styles are
rejected. The first implementation has one fixed CPU voice per target language.
The selected model owns the target language in saved-state reads and frozen call
snapshots. A stale language field cannot reverse the displayed or effective
direction; read-time projection does not rewrite the configuration file.

Interpretation sends no Agent Persona, conversation history, task results or tool
delegations to the speech model. Source speech is interpreted continuously.
Opening speech and background result narration are disabled through truthful
provider capabilities. Existing conversation providers retain their behavior.

The target-only checkpoint produces translated subtitles. It does not produce
source-language ASR; source subtitles are not fabricated. One native model
context admits one call at a time. The two directions were tested separately.

## Transport

The existing browser contract remains WebRTC audio plus `oai-events`:
`session.started`, `session.output_transcript.delta`, and the existing control
and close acknowledgments. The provider identifies itself as `hy-simulmt`;
its media descriptor selects the compatible `openai-live-v1` wire codec.
This does not claim to implement GPT-Live's conversational or delegation model.

The configured Compute service advertises `hashi.phone.webrtc` and
`hashi.phone.sideband`. The adapter resolves an exact configured
node/service/model through the existing Compute directory. It freezes the
resolved sideband endpoint for that provider session; browser fields cannot
choose a provider URL. Credential headers come from the canonical Compute client.

The sidecar separately exposes the OpenAI translation WebSocket event subset:
`session.created`, `session.update`, `session.input_audio_buffer.append`,
`session.output_transcript.delta`, `session.output_audio.delta`, and
`session.close` / `session.closed`. PCM is mono signed 16-bit at 24 kHz.
The service resamples for the model and WebRTC transport.

## Instance configuration

Configure these variables in the shared Frontend Function host environment:
`HASHI_SIMULMT_COMPUTE_CONFIG`, `HASHI_SIMULMT_NODE`, and
`HASHI_SIMULMT_SERVICE`. They select an existing versioned
`compute_services.json` file and exact node/service IDs, without introducing
another configuration writer. The provider is unavailable without this selection.

The node uses the deployed Compute origin and the formal discovery path
`/v1/translation/manifest`; health path is `/health`. It can be local or LAN.
No cloud fallback is selected. Choose `/phone provider hy-simulmt`, then the
direction with `/phone model hy-simulmt-en-zh` or
`/phone model hy-simulmt-zh-en`.

Activation uses the existing qualified Functions lifecycle at its actual shared
owner scope. A committed source generation is required. Neither a source edit
nor a successful isolated canary proves adoption by a running instance.

## Verification

Focused tests cover interpreter projection, direction persistence, unsupported
capabilities and delegation rejection, in addition to existing Phone, opening,
call-manager and Compute-directory regressions. A real canary used the complete
Phone manager, Compute HTTP/WebSocket gateway, WebRTC peers, HY-SimulMT Q4_K_M,
and Windows CPU speech.

Bidirectional short calls produced audio while input was still arriving. A
126.32-second paced source completed without growing translation backlog.
Mute/unmute, close confirmation and immediate direction switching were tested.
The close-capacity regression has a recorded red/green test: confirmation must
follow model-context release, and an old media close cannot release a new lease.

These tests use synthetic microphone input. Physical microphone/speaker use,
multiple simultaneous calls, a 30-minute call and speaker cloning are outside
this qualification. Exact receipts, waveforms, source hashes and deployment notes
belong to the instance evidence bundle, not this source repository.
