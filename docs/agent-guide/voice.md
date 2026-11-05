# Voice, Call, Phone and Presence

## Purpose and when to read
Read for speech input/output, Phone translation, Call interaction, voice routing, playback or Agent Presence integration.

## Ownership and layer
PAO owns admission, Run binding and speech coordination; PCM owns Phone context projection; HERV3 owns its model loop. Frontend Connectors and external Presence own presentation. Compute supplies advertised speech services through Functions adapters.

## Distinctions
`/voice` controls speech policy; `/call` controls Call interaction; `/phone` binds a live foreground conversation. Presence presentation is distinct from speech generation. Audio provenance, authorized transcription and native audio differ. A preview or synthesized file does not prove playback or an accepted live call.

## Authoritative documents
- [Speech performance and reading](../HASHI_SPEECH_PERFORMANCE.md).
- [Phone contract and context handoff](../HASHI_PHONE_CONTEXT_HANDOFF.md).
- [Call interaction](../HASHI_CALL_INTERACTION_2026-10-04.md).
- [Native audio](../HASHI_NATIVE_AUDIO_CHAT_DESIGN.md).
- [TTS catalogue](../HASHI_VOICE_TTS_CATALOG.md).
- [Presence video policy](../HASHI_CALL_AGENTPRESENCE_VIDEO.md) and [owner feedback](../HASHI_AGENTPRESENCE_INPUT_FEEDBACK.md).
- [Compute speech service contract](../HASHI_COMPUTE_SERVICE_DIRECTORY.md).
- [Phone translation capability boundary](../HASHI_PHONE_TRANSLATION_BOUNDARY.md): this generation has no separate translation service contract.

## Source and verification
For Phone, start with `orchestrator/phone_manager.py`, `orchestrator/phone_context_handoff.py` and `tests/test_phone_context_handoff.py`. Follow the Phone contract to the translation boundary before selecting a Compute service; inspect `orchestrator/compute_services.py` and `tests/test_compute_services.py`. For speech policy use `orchestrator/voice_manager.py` and its focused tests. Verify cancellation, provenance, privacy routing and uncertain outcomes.

## Adoption
Configuration, provider readiness, generated audio, playback, listening and real-device acceptance are separate. A synthetic or local cascade test does not qualify a physical call. Keep historical repair and portable-packaging observations separate from the current runtime contract.
