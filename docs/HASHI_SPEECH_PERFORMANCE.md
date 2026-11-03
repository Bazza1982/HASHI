# Automatic performance for advanced TTS

PAO owns this capability in replaceable Functions and per-Agent instance settings. Core, Engine conversation instructions and native audio-model conversations are unchanged.

`/voice` offers automatic performance ON/OFF; `/voice performance on|off` is its text equivalent. The preference is persistent and defaults to OFF. It is independent of the selected voice, reply switch and voice preset. Prerecorded previews remain prerecorded; actual text reading is needed to hear a directed performance.

VoiceManager owns the boolean in the existing revision-aware voice configuration. A private OpenAI-compatible service can prepare through its Compute `/v1/speech/performance` endpoint. Explicitly unsupported services continue ordinary reading with a notice. A cloud TTS endpoint is never used for the local planning call. The optional catalogue field `automatic_performance:false` disables that service's preparation; no new instance field is required to enable an existing private Compute service.

Compute owns model selection, the short open-ended direction prompt, input sampling, resource admission and validation. A failed plan is carried as an unapplied context so later chunks cannot silently retry judgment. Cancellation propagates. Plans are transient and are never saved with voice settings.

## Logical preparation protocol

The existing authenticated `/api/tui/speech` operation accepts `operation:"prepare"`, whole reading text and its request ID. It returns `performance_context` without generating audio or an owner-mirror notification. The caller carries this context in subsequent bounded synthesis requests; both shared Functions and the Agent Worker forward it through the existing typed RPC envelope.

Workbench reads authoritative voice preferences once before a logical reading. When speech-reading-selection-v1 is negotiated, it prepares once before splitting playback batches and provider requests, even with performance off. Direct HASHI TTS replies prepare once in VoiceManager. A prepared logical reading freezes its effective routed voice, rate, provider options, catalogue and direction in bounded server-side state. Settings changes apply to the next reading; all segments of the current reading retain that snapshot. An expired/lost reference requires a new reading, while completed task results remain recoverable. Legacy unbound contexts are invalidated by provider/direction changes. Explicit manual instructions retain precedence.

Audio metadata reports requested/applied status, reason, plan correlation ID and preparation time; it excludes the instructions themselves. A failed or unsupported judgment displays a plain-reading notice. A successful prompt is experimental model direction, not an acoustic or subjective-quality guarantee.

## Adoption and validation

Implementation and offline checks do not hot-adopt a Function generation. MSI adoption must verify both the shared service and Agent Workers after the user-directed hot `/reboot max`; do not cold restart Core. Compute and Workbench use their own immutable deployment paths.

Validation covers default OFF, persistence and command/button controls, one plan across chunking, no stale ON-to-OFF state, failed preparation, cancellation, unchanged text/voice payloads, native audio delivery and existing Function handoff checks. Live checks and source/runtime identities belong in dated receipts; they are separate from this protocol.

## Manual direction
The user approved independent manual controls in both /voice and /call.
VoiceManager owns /voice's revisioned manual_direction; CallConfig owns /call's
per-owner/Agent profile, frozen at call creation. Each stores enabled and a
trimmed instruction string of at most 500 Unicode characters. Enabling an empty
value is rejected. Saving valid text enables manual direction. Turning it off
retains the text and restores that entrance's prior behavior.

Manual direction precedes automatic planning. HASHI creates a bounded prepared
plan locally and sends it through the existing validated performance_plan API,
which selects expressive synthesis without any Qwen/9B invocation. It does not
change the spoken text or selected voice. Voice segments reuse the prepared
context; call segments reuse their frozen profile. An unsupported provider
rejects manual synthesis rather than silently invoking a planner. A changed
legacy voice/manual context requires a new reading. Call applies its frozen direction after each utterance resolves its effective language/voice; language switching never overwrites it or changes the selected preset.

Workbench renders a bounded textarea attached only to the server-issued manual
save action. The value travels through the existing authenticated command
interaction with its revision, connection and Session fences; the browser never
submits callback data. Empty/oversized input is rejected before consuming the
action. Ordinary callbacks cannot accept text. Telegram retains the explicit
/voice manual <on|off|TEXT> and /call manual <on|off|TEXT> command forms.
The recording stores the actual manual source and direction separately from the
body, so replay needs neither planning nor synthesis.


## Confirmed failure and retry
A new attempt carries retry_of referencing a confirmed failed task with the
same phase, text digest and selection. Each failure has at most one successor;
replaying that successor is idempotent. A later failed successor can itself be
retried. Active, cancelled, expired and uncertain outcomes cannot authorize a
new attempt. Workbench retains completed subsegments until the playback part
is saved, and only retries the failed remainder. Lost acknowledgements use
status retrieval under the same identity.
