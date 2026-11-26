from __future__ import annotations

import json

import httpx
import pytest

from types import SimpleNamespace

from adapters.base import BackendResponse
from adapters.deepseek_api import DeepSeekAdapter
from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
from adapters.openrouter_api import _APIResult
from adapters.stream_events import KIND_TEXT_DELTA, StreamEvent
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.models import Effort, Stage, StageRequest
from tests.test_hashi_api import _adapter
from tools.registry import ToolRegistry


def _deepseek_adapter(tmp_path, *, model="deepseek-v4-flash", engine="deepseek-api"):
    cfg = SimpleNamespace(
        name="ying",
        engine=engine,
        model=model,
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    adapter = DeepSeekAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter.tool_registry = None
    return adapter


def _request(request_ref: str = "req-fallback") -> StageRequest:
    return StageRequest(
        turn_id="turn-fallback",
        request_ref=request_ref,
        invocation_id="invocation-fallback",
        stage=Stage.EXECUTION,
        role="primary",
        attempt=1,
        goal="Exercise fallback.",
        classification=None,
        effort=Effort.HIGH,
        allow_tools=True,
    )


def _checkpoint_registry(tmp_path, request):
    base = ToolRegistry(
        ["file_write"],
        tmp_path,
        tmp_path,
        {},
        audit_context={"request_id": request.request_ref},
    )
    return _EvidenceRecordingToolRegistry(
        base,
        request,
        audit_log=DurableAuditLog(
            tmp_path / "recovery.jsonl", tmp_path / "fallback.jsonl"
        ),
    )


class _ScriptedPort:
    """Deterministic consumer stand-in that still drives a real target adapter."""

    def __init__(self, target_adapter, *, allowed=True, disposition=None, reason=None):
        self.target_adapter = target_adapter
        self.evaluations = []
        self.target_calls = []
        self.allowed = allowed
        self.disposition = disposition or ("ready" if allowed else "disabled")
        self.reason = reason if reason is not None else (None if allowed else "disabled")

    def evaluate_provider_fallback(self, **kwargs):
        self.evaluations.append(kwargs)
        return {
            "allowed": self.allowed,
            "disposition": self.disposition,
            "reason": self.reason,
            "target_label": "openrouter-api/deepseek/deepseek-v4-flash",
            "mode": "auto",
            "additional_calls_limit": 1,
            "additional_calls_used": 0,
        }

    async def continue_on_target(
        self, target_label, *, messages, request_id, on_stream_event, **kwargs
    ):
        self.target_calls.append(
            {
                "target": target_label,
                "messages": json.dumps(messages, sort_keys=True, default=str),
                "request_id": request_id,
            }
        )
        return await self.target_adapter.continue_generation(
            messages, request_id, on_stream_event=on_stream_event
        )


class _CrossEnginePolicy:
    """Stand-in for a policy which refuses cross-engine replay after activity."""

    def __init__(self):
        self.evaluations = []
        self.targets = []

    def evaluate_provider_fallback(self, **facts):
        self.evaluations.append(facts)
        blocked = facts["visible_partial_answer"] or facts["tool_activity"]
        return {
            "allowed": not blocked,
            "disposition": "blocked" if blocked else "ready",
            "reason": "activity_already_observed" if blocked else None,
            "target_label": "another-engine/model",
            "mode": "auto",
            "additional_calls_limit": 1,
        }

    async def continue_on_target(self, target, **kwargs):
        self.targets.append(target)
        return BackendResponse(text="fallback answer", is_success=True, duration_ms=1)


@pytest.mark.asyncio
async def test_hashi_api_audit_reports_actual_same_target_retry(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    attempts = 0

    async def flaky(payload, headers, on_stream_event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("no response")
        return _APIResult("done", None, "stop", prompt_tokens=10, completion_tokens=3, cost_usd=0.025)

    monkeypatch.setattr(adapter, "_call_api_once", flaky)
    response = await adapter.generate_response("hello", "req-retry")
    assert response.is_success is True
    assert attempts == 2
    calls = response.stream_metadata["meter"]["provider_calls"]
    assert calls[0]["decision"] == "retry_unfinished_provider_call"
    assert calls[0]["cost_usd"] is None
    assert calls[1]["decision_success"] is True
    assert calls[1]["cost_usd"] == pytest.approx(0.025)
    assert response.cost_usd is None  # The failed physical call may also have incurred cost.
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_openrouter_retry_keeps_unknown_failed_attempt_cost(monkeypatch, tmp_path):
    adapter = _deepseek_adapter(tmp_path)
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    attempts = 0

    async def flaky(payload, headers, on_stream_event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("no response")
        return _APIResult("done", None, "stop", prompt_tokens=10, completion_tokens=3, cost_usd=0.025)

    monkeypatch.setattr(adapter, "_call_api_once", flaky)
    response = await adapter.generate_response("hello", "req-retry")
    assert response.is_success is True
    assert attempts == 2
    calls = response.stream_metadata["meter"]["provider_calls"]
    assert calls[0]["cost_usd"] is None
    assert calls[1]["cost_usd"] == pytest.approx(0.025)
    assert response.cost_usd is None
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_hashi_api_reports_visible_partial_to_fallback_policy(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    port = _CrossEnginePolicy()
    adapter.set_provider_fallback_port(port)
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_LIMIT = 0

    async def interrupted(payload, headers, on_stream_event):
        await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary="partial answer"))
        raise httpx.ReadTimeout("stream interrupted")

    monkeypatch.setattr(adapter, "_stream_api_once", interrupted)
    events = []

    async def capture(event):
        events.append(event.summary)
        return True

    response = await adapter.generate_response("hello", "req-partial", on_stream_event=capture)
    assert port.evaluations[0]["visible_partial_answer"] is True
    assert port.targets == []
    assert response.is_success is False
    assert response.stream_metadata["provider_fallback"]["reason"] == "activity_already_observed"
    assert events == ["partial answer"]
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_internal_stream_text_does_not_falsely_block_fallback(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    port = _CrossEnginePolicy()
    adapter.set_provider_fallback_port(port)
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_LIMIT = 0

    async def interrupted(payload, headers, on_stream_event):
        await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary="internal draft"))
        raise httpx.ReadTimeout("stream interrupted")

    async def internal_only(event):
        return False

    monkeypatch.setattr(adapter, "_stream_api_once", interrupted)
    response = await adapter.generate_response("hello", "req-internal", on_stream_event=internal_only)
    assert port.evaluations[0]["visible_partial_answer"] is False
    assert port.targets == ["another-engine/model"]
    assert response.is_success is True
    await adapter.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_openrouter_fallback_receives_preview_delivery_truth(monkeypatch, tmp_path, delivered):
    adapter = _deepseek_adapter(tmp_path)
    adapter.TRANSIENT_PROVIDER_CALL_RETRIES = 0
    port = _CrossEnginePolicy()
    adapter.set_provider_fallback_port(port)

    async def interrupted(payload, headers, on_stream_event):
        await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary="draft"))
        raise httpx.ReadTimeout("stream interrupted")

    async def capture(event):
        return delivered

    monkeypatch.setattr(adapter, "_stream_api_once", interrupted)
    response = await adapter.generate_response("hello", "req-deepseek", on_stream_event=capture)
    assert port.evaluations[0]["visible_partial_answer"] is delivered
    assert bool(port.targets) is not delivered
    assert response.is_success is not delivered
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_checkpoint_retry_does_not_replace_a_delivered_partial_answer(monkeypatch, tmp_path):
    adapter = _deepseek_adapter(tmp_path)
    registry = _checkpoint_registry(tmp_path, _request())
    adapter.tool_registry = registry
    adapter.TRANSIENT_PROVIDER_CALL_RETRIES = 1
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    port = _CrossEnginePolicy()
    adapter.set_provider_fallback_port(port)
    attempts = 0

    async def interrupted(payload, headers, on_stream_event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _APIResult("", [{
                "id": "write-once", "type": "function",
                "function": {"name": "file_write", "arguments": json.dumps({
                    "path": "once.txt", "content": "only one write"
                })},
            }], "tool_calls")
        if attempts == 2:
            await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary="visible partial"))
            raise httpx.ReadTimeout("stream interrupted after visible answer")
        return _APIResult("replacement answer", None, "stop")

    async def delivered(event):
        return True

    monkeypatch.setattr(adapter, "_stream_api_once", interrupted)
    response = await adapter.generate_response("save once", "req-fallback", on_stream_event=delivered)
    assert attempts == 2
    assert len(registry.receipts) == 1
    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "only one write"
    assert port.evaluations[0]["visible_partial_answer"] is True
    assert port.targets == []
    assert response.is_success is False
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_hashi_checkpoint_fallback_continues_without_reexecuting_write(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    registry = _checkpoint_registry(tmp_path, _request())
    adapter.tool_registry = registry
    target = _deepseek_adapter(tmp_path, model="deepseek/deepseek-v4-flash", engine="openrouter-api")
    port = _ScriptedPort(target)
    adapter.set_provider_fallback_port(port)
    attempts = 0

    async def source_call(payload, headers, on_stream_event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _APIResult("", [{
                "id": "write-once", "type": "function",
                "function": {"name": "file_write", "arguments": json.dumps({
                    "path": "once.txt", "content": "only one write"
                })},
            }], "tool_calls")
        raise httpx.ReadTimeout("source failed after completed write")

    async def target_call(payload, headers, on_stream_event):
        return _APIResult("continued safely", None, "stop", prompt_tokens=10, completion_tokens=3, cost_usd=0.01)

    monkeypatch.setattr(adapter, "_call_api_once", source_call)
    monkeypatch.setattr(target, "_call_api_once", target_call)
    response = await adapter.generate_response("save once", "req-fallback")
    assert response.is_success is True, response.error
    assert response.text == "continued safely"
    assert response.cost_usd is None  # The interrupted source call may have been billable.
    assert port.evaluations[0]["tool_activity"] is True
    assert port.evaluations[0]["checkpoint_safe"] is True
    assert len(port.target_calls) == 1
    assert "write-once" in port.target_calls[0]["messages"]
    assert len(registry.receipts) == 1
    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "only one write"
    await adapter.shutdown()
    await target.shutdown()


@pytest.mark.asyncio
async def test_hashi_api_reports_real_completed_write_to_fallback_policy(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    registry = _checkpoint_registry(tmp_path, _request())
    adapter.tool_registry = registry
    port = _CrossEnginePolicy()
    adapter.set_provider_fallback_port(port)
    attempts = 0

    async def interrupted_after_tool(payload, headers, on_stream_event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            call = {
                "id": "write-once", "type": "function",
                "function": {"name": "file_write", "arguments": json.dumps({
                    "path": "once.txt", "content": "only one write"
                })},
            }
            return _APIResult("", [call], "tool_calls")
        raise httpx.ReadTimeout("stream interrupted after write")

    monkeypatch.setattr(adapter, "_call_api_once", interrupted_after_tool)
    response = await adapter.generate_response("save once", "req-fallback")
    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "only one write"
    assert len(registry.receipts) == 1
    assert port.evaluations[0]["tool_activity"] is True
    assert port.targets == []
    assert response.is_success is False
    assert attempts == 2
    await adapter.shutdown()
# HASHI-Fingerprint: hfp_b757741c271248df808439b53ef77401
