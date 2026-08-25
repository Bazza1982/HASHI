import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.hashi_api import HashiApiAdapter
from adapters.openrouter_api import _APIResult
from adapters.registry import get_backend_class
from orchestrator.flexible_backend_registry import (
    get_available_efforts,
    get_available_models,
)
from orchestrator.multimodal_contract import canonical_request_content


class _MediaFallbackRegistry:
    def is_allowed(self, name):
        return name == "media_read"

    def get_tool_definitions(self, tiers=None):
        del tiers
        return [{"type": "function", "function": {"name": "media_read"}}]

    async def execute(self, *_args, **_kwargs):
        raise AssertionError("the mocked fallback response should not call a tool")


def _adapter(
    tmp_path,
    *,
    base_url="http://gateway.invalid/v1",
    model="gpt-5.6-luna",
    effort=None,
    provider_reasoning=None,
):
    extra = {}
    if effort is not None:
        extra["effort"] = effort
    if provider_reasoning is not None:
        extra["provider_reasoning"] = provider_reasoning
    config = SimpleNamespace(
        name="arale",
        model=model,
        workspace_dir=tmp_path,
        system_md=None,
        extra=extra,
    )
    global_config = SimpleNamespace(
        her_providers={
            "providers": {
                "hashi": {
                    "engine": "hashi-api",
                    "base_url": base_url,
                    "status": "provisional",
                }
            }
        }
    )
    adapter = HashiApiAdapter(config, global_config)
    adapter.tool_registry = None
    return adapter


def test_hashi_api_is_registered_with_concrete_models():
    assert get_backend_class("hashi-api") is HashiApiAdapter
    assert get_available_models("hashi-api") == [
        "gpt-5.6-luna",
        "gpt-5.6-sol",
    ]
    assert get_available_efforts("hashi-api", "gpt-5.6-luna") == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


@pytest.mark.parametrize(
    ("model", "configured", "expected"),
    [
        ("gpt-5.6-luna", "high", "high"),
        ("gpt-5.6-sol", "max", "max"),
        ("gpt-5.6-luna", "off", "none"),
    ],
)
def test_hashi_api_sends_gateway_reasoning_effort_not_openrouter_reasoning(
    tmp_path, model, configured, expected
):
    adapter = _adapter(
        tmp_path,
        model=model,
        effort="low",
        provider_reasoning=configured,
    )

    payload = adapter._build_payload([{"role": "user", "content": "hello"}])

    assert payload["reasoning_effort"] == expected
    assert "reasoning" not in payload


