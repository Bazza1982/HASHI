# HASHI Native Audio Chat — Product Requirements and Technical Design

## Document control

| Field | Value |
|---|---|
| Status | Implemented and qualified on HASHI1; explicit per-Agent rollout gate retained |
| Decision date | 2026-08-28 |
| Qualification date | 2026-08-28 |
| Scope | Turn-based voice messages with native audio input and output |
| Initial provider route | Configured OpenRouter GPT Audio Mini route |
| Initial delivery surfaces | Telegram, Workbench, and the generic Persistent Session API |
| Architecture rule | Provider-, model-, and terminal-neutral |
| Realtime calls | Explicitly out of scope for this stage |
| Audio-model tools | Represented by the contract but disabled in the proof of concept |

This document defines HASHI-native audio chat. It is separate from the deferred
real-time call work in
[HASHI_VOICE_BRIDGE_PLAN.md](HASHI_VOICE_BRIDGE_PLAN.md). The call plan covers
continuous live audio transports. This design covers bounded voice messages
submitted as one HASHI Turn.

It extends, rather than replaces, the following accepted contracts:

- [HASHI_PERSISTENT_MULTI_SESSION_FRONTEND_DESIGN.md](HASHI_PERSISTENT_MULTI_SESSION_FRONTEND_DESIGN.md)
  for client-neutral Sessions, Messages, Runs, Events, attachments, fencing,
  idempotency, and replay;
- [PROVIDER_AGNOSTIC_MULTIMODAL_INPUT_UPGRADE_TEST_PLAN.md](PROVIDER_AGNOSTIC_MULTIMODAL_INPUT_UPGRADE_TEST_PLAN.md)
  for canonical media parts and exact provider/model/modality routing;
- [HASHI_PCM_SYSTEM_DESIGN.md](HASHI_PCM_SYSTEM_DESIGN.md) for authoritative
  Persona, Context, and Memory assembly; and
- [HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md)
  for Direct, Immediate Response, Triage, work, and delivery semantics.

## 1. Executive decision

HASHI will support native audio-chat models as first-class model targets.
Voice input will no longer be forced through a local
speech-to-text-to-text-model-to-TTS chain before a native audio model can hear
it.

For a voice-origin Turn, HASHI will start independent paths as soon as the
attachment is committed:

~~~text
voice attachment
  |
  +--> native Audio Direct or Audio Immediate model
  |      receives original audio plus HASHI PCM
  |      returns model-authored audio plus output transcript
  |
  +--> local speech transcription
  |      supplies the input transcript for logs, PCM, and text-only stages
  |
  +--> Triage
         receives original audio when its selected model supports audio input
         otherwise receives the local transcript when it becomes available
~~~

Whichever user-facing output becomes ready first is emitted first. HASHI must
not aggregate the whole Turn merely to preserve a single-response shape.

The proof of concept uses the currently configured OpenRouter GPT Audio Mini
route. The observed OpenRouter model slug at design time is
**openai/gpt-audio-mini**, but that value is configuration, not an
architectural constant. Future OpenAI-direct, OpenRouter, or other provider
models must use the same internal contracts.

## 2. Approved product behaviour

The following decisions are fixed for the first implementation:

1. The feature covers turn-based voice messages, not real-time calls.
2. Telegram and Workbench are first-party clients, but no client name appears
   in the core routing rules.
3. Any conforming AI frontend can submit an audio Turn through the generic
   Persistent Session API and consume ordered HASHI Events.
4. Voice-origin Turns use native audio output; text-origin Turns remain text
   replies. A text-origin Turn may use a request-scoped derived TTS asset only
   when an exact stage capability requires audio-bearing input.
5. The default voice-origin reply contains both audio and the associated text
   transcript on every terminal. Each terminal or conversation may override
   that presentation.
6. Effort Zero can use Audio Direct. Low and above can use Audio Immediate in
   parallel with Triage.
7. Audio Direct and Audio Immediate tool calls are disabled in the proof of
   concept. Ordinary Low-and-above work models retain their existing tools.
8. Immediate audio is delivered as soon as it is ready. It is not held for
   Triage, re-rendered, recalled, or edited.
9. Triage may be audio-native or text-only. It never needs audio output.
10. Local input transcription runs independently even when every active stage
    can hear the original audio, because the transcript is required for logs
    and future PCM.
11. Native audio failure falls back to local transcription, an ordinary text
    model, and the existing TTS path. HASHI emits a simple visible warning.
12. Raw audio uses an adjustable retention policy. The default is 60 minutes;
    the allowed range is one minute through indefinite retention.
13. Indefinite retention means the asset is deliberately preserved for later
    download and for future authorized model access.
14. Safe Voice retains its confirm-or-discard design, but it is requested only
    when Triage, fallback, or a tool-capable route is about to consume the
    voice. A no-tool Audio Direct/Immediate chat never opens Safe Voice merely
    because its independent audit transcript finished. No correction editor is
    added.

## 3. Goals

### 3.1 Functional goals

- Accept voice messages from Telegram, Workbench, and any compatible frontend.
- Preserve one canonical input shape from terminal admission through HER.
- Send original audio to an exact audio-capable provider/model when allowed.
- Produce native model audio and its associated transcript as one typed output.
- Deliver Immediate audio without waiting for Triage.
- Allow Triage to choose its own input path independently from the response
  model.
- Preserve current HER work execution after Triage.
- Preserve local STT and TTS as explicit fallback capabilities.
- Insert accepted input transcripts and model output transcripts into HASHI
  conversation context with provenance.
- Extend the existing voice menu so users can control native audio, TTS,
  transcript presentation, fallback, voice, model target, and retention.
- Make all provider and terminal differences configuration or adapter concerns.

### 3.2 Architectural goals

- Keep HASHI PCM authoritative. A terminal never submits Persona, memory,
  system instructions, or prior chat history.
- Keep external clients independent from OpenAI- or OpenRouter-specific wire
  formats.
- Make input and output modality capabilities exact to provider plus model.
- Keep raw media bytes out of transcript, ledger, audit payload, and Message
  JSON.
- Reuse Persistent Session Run identity, idempotency, ordered Events,
  attachment authorization, consumer ACK, and fencing.
- Allow future tool-capable audio models without redesigning the result or
  routing contracts.

## 4. Non-goals

- Real-time calls, WebRTC, SIP, VAD turn-taking, barge-in, or interruption.
- Enabling tools for Audio Direct or Audio Immediate in the proof of concept.
- Disabling tools for ordinary Low-and-above work stages.
- Replacing all existing TTS voices or removing the current TTS manager.
- Building a client-specific endpoint or client-specific PCM path.
- Letting a frontend provide provider payloads, local file paths, prior
  conversation history, Persona, memory, or authoritative Run identity.
- Persisting Base64 audio or raw audio bytes in logs or structured Events.
- Adding transcript editing or correction. Safe Voice remains confirmation or
  discard.
- Treating a model's presence in an available-model list as sufficient proof of
  input, output, format, transcript, streaming, or tool capability.
