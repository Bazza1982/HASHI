"""Regressions for the PR 204 media review."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from adapters.deepseek_api import DeepSeekAdapter, _structured_tool_media
from adapters.openrouter_api import OpenRouterAdapter
from orchestrator.multimodal_contract import MultimodalContractError
from tools.registry import StructuredToolOutput, ToolRegistry


def test_openrouter_uncertain_capacity_does_not_block():
    adapter = object.__new__(OpenRouterAdapter)
    adapter.config = SimpleNamespace(_hashi_runtime=object(), model="m", engine="openrouter-api")
    profile = SimpleNamespace(usable_input_tokens=1, provider="openrouter", model="m")
    with patch("orchestrator.context_compaction.resolve_capacity_profile", return_value=profile):
        adapter._preflight_payload_capacity({"model": "m", "messages": [{"role": "user", "content": "ordinary text"}]})


def test_deepseek_structured_https_url_is_preserved():
    adapter = object.__new__(DeepSeekAdapter)
    adapter.resolve_input_capability = lambda: SimpleNamespace(supports=lambda kind, transport: kind == "image" and transport == "remote_url")
    messages = adapter._tool_result_messages(tool_name="capture", tool_call_id="abc", output="captured",
        content=[{"type":"image_url", "image_url":{"url":"https://example.com/image.png"}}])
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "abc"
    assert messages[0]["content"][-1]["image_url"]["url"] == "https://example.com/image.png"


def test_deepseek_unsupported_typed_image_is_not_silent():
    with pytest.raises(MultimodalContractError):
        _structured_tool_media([{"type":"image", "mimeType":"image/svg+xml", "data":"AAAA"}])


@pytest.mark.asyncio
async def test_unstructured_legacy_session_screenshot_reports_upgrade(monkeypatch):
    from tools import browser
    async def bridge(action, args):
        return "[get_text] data:image/png;base64,AAAA\n[screenshot] old"
    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", bridge)
    result = await browser.execute_browser_session({"steps":[{"action":"get_text"},{"action":"screenshot"}]}, typed_media=True)
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "worker" in result.lower()


@pytest.mark.asyncio
async def test_capability_legacy_screenshot_rejected_before_truncation(tmp_path):
    class Facade:
        async def invoke_capability(self, *args, **kwargs):
            return "screenshot:" + "A" * 30000
    registry = ToolRegistry(["browser_screenshot"], tmp_path, tmp_path, {})
    result = await registry._dispatch_device_capability(Facade(), "browser_control", "screenshot", {}, tool_call_id="x")
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert len(result) < 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "bad-envelope", "data:image/png;base64,AAAA"])
async def test_bad_session_screenshot_preserves_surrounding_steps(monkeypatch, bad):
    from tools import browser
    async def bridge(action, args):
        return json.dumps({"steps":[
            {"index":0,"action":"get_text","ok":True,"output":"before"},
            {"index":1,"action":"screenshot","ok":True,"output":bad},
            {"index":2,"action":"get_text","ok":True,"output":"after"}]})
    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", bridge)
    result = await browser.execute_browser_session({"steps":[{"action":"get_text"},{"action":"screenshot"},{"action":"get_text"}]}, typed_media=True)
    assert isinstance(result, StructuredToolOutput)
    texts = [part["text"] for part in result.content if part["type"] == "text"]
    assert "before" in texts[0]
    assert "error" in texts[1].lower()
    assert "after" in texts[2]

@pytest.mark.parametrize("url", ["ftp://example.com/a.png", "https://", "https://user:pass@example.com/a.png", "https://example.com/with space"])
def test_deepseek_invalid_remote_image_url_is_explicit(url):
    with pytest.raises(MultimodalContractError):
        _structured_tool_media([{"type": "image_url", "image_url": {"url": url}}])


@pytest.mark.asyncio
async def test_capability_text_session_stays_text(tmp_path):
    class Facade:
        async def invoke_capability(self, *args, **kwargs):
            return "source data:image/png;base64,AAAA"
    registry = ToolRegistry(["browser_session"], tmp_path, tmp_path, {})
    result = await registry._dispatch_device_capability(Facade(), "browser_control", "session", {"steps": [{"action": "get_text"}]}, tool_call_id="x")
    assert result == "source data:image/png;base64,AAAA"


@pytest.mark.asyncio
async def test_capability_unstructured_screenshot_mapping_reports_upgrade(tmp_path):
    class Facade:
        async def invoke_capability(self, *args, **kwargs):
            return {"output": "screenshot:" + "A" * 30000}
    registry = ToolRegistry(["browser_screenshot"], tmp_path, tmp_path, {})
    result = await registry._dispatch_device_capability(Facade(), "browser_control", "screenshot", {}, tool_call_id="x")
    assert result.startswith("Error:")
    assert len(result) < 1000


def test_deepseek_unavailable_remote_transport_is_explained():
    adapter = object.__new__(DeepSeekAdapter)
    adapter.resolve_input_capability = lambda: SimpleNamespace(supports=lambda kind, transport: False)
    messages = adapter._tool_result_messages(tool_name="capture", tool_call_id="abc", output="captured",
        content=[{"type":"image_url", "image_url":{"url":"https://example.com/image.png"}}])
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "abc"
    assert "omitted" in messages[0]["content"].lower()


def test_deepseek_source_text_example_remains_text():
    example = "data:" + "image/png;base64," + "AAAA"
    texts, urls = _structured_tool_media([{"type":"text", "text": example}])
    assert texts == [example]
    assert urls == []


def test_deepseek_official_remote_url_length_limit():
    prefix = "https://example.com/"
    accepted = prefix + "a" * (8192 - len(prefix))
    _, urls = _structured_tool_media([{"type":"image_url", "image_url":{"url":accepted}}])
    assert urls == [accepted]
    with pytest.raises(MultimodalContractError):
        _structured_tool_media([{"type":"image_url", "image_url":{"url":accepted + "a"}}])

@pytest.mark.asyncio
async def test_partial_session_explicit_status(monkeypatch):
    from tools import browser
    async def bridge(action, args):
        return json.dumps({"steps": [
            {"index": 0, "action": "get_text", "ok": True, "output": "before"},
            {"index": 1, "action": "screenshot", "ok": False, "error": "capture failed"},
            {"index": 2, "action": "get_text", "ok": True, "output": "after"}]})
    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", bridge)
    result = await browser.execute_browser_session({"steps": [{"action":"get_text"}, {"action":"screenshot"}, {"action":"get_text"}]}, typed_media=True)
    assert result.is_error is True
    assert [x["text"] for x in result.content] == ["[0:get_text] before", "[1:screenshot] capture failed", "[2:get_text] after"]

@pytest.mark.asyncio
async def test_partial_session_status_survives_worker_boundary(monkeypatch):
    from tools import browser
    from tools.device_control_worker import _execute_browser_action
    async def bridge(action, args):
        return json.dumps({"steps": [
            {"index": 0, "action": "get_text", "ok": True, "output": "before"},
            {"index": 1, "action": "screenshot", "ok": False, "error": "capture failed"},
            {"index": 2, "action": "get_text", "ok": True, "output": "after"}]})
    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", bridge)
    result = await _execute_browser_action("session", {"steps": [{"action":"get_text"}, {"action":"screenshot"}, {"action":"get_text"}]})
    assert result["is_error"] is True
    assert [x["text"] for x in result["content"]] == ["[0:get_text] before", "[1:screenshot] capture failed", "[2:get_text] after"]


def test_partial_session_provider_message_retains_failure_and_text():
    adapter = object.__new__(DeepSeekAdapter)
    parts = [{"type":"text", "text":"before"}, {"type":"text", "text":"screenshot failed"}, {"type":"text", "text":"after"}]
    messages = adapter._tool_result_messages(tool_name="browser_session", tool_call_id="partial", output="before\nscreenshot failed\nafter", content=parts)
    assert messages[0]["tool_call_id"] == "partial"
    assert "before" in messages[0]["content"]
    assert "screenshot failed" in messages[0]["content"]
    assert "after" in messages[0]["content"]
