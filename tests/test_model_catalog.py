from orchestrator.flexible_backend_registry import get_available_models, is_cli_backend
from orchestrator.model_catalog import AVAILABLE_CODEX_MODELS


def test_codex_spark_model_is_available_to_gateway_catalog():
    assert "gpt-5.3-codex-spark" in AVAILABLE_CODEX_MODELS


def test_codex_spark_model_is_available_to_flex_backend_registry():
    assert "gpt-5.3-codex-spark" in get_available_models("codex-cli")


def test_grok_build_model_is_available_to_flex_backend_registry():
    assert "grok-build-0.1" in get_available_models("grok-cli")
    assert is_cli_backend("grok-cli") is True
