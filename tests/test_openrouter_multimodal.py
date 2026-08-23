from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.openrouter_api import OpenRouterAdapter, _APIResult
from orchestrator.multimodal_contract import canonical_request_content


class _FallbackRegistry:
    def __init__(self):
        self.executions = 0

    def is_allowed(self, name):
        return name in {"media_read", "vision_inspect"}

    def get_tool_definitions(self, tiers=None):
        del tiers
        return [
            {
                "type": "function",
                "function": {
                    "name": "vision_inspect",
                    "parameters": {"type": "object"},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "media_read",
                    "parameters": {"type": "object"},
                },
            },
        ]

    async def execute(self, *args, **kwargs):
        self.executions += 1
        raise AssertionError("native success must not invoke media fallback")


def _adapter(tmp_path, model="google/gemini-2.5-pro"):
    config = SimpleNamespace(
        name="multimodal",
        engine="openrouter-api",
        model=model,
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    adapter = OpenRouterAdapter(
        config,
        SimpleNamespace(
            openrouter_url="https://openrouter.invalid/v1/chat/completions",
            base_media_dir=tmp_path,
        ),
        api_key="test-key",
    )
    return adapter


def _write_png(path, marker=b"one"):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker)


def _image_part(path, index, attachment_id, *, detail=""):
    payload = path.read_bytes()
    part = {
        "type": "media",
        "item_index": index,
        "attachment_id": attachment_id,
        "modality": "image",
        "kind": "photo",
        "mime_type": "image/png",
        "filename": path.name,
        "caption": "",
        "local_ref": str(path),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "transport": {"message_id": index},
    }
    if detail:
        part["detail"] = detail
    return part


def _two_images(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    _write_png(first, b"first")
    _write_png(second, b"second")
    return canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Compare both."},
            _image_part(first, 2, "attachment-1", detail="high"),
            _image_part(second, 3, "attachment-2"),
        ]
    )


