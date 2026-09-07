import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.openai_compatible_api import (
    OpenAICompatibleAdapter,
    normalize_chat_completions_url,
)
from adapters.registry import get_backend_class
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.flexible_backend_registry import (
    PROVIDER_ONLY_ENGINE_IDS,
    allows_custom_models,
)


def _adapter(tmp_path, **extra):
    cfg = SimpleNamespace(
        name="portable",
        engine="openai-compatible-api",
        model="qwen-plus",
        workspace_dir=tmp_path,
        system_md=None,
        extra={
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            **extra,
        },
    )
    return OpenAICompatibleAdapter(cfg, SimpleNamespace(), api_key="qwen-key")


def test_openai_compatible_base_url_resolves_chat_endpoint():
    assert normalize_chat_completions_url(
        "https://dashscope.aliyuncs.com/compatible-mode/v1/"
    ) == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    assert normalize_chat_completions_url(
        "https://api.example.cn/v1/chat/completions"
    ) == "https://api.example.cn/v1/chat/completions"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://provider.example/v1",
        "https://user:password@provider.example/v1",
        "https://provider.example/v1?token=secret",
        "https://provider.example/v1#fragment",
    ],
)
def test_openai_compatible_rejects_unsafe_or_invalid_endpoint(url):
    with pytest.raises(ValueError):
        normalize_chat_completions_url(url)


def test_openai_compatible_payload_uses_provider_options_without_openrouter_fields(
    tmp_path,
):
    adapter = _adapter(
        tmp_path,
        provider_reasoning="high",
        request_options={
            "enable_thinking": True,
            "model": "must-not-override",
            "messages": ["must-not-override"],
        },
        headers={
            "X-Provider-Feature": "portable",
            "Authorization": "must-not-override",
        },
    )

    payload = adapter._build_payload([{"role": "user", "content": "hello"}])
    headers = adapter._request_headers()

    assert payload["model"] == "qwen-plus"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert "reasoning" not in payload
    assert payload["enable_thinking"] is True
    assert headers["Authorization"] == "Bearer qwen-key"
    assert headers["X-Provider-Feature"] == "portable"
    assert "HTTP-Referer" not in headers
    assert "X-Title" not in headers


@pytest.mark.asyncio
async def test_openai_compatible_calls_configured_endpoint(tmp_path):
    adapter = _adapter(tmp_path)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }

    adapter.client = SimpleNamespace(post=AsyncMock(return_value=Response()))

    result = await adapter._call_api_once(
        {"model": "qwen-plus", "messages": []},
        adapter._request_headers(),
        None,
    )

    assert result.text == "ok"
    assert adapter.client.post.await_args.args[0] == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )


def test_openai_compatible_is_provider_only_and_custom_model_capable():
    assert "openai-compatible-api" in PROVIDER_ONLY_ENGINE_IDS
    assert allows_custom_models("openai-compatible-api") is True
    assert get_backend_class("openai-compatible-api") is OpenAICompatibleAdapter


def test_configured_provider_secret_name_is_resolved():
    manager = object.__new__(FlexibleBackendManager)
    manager.config = SimpleNamespace(name="portable")
    manager.secrets = {
        "dashscope_api_key": "configured-key",
        "openai-compatible-api_key": "fallback-key",
    }
    manager.logger = logging.getLogger("test.openai-compatible")

    assert manager._resolve_api_key(
        "openai-compatible-api",
        {"api_key_secret": "dashscope_api_key"},
    ) == "configured-key"
