from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from adapters.base import BackendResponse, TokenUsage
from adapters.stream_events import (
    DELIVERY_INTERNAL,
    HASHI_PROVIDER_ACTIVITY_SSE_TYPE,
    KIND_PROVIDER_ACTIVITY,
    KIND_TEXT_DELTA,
    KIND_TOOL_END,
    StreamEvent,
)
from orchestrator.api_gateway import (
    API_GATEWAY_MAX_REQUEST_BYTES,
    APIGatewayServer,
    MAX_INLINE_MEDIA_BYTES,
    _AdapterPool,
    _contains_inline_media,
    _validate_inline_image_url,
    _validate_structured_conversation,
    _uses_external_tool_protocol,
    _uses_structured_conversation,
)
from orchestrator.multimodal_contract import (
    HASHI_API_MAX_IMAGE_BYTES,
    HASHI_API_MAX_REQUEST_BYTES,
)
from orchestrator.service_manager import ServiceManager


TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "local_read",
        "description": "Read a local frontend file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

TOOL_CALL = {
    "id": "call-local-1",
    "type": "function",
    "function": {"name": "local_read", "arguments": '{"path":"notes.txt"}'},
}


def test_empty_tools_do_not_force_legacy_clients_into_external_mode():
    messages = [{"role": "user", "content": "hello"}]
    assert _uses_external_tool_protocol({"tools": []}, messages) is False


def test_multipart_messages_use_structured_path_independently_from_tools():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Compare."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        }
    ]

    assert _uses_external_tool_protocol({"tools": []}, messages) is False
    assert _uses_structured_conversation(messages) is True
    assert _contains_inline_media(messages) is True


def test_gateway_body_budget_fits_one_50_mib_image_with_189_mib_headroom(
    tmp_path,
):
    server = _server(tmp_path, _StructuredAdapter())
    encoded_image_bytes = 4 * ((MAX_INLINE_MEDIA_BYTES + 2) // 3)

    assert MAX_INLINE_MEDIA_BYTES == 50 * 1024 * 1024
    assert API_GATEWAY_MAX_REQUEST_BYTES == 256 * 1024 * 1024
    assert MAX_INLINE_MEDIA_BYTES == HASHI_API_MAX_IMAGE_BYTES
    assert API_GATEWAY_MAX_REQUEST_BYTES == HASHI_API_MAX_REQUEST_BYTES
    assert server.app._client_max_size == API_GATEWAY_MAX_REQUEST_BYTES
    assert encoded_image_bytes == pytest.approx(
        66.7 * 1024 * 1024,
        abs=0.1 * 1024 * 1024,
    )
    assert API_GATEWAY_MAX_REQUEST_BYTES - encoded_image_bytes > 189 * 1024 * 1024


def test_gateway_codex_path_allows_50_mib_image_plus_historical_screenshot(
    monkeypatch,
):
    decoded_sizes = iter((50 * 1024 * 1024, 1024 * 1024))

    def validate_without_allocating(_value, *, max_bytes):
        decoded_size = next(decoded_sizes)
        assert max_bytes == 50 * 1024 * 1024
        return "image/png", decoded_size

    monkeypatch.setattr(
        "orchestrator.multimodal_contract.validate_inline_image_data_url",
        validate_without_allocating,
    )
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,current"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,history"},
                },
            ],
        }
    ]

    assert (
        _validate_structured_conversation(
            messages,
            engine="codex-cli",
            model="gpt-5.5",
        )
        is None
    )


