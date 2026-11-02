# Automatic performance for advanced TTS

PAO owns this capability in replaceable Functions and per-Agent instance settings. Core, Engine conversation instructions and native audio-model conversations are unchanged.

`/voice` offers automatic performance ON/OFF; `/voice performance on|off` is its text equivalent. The preference is persistent and defaults to OFF. It is independent of the selected voice, reply switch and voice preset. Prerecorded previews remain prerecorded; actual text reading is needed to hear a directed performance.

VoiceManager owns the boolean in the existing revision-aware voice configuration. A private OpenAI-compatible service can prepare through its Compute `/v1/speech/performance` endpoint. Explicitly unsupported services continue ordinary reading with a notice. A cloud TTS endpoint is never used for the local planning call. The optional catalogue field `automatic_performance:false` disables that service's preparation; no new instance field is required to enable an existing private Compute service.

Compute owns model selection, the short open-ended direction prompt, input sampling, resource admission and validation. A failed plan is carried as an unapplied context so later chunks cannot silently retry judgment. Cancellation propagates. Plans are transient and are never saved with voice settings.

## Logical preparation protocol

The existing authenticated `/api/tui/speech` operation accepts `operation:"prepare"`, whole reading text and its request ID. It returns `performance_context` without generating audio or an owner-mirror notification. The caller carries this context in subsequent bounded synthesis requests; both shared Functions and the Agent Worker forward it through the existing typed RPC envelope.

Workbench reads authoritative voice preferences once before a logical reading. If automatic performance is enabled, it prepares once before splitting playback batches and provider requests. Direct HASHI TTS replies prepare once in VoiceManager. Turning the preference off prevents old contexts from applying; changing providers invalidates a prepared context rather than misrouting it. Explicit manual instructions retain precedence.

Audio metadata reports requested/applied status, reason, plan correlation ID and preparation time; it excludes the instructions themselves. A failed or unsupported judgment displays a plain-reading notice. A successful prompt is experimental model direction, not an acoustic or subjective-quality guarantee.

## Adoption and validation

Implementation and offline checks do not hot-adopt a Function generation. MSI adoption must verify both the shared service and Agent Workers after the user-directed hot `/reboot max`; do not cold restart Core. Compute and Workbench use their own immutable deployment paths.

Validation covers default OFF, persistence and command/button controls, one plan across chunking, no stale ON-to-OFF state, failed preparation, cancellation, unchanged text/voice payloads, native audio delivery and existing Function handoff checks. Live checks and source/runtime identities belong in dated receipts; they are separate from this protocol.