@pytest.mark.asyncio
async def test_gemini_receives_ordered_native_images_without_tools_sync(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult("done", None, "stop", 10, 2)
    )

    response = await adapter.generate_response(
        "Compare both.", "request-1", request_content=_two_images(tmp_path)
    )

    assert response.is_success is True
    payload = adapter._call_api_once.call_args.args[0]
    user_content = payload["messages"][1]["content"]
    assert [part["type"] for part in user_content] == [
        "text",
        "image_url",
        "image_url",
    ]
    assert user_content[1]["image_url"]["detail"] == "high"
    assert user_content[1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert user_content[2]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert [item["attachment_id"] for item in response.stream_metadata["multimodal_routing"]] == [
        "attachment-1",
        "attachment-2",
    ]


@pytest.mark.asyncio
async def test_gemini_receives_ordered_native_images_without_tools_stream(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._stream_api_once = AsyncMock(
        return_value=_APIResult("done", None, "stop", 10, 2)
    )

    async def on_event(_event):
        return None

    response = await adapter.generate_response(
        "Compare both.",
        "request-2",
        request_content=_two_images(tmp_path),
        on_stream_event=on_event,
    )

    assert response.is_success is True
    payload = adapter._stream_api_once.call_args.args[0]
    assert payload["stream"] is True
    assert len(payload["messages"][1]["content"]) == 3


@pytest.mark.asyncio
async def test_streamed_typed_modality_error_replays_once_before_provider_activity(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    adapter.tool_registry = _FallbackRegistry()
    attempts = 0

    async def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            body = (
                'data: {"choices":[{"delta":{"role":"assistant"},'
                '"finish_reason":null}]}\n\n'
                'data: {"error":{"message":"unsupported image",'
                '"code":"unsupported_modality","status":400}}\n\n'
                'data: [DONE]\n\n'
            )
        else:
            body = (
                'data: {"choices":[{"delta":{"content":"done"},'
                '"finish_reason":"stop"}]}\n\n'
                'data: [DONE]\n\n'
            )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def on_event(_event):
        return None

    response = await adapter.generate_response(
        "Use the received image paths if native input fails.",
        "request-stream-drift",
        request_content=_two_images(tmp_path),
        on_stream_event=on_event,
    )

    assert response.is_success is True
    assert attempts == 2
    assert response.text == "done"
    assert response.stream_metadata["multimodal_fallback_attempted"] is True
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_openrouter_native_success_does_not_call_media_fallback(tmp_path):
    adapter = _adapter(tmp_path)
    registry = _FallbackRegistry()
    adapter.tool_registry = registry
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult("done", None, "stop", 10, 2)
    )

    response = await adapter.generate_response(
        "Compare both.", "request-3", request_content=_two_images(tmp_path)
    )

    assert response.is_success is True
    assert registry.executions == 0
    payload = adapter._call_api_once.call_args.args[0]
    assert "tools" not in payload


@pytest.mark.asyncio
async def test_typed_modality_drift_replays_once_through_local_fallback(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.tool_registry = _FallbackRegistry()
    request = httpx.Request(
        "POST",
        "https://openrouter.invalid/v1/chat/completions",
    )
    rejected = httpx.Response(
        400,
        request=request,
        json={"error": {"code": "unsupported_modality"}},
    )
    typed_error = httpx.HTTPStatusError(
        "unsupported modality",
        request=request,
        response=rejected,
    )
    adapter._call_api_once = AsyncMock(
        side_effect=[
            typed_error,
            _APIResult("done", None, "stop", 10, 2),
        ]
    )

    response = await adapter.generate_response(
        "Use the received image paths if native input fails.",
        "request-drift",
        request_content=_two_images(tmp_path),
    )

    assert response.is_success is True
    assert adapter._call_api_once.call_count == 2
    first_payload = adapter._call_api_once.call_args_list[0].args[0]
    second_payload = adapter._call_api_once.call_args_list[1].args[0]
    assert any(
        part["type"] == "image_url"
        for part in first_payload["messages"][1]["content"]
    )
    replay_content = second_payload["messages"][1]["content"]
    assert replay_content[0] == {
        "type": "text",
        "text": "Use the received image paths if native input fails.",
    }
    assert replay_content[1] == {"type": "text", "text": "Compare both."}
    assert ["attachment-1" in part["text"] for part in replay_content] == [
        False,
        False,
        True,
        False,
    ]
    assert "attachment-2" in replay_content[3]["text"]
    assert str(tmp_path / "one.png") in replay_content[2]["text"]
    assert str(tmp_path / "two.png") in replay_content[3]["text"]
    assert {
        item["route"] for item in response.stream_metadata["multimodal_routing"]
    } == {"local_fallback"}
    assert "tools" in second_payload


@pytest.mark.asyncio
async def test_authentication_failure_does_not_trigger_media_fallback(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.tool_registry = _FallbackRegistry()
    request = httpx.Request(
        "POST",
        "https://openrouter.invalid/v1/chat/completions",
    )
    rejected = httpx.Response(401, request=request)
    auth_error = httpx.HTTPStatusError(
        "authentication failed",
        request=request,
        response=rejected,
    )
    adapter._call_api_once = AsyncMock(side_effect=auth_error)

    response = await adapter.generate_response(
        "Compare both.",
        "request-auth-failure",
        request_content=_two_images(tmp_path),
    )

    assert response.is_success is False
    assert response.error_code == "PROVIDER_AUTHENTICATION_FAILED"
    assert adapter._call_api_once.call_count == 1


@pytest.mark.asyncio
async def test_hashi_gateway_stable_unsupported_media_code_can_trigger_fallback(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    adapter.tool_registry = _FallbackRegistry()
    request = httpx.Request(
        "POST",
        "https://openrouter.invalid/v1/chat/completions",
    )
    rejected = httpx.Response(
        400,
        request=request,
        json={"error": {"code": "unsupported_media"}},
    )
    adapter._call_api_once = AsyncMock(
        side_effect=[
            httpx.HTTPStatusError(
                "unsupported media",
                request=request,
                response=rejected,
            ),
            _APIResult("done", None, "stop", 10, 2),
        ]
    )

    response = await adapter.generate_response(
        "Use the received image paths if native input fails.",
        "request-gateway-drift",
        request_content=_two_images(tmp_path),
    )

    assert response.is_success is True
    assert adapter._call_api_once.call_count == 2
    assert response.stream_metadata["multimodal_fallback_attempted"] is True


@pytest.mark.asyncio
async def test_native_attachment_cannot_be_processed_again_by_fallback_tool(tmp_path):
    adapter = _adapter(tmp_path)
    registry = _FallbackRegistry()
    adapter.tool_registry = registry
    adapter._call_api_once = AsyncMock(
        side_effect=[
            _APIResult(
                "",
                [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "vision_inspect",
                            "arguments": '{"image_ref":"attachment-1","question":"again"}',
                        },
                    }
                ],
                "tool_calls",
                10,
                2,
            ),
            _APIResult("done", None, "stop", 4, 1),
        ]
    )

    response = await adapter.generate_response(
        "Compare both.", "request-guard", request_content=_two_images(tmp_path)
    )

    assert response.is_success is True
    assert registry.executions == 0
    second_payload = adapter._call_api_once.call_args_list[1].args[0]
    tool_result = next(
        message for message in second_payload["messages"] if message["role"] == "tool"
    )
    assert "duplicate fallback processing is blocked" in tool_result["content"]


@pytest.mark.asyncio
async def test_openrouter_text_model_uses_local_fallback(tmp_path):
    adapter = _adapter(tmp_path, model="deepseek/deepseek-chat")
    adapter.tool_registry = _FallbackRegistry()
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult("done", None, "stop", 10, 2)
    )

    response = await adapter.generate_response(
        "Use media_read on the received paths.",
        "request-4",
        request_content=_two_images(tmp_path),
    )

    assert response.is_success is True
    payload = adapter._call_api_once.call_args.args[0]
    assert isinstance(payload["messages"][1]["content"], list)
    fallback_content = payload["messages"][1]["content"]
    assert fallback_content[0] == {
        "type": "text",
        "text": "Use media_read on the received paths.",
    }
    assert fallback_content[1] == {"type": "text", "text": "Compare both."}
    assert "attachment-1" in fallback_content[2]["text"]
    assert str(tmp_path / "one.png") in fallback_content[2]["text"]
    assert "attachment-2" in fallback_content[3]["text"]
    assert str(tmp_path / "two.png") in fallback_content[3]["text"]
    assert {
        item["route"] for item in response.stream_metadata["multimodal_routing"]
    } == {"local_fallback"}


@pytest.mark.asyncio
async def test_local_fallback_rechecks_attachment_integrity_before_provider(tmp_path):
    adapter = _adapter(tmp_path, model="deepseek/deepseek-chat")
    adapter.tool_registry = _FallbackRegistry()
    content = _two_images(tmp_path)
    (tmp_path / "one.png").write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult("should not run", None, "stop")
    )

    response = await adapter.generate_response(
        "Use media_read on both paths.",
        "request-fallback-integrity",
        request_content=content,
    )

    assert response.is_success is False
    assert response.error_code == "MEDIA_INTEGRITY_CHANGED"
    assert response.stream_metadata["attachment_id"] == "attachment-1"
    adapter._call_api_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_engine_models_do_not_share_image_capability(tmp_path):
    native = _adapter(tmp_path, model="google/gemini-2.5-pro")
    text_only = _adapter(tmp_path, model="deepseek/deepseek-chat")

    assert native.resolve_input_capability().supports("image") is True
    assert text_only.resolve_input_capability().supports("image") is False


@pytest.mark.asyncio
async def test_openrouter_mixed_image_audio_routes_each_attachment_separately(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    adapter.tool_registry = _FallbackRegistry()
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult("done", None, "stop", 10, 2)
    )
    image = tmp_path / "one.png"
    audio = tmp_path / "voice.ogg"
    _write_png(image)
    audio_payload = b"OggSvoice"
    audio.write_bytes(audio_payload)
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Use both."},
            _image_part(image, 2, "attachment-image"),
            {
                "type": "media",
                "item_index": 3,
                "attachment_id": "attachment-audio",
                "modality": "audio",
                "kind": "audio",
                "mime_type": "audio/ogg",
                "filename": audio.name,
                "caption": "",
                "local_ref": str(audio),
                "size_bytes": len(audio_payload),
                "sha256": hashlib.sha256(audio_payload).hexdigest(),
                "transport": {},
            },
        ]
    )

    response = await adapter.generate_response(
        "Use both.", "request-mixed", request_content=content
    )

    assert response.is_success is True
    user_content = adapter._call_api_once.call_args.args[0]["messages"][1]["content"]
    assert [part["type"] for part in user_content] == [
        "text",
        "image_url",
        "text",
    ]
    assert "attachment-audio" in user_content[2]["text"]
    assert str(audio) in user_content[2]["text"]
    assert [
        (item["attachment_id"], item["route"])
        for item in response.stream_metadata["multimodal_routing"]
    ] == [
        ("attachment-image", "native"),
        ("attachment-audio", "local_fallback"),
    ]