def test_gateway_uses_current_multimodal_error_generation(monkeypatch):
    class ReloadedContractError(ValueError):
        code = "MEDIA_LIMIT_EXCEEDED"

    def reject_current_generation(_value, *, max_bytes):
        assert max_bytes == 50 * 1024 * 1024
        raise ReloadedContractError("current-generation limit")

    monkeypatch.setattr(
        "orchestrator.multimodal_contract.MultimodalContractError",
        ReloadedContractError,
    )
    monkeypatch.setattr(
        "orchestrator.multimodal_contract.validate_inline_image_data_url",
        reject_current_generation,
    )

    response, decoded_bytes = _validate_inline_image_url(
        "data:image/png;base64,AAAA",
        param="messages[0].content[0].image_url",
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert decoded_bytes == 0
    assert payload["error"]["code"] == "media_too_large"


class _ExternalAdapter:
    def __init__(
        self,
        *,
        supports: bool = True,
        stream_text: str = "",
        provider_activity: bool = False,
    ):
        self.supports = supports
        self.stream_text = stream_text
        self.provider_activity = provider_activity
        self.calls = []

    def supports_external_tool_passthrough(self, model=None):
        self.capability_model = model
        return self.supports

    async def generate_response(
        self,
        prompt,
        request_id,
        *,
        is_retry=False,
        silent=True,
        on_stream_event=None,
        **request_options,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "request_id": request_id,
                "is_retry": is_retry,
                "silent": silent,
                "request_options": request_options,
            }
        )
        text = self.stream_text or "done"
        if on_stream_event is not None:
            await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary=text))
        return BackendResponse(
            text=text,
            duration_ms=1,
            is_success=True,
            stop_reason="stop",
            usage=TokenUsage(input_tokens=5, output_tokens=1),
        )

    async def generate_external_tool_response(
        self,
        messages,
        tools,
        request_id,
        *,
        tool_choice=None,
        parallel_tool_calls=None,
        use_streaming=False,
        request_options=None,
        on_stream_event=None,
        model=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "request_id": request_id,
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
                "use_streaming": use_streaming,
                "request_options": request_options,
                "model": model,
            }
        )
        if use_streaming and on_stream_event is not None:
            if self.provider_activity:
                await on_stream_event(
                    StreamEvent(
                        kind=KIND_PROVIDER_ACTIVITY,
                        summary="private reasoning must not cross the gateway",
                        delivery_class=DELIVERY_INTERNAL,
                        origin="codex-app-server",
                    )
                )
            if self.stream_text:
                await on_stream_event(
                    StreamEvent(kind=KIND_TEXT_DELTA, summary=self.stream_text)
                )
        return BackendResponse(
            text=self.stream_text,
            duration_ms=1,
            is_success=True,
            tool_calls=[TOOL_CALL],
            stop_reason="tool_calls",
            usage=TokenUsage(input_tokens=12, output_tokens=4),
            tool_call_count=1,
            tool_loop_count=0,
        )


class _StructuredAdapter(_ExternalAdapter):
    def supports_structured_conversation(self, model=None):
        self.structured_capability_model = model
        return self.supports

    async def generate_structured_response(
        self,
        messages,
        request_id,
        *,
        use_streaming=False,
        request_options=None,
        on_stream_event=None,
        model=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "request_id": request_id,
                "use_streaming": use_streaming,
                "request_options": request_options,
                "model": model,
            }
        )
        text = self.stream_text or "I saw both images."
        if use_streaming and on_stream_event is not None:
            await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary=text))
        return BackendResponse(
            text=text,
            duration_ms=1,
            is_success=True,
            stop_reason="stop",
            usage=TokenUsage(input_tokens=18, output_tokens=6),
        )


class _Pool:
    def __init__(self, adapter):
        self.adapter = adapter
        self._adapters = {}
        self.calls = []

    async def get(self, engine, model):
        self.calls.append((engine, model))
        self._adapters[engine] = self.adapter
        return self.adapter

    async def update_model(self, engine, model):
        self.calls.append(("update", engine, model))

    async def shutdown(self):
        return None


class _Request:
    def __init__(self, body):
        self.body = body
        self.headers = {}

    async def json(self):
        return self.body


def _server(tmp_path: Path, adapter: _ExternalAdapter) -> APIGatewayServer:
    config = SimpleNamespace(
        api_gateway_port=18803,
        api_host="127.0.0.1",
        project_root=tmp_path,
    )
    server = APIGatewayServer(config, secrets={}, workspace_root=tmp_path)
    server._engine_status["xai-api"] = {"available": True, "reason": "test"}
    server._engine_status["codex-cli"] = {"available": True, "reason": "test"}
    server._pool = _Pool(adapter)
    return server