- Implementing local transcription of model output in the proof of concept.
  The output contract permits it later when a provider does not supply a
  transcript.

## 5. Terminology

**Voice-origin Turn**
: A Turn containing at least one audio part whose semantic role is
  **voice_message**. A generic music or sound attachment does not automatically
  request a spoken reply.

**Native audio route**
: A provider/model invocation that receives original user audio as an audio
  content part, generates model-authored audio, or both.

**Input transcript**
: The local transcription of the user's voice message. It is used for logs,
  PCM, and any selected stage that cannot consume audio natively.

**Output transcript**
: Text associated with model-generated audio. Provider-authored output
  transcript is preferred. A future local output transcription is allowed only
  as a clearly labelled fallback.

**Audio asset**
: An authorized HASHI media object containing input or generated audio. Its
  bytes are stored separately from Messages, Events, transcripts, and audit
  records.

**Terminal**
: A delivery surface such as Telegram, Workbench, desktop, web, mobile, IDE,
  operations console, or another compatible AI frontend.

**Immediate output**
: The no-tool Audio Immediate response generated concurrently with Triage at
  Low effort or above.

## 6. Current baseline and required changes

### 6.1 Reusable foundations

HASHI already has:

- a canonical multimodal request representation that includes audio;
- authorized media references, MIME/signature checks, hashes, and size limits;
- exact provider/model input-capability resolution with fail-closed fallback;
- HER attachment propagation to Immediate Response, Triage, and work stages;
- full HASHI PCM assembly and Persona injection for Direct and Immediate
  response stages;
- concurrent Immediate Response and Triage execution;
- initial-response resolution records;
- a local voice transcriber;
- a TTS voice manager and Telegram voice delivery;
- Persistent Session Messages, Runs, ordered Events, attachment records,
  idempotency, ACK, and fencing; and
- Safe Voice transcript confirmation.

### 6.2 Gaps

The implementation must address these current constraints:

- Telegram voice and audio are locally transcribed before the backend receives
  the Turn.
- Workbench media admission does not consistently construct canonical audio
  request content.
- Persistent Session Run admission requires a non-empty text part and cannot
  accept an audio-only Message.
- the backend capability contract describes input modalities but not typed
  output modalities, audio formats, endpoint families, transcript behaviour, or
  audio streaming.
- the backend response contract treats text as the only successful final
  payload; audio without text is considered an empty success.
- the OpenRouter adapter can materialize audio input parts but does not request
  audio output or parse audio deltas.
- the current voice menu controls post-response TTS only.
- Direct currently inherits the Quick/Fast model slot and cannot select a
  voice-origin-only exact route target.
- Workbench and generic API projections do not expose assistant audio assets.

## 7. Core invariants

1. **HASHI authority:** Session, Agent, PCM, memory, model routing, approvals,
   tools, attachments, and delivery state remain HASHI-owned.
2. **Canonical input:** Terminals submit HASHI content parts, never provider
   wire shapes.
3. **Exact capabilities:** Every native decision uses the selected provider,
   model, API surface, modality, format, limits, and policy.
4. **Original-audio semantics:** Audio Direct and Audio Immediate consume the
   original audio asset, not the local transcript, on the native success path.
5. **Independent transcription:** Local STT does not delay native Immediate
   response.
6. **First-ready delivery:** A user-visible event is emitted as soon as it is
   deliverable. The runtime does not wait for unrelated stage completion.
7. **Immutable audio delivery:** Triage resolution may change internal status
   and the companion text presentation, but not an already delivered audio
   message.
8. **Transcript continuity:** Accepted input transcription and output
   transcription become future PCM conversation context.
9. **No hidden fallback:** Native-to-local fallback produces a visible warning
   and typed audit event.
10. **No audio-model tools in PoC:** Audio model requests omit tool definitions.
    This does not alter the capabilities of other HER stages.
11. **No raw bytes in logs:** Logs persist text, IDs, hashes, size, format,
    duration, provenance, route, and lifecycle state only.
12. **Idempotent delivery:** A retried upload or Run cannot produce a duplicate
    Immediate voice reply.

## 8. Target architecture

~~~text
Terminal adapter or Persistent Session API
  |
  | authenticated Session + committed audio attachment + idempotency key
  v
Canonical Turn admission
  |
  +--> AudioAssetStore
  |      authorized bytes, hash, format, duration, retention lease
  |
  +--> LocalTranscriptTask
  |      transcript + provenance + Safe Voice state
  |
  +--> HER voice-origin routing
         |
         +--> Effort Zero: Audio Direct
         |
         +--> Low and above:
                Audio Immediate ----------------------+
                Triage (native audio or transcript)   |
                Work stages when required             |
                                                     v
Provider-neutral ModelOutput
  content parts: text transcript, audio asset, tool calls, usage, metadata
  |
  +--> Session Message/Event projection
  +--> PCM transcript projection
  +--> terminal-specific delivery
         Telegram: companion text + complete voice asset
         Workbench/generic UI: text + player/asset reference
         stream-capable UI: optional volatile audio deltas
~~~

The architecture adds audio to the existing Session and HER pipeline. It does
not create a second voice-owned memory, routing, or conversation system.

## 9. Generic frontend contract

### 9.1 Relationship to Persistent Session API

The generic entry point is an additive, capability-negotiated extension of the
Persistent Session API. It is not a separate brand-specific API.

A conforming frontend:

1. discovers audio capability versions and limits;
2. creates or selects a HASHI Session;
3. stages, uploads, and commits an audio attachment;
4. creates one idempotent Run containing an audio content part;
5. receives ordered Events as soon as HASHI appends them;
6. renders audio and text according to terminal settings; and
7. ACKs Events using the existing consumer contract.

The existing polling Event transport is sufficient for the base contract.
SSE, WebSocket, webhook, or in-process projections may deliver the same
authoritative Events more quickly, but they must not define different semantic
event types.

### 9.2 Capability publication

When the feature is qualified, capability discovery must publish additive
fields similar to:

~~~json
{
  "message_content_schema_version": "1.1",
  "audio_turn_schema_version": "1.0",
  "media_output_schema_version": "1.0",
  "voice_control_schema_version": "1.0",
  "audio": {
    "input": true,
    "output": true,
    "semantic_roles": ["voice_message", "audio_attachment"],
    "event_delivery": "ordered-at-least-once",
    "volatile_audio_delta": false,
    "retention_min_seconds": 60,
    "retention_default_seconds": 3600,
    "retention_indefinite": true
  }
}
~~~

Capabilities remain absent until the complete server path is implemented and
qualified. Partial implementation must fail closed.

### 9.3 Attachment admission

External clients do not send local paths or provider-ready Base64 fields.

The target attachment lifecycle is:

1. stage metadata including filename, MIME type, size, digest, media kind, and
   semantic role;
2. upload bytes through the capability-advertised transport;
3. validate authentication, ownership, Session, size, digest, MIME signature,
   and permitted format;
4. commit the attachment;
5. reference only the committed attachment ID in Run content.

