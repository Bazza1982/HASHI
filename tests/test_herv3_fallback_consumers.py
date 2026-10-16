"""Focused consumer tests for the HERV3/Codex CLI consented fallback.

The adapter observations drive the real DeepSeek/OpenRouter adapters with
deterministic fake HTTP, so a fallback target switch is proven by the actual
invoked adapter and its provider-call records -- never by a copied flag.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from adapters.deepseek_api import DeepSeekAdapter
from adapters.her_v2_provider import (
    _EvidenceRecordingToolRegistry,
    _Herv3ProviderFallbackPort,
)
from adapters.openrouter_api import _APIResult
from orchestrator import model_fallback_policy as policy_owner
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.models import Effort, Stage, StageRequest
from orchestrator.model_fallback_policy import (
    MODE_ASK,
    MODE_AUTO,
    SOURCE_CODEX_CLI,
    TARGET_KIND_CODEX_CLI,
    TARGET_KIND_HERV3_MODEL,
    FallbackPolicy,
    FallbackRoute,
    FallbackSourceRule,
    read_policy,
    write_policy,
)
from orchestrator.privacy_levels import PrivacyLevel
from tools.registry import ToolRegistry, ToolResult

HERV3_ROUTE = FallbackRoute(
    source=policy_owner.SOURCE_HERV3,
    target_kind=TARGET_KIND_HERV3_MODEL,
    provider="openrouter-api",
    model="deepseek/deepseek-v4-flash",
    same_model=True,
)
CODEX_ROUTE = FallbackRoute(
    source=policy_owner.SOURCE_HERV3,
    target_kind=TARGET_KIND_CODEX_CLI,
)


def _adapter(tmp_path, *, model="deepseek-v4-flash", engine="deepseek-api"):
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


async def _run_adapter_fallback(monkeypatch, tmp_path, port, *, meaningful=True):
    """Execute one adapter call that exhausts same-target recovery and falls back."""

    adapter = _adapter(tmp_path)
    adapter.TRANSIENT_PROVIDER_CALL_RETRIES = 1
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    request = _request()
    registry = _checkpoint_registry(tmp_path, request)
    adapter.tool_registry = registry
    executed = []
    real_execute = registry.execute

    async def execute_once(tool_name, arguments, tool_call_id=""):
        executed.append(tool_name)
        return await real_execute(tool_name, arguments, tool_call_id)

    monkeypatch.setattr(registry, "execute", execute_once)
    adapter.set_provider_fallback_port(port)
    seen = []
    tool_calls = [
        {
            "id": "write-once",
            "type": "function",
            "function": {
                "name": "file_write",
                "arguments": json.dumps(
                    {"path": "once.txt", "content": "saved once"}
                ),
            },
        }
    ]

    async def fake_stream(payload, headers, on_stream_event):
        seen.append(json.dumps(payload["messages"], sort_keys=True, default=str))
        if len(seen) == 1:
            return _APIResult("", tool_calls, "tool_calls")
        raise policy_owner_stream_timeout()

    monkeypatch.setattr(adapter, "_stream_api_once", fake_stream)

    async def capture(_event):
        return None

    response = await adapter.generate_response(
        "save once", "req-fallback", on_stream_event=capture
    )
    return response, executed, seen


def policy_owner_stream_timeout():
    from adapters.openrouter_api import _ProviderStreamInactivityGuard

    guard = _ProviderStreamInactivityGuard(timeout_s=300.0)
    guard.meaningful_response_started = True
    guard.waited_without_meaningful_s = guard.timeout_s
    return guard._timeout_error(SimpleNamespace())


@pytest.mark.asyncio
async def test_herv3_same_model_fallback_continues_without_replaying_tools(
    monkeypatch, tmp_path
):
    target = _adapter(tmp_path, model="deepseek/deepseek-v4-flash", engine="openrouter-api")
    target_seen = []

    async def target_stream(payload, headers, on_stream_event):
        target_seen.append(json.dumps(payload["messages"], sort_keys=True, default=str))
        return _APIResult("continued final answer", None, "stop")

    monkeypatch.setattr(target, "_stream_api_once", target_stream)
    port = _ScriptedPort(target)
    response, executed, seen = await _run_adapter_fallback(monkeypatch, tmp_path, port)

    assert response.is_success is True, response.error
    assert response.text == "continued final answer"
    # The tool executed exactly once and was never replayed by the handover.
    assert executed == ["file_write"]
    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "saved once"
    # The continuation reuses the exact assembled messages.
    assert port.target_calls and port.target_calls[0]["target"] == (
        "openrouter-api/deepseek/deepseek-v4-flash"
    )
    assert port.target_calls[0]["messages"] == seen[-1]
    assert "write-once" in port.target_calls[0]["messages"]
    # The policy decision was asked with proven facts.
    assert port.evaluations[0]["tool_activity"] is True
    assert port.evaluations[0]["checkpoint_safe"] is True
    # One additional paid call is recorded, with source, target, and reason.
    record = response.stream_metadata["provider_fallback"]
    assert record["attempted"] is True
    assert record["disposition"] == "attempted-success"
    assert record["target"] == "openrouter-api/deepseek/deepseek-v4-flash"
    assert record["source"] == "deepseek-api/deepseek-v4-flash"
    assert record["additional_calls"] == 1
    assert record["primary_failure"]["code"] == "PROVIDER_STREAM_IDLE_TIMEOUT"
    # Provider-call records keep every physical call from both targets.
    calls = response.stream_metadata["meter"]["provider_calls"]
    assert len(calls) == 4
    assert calls[-1]["recovery_kind"] == "provider_fallback_same_model"
    assert target_seen, "the real target adapter issued the continued request"


@pytest.mark.asyncio
async def test_herv3_fallback_blocked_reports_the_first_reason(monkeypatch, tmp_path):
    target = _adapter(tmp_path, model="deepseek/deepseek-v4-flash", engine="openrouter-api")
    port = _ScriptedPort(target, allowed=False, disposition="disabled", reason="disabled")
    response, executed, _seen = await _run_adapter_fallback(
        monkeypatch, tmp_path, port
    )

    assert response.is_success is False
    assert executed == ["file_write"]
    assert port.target_calls == []
    record = response.stream_metadata["provider_fallback"]
    assert record["attempted"] is False
    assert record["disposition"] == "disabled"
    assert record["reason"] == "disabled"
    assert record["additional_calls"] == 0


class _StubManager:
    def __init__(self, options):
        self.privacy_level = int(PrivacyLevel.PROVIDER_TRUST)
        self._options = list(options)

    def get_her_v2_provider_options(self):
        return [dict(item) for item in self._options]


class _StubBridge:
    def __init__(self, runtime, manager):
        self.runtime_context = runtime
        self.backend_manager = manager
        self.budget = {}

    def _fallback_budget_key(self, request):
        return str(request.request_ref)

    def _fallback_budget_used(self, key):
        return int(self.budget.get(key, 0))

    def _consume_fallback_budget(self, key):
        self.budget[key] = int(self.budget.get(key, 0)) + 1
        return self.budget[key]


def _runtime(tmp_path):
    return SimpleNamespace(
        global_config=SimpleNamespace(bridge_home=tmp_path),
    )


def _model_options(*, openrouter=True, codex=True):
    options = []
    if openrouter:
        options.append(
            {
                "name": "openrouter",
                "engine": "openrouter-api",
                "label": "openrouter",
                "available": True,
                "models": ["deepseek/deepseek-v4-flash"],
            }
        )
    if codex:
        options.append(
            {
                "name": "codex",
                "engine": "codex-cli",
                "label": "codex",
                "available": True,
                "models": [],
            }
        )
    return options


def _save_policy(tmp_path, actor, policy):
    runtime = _runtime(tmp_path)
    _saved, document = read_policy(runtime, actor, for_update=True)
    write_policy(runtime, actor, policy, document=document)


def test_bridge_port_consumes_the_real_policy_and_defaults_off(
    tmp_path, monkeypatch
):
    from adapters import her_v2_provider as bridge_module

    monkeypatch.setattr(bridge_module, "_fallback_actor", lambda runtime: "tester")
    request = _request("req-bridge")
    profile = SimpleNamespace(engine="deepseek-api", model="deepseek-v4-flash")

    # No configured policy: capability alone never enables fallback.
    manager = _StubManager(_model_options())
    port = _Herv3ProviderFallbackPort(
        _StubBridge(_runtime(tmp_path), manager), request, profile, purpose="continuation"
    )
    decision = port.evaluate_provider_fallback(
        source_provider="deepseek-api",
        source_model="deepseek-v4-flash",
        tool_activity=True,
        checkpoint_safe=True,
        visible_partial_answer=False,
        primary_failure={"code": "PROVIDER_SERVER_ERROR"},
    )
    assert decision["allowed"] is False
    assert decision["disposition"] == "disabled"
    assert decision["reason"] == "disabled"

    # An explicit automatic policy with the same-model route and a ready key
    # resolves to the authorised target.
    _save_policy(
        tmp_path,
        "tester",
        FallbackPolicy(mode=MODE_AUTO, routes=(HERV3_ROUTE,), max_additional_calls=1, allow_unknown_cost=True),
    )
    decision = port.evaluate_provider_fallback(
        source_provider="deepseek-api",
        source_model="deepseek-v4-flash",
        tool_activity=True,
        checkpoint_safe=True,
        visible_partial_answer=False,
        primary_failure={"code": "PROVIDER_SERVER_ERROR"},
    )
    assert decision["allowed"] is True
    assert decision["target_label"] == "openrouter-api/deepseek/deepseek-v4-flash"

    # A configured route whose key/runtime is missing is locked, not used.
    locked_manager = _StubManager(_model_options(openrouter=False))
    locked = _Herv3ProviderFallbackPort(
        _StubBridge(_runtime(tmp_path), locked_manager),
        request,
        profile,
        purpose="continuation",
    )
    decision = locked.evaluate_provider_fallback(
        source_provider="deepseek-api",
        source_model="deepseek-v4-flash",
        tool_activity=True,
        checkpoint_safe=True,
        visible_partial_answer=False,
        primary_failure={"code": "PROVIDER_SERVER_ERROR"},
    )
    assert decision["allowed"] is False
    assert decision["reason"] == "missing_key_or_runtime"


def test_bridge_stage_switch_is_pre_effect_and_budget_bounded(tmp_path, monkeypatch):
    from adapters import her_v2_provider as bridge_module

    monkeypatch.setattr(bridge_module, "_fallback_actor", lambda runtime: "tester")
    _save_policy(
        tmp_path,
        "tester",
        FallbackPolicy(
            mode=MODE_AUTO,
            routes=(CODEX_ROUTE,),
            max_additional_calls=1,
            allow_unknown_cost=True,
            sources={
                policy_owner.SOURCE_HERV3: FallbackSourceRule(
                    enabled=True, allow_codex_cli_targets=True
                )
            },
        ),
    )
    request = _request("req-stage")
    bridge = _StubBridge(_runtime(tmp_path), _StubManager(_model_options()))
    profile = SimpleNamespace(engine="deepseek-api", model="deepseek-v4-flash")
    resolve = bridge_module.HashiStageProvider.resolve_stage_provider_fallback

    result = resolve(
        bridge,
        request=request,
        selected=profile,
        tool_started=False,
        visible_partial_answer=False,
    )
    assert result["allowed"] is True
    assert result["target_kind"] == TARGET_KIND_CODEX_CLI
    assert result["target_label"] == "codex-cli"
    # The explicit additional-call budget entry is consumed before the switch.
    assert bridge.budget["req-stage"] == 1

    # A second switch cannot exceed the configured additional-call limit.
    exhausted = resolve(
        bridge,
        request=request,
        selected=profile,
        tool_started=False,
        visible_partial_answer=False,
    )
    assert exhausted["allowed"] is False
    assert exhausted["reason"] == "budget_exhausted"

    # After any Tool execution the cross-engine switch is blocked.
    follow_up_request = _request("req-stage-2")
    blocked = resolve(
        bridge,
        request=follow_up_request,
        selected=profile,
        tool_started=True,
        visible_partial_answer=False,
    )
    assert blocked["allowed"] is False
    assert blocked["reason"] == "unsafe_checkpoint"

    # Phone, call, and voice requests never reach target selection.
    phone_request = _request("req-stage-3")
    object.__setattr__(phone_request, "context", {"request_mode": "phone"})
    excluded = resolve(
        bridge,
        request=phone_request,
        selected=profile,
        tool_started=False,
        visible_partial_answer=False,
    )
    assert excluded["allowed"] is False
    assert excluded["reason"] == "modality_excluded"