"""Credit lookup keeps HTTP causes and never queries another Provider's key."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.deepseek_api import DeepSeekAdapter
from adapters.hashi_api import HashiApiAdapter
from adapters.ollama_api import OllamaAdapter
from adapters.openai_compatible_api import OpenAICompatibleAdapter
from adapters.xai_api import XaiApiAdapter
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from tests.test_openrouter_reasoning import _adapter


def command_runtime(backend):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.backend_manager = SimpleNamespace(current_backend=backend)
    runtime._is_authorized_user = lambda actor: actor == 1
    runtime._reply_text = AsyncMock()
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [
    (401, "authentication_rejected"), (403, "authentication_rejected"),
    (429, "rate_limited"), (502, "service_unavailable"), (404, "http_rejected"),
])
async def test_credit_command_preserves_safe_http_cause(tmp_path, status, reason):
    backend = _adapter(tmp_path)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "private-body token=private-secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend.client = client
        runtime = command_runtime(backend)
        await runtime.cmd_credit(SimpleNamespace(effective_user=SimpleNamespace(id=1)), SimpleNamespace())
    message = runtime._reply_text.await_args.args[1]
    assert "credit_request_" + reason in message and "operr_" in message
    assert "private-" not in message and backend.api_key not in message
    assert len(calls) == 1 and calls[0].method == "GET"
    assert calls[0].url.host == "openrouter.ai"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reason", [
    ("timeout", "timed_out"), ("connect", "connection_failed"),
    ("json", "response_invalid"), ("shape", "response_invalid"),
])
async def test_credit_transport_and_response_failures_are_distinct(tmp_path, kind, reason):
    backend = _adapter(tmp_path)

    def respond(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("private-timeout-url", request=request)
        if kind == "connect":
            raise httpx.ConnectError("private-connect-url", request=request)
        if kind == "json":
            return httpx.Response(200, text="private-non-json")
        return httpx.Response(200, json={"data": ["private-wrong-shape"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend.client = client
        runtime = command_runtime(backend)
        await runtime.cmd_credit(SimpleNamespace(effective_user=SimpleNamespace(id=1)), SimpleNamespace())
    message = runtime._reply_text.await_args.args[1]
    phase = "decode" if kind in {"json", "shape"} else "request"
    assert "credit_" + phase + "_" + reason in message and "operr_" in message
    assert "private-" not in message and backend.api_key not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type", [
    DeepSeekAdapter, HashiApiAdapter, OllamaAdapter, OpenAICompatibleAdapter, XaiApiAdapter,
])
async def test_other_provider_key_is_never_sent_to_openrouter(adapter_type):
    backend = adapter_type.__new__(adapter_type)
    backend.api_key = "private-other-provider-key"
    backend.config = SimpleNamespace(engine="another-provider")
    backend.logger = logging.getLogger("credit-boundary-fixture")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"label": "fixture"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend.client = client
        assert await backend.get_key_info() is None
        # The command must also use the same provider boundary.
        runtime = command_runtime(backend)
        await runtime.cmd_credit(SimpleNamespace(effective_user=SimpleNamespace(id=1)), SimpleNamespace())
    assert calls == []
    assert "only for OpenRouter" in runtime._reply_text.await_args.args[1]


@pytest.mark.asyncio
async def test_credit_success_remains_read_only_and_escapes_the_key_label(tmp_path):
    backend = _adapter(tmp_path)

    def respond(request):
        return httpx.Response(200, json={"data": {
            "label": "<fixture>", "usage": 1.0, "limit": 5.0, "limit_remaining": 4.0,
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend.client = client
        runtime = command_runtime(backend)
        await runtime.cmd_credit(SimpleNamespace(effective_user=SimpleNamespace(id=1)), SimpleNamespace())
        assert await backend.get_key_info() == {"data": {
            "label": "<fixture>", "usage": 1.0, "limit": 5.0, "limit_remaining": 4.0,
        }}
    message = runtime._reply_text.await_args.args[1]
    assert "&lt;fixture&gt;" in message and "4.0" in message


@pytest.mark.asyncio
async def test_credit_redirect_cannot_forward_credentials_to_another_host(tmp_path):
    backend = _adapter(tmp_path)
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        backend.client = client
        result = await backend.get_key_info_result()
    assert result.issue.error_code == "credit_request_http_rejected"
    assert len(calls) == 1 and calls[0].url.host == "openrouter.ai"


@pytest.mark.asyncio
async def test_missing_credit_credentials_do_not_start_a_request(tmp_path):
    backend = _adapter(tmp_path)
    backend.api_key = ""
    backend._ensure_client = lambda: pytest.fail("Missing credentials must not start a request")
    result = await backend.get_key_info_result()
    assert result.issue.error_code == "credit_configuration_authentication_rejected"


@pytest.mark.asyncio
async def test_credit_cancellation_remains_cancellation(tmp_path):
    backend = _adapter(tmp_path)

    async def cancelled(request):
        raise asyncio.CancelledError()

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancelled)) as client:
        backend.client = client
        runtime = command_runtime(backend)
        with pytest.raises(asyncio.CancelledError):
            await runtime.cmd_credit(SimpleNamespace(effective_user=SimpleNamespace(id=1)), SimpleNamespace())
    runtime._reply_text.assert_not_awaited()
