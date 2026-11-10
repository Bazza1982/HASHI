# Voice, Call, Phone, Translate and Presence

## Purpose and when to read
Read for speech input/output, Phone translation, Call interaction, voice routing, playback or Agent Presence integration.

## Ownership and layer
PAO owns admission, Run binding and speech coordination; PCM owns conversational Phone context projection; HERV3 owns its model loop. Frontend Connectors and external Presence own presentation. Compute supplies selected speech and interpretation services through Functions adapters.

## Distinctions
`/voice` controls speech policy; `/call` controls Call interaction; `/phone` binds a live foreground Agent conversation; `/translate` binds independent simultaneous interpretation. HY-SimulMT interpretation sends no Agent Persona, conversation history or task delegation to the speech model. Its model locks translation direction. Conversation handoff applies to providers that support conversation/delegation. Presence presentation, speech generation, subtitles and physical playback are separate facts. Its desktop target comes from the existing primary Conversation, route revision and context generation, not a displayed historical Session or Worker boot Session.

## Authoritative documents
- [Independent Translate entry, preferences and chat boundary](../HASHI_TRANSLATE.md).
- [Phone simultaneous interpretation and Compute transport](../HASHI_PHONE_INTERPRETATION.md).
- [Conversational Phone context handoff](../HASHI_PHONE_CONTEXT_HANDOFF.md).
- [Speech performance and reading](../HASHI_SPEECH_PERFORMANCE.md).
- [Call interaction](../HASHI_CALL_INTERACTION_2026-10-04.md) and [native audio](../HASHI_NATIVE_AUDIO_CHAT_DESIGN.md).
- [TTS catalogue](../HASHI_VOICE_TTS_CATALOG.md).
- [Presence desktop context and authenticated captions](../HASHI_AGENTPRESENCE_DESKTOP_CONTEXT.md).
- [Native caption projection](../HASHI_AGENTPRESENCE_INPUT_FEEDBACK.md#native-captions-2026-11-05).
- [Presence video policy](../HASHI_CALL_AGENTPRESENCE_VIDEO.md) and [owner feedback](../HASHI_AGENTPRESENCE_INPUT_FEEDBACK.md).
- [Compute service selection](../HASHI_COMPUTE_SERVICE_DIRECTORY.md).

## Source and verification
For interpretation, start with `orchestrator/translate_manager.py`, `orchestrator/translate_settings.py`, `orchestrator/frontend_live_voice/simulmt_provider.py`, `orchestrator/phone_catalog.py`, `tests/test_translate_entry.py` and `tests/test_phone_interpretation.py`. For conversational Phone use `orchestrator/phone_context_handoff.py` and `tests/test_phone_context_handoff.py`. Service selection uses `orchestrator/compute_services.py` and `tests/test_compute_services.py`. Speech policy starts at `orchestrator/voice_manager.py`. For Presence desktop targets and captions, start with `orchestrator/frontend_agent_presence.py` and `orchestrator/agent_presence_captions.py`; use `tests/test_agentpresence_desktop_context.py`, `tests/test_agent_presence_captions.py` and `tests/test_agent_presence_caption_completion.py`. Verify cancellation, provenance, privacy routing and uncertain outcomes.

## Adoption
Source compatibility, configured provider readiness, generated audio, playback and physical-client acceptance are separate. HY-SimulMT is experimentally qualified in an isolated canary; it is not a claim of live adoption. The [earlier translation-discovery snapshot](../HASHI_PHONE_TRANSLATION_BOUNDARY.md) is historical and cannot override the current contract.