Direct multipart upload is the required first transport. A future authorized
upload URL may be advertised as an alternative. Arbitrary remote URLs and
caller-supplied host paths are not accepted as trusted media references.

Telegram and Workbench adapters call the same internal attachment admission
service rather than bypassing it.

### 9.4 Run submission

An audio-only user Message becomes valid. The current requirement for a
non-empty text block must be relaxed when at least one committed semantic media
part is present.

Illustrative request:

~~~json
{
  "idempotency_key": "client-stable-key",
  "surface": "generic-ui",
  "message": {
    "content": [
      {
        "type": "audio",
        "attachment_id": "att_opaque",
        "semantic_role": "voice_message",
        "mime_type": "audio/ogg"
      },
      {
        "type": "text",
        "text": "Optional user caption"
      }
    ]
  },
  "response_preferences": {
    "audio_for_voice_input": true,
    "assistant_text": true
  }
}
~~~

The Session owns the Agent. The request does not submit Persona, memory,
history, backend threads, provider payloads, or an authoritative Turn identity.

The idempotency digest covers:

- ordered content parts;
- attachment identity and committed digest;
- semantic roles;
- optional caption;
- execution mode;
- permitted response preferences; and
- parent or supersession identity.

### 9.5 Output event contract

The target durable event family is:

| Event | Purpose |
|---|---|
| **voice.input.transcript_ready** | Local input transcript exists; `ready` means no consumer has requested Safe Voice yet |
| **voice.input.transcript_pending_confirmation** | Safe Voice is waiting for confirm or discard |
| **voice.input.transcript_released** | A deferred transcript was released automatically after no-tool native chat |
| **voice.input.transcript_confirmed** | User confirmation released the transcript to model stages and PCM |
| **voice.input.transcript_discarded** | User declined the transcript-dependent path |
| **assistant.output.available** | A typed Immediate or Final output is ready |
| **assistant.output.resolved** | Immediate output is internally final or an acknowledgement |
| **voice.fallback.started** | Native audio path degraded to the local chain |
| **voice.warning** | User-visible non-terminal warning |
| **audio.asset.expired** | Audio bytes were removed under retention policy |

An **assistant.output.available** payload has typed content:

~~~json
{
  "run_id": "run_opaque",
  "request_id": "req_opaque",
  "phase": "immediate",
  "disposition": "unresolved",
  "content": [
    {
      "type": "text",
      "text": "Provider-authored output transcript",
      "provenance": "provider_audio_transcript"
    },
    {
      "type": "audio",
      "asset_id": "media_opaque",
      "mime_type": "audio/wav",
      "format": "wav",
      "duration_ms": 4200,
      "sha256": "hex",
      "retention_expires_at": "2026-08-28T01:00:00Z"
    }
  ]
}
~~~

The Event never contains Base64 data or raw bytes. Authorized clients retrieve
the asset through the media endpoint while it is retained.

### 9.6 First-ready semantics

The runtime appends each output or warning when it becomes ready. It does not
wait to form one combined response.

Examples:

- Immediate audio may be Event 12, Triage resolution Event 13, and work final
  Event 19.
- A fallback warning may be emitted before its TTS result.
- A native Triage decision may arrive before local STT.
- If authoritative work completes before a still-running optional Immediate
  response, the stale late acknowledgement is cancelled rather than delivered
  after the final answer.

Clients render by Event sequence and deduplicate by Event ID. A client that
temporarily disconnects rebuilds from Session snapshot plus ordered Events.

### 9.7 Optional audio-delta projection

OpenRouter audio output is received as streaming SSE chunks. A future frontend
may negotiate a volatile **assistant.audio.delta** projection for progressive
playback. Raw audio deltas are not durable Session Events.

The first proof of concept does not require progressive playback:

- Telegram requires a complete uploadable audio asset;
- Workbench and generic clients receive **assistant.output.available** as soon
  as the complete validated asset is ready; and
- no terminal waits for Triage before receiving that asset.

## 10. Canonical audio content

### 10.1 Input part

The internal canonical media part extends the existing multimodal contract:

~~~json
{
  "type": "media",
  "item_index": 1,
  "attachment_id": "att_opaque",
  "modality": "audio",
  "kind": "voice",
  "semantic_role": "voice_message",
  "mime_type": "audio/ogg",
  "filename": "voice.ogg",
  "size_bytes": 123456,
  "duration_ms": 8500,
  "sha256": "hex",
  "local_ref": "authorized-internal-reference",
  "transport": {
    "surface": "telegram",
    "source_message_id": "opaque"
  }
}
~~~

Constraints:

- item order and attachment identity remain stable through retries and stages;
- local references resolve only inside authorized media roots;
- transport metadata is evidence of receipt, not evidence that a model heard
  the audio;
- audio bytes are materialized into provider format only at the adapter
  boundary;
- raw bytes and Base64 are not written into canonical JSON; and
- **semantic_role**, not MIME type alone, controls whether the Turn requests a
  spoken reply.

### 10.2 Output content

The backend response becomes a provider-neutral ModelOutput with:

- zero or more text parts;
- zero or more audio asset parts;
- transcript text and provenance;
- output format, MIME type, duration, digest, and retention metadata;
- tool calls and tool-loop metadata;
- usage, cost, stop reason, and provider request identity; and
- typed error metadata.

For compatibility, a legacy text field may remain as a projection of the
canonical assistant text. Success is no longer conditional on non-empty legacy
text. A valid audio part remains deliverable when its transcript is missing,
but the result is explicitly degraded as defined in Section 14.4.

## 11. Capability contract

Native audio cannot be inferred from engine name or model availability alone.
The exact capability record must express:

~~~text
provider
model
api_surface: chat_completions | realtime | provider_specific
input_modalities
input_policy: auto | audio_required
output_modalities
input_formats by modality
output_formats by modality
supported_voices
output_streaming protocol
provider_output_transcript
function_calling
tool_choice
structured_output
context and output limits
audio byte and duration limits
privacy eligibility
capability source and freshness
~~~

Resolution precedence:

1. verified model-specific registry;
2. provider model discovery plus verified adapter rules;
3. schema-validated explicit configuration;
4. optional verified probe; and
5. unknown capability fails closed to local fallback or a typed error.

Input-audio support and output-audio support are independent. Triage requires
input audio, text output, and a response shape that HASHI can validate against
the Triage contract. Direct or Immediate native voice reply requires both
input and output audio.

### 11.1 Text-origin input-modality adaptation

Text remains the authoritative PCM, history, and audit input. HASHI adapts its
transport only when the selected stage cannot accept text, or when the exact
model declaration sets **input_policy=audio_required**. The derived audio is
marked **semantic_role=audio_attachment** and **kind=derived_tts**; it is never
treated as a user voice message and therefore cannot trigger STT, Safe Voice,
or a spoken reply by itself.

When the prompt is a bridge-managed PCM envelope, TTS reads only the typed
**CURRENT USER REQUEST — AUTHORITATIVE** section and stops at the next envelope
section. Persona instructions, retrieved memory, and session context are never
synthesized into the transport asset.