@pytest.mark.asyncio
async def test_gateway_accepts_json_body_over_legacy_8_mib_limit(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    legacy_limit = 8 * 1024 * 1024
    body = {
        "model": "gpt-5.5",
        "messages": [
            {
                "role": "user",
                "content": "x" * (legacy_limit + 1),
            }
        ],
    }

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post("/v1/chat/completions", json=body)
        payload = await response.json()

    assert response.status == 200
    assert payload["choices"][0]["message"]["content"] == "done"
    assert adapter.calls[0]["prompt"].endswith("x" * 32)


@pytest.mark.asyncio
@pytest.mark.parametrize("tools_field", [None, []])
async def test_gateway_multimodal_without_external_tools_preserves_all_parts(
    tmp_path, tools_field
):
    adapter = _StructuredAdapter()
    server = _server(tmp_path, adapter)
    content = [
        {"type": "text", "text": "Compare both."},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,iVBORw0KGgo=",
                "detail": "high",
            },
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"},
        },
    ]
    body = {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": content}],
        "reasoning_effort": "high",
    }
    if tools_field is not None:
        body["tools"] = tools_field

    response = await server.handle_chat_completions(_Request(body))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["choices"][0]["message"]["content"] == "I saw both images."
    assert adapter.calls[0]["messages"][0]["content"] == content
    first_image_url = adapter.calls[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert base64.b64decode(first_image_url.partition(",")[2]) == b"\x89PNG\r\n\x1a\n"
    assert adapter.calls[0]["request_options"]["reasoning_effort"] == "high"
    assert adapter.calls[0]["use_streaming"] is False


@pytest.mark.asyncio
async def test_gateway_rejects_inline_image_signature_mismatch_before_adapter_init(
    tmp_path,
):
    adapter = _StructuredAdapter()
    server = _server(tmp_path, adapter)

    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "gpt-5.5",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Inspect this."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,bm90LWEtcG5n"
                                },
                            },
                        ],
                    }
                ],
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "invalid_media"
    assert adapter.calls == []
    assert server._pool.calls == []


@pytest.mark.asyncio
async def test_gateway_multimodal_stream_uses_same_structured_messages(tmp_path):
    adapter = _StructuredAdapter(stream_text="Both images differ.")
    server = _server(tmp_path, adapter)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Compare."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        }
    ]

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": messages, "stream": True},
        )
        body = await response.text()

    assert response.status == 200
    assert "Both images differ." in body
    assert "data: [DONE]" in body
    assert adapter.calls[0]["messages"] == messages
    assert adapter.calls[0]["use_streaming"] is True


@pytest.mark.asyncio
async def test_gateway_structured_stream_preserves_typed_backend_error(tmp_path):
    class _FailedStructuredAdapter(_StructuredAdapter):
        async def generate_structured_response(self, *_args, **_kwargs):
            return BackendResponse(
                text="",
                duration_ms=1,
                error="provider rejected image input",
                is_success=False,
                error_code="PROVIDER_MODALITY_UNSUPPORTED",
                http_status=400,
            )

    server = _server(tmp_path, _FailedStructuredAdapter())
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        }
    ]

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": messages, "stream": True},
        )
        raw = await response.text()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in raw.splitlines()
        if line.startswith("data: {")
    ]
    error = next(event["error"] for event in events if "error" in event)
    assert error["code"] == "PROVIDER_MODALITY_UNSUPPORTED"
    assert error["status"] == 400
    assert "metadata" not in error
    assert raw.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_gateway_structured_stream_preserves_reasoning_activity_marker(
    tmp_path,
):
    class _FailedStructuredAdapter(_StructuredAdapter):
        async def generate_structured_response(self, *_args, **_kwargs):
            return BackendResponse(
                text="",
                duration_ms=1,
                error="provider rejected image input after reasoning",
                is_success=False,
                error_code="PROVIDER_MODALITY_UNSUPPORTED",
                http_status=400,
                stream_metadata={"provider_activity_observed": True},
            )

    server = _server(tmp_path, _FailedStructuredAdapter())
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        }
    ]

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": messages, "stream": True},
        )
        raw = await response.text()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in raw.splitlines()
        if line.startswith("data: {")
    ]
    error = next(event["error"] for event in events if "error" in event)
    assert error["metadata"] == {"provider_activity": True}


