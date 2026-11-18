from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.openrouter_api import OpenRouterAdapter, _APIResult
from orchestrator.multimodal_contract import canonical_request_content


def _fallback_descriptor(text: str) -> dict:
    descriptor, _ = json.JSONDecoder().raw_decode(
        text.removeprefix("LOCAL_MEDIA_ATTACHMENT ")
    )
    return descriptor


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
    assert Path(_fallback_descriptor(replay_content[2]["text"])["local_ref"]) == (
        tmp_path / "one.png"
    )
    assert Path(_fallback_descriptor(replay_content[3]["text"])["local_ref"]) == (
        tmp_path / "two.png"
    )
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
    assert Path(_fallback_descriptor(fallback_content[2]["text"])["local_ref"]) == (
        tmp_path / "one.png"
    )
    assert "attachment-2" in fallback_content[3]["text"]
    assert Path(_fallback_descriptor(fallback_content[3]["text"])["local_ref"]) == (
        tmp_path / "two.png"
    )
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
    assert Path(_fallback_descriptor(user_content[2]["text"])["local_ref"]) == audio
    assert [
        (item["attachment_id"], item["route"])
        for item in response.stream_metadata["multimodal_routing"]
    ] == [
        ("attachment-image", "native"),
        ("attachment-audio", "local_fallback"),
    ]



def test_live_camera_samples_follow_model_capacity_without_failing_voice(tmp_path):
    from orchestrator.multimodal_contract import InputCapability, MultimodalContractError

    adapter = _adapter(tmp_path)
    frames = [
        {
            "data_url": "data:image/jpeg;base64,/9j/wAAICAAQABAA/9k=",
            "frame_sequence": index,
            "captured_at": f"2026-11-17T18:00:0{index}+00:00",
        }
        for index in range(1, 6)
    ]

    def capability(**limits):
        return InputCapability(
            provider="test", model="visual", input_modalities=frozenset({"text", "image"}),
            input_transports={"image": ("data_url",)}, limits=limits,
        )

    adapter.resolve_input_capability = lambda: capability(item_count=2)
    result = adapter._append_transient_images([{"role": "user", "content": "What changed?"}], frames)
    parts = result[-1]["content"]
    assert len([part for part in parts if part["type"] == "image_url"]) == 2
    text = " ".join(part["text"] for part in parts if part["type"] == "text")
    assert "2 of 5" in text and "frame_sequence=1" in text and "frame_sequence=5" in text
    assert "sample" in text.lower()

    # A small aggregate limit can still use a valid subset.
    adapter.resolve_input_capability = lambda: capability(item_count=5, total_bytes=14)
    result = adapter._append_transient_images([{"role": "user", "content": "Keep talking."}], frames)
    parts = result[-1]["content"]
    assert len([part for part in parts if part["type"] == "image_url"]) == 1
    assert any("1 of 5" in part.get("text", "") for part in parts)

    # Native attachments consume the same count and aggregate capacity.
    adapter.resolve_input_capability = lambda: capability(item_count=2, total_bytes=28)
    existing = [{"type": "text", "text": "Compare."},
                {"type": "image_url", "image_url": {"url": frames[0]["data_url"]}}]
    result = adapter._append_transient_images([{"role": "user", "content": existing}], frames)
    parts = result[-1]["content"]
    assert len([part for part in parts if part["type"] == "image_url"]) == 2
    assert any("1 of 5" in part.get("text", "") for part in parts)

    # When evenly spaced candidates are too large, smaller candidates can
    # still preserve the maximum number that fits the aggregate budget.
    import base64
    jpeg = bytes.fromhex("ffd8ffc00008080010001000ffd9")
    varied = [
        {**frame, "data_url": "data:image/jpeg;base64," + base64.b64encode(
            jpeg + (b"x" * 10 if index % 2 == 0 else b"")
        ).decode()}
        for index, frame in enumerate(frames)
    ]
    adapter.resolve_input_capability = lambda: capability(item_count=2, total_bytes=28)
    result = adapter._append_transient_images([{"role": "user", "content": "Inspect."}], varied)
    parts = result[-1]["content"]
    assert len([part for part in parts if part["type"] == "image_url"]) == 2
    text = " ".join(part["text"] for part in parts if part["type"] == "text")
    assert "frame_sequence=2" in text and "frame_sequence=4" in text

    # Nothing fits the selected image budget: keep the spoken request and say
    # that no camera frame could be included instead of failing the whole turn.
    adapter.resolve_input_capability = lambda: capability(item_bytes=1)
    result = adapter._append_transient_images([{"role": "user", "content": "Keep talking."}], frames)
    parts = result[-1]["content"]
    assert not [part for part in parts if part["type"] == "image_url"]
    assert any("0 of 5" in part.get("text", "") for part in parts)
    assert any("Keep talking." in part.get("text", "") for part in parts)

    # Normalized timestamps are a conservative bound, not an exact capture time.
    adapter.resolve_input_capability = lambda: capability()
    normalized = {**frames[0], "capture_clock": {
        "basis": "server_roundtrip_lower_bound",
        "client_captured_at": "2026-11-17T18:02:01+00:00",
        "uncertainty_ms": 400,
    }}
    result = adapter._append_transient_images([{"role": "user", "content": "Inspect."}], [normalized])
    text = " ".join(part["text"] for part in result[-1]["content"] if part["type"] == "text")
    assert "earliest_possible_capture_at=" in text and "capture_time_uncertainty_ms=400" in text

    # An invalid frame is never silently presented as a legitimate omission.
    with pytest.raises(MultimodalContractError):
        adapter._append_transient_images(
            [{"role": "user", "content": "Test."}],
            [{**frames[0], "data_url": "data:image/jpeg;base64,not-base64!"}],
        )