For Effort Zero:

- a text-capable Direct model receives text normally;
- an audio-only or audio-required Direct model receives one derived TTS asset
  alongside the authoritative text context; and
- a model accepting neither text nor audio fails with a typed modality error.

For Low and above:

| Immediate text input | Triage text input | Behaviour |
|---|---|---|
| yes | yes | Send text to both stages concurrently |
| no | yes | Skip optional Immediate and send text directly to Triage |
| yes | no | Send text to Immediate; send one derived TTS asset to Triage |
| no | no | Generate TTS once and share the same derived asset with both stages |

If optional Immediate was skipped and Triage returns **DIRECT_RESPONSE**, HASHI
invokes the configured Direct route after classification. Triage remains the
classification authority; Direct only generates the answer. If Triage selects
work, no deferred audio conversion is performed merely to recreate an optional
Immediate acknowledgement.

Triage must always declare structured text output. Required TTS conversion
failure is terminal with **INPUT_MODALITY_CONVERSION_FAILED**. Optional
Immediate failure continues to authoritative Triage, and ordinary provider
failure continues to use the shared retry/termination policy; no model name or
fallback model is hard-coded by this adaptation.

## 12. Voice-origin routing

### 12.1 Voice route overlay

Voice targets must not replace the ordinary Quick and Pro choices for text
Turns. Introduce a voice-origin route overlay:

~~~text
voice_routes.direct_target
voice_routes.immediate_target
voice_routes.fallback_text_target
voice_routes.triage_input_policy
voice_routes.tools_enabled
~~~

The overlay applies only when the canonical Turn contains a **voice_message**.
Text-origin stage selection remains unchanged; Section 11.1 may adapt only the
input transport required by that already selected stage.

This also removes the present architectural limitation in which Direct can
only inherit the Quick/Fast slot. A voice-origin Direct target may be exact
without changing the Quick model used by text Direct, Triage, Meditation, or
other fast stages.

### 12.2 Effort Zero

For a voice-origin Effort Zero Turn:

1. assemble the normal authoritative PCM;
2. start local input transcription asynchronously;
3. select **voice_routes.direct_target**;
4. verify exact audio input and output capabilities;
5. send PCM instructions plus original user audio;
6. omit tools in the proof of concept;
7. parse native audio and provider output transcript;
8. emit both immediately; and
9. store the local input transcript for future PCM when available.

Safe Voice does not gate this no-tool native chat path because no local
transcript is being released to the model. Future tool-enabled Audio Direct is
a separate activation gate.

### 12.3 Low effort and above

Admission starts these activities without unnecessary ordering:

~~~text
Task A: native Audio Immediate
Task B: local input transcription
Task C: Triage input preparation
~~~

Audio Immediate receives original audio and PCM immediately. It does not wait
for Task B or Task C.

Triage chooses independently:

- if the selected Triage model supports native audio input, text output, and
  the required structured Triage response, it may receive the original audio
  and return text-only structured Triage output;
- otherwise it receives the local transcript through the text-only path; and
- when Safe Voice is enabled, either Triage input path waits for the same
  confirm-or-discard decision before Triage consumes the voice. Audio Immediate
  remains independent and is not delayed.

An audio-capable Triage model must request text output only. It does not need
native audio output and should not pay the latency or cost of generating it.

### 12.4 Immediate resolution

Audio Immediate is expected to finish before Triage in the common path.

When it is ready:

- emit the audio asset and provider transcript immediately;
- do not wait for Triage;
- do not display a commentary emoji on the audio message;
- permit the companion text message to use the existing commentary/final
  presentation; and
- record the output as internally unresolved until Triage decides.

After Triage:

- **DIRECT_RESPONSE:** append a resolution marking the Immediate output final.
  The terminal may update the companion text from commentary to final. The
  audio remains unchanged.
- **work classification:** append a resolution marking the Immediate output an
  acknowledgement. The audio remains unchanged and work continues.
- **confirmation required:** preserve the audio and deliver the ordinary
  confirmation flow separately.

No resolution event causes audio regeneration, recall, deletion, or edit.

### 12.5 Work completion

If Triage starts ordinary work:

- existing Planning, Execution, Replanning, Review, Verification, and
  Finalisation behaviour remains authoritative;
- existing work-model tool capabilities remain unchanged;
- the Immediate audio remains an acknowledgement;
- the final work text is delivered normally; and
- because the Turn originated as voice, the proof of concept also renders the
  verified final text through the existing TTS path.

The default final delivery is therefore text plus TTS audio. A future
configuration may use a separate native final-audio renderer, but it must not
silently paraphrase or weaken an already verified final answer.

## 13. Local transcription and Safe Voice

### 13.1 One transcription, multiple uses

Local STT runs once per input audio asset and produces a typed transcript
record:

~~~json
{
  "attachment_id": "att_opaque",
  "text": "transcribed user speech",
  "engine": "local-stt",
  "language": "optional",
  "confidence": null,
  "created_at": "timestamp",
  "safe_voice_state": "ready | pending_confirmation | released | discarded | unavailable"
}
~~~

The same record supplies:

- input transcript logging;
- future PCM conversation history;
- text-only Triage;
- text-only work stages where applicable; and
- native-audio fallback.

It is not used as the semantic input to a successfully routed Audio Direct or
Audio Immediate call.

### 13.2 Safe Voice off

When Safe Voice is off:

- the transcript is accepted automatically;
- text-only Triage may consume it as soon as STT completes;
- it enters future PCM with local-transcription provenance; and
- the UI need not show a confirmation prompt.

### 13.3 Safe Voice on

Safe Voice governs voice consumption outside the no-tool native chat path.

- Audio Direct and Audio Immediate remain immediate because they consume raw
  audio and have no tools in the proof of concept.
- Finishing local STT alone records a deferred `ready` audit transcript; it
  does not display a confirmation prompt.
- A no-tool Audio Direct completion with no other consumer automatically
  releases that transcript into future PCM without a prompt.
- Any Triage path waits for confirmation before it receives either the local
  transcript or original audio. Triage is a routing/action boundary even when
  its selected model can hear audio natively.
- Fallback and any other transcript-dependent stage also wait.
- A future tool-capable Audio Direct route must wait before the audio model is
  invoked. This proof of concept keeps Audio Direct/Immediate tools disabled.
- HASHI displays the transcript and offers the existing **confirm** or
  **discard** actions.
- Confirm releases the same transcript to transcript-dependent stages and
  future PCM.
- Discard stops that transcript-dependent path.
- There is no transcript edit or correction workflow.

Safe Voice is therefore consumption-triggered, not transcription-triggered.
The no-tool native chat path remains fast while routes that can classify or act
retain the user's explicit boundary.

A terminal advertising Safe Voice must support the confirmation event and
decision control. If Safe Voice is enabled but a terminal cannot present the
challenge, the transcript-dependent path fails closed; HASHI does not silently
bypass confirmation.

