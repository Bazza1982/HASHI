from types import SimpleNamespace

from adapters.claw_cli import ClawCLIAdapter
from adapters.codex_cli import CodexCLIAdapter
from adapters.deepseek_api import DeepSeekAdapter
from adapters.ollama_api import OllamaAdapter
from adapters.openrouter_api import OpenRouterAdapter


def _agent_config(tmp_path, *, extra=None):
    return SimpleNamespace(
        name="test",
        model="test-model",
        workspace_dir=tmp_path,
        system_md=None,
        extra=extra or {},
        resolve_access_root=lambda: tmp_path,
    )


def test_openai_compatible_backends_advertise_answer_stream(tmp_path):
    cfg = _agent_config(tmp_path)

    openrouter = OpenRouterAdapter(cfg, SimpleNamespace(), api_key="test-key")
    deepseek = DeepSeekAdapter(cfg, SimpleNamespace(), api_key="test-key")
    ollama = OllamaAdapter(cfg, SimpleNamespace(), api_key=None)

    assert getattr(openrouter.capabilities, "supports_answer_stream", False) is True
    assert getattr(deepseek.capabilities, "supports_answer_stream", False) is True
    assert getattr(ollama.capabilities, "supports_answer_stream", False) is True


def test_cli_backends_do_not_advertise_answer_stream_by_default(tmp_path):
    cfg = _agent_config(tmp_path)

    codex_capabilities = CodexCLIAdapter._define_capabilities(
        CodexCLIAdapter.__new__(CodexCLIAdapter)
    )
    claw = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")

    assert getattr(codex_capabilities, "supports_answer_stream", False) is False
    assert getattr(claw.capabilities, "supports_answer_stream", False) is False