@pytest.mark.asyncio
async def test_gateway_external_stream_marks_activity_on_typed_backend_error(
    tmp_path,
):
    class _FailedExternalAdapter(_ExternalAdapter):
        async def generate_external_tool_response(
            self, *_args, on_stream_event=None, **_kwargs
        ):
            if on_stream_event is not None:
                await on_stream_event(
                    StreamEvent(kind=KIND_TEXT_DELTA, summary="partial")
                )
            return BackendResponse(
                text="",
                duration_ms=1,
                error="provider rejected image input",
                is_success=False,
                error_code="PROVIDER_MODALITY_UNSUPPORTED",
                http_status=400,
                tool_call_count=1,
                side_effects_possible=True,
            )

    server = _server(tmp_path, _FailedExternalAdapter())
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "Use the tool."}],
                "tools": [TOOL_SCHEMA],
                "stream": True,
            },
        )
        raw = await response.text()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in raw.splitlines()
        if line.startswith("data: {")
    ]
    error = next(event["error"] for event in events if "error" in event)
    assert error["code"] == "PROVIDER_MODALITY_UNSUPPORTED"
    assert error["metadata"] == {"provider_activity": True}


@pytest.mark.asyncio
async def test_gateway_rejects_media_in_developer_messages_before_adapter_init(
    tmp_path,
):
    adapter = _StructuredAdapter()
    server = _server(tmp_path, adapter)
    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "gpt-5.5",
                "messages": [
                    {
                        "role": "developer",
                        "content": [
                            {"type": "text", "text": "Inspect this policy image."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,iVBORw0KGgo="
                                },
                            },
                        ],
                    },
                    {"role": "user", "content": "Continue."},
                ],
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "unsupported_media_role"
    assert adapter.calls == []
    assert server._pool.calls == []


@pytest.mark.asyncio
async def test_gateway_never_caches_inline_media_in_session_transcript(tmp_path):
    adapter = _StructuredAdapter()
    server = _server(tmp_path, adapter)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        }
    ]

    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "gpt-5.5",
                "messages": messages,
                "session_id": "session-with-inline-image",
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "inline_media_session_unsupported"
    assert server._sessions.get("session-with-inline-image") is None
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_gateway_never_caches_embedded_data_url_text_in_session(tmp_path):
    adapter = _StructuredAdapter()
    server = _server(tmp_path, adapter)

    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "gpt-5.5",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Inspect embedded payload: "
                            "data:image/png;base64,iVBORw0KGgo="
                        ),
                    }
                ],
                "session_id": "session-with-inline-text",
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "inline_media_session_unsupported"
    assert server._sessions.get("session-with-inline-text") is None
    assert adapter.calls == []


def test_gateway_session_cache_rejects_backend_generated_inline_payload(tmp_path):
    server = _server(tmp_path, _StructuredAdapter())

    stored = server._sessions.set(
        "session-generated-inline",
        [
            {
                "role": "assistant",
                "content": "generated data:image/png;base64,iVBORw0KGgo=",
            }
        ],
    )

    assert stored is False
    assert server._sessions.get("session-generated-inline") is None


@pytest.mark.asyncio
async def test_gateway_external_tool_mode_preserves_multipart_user_content(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect, then use the tool."},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,iVBORw0KGgo=",
                        "detail": "original",
                    },
                },
            ],
        }
    ]

    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "gpt-5.5",
                "messages": messages,
                "tools": [TOOL_SCHEMA],
            }
        )
    )

    assert response.status == 200
    assert adapter.calls[0]["messages"] == messages
    assert adapter.calls[0]["tools"] == [TOOL_SCHEMA]


@pytest.mark.asyncio
async def test_gateway_pool_passes_xai_static_and_refresh_credentials(tmp_path, monkeypatch):
    captured = {}

    class _Backend:
        def __init__(self, config, global_config, api_key):
            captured["config"] = config
            captured["global_config"] = global_config
            captured["api_key"] = api_key

        async def initialize(self):
            return True

    global_config = SimpleNamespace(project_root=tmp_path)
    pool = _AdapterPool(
        global_config,
        {
            "xai_api_key": "static-secret",
            "xai_oauth_refresh_token": "refresh-secret",
        },
        tmp_path / "workspaces",
    )
    monkeypatch.setattr("orchestrator.api_gateway.get_backend_class", lambda _engine: _Backend)

    await pool.get("xai-api", "grok-4.3")

    assert captured["api_key"] == {
        "xai_api_key": "static-secret",
        "xai_oauth_refresh_token": "refresh-secret",
    }