### 13.4 Transcript persistence

The original audio Message remains immutable. Input transcription is a derived,
provenance-bearing record associated with that Message and attachment.

- Safe Voice off: the derived record is automatically eligible for PCM.
- Safe Voice on with no consumer after no-tool native chat: the derived record
  is automatically released to PCM without presenting a confirmation.
- Safe Voice on and confirmed: the confirmed record is eligible for PCM.
- Safe Voice on and discarded: it remains only as the minimum audit record
  required by configured policy and is not treated as accepted user text.
- No raw audio bytes are copied into PCM.

## 14. Failure and fallback semantics

### 14.1 Native audio model fails, STT succeeds

1. emit a simple visible native-audio fallback warning;
2. use the already running local STT result;
3. if Safe Voice is on, wait for confirm before releasing the transcript;
4. invoke the configured ordinary text fallback target with the same PCM;
5. deliver its text; and
6. synthesize that text through existing TTS.

No second native-audio attempt is required by the proof of concept.

### 14.2 STT fails, native audio model succeeds

- do not retry local STT;
- do not make an additional audio-model call;
- deliver the already obtained model audio and output transcript;
- emit a simple warning that the input transcript is unavailable; and
- record a provenance-bearing transcript-unavailable marker.

With Safe Voice off, an audio-capable Triage can continue from original audio.
With Safe Voice on, a missing transcript cannot be shown for confirmation, so
Triage ends in a typed degraded state. A text-only Triage also cannot start;
in either case the already obtained Immediate chat response remains
deliverable.

### 14.3 Both native audio and STT fail

Emit one typed terminal failure and one concise user-facing explanation. Do not
pretend the model understood the audio and do not enqueue repeated hidden
attempts.

### 14.4 Provider returns audio without an output transcript

The contract permits a future local transcription of model output. That
capability is not required in the proof of concept.

Until implemented:

- deliver valid audio if terminal policy allows;
- emit a transcript-unavailable warning;
- do not invent assistant text; and
- exclude the missing text from PCM while preserving the typed gap.

### 14.5 Format or delivery failure

- Input format mismatch is normalized once at the provider boundary.
- Provider modality rejection may perform one safe local fallback.
- Telegram output-format mismatch is normalized once at the terminal boundary.
- Delivery retries reuse one logical delivery ID and one audio asset.
- A retry cannot create a second assistant Message or duplicate voice reply.

### 14.6 Text-to-audio input conversion fails

- Do not substitute a hidden fallback model.
- If Triage or Effort Zero requires the conversion, terminate with
  **INPUT_MODALITY_CONVERSION_FAILED**.
- If text-capable Triage can proceed and only optional Immediate would require
  conversion, skip Immediate and avoid TTS entirely.
- Keep the original text authoritative; never persist or replay the derived
  asset as if it were user-authored voice.

## 15. OpenRouter proof-of-concept adapter

### 15.1 Configured target

The initial route uses the currently configured OpenRouter GPT Audio Mini model.
At design time, OpenRouter advertises **openai/gpt-audio-mini** with text and
audio input, text and audio output, and tool parameters. The proof of concept
does not send tools.

The model slug must remain ordinary configuration. Tests use a fixture
capability record and do not teach the runtime that a particular name always
means audio.

### 15.2 Request shape

At the final adapter boundary:

- materialize the canonical audio asset as an **input_audio** content part;
- Base64-encode only for the outbound provider request;
- include **modalities: [text, audio]**;
- include model-specific audio voice and output format;
- set streaming on because OpenRouter audio output requires SSE streaming; and
- preserve PCM as the authoritative system/instruction context.

Illustrative provider payload:

~~~json
{
  "model": "configured-audio-model",
  "messages": [
    {
      "role": "system",
      "content": "HASHI-assembled authoritative PCM instructions"
    },
    {
      "role": "user",
      "content": [
        {
          "type": "input_audio",
          "input_audio": {
            "data": "transient-base64",
            "format": "wav"
          }
        }
      ]
    }
  ],
  "modalities": ["text", "audio"],
  "audio": {
    "voice": "configured-voice",
    "format": "pcm16"
  },
  "stream": true
}
~~~

### 15.3 Response parsing

The adapter must:

- accumulate **delta.audio.data** in order;
- accumulate **delta.audio.transcript** in order;
- continue parsing ordinary text, usage, finish reason, error, and tool deltas;
- validate Base64 and enforce output-size limits;
- materialize one authorized output audio asset;
- attach provider transcript provenance;
- return a typed ModelOutput; and
- delete partial files after failed or cancelled requests.

### 15.4 Format normalization

OpenRouter documents multiple possible input and output formats, but support
varies by upstream provider and model. The adapter must choose from the exact
capability record.

The qualified OpenRouter GPT Audio Mini route requires **pcm16** when audio
output is streamed. HASHI requests that exact provider format, then wraps the
24 kHz mono PCM16 samples in a WAV container at the trusted provider boundary.
Terminals therefore receive a normal **audio/wav** asset without learning a
provider-specific raw-audio contract. Other exact model capability records may
select another provider format without changing Session or terminal code.

Telegram voice notes commonly arrive as OGG/Opus. OpenAI Chat audio examples
use WAV input, and the OpenAI-compatible input contract may be narrower than
the terminal format. HASHI therefore performs transient conversion when
required:

~~~text
terminal format
  -> validate
  -> transient normalize to exact provider-supported format
  -> provider request
~~~

The normalized copy inherits the original asset's retention lease and is not
written to the transcript or audit log.

### 15.5 Future providers

Future adapters can implement the same contract for:

- OpenAI-direct Chat Completions audio models;
- later OpenRouter audio models;
- provider-specific request-based audio endpoints; and
- a later Realtime transport.

Realtime session state, VAD, barge-in, and session memory are not smuggled into
this request-based proof of concept.

## 16. Terminal delivery

### 16.1 Common delivery policy

For a **voice_message** Turn, the default is:

~~~text
assistant audio: on
assistant text transcript: on
~~~

For a text-only Turn:

~~~text
assistant audio: off
assistant text: on
~~~

Presentation can be overridden by global, Agent, terminal, or conversation
settings. Core inference and transcript persistence do not change because a
terminal hides one presentation part.

### 16.2 Telegram

Telegram delivery must:

- send complete native audio as a voice/audio message in a supported format;
- send the associated output transcript as companion text by default;
- avoid a commentary emoji on the audio message;
- use the existing text presentation to mark the companion Immediate
  transcript as commentary, final, or acknowledgement;
- never re-render or edit the already sent audio after Triage;
- send final work text plus TTS audio when work was required; and
- emit fallback and STT warnings as concise text messages.

Provider output may be transcoded to OGG/Opus, MP3, or another Telegram-accepted
format at the terminal boundary. The canonical model output remains unchanged.

### 16.3 Workbench and generic frontends

Workbench and compatible clients receive the same ordered Events and authorized
asset references.

They should:

- render the output transcript immediately;
- render an audio player or download control;
- show internal Immediate resolution without replacing the audio;
- render warning Events separately from assistant content;
- use Event IDs for deduplication; and
- recover expired asset references gracefully while preserving text history.

No frontend needs to know the active provider, provider payload shape, local
media path, PCM packaging, or HER model routing.

## 17. PCM, Persona, context, and memory

### 17.1 Authority

HASHI remains the sole PCM authority.

The native audio model receives:

- permanent system instructions;
- instance and Agent system slots;
- current authoritative request packaging;
- bounded recent Session history;
- Agent memory and Memory+ material selected by HASHI;
- date, time, environment, and authorized catalogues; and
- the Agent Persona loaded from the Agent's authoritative
  **agent.md [persona]** section.

The terminal cannot replace or supplement these authority layers.

### 17.2 Current user audio

The current user Turn reaches the model as:

- the authoritative PCM instructions and context; plus
- an ordered original audio content part; plus
- any user caption or other authorized content parts.

The local input transcript is not substituted into a successful native Audio
Direct or Audio Immediate request.

### 17.3 Conversation history

Future Turns use:

- the accepted local input transcript as the user-text projection;
- the provider output transcript as the assistant-text projection;
- provenance identifying local or provider transcription; and
- typed markers for unavailable transcripts.

Raw audio is not automatically re-sent as conversation history. If retention is
indefinite and a later Turn explicitly authorizes reuse, HASHI may resolve the
stored media ID under the normal capability and authorization rules.

### 17.4 Provider session state

Request-based providers do not become a second memory owner. HASHI sends the
required context for each invocation. A future Realtime adapter must treat its
session as an ephemeral transport cache and synchronize it from HASHI PCM.

## 18. Audio retention, privacy, and audit

### 18.1 Retention policy

The setting applies to input and generated audio assets unless a later
per-direction override is configured.

~~~text
minimum: 1 minute
default: 60 minutes
maximum: indefinite
~~~

Nominal expiry is calculated from asset receipt or creation time. An active
processing or delivery lease prevents deletion while the asset is in use. When
the lease is released after nominal expiry, cleanup occurs promptly.

### 18.2 Indefinite retention

Indefinite retention is an explicit archive choice:

- the asset is not automatically deleted;
- the user may retrieve or download it later;
- a later authorized Turn may reference its media ID;
- normal owner, Session, Agent, privacy, and attachment checks still apply; and
- deletion remains an explicit user-controlled operation.

### 18.3 Durable metadata

Durable records may contain:

- asset and attachment IDs;
- owner, Session, Message, Run, and request correlation;
- hash, size, format, MIME type, duration, and timestamps;
- input or output direction;
- provider/model route and format conversion metadata;
- retention state and expiry;
- transcript text and provenance; and
- delivery and fallback results.

They must not contain raw audio bytes, Base64 audio, secrets, or unauthorized
local paths.

### 18.4 Cleanup

The cleanup worker must be:

- idempotent;
- lease-aware;
- safe across restart;
- able to delete partial and normalized derivatives;
- able to preserve indefinite assets;
- auditable without logging bytes; and
- able to emit one **audio.asset.expired** Event when a referenced asset expires.

### 18.5 Provider privacy

Local retention settings do not control an upstream provider's data policy.
Provider privacy eligibility and configured data-retention policy remain part
of route capability and must be visible in status diagnostics.

## 19. Voice configuration and menu

`/voice` is the shared control surface for native audio and TTS, but its default
menu stays deliberately small. Provider/model vocabulary belongs in advanced
typed commands and diagnostics, not in the everyday picker.

The default inline menu contains two compact groups:

~~~text
[ Auto ]   [ Native ]
[ TTS  ]   [ Off    ]
[ Audio + text ] [ Audio only ]
[ 👩 Warm ] [ 👩 Clear ]
[ 👨 Warm ] [ 👨 Calm  ]
~~~

The four voice choices are semantic profiles, aligned with Aptenra's
`warm_female`, `clear_female`, `warm_male`, and `calm_male` profiles. A single
selection controls both:

- the concrete native Audio model voice; and
- the language-aware Edge TTS voice used by ordinary TTS and the local fallback.

The provider/model capability declaration resolves a semantic profile to a
supported native voice. It must never persist an unsupported voice merely
because one provider happens to use a different raw voice name. Exact raw TTS
aliases and provider voice names remain available through advanced typed
commands for compatibility.

Required settings:

| Setting | Meaning | PoC default |
|---|---|---|
| mode | off, TTS, native, or automatic native-with-fallback | automatic when explicitly enabled |
| reply trigger | voice message or all input | voice message |
| reply content | audio and text, audio only, or text only | audio and text |
| native provider/model | exact Audio Direct/Immediate target | configured OpenRouter GPT Audio Mini |
| native voice | provider/model voice | configured value |
| native format | requested provider output format | capability-selected |
| fallback | native only or local STT-text-TTS | local chain enabled |
| TTS provider/voice/rate | existing final/fallback renderer | existing settings |
| input transcript echo | terminal presentation | off unless Safe Voice requires it |
| output transcript echo | terminal presentation | on |
| retention | one minute through indefinite | 60 minutes |
| audio-model tools | route capability switch | off |

Configuration precedence:

~~~text
global defaults
  -> Agent defaults
      -> terminal override
          -> conversation override
~~~

The most specific valid setting wins. Server policy may narrow, but not expand,
what a terminal requests.

The default menu shows only the effective mode, semantic voice, reply shape,
and the choices above. Detailed provider/model capabilities, formats, fallback,
retention, terminal overrides, Safe Voice, and tool status remain available in
typed commands and status diagnostics. This keeps the common path usable even
on Telegram's compact inline keyboard.

Existing TTS presets and one-shot speech remain backward compatible.
Existing installations do not silently enable native audio merely because an
audio model appears in a provider catalogue.

## 20. Tool-use extension boundary

The internal contracts retain:

- model tool capability;
- tool definitions;
- tool calls;
- tool results;
- tool-loop count;
- side-effect metadata; and
- approval state.

For the proof of concept:

- Audio Direct sends no tools;
- Audio Immediate sends no tools;
- Triage sends no tools as today; and
- ordinary work stages retain their existing tools and approvals.

A future release may enable tool-capable Audio Direct when a selected model is
both fast at speech-to-speech and reliable at tools. Activation must be
route-specific and must define:

- tool eligibility;
- read-only versus side-effect tools;
- approval and confirmation presentation;
- replay and duplicate-side-effect protection;
- spoken acknowledgement versus authoritative tool result;
- Safe Voice interaction; and
- audit evidence.

Safe Voice alone is not approval for a destructive tool. Tool-side approval and
fencing remain HASHI-owned.

## 21. Edge cases

### 21.1 Multiple audio parts

- Preserve original order and distinct attachment IDs.
- Run STT once per voice part.
- A Turn is voice-origin if any part is marked **voice_message**.
- The model receives every supported required part or the stage fails/falls
  back; it must not pretend to hear an omitted part.

### 21.2 Voice plus caption or other media

