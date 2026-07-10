from orchestrator.flexible_backend_registry import get_available_models, is_cli_backend
from orchestrator.model_catalog import AVAILABLE_CODEX_MODELS, AVAILABLE_XAI_API_MODELS


def test_codex_spark_model_is_available_to_gateway_catalog():
    assert "gpt-5.3-codex-spark" in AVAILABLE_CODEX_MODELS


def test_codex_spark_model_is_available_to_flex_backend_registry():
    assert "gpt-5.3-codex-spark" in get_available_models("codex-cli")


def test_codex_gpt56_variants_are_available_without_unsupported_alias():
    expected = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
    assert expected.issubset(set(AVAILABLE_CODEX_MODELS))
    assert expected.issubset(set(get_available_models("codex-cli")))
    assert "gpt-5.6" not in AVAILABLE_CODEX_MODELS
    assert "gpt-5.6" not in get_available_models("codex-cli")


def test_grok_build_model_is_available_to_flex_backend_registry():
    assert "grok-composer-2.5-fast" in get_available_models("grok-cli")
    assert "grok-build" in get_available_models("grok-cli")
    assert is_cli_backend("grok-cli") is True


def test_xai_api_models_are_available_to_gateway_catalog():
    assert "grok-4.3" in AVAILABLE_XAI_API_MODELS
    assert "grok-4.3" in get_available_models("xai-api")