@pytest.mark.asyncio
async def test_reboot_min_drains_active_tool_request_before_adapter_shutdown(
    tmp_path, monkeypatch
):
    events = []
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    class _BlockingAdapter(_ExternalAdapter):
        async def generate_external_tool_response(self, *args, **kwargs):
            events.append("request_started")
            request_started.set()
            await release_request.wait()
            response = await super().generate_external_tool_response(*args, **kwargs)
            events.append("request_finished")
            return response

    class _DrainingPool(_Pool):
        async def shutdown(self):
            events.append("adapter_pool_shutdown")

    adapter = _BlockingAdapter()
    server = _server(tmp_path, adapter)
    server._pool = _DrainingPool(adapter)
    kernel = SimpleNamespace(
        paths=SimpleNamespace(
            bridge_home=tmp_path,
            workspaces_root=tmp_path / "workspaces",
        ),
        global_cfg=server.global_config,
        secrets={},
        api_gateway=server,
        enable_api_gateway=True,
    )
    manager = ServiceManager(kernel)
    monkeypatch.setattr(
        manager,
        "_load_api_gateway_state",
        lambda: {"enabled": True, "default_model": "gpt-5.5"},
    )

    async def start_reloaded_gateway(_global_cfg, _secrets):
        events.append("reloaded_gateway_started")
        kernel.api_gateway = SimpleNamespace(bind_host="127.0.0.1")

    monkeypatch.setattr(manager, "start_api_gateway", start_reloaded_gateway)

    body = {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "Use the tool"}],
        "tools": [TOOL_SCHEMA],
    }

    async def wait_until_draining():
        while not server._draining:
            await asyncio.sleep(0)

    async with TestClient(TestServer(server.app)) as client:
        active_request = asyncio.create_task(
            client.post("/v1/chat/completions", json=body)
        )
        await asyncio.wait_for(request_started.wait(), timeout=1)

        reboot_refresh = asyncio.create_task(manager.restart_api_gateway())
        await asyncio.wait_for(wait_until_draining(), timeout=1)

        rejected = await client.post("/v1/chat/completions", json=body)
        rejected_payload = await rejected.json()
        assert rejected.status == 503
        assert rejected.headers["Retry-After"] == "1"
        assert rejected_payload["error"]["code"] == "gateway_draining"
        assert "adapter_pool_shutdown" not in events

        release_request.set()
        completed = await active_request
        assert completed.status == 200
        await reboot_refresh

    assert events == [
        "request_started",
        "request_finished",
        "adapter_pool_shutdown",
        "reloaded_gateway_started",
    ]


@pytest.mark.asyncio
async def test_gateway_does_not_shutdown_adapters_when_transport_drain_fails(
    tmp_path, monkeypatch
):
    events = []
    server = _server(tmp_path, _ExternalAdapter())
    release_cancelled_handler = asyncio.Event()
    handler_started = asyncio.Event()

    class _ObservedPool:
        async def shutdown(self):
            events.append("adapter_pool_shutdown")

    server._pool = _ObservedPool()
    monkeypatch.setattr(
        "orchestrator.api_gateway.API_GATEWAY_DRAIN_TIMEOUT_SEC",
        0.01,
    )
    monkeypatch.setattr(
        "orchestrator.api_gateway.API_GATEWAY_CANCEL_TIMEOUT_SEC",
        0.01,
    )

    loop = asyncio.get_running_loop()
    completion = loop.create_future()

    async def cancellation_resistant_handler():
        handler_started.set()
        try:
            await loop.create_future()
        except asyncio.CancelledError:
            await release_cancelled_handler.wait()
        finally:
            server._active_requests.pop(completion, None)
            if not completion.done():
                completion.set_result(None)

    handler_task = asyncio.create_task(cancellation_resistant_handler())
    server._active_requests[completion] = handler_task
    await handler_started.wait()

    with pytest.raises(TimeoutError, match="could not quiesce 1 active request"):
        await server.stop()

    assert events == []
    release_cancelled_handler.set()
    await handler_task


