"""Image-vs-text PIT regression matrix for native HERV3 media delivery."""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from adapters.openrouter_api import OpenRouterAdapter
from orchestrator.her_v2.privacy_gate import OutboundPrivacyGate
from orchestrator.multimodal_contract import (
    canonical_request_content,
    contains_persistent_inline_media,
)
from orchestrator.provider_text_projection import provider_text_capacity_json
from tools.typed_media_result import screenshot_tool_result


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
ENCODED = base64.b64encode(PNG).decode("ascii")
URL = "data:image/png;base64," + ENCODED


@pytest.mark.parametrize("description", [
    "plain: " + URL,
    "pytest stdout: image_url = " + repr(URL),
    '{"snippet": ' + repr(URL) + "}",
    "code fenced:\n```python\nimage = " + repr(URL) + "\n```",
])
def test_text_examples_are_not_typed_inline_media(description):
    parts = [{"type": "text", "item_index": 1, "text": description}]
    saved = canonical_request_content(parts)
    assert saved["parts"][0]["text"] == description
    assert contains_persistent_inline_media(parts) is False
    assert contains_persistent_inline_media([
        {"role": "tool", "content": description},
    ]) is False
    assert contains_persistent_inline_media([
        {"type": "image_url", "image_url": {"url": URL}},
    ]) is True


def test_provider_estimate_retains_text_but_not_structured_media_bytes():
    payload = {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Real literal in code: " + URL},
            {"type": "image_url", "image_url": {"url": URL}},
        ]}],
        "tools": [{"type": "function", "function": {
            "name": "read", "description": "Technical example " + URL,
        }}],
    }
    projection = provider_text_capacity_json(payload)
    assert "Real literal in code: " + URL in projection
    assert "Technical example " + URL in projection
    assert projection.count(URL) == 2
    assert '"media_payload":"[typed media]"' in projection
    assert payload["messages"][0]["content"][1]["image_url"]["url"] == URL


@pytest.mark.asyncio
async def test_privacy_gate_treats_technical_example_as_text():
    async def detector(texts):
        return [[] for _ in texts]

    gate = OutboundPrivacyGate(detector=detector)
    payload = {"model": "deepseek-flash", "messages": [
        {"role": "user", "content": URL}
    ]}
    result = await gate.sanitize(payload, request_id="text-not-media")
    assert result == payload


def test_capture_produces_structured_image_without_stdout_base64():
    native = screenshot_tool_result(PNG, "captured")
    assert native.output == "captured"
    assert native.content[0] == {"type": "text", "text": "captured"}
    assert native.content[1]["type"] == "image"
    assert base64.b64decode(native.content[1]["data"]) == PNG
    assert ENCODED not in native.output


def test_openrouter_base_adapter_preserves_typed_image():
    adapter = object.__new__(OpenRouterAdapter)
    adapter.resolve_input_capability = lambda: SimpleNamespace(
        supports=lambda kind, transport: kind == "image" and transport == "data_url",
    )
    messages = adapter._tool_result_messages(
        tool_name="browser_screenshot", tool_call_id="call-image",
        output="captured",
        content=[{"type": "text", "text": "captured"}, {
            "type": "image", "mimeType": "image/png", "data": ENCODED,
        }],
    )
    assert [m["role"] for m in messages] == ["tool", "user"]
    assert messages[0]["tool_call_id"] == "call-image"
    assert messages[1]["content"][1]["type"] == "image_url"
    assert messages[1]["content"][1]["image_url"]["url"] == URL
