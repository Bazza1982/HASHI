"""Focused proof of the local HERV3 outbound privacy gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.deepseek_api import DeepSeekAdapter
from adapters.openrouter_api import _APIResult
from orchestrator.her_v2.privacy_gate import OutboundPrivacyGate, PrivacyGateError
from tools.registry import ToolResult


async def _synthetic_detector(texts: list[str]) -> list[list[dict]]:
    targets = (("Jordan Lee", "PERSON"), ("jordan@example.com", "EMAIL_ADDRESS"))
    return [
        [
            {"start": text.index(value), "end": text.index(value) + len(value),
             "label": label, "score": 0.9}
            for value, label in targets if value in text
        ]
        for text in texts
    ]


def _deepseek(tmp_path) -> DeepSeekAdapter:
    config = SimpleNamespace(
        name="privacy-pilot", engine="deepseek-api", model="deepseek-flash",
        workspace_dir=tmp_path, system_md=None, extra={},
    )
    return DeepSeekAdapter(config, SimpleNamespace(), api_key="synthetic-key")


@pytest.mark.asyncio
async def test_pii_is_masked_before_deepseek_without_breaking_reasoning_result(tmp_path):
    adapter = _deepseek(tmp_path)
    adapter.privacy_level = 2
    adapter._herv3_privacy_scope = True
    adapter._privacy_gate = OutboundPrivacyGate(detector=_synthetic_detector)
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult("Two invoices of $50 total $100.", None, "stop", 25, 12)
    )
    wire_records = []

    def record(event, **kwargs):
        wire_records.append((event, kwargs["payload"]))
        return f"wire:{len(wire_records)}"

    adapter._record_provider_wire_evidence = record
    response = await adapter.generate_response(
        "Jordan Lee owes two invoices of $50 each. Email jordan@example.com. "
        "What is the total?",
        "req-privacy-pilot",
    )

    assert response.is_success is True
    assert "$100" in response.text
    outbound = adapter._call_api_once.call_args.args[0]
    rendered = json.dumps(outbound)
    assert "Jordan Lee" not in rendered
    assert "jordan@example.com" not in rendered
    assert "[PERSON_1]" in rendered
    assert "[EMAIL_ADDRESS_1]" in rendered
    assert "$50" in rendered
    assert "Jordan Lee" not in json.dumps(wire_records)
    assert "jordan@example.com" not in json.dumps(wire_records)


@pytest.mark.asyncio
async def test_level_two_cannot_use_deepseek_outside_herv3(tmp_path):
    adapter = _deepseek(tmp_path)
    adapter.privacy_level = 2
    adapter._privacy_gate = OutboundPrivacyGate(detector=_synthetic_detector)
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult("unexpected", None, "stop", 1, 1)
    )

    response = await adapter.generate_response("hello", "req-outside-herv3")

    assert response.is_success is False
    adapter._call_api_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_detector_failure_and_unreadable_media_block_before_network(tmp_path):
    async def broken_detector(_texts):
        raise RuntimeError("synthetic local model failed")

    gate = OutboundPrivacyGate(detector=broken_detector)
    with pytest.raises(PrivacyGateError):
        await gate.sanitize(
            {"model": "deepseek-flash", "messages": [{"content": "Jordan Lee"}]},
            request_id="req-failure",
        )
    with pytest.raises(PrivacyGateError):
        await OutboundPrivacyGate(detector=_synthetic_detector).sanitize(
            {"model": "deepseek-flash", "messages": [{"content": [{"type": "image_url", "image_url": {
                "url": "data:image/png;base64,AAAA",
            }}]}]},
            request_id="req-media",
        )


@pytest.mark.asyncio
async def test_tool_result_is_masked_on_second_deepseek_call(tmp_path):
    class Registry:
        def get_tool_definitions(self, tiers=None):
            return [{"type": "function", "function": {
                "name": "file_list", "description": "List files.",
                "parameters": {"type": "object", "properties": {}},
            }}]

        async def execute(self, tool_name, arguments, tool_call_id=""):
            return ToolResult(
                tool_call_id=tool_call_id,
                output="Jordan Lee has email jordan@example.com and owes $50.",
            )

    adapter = _deepseek(tmp_path)
    adapter.tool_registry = Registry()
    adapter.privacy_level = 2
    adapter._herv3_privacy_scope = True
    adapter._privacy_gate = OutboundPrivacyGate(detector=_synthetic_detector)
    seen = []

    async def fake_call(payload, _headers, _callback):
        seen.append(payload)
        if len(seen) == 1:
            return _APIResult(text="", tool_calls=[{
                "id": "call_1", "type": "function", "function": {
                    "name": "file_list", "arguments": "{}",
                },
            }], finish_reason="tool_calls")
        return _APIResult(text="The debt is $50.", tool_calls=None, finish_reason="stop")

    adapter._call_api_once = fake_call
    response = await adapter.generate_response("Check the record.", "req-tool-pii")

    assert response.is_success is True
    assert response.tool_call_count == 1
    assert len(seen) == 2
    second_call = json.dumps(seen[1])
    assert "Jordan Lee" not in second_call
    assert "jordan@example.com" not in second_call
    assert "[PERSON_1]" in second_call
    assert "[EMAIL_ADDRESS_1]" in second_call
    assert "$50" in second_call


@pytest.mark.asyncio
async def test_streaming_deepseek_call_receives_only_masked_payload(tmp_path):
    adapter = _deepseek(tmp_path)
    adapter.privacy_level = 2
    adapter._herv3_privacy_scope = True
    adapter._privacy_gate = OutboundPrivacyGate(detector=_synthetic_detector)
    adapter._stream_api_once = AsyncMock(
        return_value=_APIResult("The total is $100.", None, "stop", 25, 12)
    )

    async def on_stream_event(_event):
        return None

    response = await adapter.generate_response(
        "Jordan Lee owes two $50 invoices. Contact jordan@example.com. Total?",
        "req-stream-pii",
        on_stream_event=on_stream_event,
    )

    assert response.is_success is True
    outbound = json.dumps(adapter._stream_api_once.call_args.args[0])
    assert "Jordan Lee" not in outbound
    assert "jordan@example.com" not in outbound
    assert "[PERSON_1]" in outbound
    assert "[EMAIL_ADDRESS_1]" in outbound


@pytest.mark.platform
@pytest.mark.asyncio
async def test_isolated_local_model_detects_synthetic_pii():
    interpreter = os.getenv("HASHI_PRIVACY_FILTER_PYTHON")
    if not interpreter:
        pytest.skip("isolated privacy model interpreter is not configured")
    gate = OutboundPrivacyGate(python_executable=interpreter)
    cleaned = await gate.sanitize(
        {"model": "deepseek-flash", "messages": [{"role": "user", "content":
            "Jordan Lee owes two invoices of $50 each. Email jordan@example.com. "
            "What is the total?"}]},
        request_id="req-local-model",
    )
    rendered = json.dumps(cleaned)
    assert "Jordan Lee" not in rendered
    assert "jordan@example.com" not in rendered
    assert "[PERSON_1]" in rendered
    assert "[EMAIL_ADDRESS_1]" in rendered
    assert "$50" in rendered


@pytest.mark.platform
@pytest.mark.asyncio
async def test_opt_in_real_deepseek_receives_redacted_pii_and_solves_task(tmp_path):
    """Opt-in synthetic canary; no real personal data is sent to DeepSeek."""
    if os.getenv("HASHI_PRIVACY_LIVE_CANARY") != "1":
        pytest.skip("live privacy canary is not enabled")
    interpreter = os.getenv("HASHI_PRIVACY_FILTER_PYTHON")
    secrets_path = os.getenv("HASHI_PRIVACY_CANARY_SECRETS")
    if not interpreter or not secrets_path:
        pytest.fail("isolated filter and canary secrets path are required")
    api_key = json.loads(Path(secrets_path).read_text(encoding="utf-8")).get(
        "deepseek_api_key"
    )
    if not isinstance(api_key, str) or not api_key:
        pytest.fail("DeepSeek canary credential is unavailable")

    adapter = _deepseek(tmp_path)
    adapter.api_key = api_key
    adapter.config.extra = {"provider_reasoning": "high"}
    adapter.privacy_level = 2
    adapter._herv3_privacy_scope = True
    adapter._privacy_gate = OutboundPrivacyGate(python_executable=interpreter)
    original_call = adapter._call_api_once
    observed = {"safe_http_calls": 0}

    async def checked_call(payload, headers, callback):
        outbound = json.dumps(payload, ensure_ascii=False)
        assert "Jordan Lee" not in outbound
        assert "jordan@example.com" not in outbound
        assert "[PERSON_1]" in outbound
        assert "[EMAIL_ADDRESS_1]" in outbound
        assert "$50" in outbound
        observed["safe_http_calls"] += 1
        return await original_call(payload, headers, callback)

    adapter._call_api_once = checked_call
    try:
        assert await adapter.initialize()
        response = await adapter.generate_response(
            "Jordan Lee has two invoices of $50 each. Contact: "
            "jordan@example.com. What is the total amount? Answer with the total.",
            "req-synthetic-deepseek-canary",
        )
        assert observed["safe_http_calls"] == 1
        assert response.is_success is True
        assert "100" in response.text
    finally:
        await adapter.shutdown()