@pytest.mark.asyncio
async def test_sync_external_tools_preserve_full_protocol_and_return_tool_calls(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    messages = [
        {"role": "user", "content": "Read the note"},
        {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]},
        {"role": "tool", "tool_call_id": "call-local-1", "content": "local result"},
    ]
    body = {
        "model": "grok-4.3",
        "messages": messages,
        "tools": [TOOL_SCHEMA],
        "tool_choice": "required",
        "parallel_tool_calls": False,
    }

    response = await server.handle_chat_completions(_Request(body))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["choices"][0]["message"] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [TOOL_CALL],
    }
    assert payload["choices"][0]["finish_reason"] == "tool_calls"
    assert payload["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }
    assert adapter.calls[0]["messages"] == messages
    assert adapter.calls[0]["tools"] == [TOOL_SCHEMA]
    assert adapter.calls[0]["tool_choice"] == "required"
    assert adapter.calls[0]["parallel_tool_calls"] is False
    assert adapter.calls[0]["use_streaming"] is False
    assert adapter.calls[0]["model"] == "grok-4.3"


@pytest.mark.asyncio
async def test_external_tools_route_codex_models_through_adapter_contract(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "Use the tool"}],
                "tools": [TOOL_SCHEMA],
                "reasoning_effort": "high",
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["choices"][0]["message"]["tool_calls"] == [TOOL_CALL]
    assert payload["choices"][0]["finish_reason"] == "tool_calls"
    assert server._pool.calls[0] == ("codex-cli", "gpt-5.5")
    assert adapter.calls[0]["model"] == "gpt-5.5"
    assert adapter.calls[0]["request_options"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_external_tools_still_reject_unsupported_cli_models_before_init(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "Use the tool"}],
                "tools": [TOOL_SCHEMA],
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "external_tool_passthrough_unsupported"
    assert server._pool.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_patch", "error_code"),
    [
        (
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "invalid.tool.name"},
                    }
                ]
            },
            "invalid_tool_schema",
        ),
        ({"tools": [], "tool_choice": "required"}, "invalid_tool_choice"),
    ],
)
async def test_external_tools_reject_invalid_contract_before_adapter_init(
    tmp_path, body_patch, error_code
):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    body = {
        "model": "gpt-5.6-luna",
        "messages": [{"role": "user", "content": "Use a tool"}],
        **body_patch,
    }

    response = await server.handle_chat_completions(_Request(body))
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == error_code
    assert server._pool.calls == []


@pytest.mark.asyncio
async def test_external_tools_reject_gateway_session_cache(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": "Use the tool"}],
                "tools": [TOOL_SCHEMA],
                "session_id": "unsafe-shared-session",
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "external_tools_session_unsupported"
    assert server._pool.calls == []


@pytest.mark.asyncio
async def test_internal_external_tool_session_reconstructs_incremental_suffix(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    workspace = tmp_path / "agent1"
    workspace.mkdir()
    first_request = _Request(
        {
            "model": "gpt-5.6-luna",
            "messages": [{"role": "user", "content": "Read notes"}],
            "tools": [TOOL_SCHEMA],
            "session_id": "internal-tool-session",
            "hashi_tool_workspace": str(workspace),
        }
    )
    first_request.headers = {"X-Hashi-External-Tool-Session": "v1"}

    first_response = await server.handle_chat_completions(first_request)

    assert first_response.status == 200
    cached = server._sessions.get("internal-tool-session")
    assert cached is not None
    assert [message["role"] for message in cached] == ["user", "assistant"]
    assert cached[-1]["tool_calls"] == [TOOL_CALL]
    assert adapter.calls[-1]["request_options"][
        "_hashi_internal_tool_workspace"
    ] == str(workspace.resolve())
    assert "hashi_tool_workspace" not in adapter.calls[-1]["request_options"]

    second_request = _Request(
        {
            "model": "gpt-5.6-luna",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call-local-1",
                    "content": "notes contents",
                }
            ],
            "tools": [TOOL_SCHEMA],
            "session_id": "internal-tool-session",
            "hashi_tool_workspace": str(workspace),
        }
    )
    second_request.headers = {"X-Hashi-External-Tool-Session": "v1"}

    second_response = await server.handle_chat_completions(second_request)

    assert second_response.status == 200
    reconstructed = adapter.calls[-1]["messages"]
    assert [message["role"] for message in reconstructed] == [
        "user",
        "assistant",
        "tool",
    ]
    assert reconstructed[-1]["content"] == "notes contents"
    assert adapter.calls[-1]["request_options"][
        "_hashi_internal_tool_workspace"
    ] == str(workspace.resolve())