- Preserve ordered mixed content.
- Resolve each stage's exact capabilities.
- Audio Immediate may run only when it can consume every part required for its
  claimed answer.
- Otherwise it may provide a bounded acknowledgement while work handles the
  unsupported media.

### 21.3 Long or oversized audio

- Enforce advertised terminal, HASHI, provider, and model limits before
  invocation.
- Do not silently truncate speech.
- Use a typed limit error or configured local fallback.
- Chunking is a separate explicitly advertised capability.

### 21.4 Cancellation

- Cancel stops provider streaming and STT where possible.
- Partial output files are deleted.
- Fencing rejects late output.
- Already delivered Immediate audio remains visible and is marked internally
  cancelled/superseded as appropriate.

### 21.5 Duplicate submission

- Attachment digest plus Run idempotency prevents duplicate model calls.
- At-least-once Event delivery may repeat an Event, but never creates another
  assistant output.
- Terminal retries reuse the same delivery ID.

### 21.6 Terminal without audio playback

- Capability negotiation disables audio presentation.
- The output transcript remains available.
- HASHI does not regenerate the answer merely because the terminal is text-only.

### 21.7 Asset expiry before client retrieval

- The Event and transcript remain durable.
- Asset retrieval returns a typed expired result.
- HASHI does not silently regenerate audio.
- Indefinite retention prevents automatic expiry.

## 22. Implementation plan

### Phase 0 — Contract freeze and fixtures

- Approve this design.
- Add input/output/capability schemas and fixtures.
- Freeze Event names, idempotency inputs, retention semantics, and Safe Voice
  boundaries.
- Add failing contract tests before adapter implementation.

Phase 0 test retirement and replacement was completed on 2026-08-28:

- retired the two runtime-media tests that required a failed first local STT
  attempt to enqueue a second **media_read** audio interpretation attempt;
- retained Safe Voice confirm/discard, local media fallback, TTS, **/say**,
  exact per-model media routing, and existing HER first-ready resolution tests,
  because those behaviours remain part of this design; and
- added eight deliberately red, implementation-facing scenarios in
  **tests/contract/test_native_audio_chat_contract.py**. They freeze canonical
  voice roles, exact output capability dimensions, audio-only Session
  admission, audio-only assistant success, STT-independent admission, STT
  failure behaviour, OpenRouter audio round-trip shape, and generic capability
  publication.

The normal fast test gate does not collect **tests/contract**. The new contract
file is run explicitly during Phases 1-4 and must be fully green before the
Phase 5 canary.

### Phase 1 — Canonical audio admission and asset lifecycle

- Unify Telegram, Workbench, and Session API audio admission.
- Permit audio-only Session Messages.
- Implement real attachment byte upload, verification, commit, and authorized
  retrieval.
- Add audio metadata, duration probing, leases, retention, cleanup, and
  indefinite archive.
- Preserve raw-byte exclusion from logs and Events.

### Phase 2 — Typed backend output and OpenRouter audio

- Extend backend capabilities with output audio and endpoint dimensions.
- Extend backend responses to typed ModelOutput.
- Upgrade OpenRouter request construction and SSE audio parsing.
- Add format normalization and partial-file cleanup.
- Add model-specific capability discovery with explicit override and fail-closed
  behaviour.

### Phase 3 — HER routing and transcription

- Add voice-origin Direct and Immediate route targets.
- Start STT concurrently with native audio.
- Route Triage to original audio or transcript according to exact capability.
- Trigger Safe Voice only when Triage, fallback, or an actionable route is
  about to consume the voice; never on STT completion alone.
- Implement Immediate audio resolution without audio mutation.
- Implement warning and local fallback state.

### Phase 4 — Delivery and controls

- Deliver native audio plus transcript to Telegram.
- Add Workbench audio player and generic media retrieval.
- Emit Session audio Events in first-ready order.
- Upgrade the voice menu and retain current TTS controls.
- Deliver final work text plus existing TTS for voice-origin work Turns.

### Phase 5 — Qualification and canary

- Run offline contract and regression tests.
- Run one controlled OpenRouter audio-in/audio-out canary.
- Run Telegram and Workbench end-to-end voice Turns.
- Qualify one generic frontend against capability, attachment, Run, Event,
  replay, ACK, and retention contracts.
- Keep native voice behind an explicit feature/configuration gate until all
  required tests pass.

### Qualification record — 2026-08-28

The proof-of-concept implementation completed qualification on HASHI1:

- the authoritative Python suite completed with **3021 passed, 6 skipped, and
  0 failed**;
- focused native-audio, Session, HER, media, and pipeline coverage completed
  with **315 passed, 1 skipped, and 0 failed**;
- all **18** Workbench server tests passed under their required serial fixture
  isolation, and the Workbench production build completed successfully;
- a live generic Session canary sent original WAV speech to Arale's configured
  OpenRouter **openai/gpt-audio-mini** Audio Direct route and received provider
  audio plus its associated transcript in about four seconds;
- the provider PCM16 stream was converted into one integrity-checked WAV asset,
  published through ordered Events, retrieved through the authorized asset
  endpoint, and not replayed after Event ACK;
- deterministic regression coverage now proves that a local STT result is
  deferred without presenting Safe Voice, then automatically enters the
  canonical user Message after a no-tool native Direct reply; Triage, fallback,
  and future tool-capable audio routes remain confirmation-gated; and
- only Arale was enabled for the canary. HASHI1 remained online and retained
  its original main process throughout Agent-local reloads.

Telegram and Workbench terminal projections are covered by deterministic
integration tests. Qualification did not fabricate a production Telegram
inbound update; a normal user-originated voice message remains the appropriate
live terminal exercise.

The first production Telegram exercise exposed a beta defect in which STT
completion itself opened Safe Voice after a successful no-tool Audio Direct
reply. The consumption-triggered gate above replaces that behaviour. The two
affected completed Arale Turns were reconciled as automatic transcript
releases rather than being recorded as user confirmations.

After the correction, a second live Arale Session canary ran with Safe Voice
still enabled. Run `run_01d239e554d3400bbe250e8db50154ae` completed with a
native WAV reply; its later local STT Event had state `released`, the canonical
user Message received the transcript, and the Event stream contained zero
`voice.input.transcript_pending_confirmation` Events.

## 23. Implementation component map

The likely code ownership boundaries are:

| Component | Required change |
|---|---|
| **orchestrator.multimodal_contract** | Add semantic audio role, duration, exact formats, and output-adjacent helpers |
| **adapters.base** | Add output modalities and typed ModelOutput |
| **adapters.openrouter_api** | Request and parse native audio streaming |
| **orchestrator.runtime_media** | Replace transcript-first Telegram admission with canonical parallel admission |
| **orchestrator.voice_transcriber** | Produce one typed, provenance-bearing transcript task |
| **orchestrator.her_v2 configuration/runtime** | Add voice-origin targets and per-stage audio/transcript input selection |
| **orchestrator.runtime_pipeline** | Accept audio success, persist transcript, and deliver typed outputs |
| **orchestrator.voice_manager** | Separate native voice policy from TTS rendering |
| **orchestrator.workbench_api** | Extend Session capabilities, upload, audio-only Run input, asset retrieval, and Events |
| **orchestrator.session_store** | Store derived transcripts, asset/event metadata, and idempotent correlations |
| **terminal delivery adapters** | Project common output into Telegram, Workbench, or future clients |
| **audio asset service** | Own bytes, normalization derivatives, leases, retention, archive, retrieval, and cleanup |

