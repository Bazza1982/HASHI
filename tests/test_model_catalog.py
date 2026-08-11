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


def test_codex_gpt56_sol_exposes_max_effort_without_offering_it_to_other_variants():
    assert get_available_efforts("codex-cli", "gpt-5.6-sol") == ["low", "medium", "high", "xhigh", "max"]
    assert get_available_efforts("codex-cli", "gpt-5.6-terra") == ["low", "medium", "high", "xhigh"]
    assert get_available_efforts("codex-cli", "gpt-5.6-luna") == ["low", "medium", "high", "xhigh"]
    assert normalize_effort("codex-cli", "max", "gpt-5.6-sol") == "max"
    assert normalize_effort("codex-cli", "max", "gpt-5.6-terra") == "medium"


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


def test_her_exposes_agentic_execution_effort_with_high_default():
    expected = ["low", "medium", "high", "xhigh", "max", "max+"]
    assert get_available_efforts("her", "deepseek/deepseek-v4-pro") == expected
    assert normalize_effort("her", None, "deepseek/deepseek-v4-pro") == "high"
    assert normalize_effort("her", "max", "deepseek/deepseek-v4-pro") == "max"
    assert normalize_effort("her", "max+", "deepseek/deepseek-v4-pro") == "max+"


def test_xai_api_models_are_available_to_gateway_catalog():
    assert "grok-4.3" in AVAILABLE_XAI_API_MODELS
    assert "grok-4.3" in get_available_models("xai-api")


def test_compatibility_catalog_is_derived_from_backend_registry():
    assert available_gateway_models() == get_all_gateway_models()
    assert default_gateway_model() == "gpt-5.4"
    assert "anthropic/claude-sonnet-4.6" in AVAILABLE_OPENROUTER_MODELS
    assert "grok-imagine-video-1.5-preview" in AVAILABLE_XAI_API_MODELS