@pytest.mark.asyncio
async def test_internal_external_tool_workspace_cannot_escape_instance_root(
    tmp_path,
):
    instance_root = tmp_path / "workspaces"
    instance_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    adapter = _ExternalAdapter()
    server = _server(instance_root, adapter)
    request = _Request(
        {
            "model": "gpt-5.6-luna",
            "messages": [{"role": "user", "content": "Read notes"}],
            "tools": [TOOL_SCHEMA],
            "session_id": "internal-tool-session",
            "hashi_tool_workspace": str(outside),
        }
    )
    request.headers = {"X-Hashi-External-Tool-Session": "v1"}

    response = await server.handle_chat_completions(request)
    payload = json.loads(response.text)

    assert response.status == 403
    assert payload["error"]["code"] == "external_tool_workspace_forbidden"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_responses_api_model_is_rejected_by_adapter_capability(tmp_path):
    adapter = _ExternalAdapter(supports=False)
    server = _server(tmp_path, adapter)
    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "grok-4.5",
                "messages": [{"role": "user", "content": "Use the tool"}],
                "tools": [TOOL_SCHEMA],
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "external_tool_passthrough_unsupported"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_streaming_external_tools_emit_openai_tool_delta_and_finish_reason(tmp_path):
    adapter = _ExternalAdapter(stream_text="Checking locally. ")
    server = _server(tmp_path, adapter)

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": "Read the note"}],
                "tools": [TOOL_SCHEMA],
                "stream": True,
            },
        )
        raw = await response.text()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in raw.splitlines()
        if line.startswith("data: {")
    ]
    assert response.status == 200
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert any(
        event["choices"][0]["delta"].get("content") == "Checking locally. "
        for event in events
    )
    tool_event = next(
        event for event in events if event["choices"][0]["delta"].get("tool_calls")
    )
    assert tool_event["choices"][0]["delta"]["tool_calls"] == [
        {"index": 0, **TOOL_CALL}
    ]
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert raw.rstrip().endswith("data: [DONE]")
    assert adapter.calls[0]["use_streaming"] is True


@pytest.mark.asyncio
async def test_gateway_streams_content_free_private_provider_activity(tmp_path):
    adapter = _ExternalAdapter(provider_activity=True)
    server = _server(tmp_path, adapter)

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "Keep working"}],
                "tools": [TOOL_SCHEMA],
                "stream": True,
            },
        )
        raw = await response.text()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in raw.splitlines()
        if line.startswith("data: {")
    ]
    activity_event = next(
        event
        for event in events
        if event.get("hashi", {}).get("type")
        == HASHI_PROVIDER_ACTIVITY_SSE_TYPE
    )
    assert response.status == 200
    assert activity_event["hashi"] == {
        "type": HASHI_PROVIDER_ACTIVITY_SSE_TYPE,
        "source": "codex-app-server",
        "activity": "protocol_progress",
    }
    assert activity_event["choices"][0]["delta"] == {}
    assert "private reasoning" not in raw
    assert raw.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_text_stream_persists_correlated_lifecycle_events(tmp_path):
    class _ToolThenTextAdapter(_ExternalAdapter):
        async def generate_response(self, *args, on_stream_event=None, **kwargs):
            if on_stream_event is not None:
                await on_stream_event(
                    StreamEvent(
                        kind=KIND_TOOL_END,
                        summary="completed",
                        tool_name="Bash",
                    )
                )
            return await super().generate_response(
                *args,
                on_stream_event=on_stream_event,
                **kwargs,
            )

    adapter = _ToolThenTextAdapter(stream_text="observable")
    server = _server(tmp_path, adapter)

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "X-Hashi-Correlation-ID": "req-review-1",
                "X-Hashi-Provider-Call": "2",
                "X-Hashi-After-Tool-End": "true",
            },
            json={
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": "observe"}],
                "stream": True,
            },
        )
        await response.text()

    records = [
        json.loads(line)
        for line in server.observability_path.read_text(encoding="utf-8").splitlines()
    ]
    received = next(item for item in records if item["event"] == "request_received")
    assert received["upstream_request_id"] == "req-review-1"
    assert received["provider_call"] == "2"
    assert received["after_tool_end"] is True
    assert any(item["event"] == "backend_invocation_started" for item in records)
    completed = next(
        item for item in records if item["event"] == "backend_invocation_completed"
    )
    assert completed["tool_end_count"] == 1
    assert any(
        item["event"] == "backend_stream_event"
        and item["event_kind"] == "tool_end"
        and item["tool_name"] == "Bash"
        for item in records
    )
    assert any(item["event"] == "stream_terminal_sent" for item in records)
    assert response.headers["X-Hashi-Gateway-Request-ID"].startswith("apireq-")
