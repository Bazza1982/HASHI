from orchestrator.flexible_backend_registry import (
    get_all_gateway_models,
    get_available_efforts,
    get_available_models,
    get_provider_reasoning_efforts,
    is_cli_backend,
    normalize_effort,
)
from orchestrator.model_catalog import (
    AVAILABLE_CODEX_MODELS,
    AVAILABLE_OPENROUTER_MODELS,
    AVAILABLE_XAI_API_MODELS,
    available_gateway_models,
    default_gateway_model,
)


def test_codex_catalog_exposes_only_the_supported_hashi_models():
    expected = [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-6-astra",
    ]
    assert AVAILABLE_CODEX_MODELS == expected
    assert get_available_models("codex-cli") == expected


def test_codex_gateway_models_expose_live_probed_reasoning_efforts():
    expected = ["none", "low", "medium", "high", "xhigh", "max"]
    assert get_available_efforts("codex-cli", "gpt-5.6-sol") == expected
    assert get_available_efforts("codex-cli", "gpt-5.6-luna") == expected
    assert get_available_efforts("codex-cli", "gpt-5.6-terra") == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert normalize_effort("codex-cli", "none", "gpt-5.6-luna") == "none"
    assert normalize_effort("codex-cli", "max", "gpt-5.6-luna") == "max"
    assert normalize_effort("codex-cli", "max", "gpt-5.6-sol") == "max"
    assert normalize_effort("codex-cli", "max", "gpt-5.6-terra") == "medium"
    assert get_available_efforts("codex-cli", "gpt-6-astra") == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_hashi_api_declares_reasoning_efforts_for_both_gateway_models():
    expected = ["none", "low", "medium", "high", "xhigh", "max"]
    for model in ("gpt-5.6-luna", "gpt-5.6-sol"):
        assert get_available_efforts("hashi-api", model) == expected
        assert get_provider_reasoning_efforts("hashi-api", model) == expected
        assert normalize_effort("hashi-api", None, model) == "medium"
        assert normalize_effort("hashi-api", "high", model) == "high"
        assert normalize_effort("hashi-api", "max", model) == "max"


def test_current_grok_cli_models_are_available_to_flex_backend_registry():
    assert "grok-4.5" in get_available_models("grok-cli")
    assert "grok-composer-2.5-fast" in get_available_models("grok-cli")
    assert "grok-build" not in get_available_models("grok-cli")
    assert is_cli_backend("grok-cli") is True


def test_her_v2_is_a_stateless_runtime_not_a_cli_session_backend():
    assert is_cli_backend("her-v2") is False
    assert is_cli_backend("her") is False


def test_grok_cli_exposes_reasoning_effort_with_medium_default():
    expected = ["low", "medium", "high"]
    assert get_available_efforts("grok-cli", "grok-4.5") == expected
    assert normalize_effort("grok-cli", None, "grok-4.5") == "medium"
    assert normalize_effort("grok-cli", "high", "grok-4.5") == "high"
    assert normalize_effort("grok-cli", "xhigh", "grok-4.5") == "medium"


def test_retired_her_id_exposes_only_three_public_v2_execution_modes():
    expected = ["zero", "low", "medium"]
    assert get_available_efforts("her", "deepseek/deepseek-v4-pro") == expected
    assert normalize_effort("her", None, "deepseek/deepseek-v4-pro") == "medium"
    assert normalize_effort("her", "zero", "deepseek/deepseek-v4-pro") == "zero"
    assert normalize_effort("her", "direct", "deepseek/deepseek-v4-pro") == "zero"
    assert normalize_effort("her", "strategic", "deepseek/deepseek-v4-pro") == "low"
    assert normalize_effort("her", "fast", "deepseek/deepseek-v4-pro") == "low"
    assert normalize_effort("her", "planned", "deepseek/deepseek-v4-pro") == "medium"
    assert normalize_effort("her", "max", "deepseek/deepseek-v4-pro") == "medium"
    assert normalize_effort("her", "max+", "deepseek/deepseek-v4-pro") == "medium"
    assert normalize_effort("her", "ultra", "deepseek/deepseek-v4-pro") == "medium"


def test_xai_api_models_are_available_to_gateway_catalog():
    assert "grok-4.3" in AVAILABLE_XAI_API_MODELS
    assert "grok-4.3" in get_available_models("xai-api")


def test_provider_catalogs_expose_only_the_hashi1_supported_models():
    direct_models = get_available_models("deepseek-api")
    openrouter_models = get_available_models("openrouter-api")

    assert direct_models == [
        "deepseek-flash",
        "deepseek-v4-pro",
    ]
    assert openrouter_models == [
        "deepseek/deepseek-v3.2-exp",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "google/gemini-3.8-flash",
    ]


def test_deepseek_exposes_only_distinct_provider_reasoning_states():
    for model in (
        "deepseek-flash",
        "deepseek-v4-pro",
    ):
        assert get_available_efforts("deepseek-api", model) == []
        assert get_provider_reasoning_efforts("deepseek-api", model) == [
            "off",
            "high",
            "max",
        ]
        assert normalize_effort("deepseek-api", None, model) is None


def test_compatibility_catalog_is_derived_from_backend_registry():
    assert available_gateway_models() == get_all_gateway_models()
    assert default_gateway_model() == "gpt-5.6-sol"
    assert AVAILABLE_OPENROUTER_MODELS == [
        "deepseek/deepseek-v3.2-exp",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "google/gemini-3.8-flash",
    ]
    assert "grok-imagine-video-1.5-preview" in AVAILABLE_XAI_API_MODELS