Module boundaries may be refined during implementation, but responsibilities
must not move into client-specific branches.

## 24. Proof-of-concept acceptance matrix

| ID | Required assertion |
|---|---|
| NAC-001 | A generic client can submit an audio-only Run using a committed attachment |
| NAC-002 | The same idempotency key and digest return the original Run without a second model call |
| NAC-003 | Cross-owner, cross-Session, uncommitted, digest-mismatched, and unauthorized media fail closed |
| NAC-004 | A voice-origin Effort Zero Turn routes original audio to the configured Audio Direct target |
| NAC-005 | A voice-origin Low-or-above Turn starts Audio Immediate and STT concurrently |
| NAC-006 | Audio Immediate receives original audio, not the local transcript, on native success |
| NAC-007 | Native-audio Triage receives original audio and requests text-only output |
| NAC-008 | Text-only Triage receives the local transcript when available |
| NAC-009 | Immediate audio is delivered before slower Triage without waiting for it |
| NAC-010 | Triage resolution changes internal status and companion text presentation but never the audio asset/message |
| NAC-011 | DIRECT_RESPONSE produces no duplicate final audio |
| NAC-012 | A work classification preserves Immediate as acknowledgement and later delivers final text plus TTS |
| NAC-013 | Voice-origin default delivery includes both audio and text on Telegram, Workbench, and generic Event projection |
| NAC-014 | A text-only Turn does not trigger audio reply |
| NAC-015 | Native audio failure emits a warning and falls back through STT, text model, and TTS |
| NAC-016 | STT failure does not retry STT or call the audio model again; successful native audio and output text still deliver |
| NAC-017 | Safe Voice off automatically releases the transcript to text-only Triage and PCM |
| NAC-018 | Safe Voice on is consumption-triggered: no-tool Audio Direct/Immediate never prompts, while any Triage, fallback, or future tool-capable audio route waits for confirm |
| NAC-019 | Safe Voice discard stops the transcript-dependent path and does not offer transcript editing |
| NAC-020 | Provider output audio and transcript are parsed from OpenRouter SSE deltas in order |
| NAC-021 | OGG/Opus input is normalized only when the exact provider target requires another format |
| NAC-022 | Telegram receives a supported voice/audio format and the companion transcript |
| NAC-023 | Audio-only ModelOutput is not classified as empty success |
| NAC-024 | PCM contains Agent Persona and HASHI context while the current user content remains original audio |
| NAC-025 | Future PCM contains accepted local input transcript and provider output transcript with provenance |
| NAC-026 | No raw bytes or Base64 audio appear in Message JSON, Event JSON, audit logs, ledger, or transcript |
| NAC-027 | Default cleanup removes eligible audio after 60 minutes, respects active leases, and preserves indefinite assets |
| NAC-028 | An indefinite asset remains downloadable and may be reused through an authorized media ID |
| NAC-029 | Audio Direct and Audio Immediate requests contain no tools in the proof of concept |
| NAC-030 | Ordinary Low-and-above work models retain their configured tools |
| NAC-031 | Unknown or stale audio capability fails closed rather than sending an invalid provider payload |
| NAC-032 | Provider, model, voice, format, transcript presentation, fallback, and retention can change without terminal-specific code |
| NAC-033 | Event replay and ACK do not duplicate assistant audio delivery |
| NAC-034 | Cancellation removes partial files and fencing rejects late output |
| NAC-035 | An explicit audio-only capability does not implicitly gain text input |
| NAC-036 | Effort Zero converts authoritative current-request text once when Direct is audio-only or audio-required |
| NAC-037 | Low+ covers all four Immediate/Triage text-input combinations and shares one derived TTS asset where required |
| NAC-038 | Skipped Immediate plus DIRECT_RESPONSE invokes the configured Direct route after Triage |
| NAC-039 | Triage without structured text output and required TTS conversion failure both fail with typed errors |

## 25. Rollout, compatibility, and rollback

- Native audio is feature-gated and default-off for existing installations.
- Existing TTS-only voice settings continue to work.
- Existing text, image, document, video, Safe Voice, HER, and Session paths
  remain backward compatible.
- A provider model is selectable for native voice only after exact capability
  validation.
- If the audio capability gate is absent or disabled, the current local
  STT-text-TTS path remains available.
- Persistent Session capability publication remains fail-closed until the
  complete audio contract is qualified.
- Rollback changes executable/configuration selection, not canonical Session
  state, transcript records, or retained user assets.
- Partial audio assets created by a rolled-back attempt are cleaned by the
  lease-aware asset service.

## 26. Deferred extensions

The following are conceptually supported by this architecture but deferred:

- OpenAI-direct audio-chat adapters;
- local fallback transcription of provider output audio;
- progressive playback in stream-capable frontends;
- native final-audio rendering for completed work;
- tool-enabled Audio Direct;
- audio-aware approval UX for side-effect tools;
- live Realtime, WebRTC, SIP, barge-in, and continuous sessions; and
- optional transcript correction workflows.

Each extension must preserve HASHI authority, exact capability routing,
idempotency, fencing, transcript provenance, and terminal neutrality.

## 27. External protocol evidence

The design was checked on 2026-08-28 against the following current official
documentation:

- [OpenAI Audio and speech](https://developers.openai.com/api/docs/guides/audio.md)
  describes audio input, audio output, transcripts, request-based chat audio,
  and Realtime as distinct architectures.
- [OpenAI GPT-Audio-1.5](https://developers.openai.com/api/docs/models/gpt-audio-1.5.md)
  documents text/audio input, text/audio output, Chat Completions support,
  streaming, and function calling.
- [OpenAI Realtime and audio](https://developers.openai.com/api/docs/guides/realtime.md)
  supports the decision to keep live sessions separate from bounded
  request-based voice messages.
- [OpenRouter Audio](https://openrouter.ai/docs/guides/overview/multimodal/audio.md)
  documents Base64 **input_audio**, provider-dependent formats,
  **modalities**, audio options, required SSE output streaming, and
  **delta.audio.data/transcript**.
- [OpenRouter model discovery](https://openrouter.ai/api/v1/models?output_modalities=audio)
  exposes model-specific input/output modalities and supported parameters.
- [Telegram Bot API sendVoice](https://core.telegram.org/bots/api#sendvoice)
  defines the terminal upload boundary for Telegram voice messages.

## 28. Final approval boundary

No unresolved product-design question blocks the proof of concept.

Implementation is approved only within the scope and invariants of this
document. Live calls and audio-model tools remain separate future activation
decisions.
