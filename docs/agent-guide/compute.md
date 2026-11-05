# Model capability and Compute services

## Purpose and when to read
Read for model discovery, Engine/Model Provider distinctions, capability/pricing, translation, transcription, TTS, vision service selection and fallback.

## Ownership and layer
PAO selects approved Compute service targets and outer Engine bindings in Functions/instance configuration. HERV3 owns its selected Model Provider loop. Compute is an external capability provider, not a new owner of HASHI Session or configuration state.

## Distinctions
Unknown capability is not unsupported; unknown price is not free. Instance model opt-ins do not redefine the shared compatibility registry. A configured endpoint is not a verified provider or adopted runtime. Failed requests do not authorize silent cross-network fallback. Interpretation direction and capabilities belong to the selected provider/model.

## Authoritative documents
- [Compute service directory](../HASHI_COMPUTE_SERVICE_DIRECTORY.md).
- [Phone interpretation and translation-service transport](../HASHI_PHONE_INTERPRETATION.md).
- [Model availability](../HASHI_MODEL_AVAILABILITY.md).
- [Capability discovery](../HASHI_MODEL_CAPABILITY_DISCOVERY.md).
- [User-controlled fallback](../HASHI_MODEL_FALLBACK_POLICY.md).
- [HERV3 ownership and lifecycle](../HERV3_UPGRADE.md).
- [Ollama capacity and history](../HERV3_OLLAMA_CONTEXT_PILOT.md).

## Source and verification
Start with `orchestrator/compute_services.py`, `orchestrator/compute_vision.py`, `orchestrator/flexible_backend_registry.py` and the selected adapter. Interpretation uses `orchestrator/frontend_live_voice/simulmt_provider.py` and `tests/test_phone_interpretation.py`; also run `tests/test_compute_services.py`. Measure actual encoded requests and native admission, including continuation and reference rereading.

## Adoption
Separate source compatibility, service discovery, privacy admission, native provider results and deployed generation. Do not install optional dependencies into a running Core interpreter. Retired Engine compatibility is documented in [Gemini CLI retirement](../HASHI_GEMINI_CLI_RETIREMENT.md); it does not ban qualified Gemini models.
