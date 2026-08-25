from orchestrator.flexible_backend_registry import (
    get_all_gateway_models,
    get_available_efforts,
    get_available_models,
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


def test_codex_spark_model_is_available_to_gateway_catalog():
    assert "gpt-5.3-codex-spark" in AVAILABLE_CODEX_MODELS


def test_codex_spark_model_is_available_to_flex_backend_registry():
    assert "gpt-5.3-codex-spark" in get_available_models("codex-cli")


def test_codex_gpt56_variants_are_available_in_gateway_catalog():
    expected = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
    assert expected.issubset(set(AVAILABLE_CODEX_MODELS))
    assert "gpt-5.6" not in AVAILABLE_CODEX_MODELS


def test_codex_gpt56_variants_are_available_in_flex_backend_registry():
    models = get_available_models("codex-cli")
    assert "gpt-5.6-sol" in models
    assert "gpt-5.6-terra" in models
    assert "gpt-5.6-luna" in models
    assert "gpt-5.5" in models
    assert "gpt-5.6" not in models


def test_codex_gateway_models_expose_live_probed_reasoning_efforts():
    expected = ["none", "low", "medium", "high", "xhigh", "max"]
    assert get_available_efforts("codex-cli", "gpt-5.6-sol") == expected
    assert get_available_efforts("codex-cli", "gpt-5.6-luna") == expected
    assert get_available_efforts("codex-cli", "gpt-5.6-terra") == ["low", "medium", "high", "xhigh"]
    assert normalize_effort("codex-cli", "none", "gpt-5.6-luna") == "none"
    assert normalize_effort("codex-cli", "max", "gpt-5.6-luna") == "max"
    assert normalize_effort("codex-cli", "max", "gpt-5.6-sol") == "max"
    assert normalize_effort("codex-cli", "max", "gpt-5.6-terra") == "medium"


def test_hashi_api_declares_reasoning_efforts_for_both_gateway_models():
    expected = ["none", "low", "medium", "high", "xhigh", "max"]
    for model in ("gpt-5.6-luna", "gpt-5.6-sol"):
        assert get_available_efforts("hashi-api", model) == expected
        assert normalize_effort("hashi-api", None, model) == "medium"
        assert normalize_effort("hashi-api", "high", model) == "high"
        assert normalize_effort("hashi-api", "max", model) == "max"


def test_current_grok_cli_models_are_available_to_flex_backend_registry():
    assert "grok-4.5" in get_available_models("grok-cli")
    assert "grok-composer-2.5-fast" in get_available_models("grok-cli")
    assert "grok-build" not in get_available_models("grok-cli")
    assert is_cli_backend("grok-cli") is True


def test_grok_cli_exposes_reasoning_effort_with_medium_default():
    expected = ["low", "medium", "high"]
    assert get_available_efforts("grok-cli", "grok-4.5") == expected
    assert normalize_effort("grok-cli", None, "grok-4.5") == "medium"
    assert normalize_effort("grok-cli", "high", "grok-4.5") == "high"
    assert normalize_effort("grok-cli", "xhigh", "grok-4.5") == "medium"


def test_retired_her_id_exposes_only_v2_orchestration_efforts():
    expected = ["zero", "low", "medium", "high", "xhigh", "max"]
    assert get_available_efforts("her", "deepseek/deepseek-v4-pro") == expected
    assert normalize_effort("her", None, "deepseek/deepseek-v4-pro") == "medium"
    assert normalize_effort("her", "zero", "deepseek/deepseek-v4-pro") == "zero"
    assert normalize_effort("her", "max", "deepseek/deepseek-v4-pro") == "max"
    assert normalize_effort("her", "max+", "deepseek/deepseek-v4-pro") == "medium"
    assert normalize_effort("her", "ultra", "deepseek/deepseek-v4-pro") == "medium"


def test_xai_api_models_are_available_to_gateway_catalog():
    assert "grok-4.3" in AVAILABLE_XAI_API_MODELS
    assert "grok-4.3" in get_available_models("xai-api")


def test_current_deepseek_models_replace_retired_and_experimental_ids():
    direct_models = get_available_models("deepseek-api")
    openrouter_models = get_available_models("openrouter-api")

    assert direct_models == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert "deepseek-chat" not in direct_models
    assert "deepseek-reasoner" not in direct_models
    assert "deepseek/deepseek-v4-pro" in openrouter_models
    assert "deepseek/deepseek-v4-flash" in openrouter_models
    assert "deepseek/deepseek-v3.2-exp" not in openrouter_models


def test_compatibility_catalog_is_derived_from_backend_registry():
    assert available_gateway_models() == get_all_gateway_models()
    assert default_gateway_model() == "gpt-5.4"
    assert "anthropic/claude-sonnet-4.6" in AVAILABLE_OPENROUTER_MODELS
    assert "grok-imagine-video-1.5-preview" in AVAILABLE_XAI_API_MODELS