def test_hashi_api_rejects_unknown_reasoning_effort_before_http(tmp_path):
    adapter = _adapter(tmp_path, provider_reasoning="ultra")

    with pytest.raises(ValueError, match="HASHI reasoning effort"):
        adapter._build_payload([{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_hashi_api_initializes_without_a_provider_secret(tmp_path):
    adapter = _adapter(tmp_path, base_url="http://127.0.0.1:18801/v1/")

    assert await adapter.initialize() is True
    assert adapter.hashi_url == "http://127.0.0.1:18801/v1/chat/completions"
    assert adapter._hashi_headers() == {
        "Content-Type": "application/json",
        "X-Hashi-After-Tool-End": "false",
    }

    await adapter.shutdown()


@pytest.mark.asyncio
async def test_hashi_api_reports_usage_and_never_adds_openrouter_headers(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult(
            text="done",
            tool_calls=None,
            finish_reason="stop",
            prompt_tokens=120,
            completion_tokens=30,
            thinking_tokens=10,
            cost_usd=0.0025,
            structured_data={"result": "ok"},
        )
    )

    response = await adapter.generate_response("hello", "request-1")

    assert response.is_success is True
    assert response.text == "done"
    assert response.structured_data == {"result": "ok"}
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 30
    assert response.usage.thinking_tokens == 10
    assert response.cost_usd == pytest.approx(0.0025)


@pytest.mark.asyncio
async def test_hashi_api_preserves_multipart_messages_and_reasoning_effort(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nhashi-api")
    payload = image.read_bytes()
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Describe it."},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "attachment-1",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": image.name,
                "caption": "",
                "local_ref": str(image),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "transport": {},
            },
        ]
    )
    adapter = _adapter(tmp_path, provider_reasoning="high")
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult("done", None, "stop", 12, 3)
    )

    response = await adapter.generate_response(
        "Describe it.", "request-multimodal", request_content=content
    )

    assert response.is_success is True
    request_payload = adapter._call_api_once.call_args.args[0]
    assert request_payload["reasoning_effort"] == "high"
    assert "reasoning" not in request_payload
    assert [
        part["type"] for part in request_payload["messages"][1]["content"]
    ] == ["text", "image_url"]
    image_url = request_payload["messages"][1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(image_url.partition(",")[2]) == payload
    _payload, headers, _callback = adapter._call_api_once.await_args.args
    assert headers == {
        "Content-Type": "application/json",
        "X-Hashi-Correlation-ID": "request-multimodal",
        "X-Hashi-Provider-Call": "1",
        "X-Hashi-After-Tool-End": "false",
    }
    assert "Authorization" not in headers

    await adapter.shutdown()


@pytest.mark.asyncio
async def test_hashi_api_typed_modality_drift_replays_once_without_media(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nhashi-api")
    image_payload = image.read_bytes()
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Describe it."},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "attachment-1",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": image.name,
                "caption": "",
                "local_ref": str(image),
                "size_bytes": len(image_payload),
                "sha256": hashlib.sha256(image_payload).hexdigest(),
                "transport": {},
            },
        ]
    )
    adapter = _adapter(tmp_path, provider_reasoning="high")
    adapter.tool_registry = _MediaFallbackRegistry()
    request = httpx.Request("POST", adapter.hashi_url)
    rejected = httpx.Response(
        400,
        request=request,
        json={"error": {"code": "unsupported_modality"}},
    )
    adapter._call_api_once = AsyncMock(
        side_effect=[
            httpx.HTTPStatusError(
                "unsupported modality",
                request=request,
                response=rejected,
            ),
            _APIResult("done", None, "stop", 12, 3),
        ]
    )

    response = await adapter.generate_response(
        "Use media_read on the received path.",
        "request-drift",
        request_content=content,
    )

    assert response.is_success is True
    assert adapter._call_api_once.call_count == 2
    replay_payload = adapter._call_api_once.call_args_list[1].args[0]
    replay_content = replay_payload["messages"][1]["content"]
    assert replay_content[0] == {
        "type": "text",
        "text": "Use media_read on the received path.",
    }
    assert replay_content[1] == {"type": "text", "text": "Describe it."}
    assert "attachment-1" in replay_content[2]["text"]
    assert str(image) in replay_content[2]["text"]
    assert replay_payload["reasoning_effort"] == "high"
    assert {
        item["route"] for item in response.stream_metadata["multimodal_routing"]
    } == {"local_fallback"}


@pytest.mark.asyncio
async def test_hashi_stream_does_not_replay_after_partial_provider_output(tmp_path):
    image = tmp_path / "partial.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\npartial")
    image_payload = image.read_bytes()
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Describe it."},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "attachment-partial",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": image.name,
                "caption": "",
                "local_ref": str(image),
                "size_bytes": len(image_payload),
                "sha256": hashlib.sha256(image_payload).hexdigest(),
                "transport": {},
            },
        ]
    )
    adapter = _adapter(tmp_path)
    adapter.tool_registry = _MediaFallbackRegistry()
    attempts = 0

    async def handler(_request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"partial"},'
                '"finish_reason":null}]}\n\n'
                'data: {"error":{"message":"unsupported image",'
                '"code":"provider_modality_unsupported","status":400}}\n\n'
                'data: [DONE]\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events = []

    async def on_event(event):
        events.append(event)

    response = await adapter.generate_response(
        "Use media_read on the received path.",
        "request-partial-stream-drift",
        request_content=content,
        on_stream_event=on_event,
    )

    assert response.is_success is False
    assert response.error_code == "PROVIDER_MODALITY_UNSUPPORTED"
    assert attempts == 1
    assert [event.summary for event in events] == ["partial"]
    assert response.stream_metadata["provider_activity_observed"] is True
    assert response.stream_metadata["multimodal_fallback_attempted"] is False
    await adapter.shutdown()