def test_live_camera_checkpoint_omits_only_transient_images_and_preserves_tool_context(tmp_path):
    adapter = _adapter(tmp_path)
    image = "data:image/jpeg;base64,/9j/wAAICAAQABAA/9k="
    frame = {"data_url": image, "frame_sequence": 1, "captured_at": "2026-11-17T18:00:00+00:00"}
    messages = [
        {"role": "system", "content": "Assist."},
        {"role": "user", "content": [
            {"type": "text", "text": "Compare with this uploaded image."},
            {"type": "image_url", "image_url": {"url": image}},
        ]},
    ]
    adapter._append_transient_images(messages, [frame])
    messages.extend([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tool-1", "type": "function",
             "function": {"name": "file_read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "tool-1", "content": "already completed"},
    ])
    checkpoints = []
    adapter.set_provider_context_observer(checkpoints.append)

    adapter._observe_provider_context(
        boundary="completed_tool_batch", messages=messages, request_id="request-1",
        result=_APIResult("", None, "stop"), provider_request_id="provider-1",
    )

    checkpoint = checkpoints[0]
    parts = checkpoint["messages"][1]["content"]
    assert len([part for part in parts if part.get("type") == "image_url"]) == 1
    assert any("unavailable" in part.get("text", "").lower() for part in parts)
    assert checkpoint["ephemeral_media_omitted"] is True
    assert checkpoint["messages"][2:] == messages[2:]
    assert len([part for part in messages[1]["content"] if part.get("type") == "image_url"]) == 2


@pytest.mark.asyncio
async def test_camera_expiring_during_provider_preflight_is_not_sent(tmp_path):
    import asyncio
    from datetime import datetime, timezone
    from orchestrator.frontend_call.transient_live_media import bind_live_frames, live_frames_for

    now = datetime.now(timezone.utc)
    frame = {
        "data_url": "data:image/jpeg;base64,/9j/wAAICAAQABAA/9k=",
        "frame_sequence": 1,
        "captured_at": now.isoformat(),
    }
    runtime = SimpleNamespace()
    bind_live_frames(runtime, "request-camera-expiry", [frame], freshness_seconds=1)
    frames = live_frames_for(runtime, "request-camera-expiry", now=now)
    adapter = _adapter(tmp_path)
    async def slow_preflight(payload, *, request_id, provider_calls):
        del payload, request_id, provider_calls
        await asyncio.sleep(1.2)
    adapter._prepare_payload_capacity = slow_preflight
    adapter._call_api_once = AsyncMock(return_value=_APIResult("Voice continues.", None, "stop"))

    response = await adapter.generate_response(
        "Keep speaking.", "request-camera-expiry", transient_images=frames,
    )

    assert response.is_success
    parts = adapter._call_api_once.call_args.args[0]["messages"][1]["content"]
    assert not any(part.get("type") == "image_url" for part in parts)
    assert any("expired" in part.get("text", "").lower() for part in parts)
    assert any("Keep speaking." in part.get("text", "") for part in parts)


@pytest.mark.asyncio
async def test_camera_expiring_during_tool_execution_is_not_resent(tmp_path):
    import asyncio
    from datetime import datetime, timezone
    from orchestrator.frontend_call.transient_live_media import bind_live_frames, live_frames_for

    now = datetime.now(timezone.utc)
    runtime = SimpleNamespace()
    bind_live_frames(runtime, "request-tool-camera", [{
        "data_url": "data:image/jpeg;base64,/9j/wAAICAAQABAA/9k=",
        "frame_sequence": 1, "captured_at": now.isoformat(),
    }], freshness_seconds=1)
    frames = live_frames_for(runtime, "request-tool-camera", now=now)
    adapter = _adapter(tmp_path)
    adapter.tool_registry = _FallbackRegistry()
    call = {"id": "one-tool", "type": "function",
            "function": {"name": "vision_inspect", "arguments": "{}"}}
    adapter._call_api_once = AsyncMock(side_effect=[
        _APIResult("", [call], "tool_calls"),
        _APIResult("Finished.", None, "stop"),
    ])
    executed = []
    async def slow_tool(calls, messages, callback, **kwargs):
        del callback
        if callable(kwargs.get("before_tool_dispatch")):
            kwargs["before_tool_dispatch"]()
        executed.extend(calls)
        await asyncio.sleep(1.2)
        messages.append({"role": "tool", "tool_call_id": "one-tool", "content": "completed"})
        return ()
    adapter._run_tool_calls = slow_tool

    response = await adapter.generate_response(
        "Keep speaking.", "request-tool-camera", transient_images=frames,
    )

    assert response.is_success
    assert len(executed) == 1
    first = adapter._call_api_once.call_args_list[0].args[0]["messages"][1]["content"]
    second = adapter._call_api_once.call_args_list[1].args[0]["messages"][1]["content"]
    assert any(part.get("type") == "image_url" for part in first)
    assert not any(part.get("type") == "image_url" for part in second)
    assert any("expired" in part.get("text", "").lower() for part in second)
    assert adapter._call_api_once.call_args_list[1].args[0]["messages"][-1]["content"] == "completed"


def test_live_camera_wire_evidence_omits_camera_bytes_but_keeps_upload_and_original_digest(tmp_path):
    import base64
    from adapters.openrouter_api import _request_wire_evidence

    jpeg = bytes.fromhex("ffd8ffc00008080010001000ffd9")
    ordinary = "data:image/jpeg;base64," + base64.b64encode(jpeg + b"ordinary-upload").decode()
    camera = "data:image/jpeg;base64," + base64.b64encode(jpeg + b"camera-canary").decode()
    adapter = _adapter(tmp_path)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Compare."},
        {"type": "image_url", "image_url": {"url": ordinary}},
    ]}]
    adapter._append_transient_images(messages, [{
        "data_url": camera, "captured_at": "2026-11-17T18:00:00+00:00", "frame_sequence": 1,
    }])
    payload = {"model": "visual", "messages": messages}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    captured = []
    adapter._provider_protocol_forensic_sink = lambda kind, value: captured.append(value) or "private:wire"

    wire = _request_wire_evidence(payload, SimpleNamespace(content=raw))
    adapter._record_provider_wire_evidence(
        "request_prepared", request_id="camera", call_serial=1, payload={"body": payload},
    )

    assert wire["sha256"] == hashlib.sha256(raw).hexdigest()
    assert wire["body_is_exact"] is False
    assert wire["transient_camera_images_omitted"] == 1
    assert camera not in json.dumps(wire)
    assert ordinary in json.dumps(wire)
    assert camera not in json.dumps(captured)
    assert ordinary in json.dumps(captured)
    assert camera in raw.decode(), "the actual provider request remains unchanged"

    ordinary_payload = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": ordinary}},
    ]}]}
    ordinary_raw = json.dumps(ordinary_payload).encode()
    untouched = _request_wire_evidence(ordinary_payload, SimpleNamespace(content=ordinary_raw))
    assert untouched["body"] == ordinary_raw.decode()
    assert untouched["sha256"] == hashlib.sha256(ordinary_raw).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_camera_is_not_retained_in_provider_http_failure_diagnostics(tmp_path, streaming):
    import base64
    from adapters.openrouter_api import _provider_http_failure_diagnostics

    jpeg = bytes.fromhex("ffd8ffc00008080010001000ffd9")
    ordinary = "data:image/jpeg;base64," + base64.b64encode(jpeg + b"upload-canary").decode()
    camera = "data:image/jpeg;base64," + base64.b64encode(jpeg + b"camera-canary").decode()
    adapter = _adapter(tmp_path)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Compare."},
        {"type": "image_url", "image_url": {"url": ordinary}},
    ]}]
    adapter._append_transient_images(messages, [{
        "data_url": camera, "captured_at": "2026-11-17T18:00:00+00:00", "frame_sequence": 1,
    }])
    payload = {"model": "visual", "messages": messages}

    async def fail(request):
        raise httpx.ConnectError("synthetic failure", request=request)

    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    with pytest.raises(httpx.ConnectError) as raised:
        if streaming:
            await adapter._stream_api_once(payload, {}, None)
        else:
            await adapter._call_api_once(payload, {}, None)
    diagnostics = _provider_http_failure_diagnostics(raised.value)
    evidence = diagnostics["request"]
    assert evidence["body_is_exact"] is False
    assert evidence["transient_camera_images_omitted"] == 1
    assert evidence["body_sha256"] == hashlib.sha256(raised.value.request.content).hexdigest()
    assert camera not in json.dumps(evidence)
    assert ordinary in json.dumps(evidence)
    assert camera in raised.value.request.content.decode()
    await adapter.client.aclose()
