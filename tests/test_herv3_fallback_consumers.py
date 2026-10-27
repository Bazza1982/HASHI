"""Focused consumer tests for the HERV3/Codex CLI consented fallback.

The adapter observations drive the real DeepSeek/OpenRouter adapters with
deterministic fake HTTP, so a fallback target switch is proven by the actual
invoked adapter and its provider-call records -- never by a copied flag.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from adapters.base import BackendResponse
from adapters.deepseek_api import DeepSeekAdapter
from adapters.her_v2_provider import (
    _EvidenceRecordingToolRegistry,
    _Herv3ProviderFallbackPort,
    HashiStageProvider,
    Herv3ProviderContinuationContract,
)
from adapters.openrouter_api import _APIResult
from orchestrator import model_fallback_policy as policy_owner
from adapters.openrouter_api import ProviderToolReplayError
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.models import Effort, Stage, StageRequest
from orchestrator.multimodal_contract import canonical_request_content
from orchestrator.model_fallback_policy import (
    MODE_AUTO,
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
SECOND_HERV3_ROUTE = FallbackRoute(
    source=policy_owner.SOURCE_HERV3,
    target_kind=TARGET_KIND_HERV3_MODEL,
    provider="openrouter-api",
    model="deepseek/deepseek-v4-pro",
)
CODEX_ROUTE = FallbackRoute(
    source=policy_owner.SOURCE_HERV3,
    target_kind=TARGET_KIND_CODEX_CLI,
    model="gpt-6-sol",
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


@pytest.mark.asyncio
async def test_herv3_capacity_retry_consumes_the_shared_per_source_policy(tmp_path):
    runtime = SimpleNamespace(
        global_config=SimpleNamespace(bridge_home=tmp_path, authorized_id=0),
    )
    manager = SimpleNamespace(privacy_level=int(PrivacyLevel.PROVIDER_TRUST))
    provider = HashiStageProvider(
        backend_manager=manager,
        runtime_context=runtime,
    )
    _old, document = read_policy(runtime, "user:0", for_update=True)
    write_policy(
        runtime,
        "user:0",
        FallbackPolicy(
            mode=policy_owner.MODE_RETRY,
            sources={
                policy_owner.SOURCE_HERV3: FallbackSourceRule(
                    side_effect_mode=policy_owner.EFFECT_RESUME
                ),
                policy_owner.SOURCE_CODEX_CLI: FallbackSourceRule(
                    side_effect_mode=policy_owner.EFFECT_PAUSE
                ),
            },
        ),
        document=document,
    )

    decision = await provider.resolve_stage_capacity_retry(
        request=_request("req-capacity-policy"),
        selected=SimpleNamespace(engine="deepseek-api", model="deepseek-v4-flash"),
        failure_code="PROVIDER_CAPACITY_UNAVAILABLE",
        retryable=True,
        retry_after_s=90,
        retry_index=0,
        side_effects_possible=True,
    )

    assert decision["allowed"] is True
    assert decision["delay_s"] == 90
    assert decision["continue_session"] is True
    assert decision["source"] == policy_owner.SOURCE_HERV3

    # The policy source is the selected outer Engine, not HERV3's current
    # internal model Provider.  A Codex provider inside one HERV3 Turn must
    # therefore keep using HERV3's independently configured A/B choice.
    codex_provider_decision = await provider.resolve_stage_capacity_retry(
        request=_request("req-capacity-policy-codex-provider"),
        selected=SimpleNamespace(engine="codex-cli", model="gpt-6-sol"),
        failure_code="PROVIDER_CAPACITY_UNAVAILABLE",
        retryable=True,
        retry_after_s=None,
        retry_index=0,
        side_effects_possible=True,
    )
    assert codex_provider_decision["allowed"] is True
    assert codex_provider_decision["continue_session"] is True
    assert codex_provider_decision["source"] == policy_owner.SOURCE_HERV3


@pytest.mark.asyncio
@pytest.mark.parametrize("request_mode", ["call", "phone", "voice"])
async def test_herv3_capacity_retry_excludes_live_request_modes(
    tmp_path, request_mode
):
    runtime = SimpleNamespace(
        global_config=SimpleNamespace(bridge_home=tmp_path, authorized_id=0),
    )
    provider = HashiStageProvider(
        backend_manager=SimpleNamespace(
            privacy_level=int(PrivacyLevel.PROVIDER_TRUST)
        ),
        runtime_context=runtime,
    )
    _old, document = read_policy(runtime, "user:0", for_update=True)
    write_policy(
        runtime,
        "user:0",
        FallbackPolicy(mode=policy_owner.MODE_RETRY),
        document=document,
    )
    request = _request(f"req-capacity-{request_mode}")
    object.__setattr__(request, "context", {"request_mode": request_mode})

    decision = await provider.resolve_stage_capacity_retry(
        request=request,
        selected=SimpleNamespace(
            engine="deepseek-api", model="deepseek-v4-flash"
        ),
        failure_code=policy_owner.CAPACITY_FAILURE_CODE,
        retryable=True,
        retry_after_s=600,
        retry_index=0,
        side_effects_possible=False,
    )

    assert decision["allowed"] is False
    assert decision["delay_s"] is None
    assert decision["blocking_reason"] == policy_owner.REASON_MODALITY_EXCLUDED


@pytest.mark.asyncio
async def test_herv3_capacity_retry_excludes_semantic_voice_origin(tmp_path):
    runtime = SimpleNamespace(
        global_config=SimpleNamespace(bridge_home=tmp_path, authorized_id=0),
    )
    provider = HashiStageProvider(
        backend_manager=SimpleNamespace(
            privacy_level=int(PrivacyLevel.PROVIDER_TRUST)
        ),
        runtime_context=runtime,
    )
    _old, document = read_policy(runtime, "user:0", for_update=True)
    write_policy(
        runtime,
        "user:0",
        FallbackPolicy(mode=policy_owner.MODE_RETRY),
        document=document,
    )
    request = _request("req-capacity-voice-origin")
    object.__setattr__(
        request,
        "request_content",
        canonical_request_content(
            [
                {
                    "type": "media",
                    "item_index": 1,
                    "attachment_id": "att-voice-origin",
                    "modality": "audio",
                    "kind": "voice",
                    "semantic_role": "voice_message",
                    "mime_type": "audio/wav",
                    "filename": "voice.wav",
                    "caption": "",
                    "local_ref": str(tmp_path / "voice.wav"),
                    "size_bytes": 5,
                    "sha256": "0" * 64,
                    "transport": {},
                }
            ]
        ),
    )

    decision = await provider.resolve_stage_capacity_retry(
        request=request,
        selected=SimpleNamespace(
            engine="deepseek-api", model="deepseek-v4-flash"
        ),
        failure_code=policy_owner.CAPACITY_FAILURE_CODE,
        retryable=True,
        retry_after_s=600,
        retry_index=0,
        side_effects_possible=False,
    )

    assert decision["allowed"] is False
    assert decision["delay_s"] is None
    assert decision["blocking_reason"] == policy_owner.REASON_MODALITY_EXCLUDED


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


class _ChainedPort:
    def __init__(self):
        self.evaluations = []
        self.targets = []

    def evaluate_provider_fallback(self, **kwargs):
        self.evaluations.append(kwargs)
        index = len(self.evaluations) - 1
        decisions = (
            {
                "allowed": True,
                "disposition": "ready",
                "reason": None,
                "target_label": "openrouter-api/first-model",
                "route_key": "herv3|herv3_model|openrouter-api|first-model",
                "mode": "auto",
                "additional_calls_limit": 2,
                "additional_calls_used": 0,
            },
            {
                "allowed": True,
                "disposition": "ready",
                "reason": None,
                "target_label": "xai-api/second-model",
                "route_key": "herv3|herv3_model|xai-api|second-model",
                "mode": "auto",
                "additional_calls_limit": 2,
                "additional_calls_used": 1,
            },
        )
        if index < len(decisions):
            return decisions[index]
        return {
            "allowed": False,
            "disposition": "unavailable",
            "reason": "no_allowed_target",
            "target_label": "",
            "mode": "auto",
            "additional_calls_limit": 2,
            "additional_calls_used": 2,
        }

    async def continue_on_target(self, target_label, **kwargs):
        del kwargs
        self.targets.append(target_label)
        if len(self.targets) == 1:
            return BackendResponse(
                text="",
                duration_ms=5.0,
                error="first target failed",
                is_success=False,
                error_code="PROVIDER_SERVER_ERROR",
                error_retryable=True,
                stream_metadata={
                    "meter": {
                        "provider_calls": [
                            {"provider": "openrouter-api", "model": "first-model"}
                        ]
                    }
                },
            )
        return BackendResponse(
            text="second target worked",
            duration_ms=7.0,
            is_success=True,
            stream_metadata={
                "meter": {
                    "provider_calls": [
                        {"provider": "xai-api", "model": "second-model"}
                    ]
                }
            },
        )


@pytest.mark.asyncio
async def test_fallback_chain_uses_next_ordered_route_once(tmp_path):
    adapter = _adapter(tmp_path)
    port = _ChainedPort()
    adapter.set_provider_fallback_port(port)
    response = await adapter._attempt_provider_fallback(
        policy_owner_stream_timeout(),
        messages=[{"role": "user", "content": "continue"}],
        request_id="req-chain",
        on_stream_event=None,
        effect_recovery={
            "executed_tool_count": 0,
            "safe_current_call_continuation": True,
        },
        provider_calls=[{"provider": "deepseek-api", "model": "source"}],
        provider_transport_retry_count=0,
        total_local_recovery_requests=0,
        total_tool_calls=0,
        tool_loop_count=0,
    )

    assert response is not None and response.is_success is True
    assert response.text == "second target worked"
    assert port.targets == [
        "openrouter-api/first-model",
        "xai-api/second-model",
    ]
    record = response.stream_metadata["provider_fallback"]
    assert record["additional_calls"] == 2
    assert [item["disposition"] for item in record["attempts"]] == [
        "attempted-failed",
        "attempted-success",
    ]
    assert len(response.stream_metadata["meter"]["provider_calls"]) == 3


@pytest.mark.asyncio
async def test_pre_call_fallback_failure_does_not_report_a_paid_call(tmp_path):
    class PreCallFailurePort:
        def __init__(self):
            self.evaluations = 0

        def evaluate_provider_fallback(self, **kwargs):
            del kwargs
            self.evaluations += 1
            if self.evaluations == 1:
                return {
                    "allowed": True,
                    "disposition": "ready",
                    "reason": None,
                    "target_label": "openrouter-api/target-model",
                    "route_key": "herv3|herv3_model|openrouter-api|target-model",
                    "mode": "auto",
                    "additional_calls_limit": 1,
                    "additional_calls_used": 0,
                }
            return {
                "allowed": False,
                "disposition": "unavailable",
                "reason": "no_allowed_target",
                "target_label": "",
                "mode": "auto",
                "additional_calls_limit": 1,
                "additional_calls_used": 0,
            }

        async def continue_on_target(self, target_label, **kwargs):
            del target_label, kwargs
            raise RuntimeError("target initialization failed before request")

    adapter = _adapter(tmp_path)
    adapter.set_provider_fallback_port(PreCallFailurePort())
    failure = policy_owner_stream_timeout()

    response = await adapter._attempt_provider_fallback(
        failure,
        messages=[{"role": "user", "content": "continue"}],
        request_id="req-pre-call-failure",
        on_stream_event=None,
        effect_recovery={
            "executed_tool_count": 0,
            "safe_current_call_continuation": True,
        },
        provider_calls=[],
        provider_transport_retry_count=0,
        total_local_recovery_requests=0,
        total_tool_calls=0,
        tool_loop_count=0,
    )

    assert response is None
    record = failure.hashi_provider_fallback
    assert record["attempted"] is True
    assert record["additional_calls"] == 0
    assert len(record["attempts"]) == 1


@pytest.mark.asyncio
async def test_real_fallback_target_keeps_its_normal_continuation_loop(
    monkeypatch, tmp_path
):
    target = _adapter(
        tmp_path,
        model="deepseek/deepseek-v4-flash",
        engine="openrouter-api",
    )
    target.TRANSIENT_PROVIDER_CALL_RETRIES = 3
    target.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    physical_calls = []

    async def initialize():
        return True

    async def shutdown():
        return None

    async def fail_stream(payload, headers, on_stream_event):
        physical_calls.append(payload)
        raise httpx.ConnectError(
            "target unavailable",
            request=httpx.Request("POST", "https://fallback.invalid/v1/chat"),
        )

    monkeypatch.setattr(target, "initialize", initialize)
    monkeypatch.setattr(target, "shutdown", shutdown)
    monkeypatch.setattr(target, "_stream_api_once", fail_stream)

    class Manager:
        privacy_level = int(PrivacyLevel.PROVIDER_TRUST)

        def create_ephemeral_backend(self, engine, target_model=None):
            assert engine == "openrouter-api"
            assert target_model == "deepseek/deepseek-v4-flash"
            return target

    provider = HashiStageProvider(backend_manager=Manager())
    request = _request("req-paid-cap")
    port = _Herv3ProviderFallbackPort(
        provider,
        request,
        SimpleNamespace(reasoning=None),
        purpose="continuation",
    )

    async def capture(_event):
        return None

    response = await port.continue_on_target(
        "openrouter-api/deepseek/deepseek-v4-flash",
        messages=[{"role": "user", "content": "continue"}],
        request_id="req-paid-cap",
        on_stream_event=capture,
        source_backend=_adapter(tmp_path),
        route_key=HERV3_ROUTE.key(),
        additional_calls_limit=1,
    )

    assert response.is_success is False
    assert len(physical_calls) == 4
    assert provider._fallback_budget_used("req-paid-cap") == 1
    assert len(response.stream_metadata["meter"]["provider_calls"]) == 4


def test_herv3_failure_report_says_retry_already_failed():
    from adapters.her_v2_provider import _backend_response_error
    from orchestrator import runtime_delivery, runtime_pipeline

    wrapped = _backend_response_error(
        BackendResponse(
            text="",
            duration_ms=10.0,
            error="provider still unavailable",
            is_success=False,
            error_code="PROVIDER_SERVER_ERROR",
            error_retryable=False,
            stream_metadata={
                "provider_transport_retry_count": 1,
                "provider_local_recovery_count": 1,
                "provider_local_recovery_limit": 1,
                "provider_turn_recovery_total": 2,
            },
        ),
        fallback="provider failed",
    )
    assert wrapped.details["provider_transport_retry_count"] == 1
    final = BackendResponse(
        text="",
        duration_ms=10.0,
        error=str(wrapped),
        is_success=False,
        error_code="PROVIDER_SERVER_ERROR",
        stream_metadata={
            "her_v2": {
                "failure_chain": {
                    "primary_failure": {"details": dict(wrapped.details)}
                }
            }
        },
    )
    fields = runtime_pipeline.backend_failure_fields(final)
    report = runtime_delivery.build_public_failure(
        "her-v3",
        str(wrapped),
        locale="zh-CN",
        error_context=fields,
    )
    assert "已重试 1 次，仍然失败。" in report["text"]
    assert "整轮累计恢复动作：2 次。" in report["text"]


def test_continuation_contract_preserves_request_semantics():
    observer = object()
    registry = object()
    source = SimpleNamespace(
        privacy_level=0,
        tool_registry=registry,
        _provider_call_observer=observer,
        _provider_invocation_context={"request": {"id": "req-contract"}},
        reasoning_enabled=False,
        config=SimpleNamespace(
            extra={
                "reasoning_effort": "high",
                "temperature": 0.2,
                "max_output_tokens": 2048,
            }
        ),
        _her_v2_stream_inactivity_timeout_s=17.0,
        _herv3_privacy_scope=True,
    )
    applied = {}
    target = SimpleNamespace(
        privacy_level=2,
        tool_registry=None,
        config=SimpleNamespace(extra={"target_only": True}),
        _herv3_privacy_scope=False,
        set_provider_call_observer=lambda value: applied.update(observer=value),
        set_provider_invocation_context=lambda value: applied.update(context=value),
        set_reasoning_enabled=lambda value: applied.update(reasoning=value),
        set_her_v2_stream_inactivity_timeout=lambda value: applied.update(
            timeout=value
        ),
    )

    contract = Herv3ProviderContinuationContract.from_backend(
        source,
        fallback_privacy_level=1,
    )
    contract.apply(target)

    assert target.privacy_level == 0
    assert target.tool_registry is registry
    assert applied == {
        "observer": observer,
        "context": {"request": {"id": "req-contract"}},
        "reasoning": False,
        "timeout": 17.0,
    }
    assert target.config.extra == {
        "target_only": True,
        "reasoning_effort": "high",
        "temperature": 0.2,
    }
    assert target._herv3_privacy_scope is True


def _tool_call(call_id="tool-1", *, path="one.txt"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "file_write",
            "arguments": json.dumps({"path": path, "content": "once"}),
        },
    }


@pytest.mark.asyncio
async def test_completed_tool_id_reuses_result_and_mutation_is_blocked(
    monkeypatch, tmp_path
):
    adapter = _adapter(tmp_path)
    calls = []

    async def execute(tool_name, arguments, tool_call_id=""):
        calls.append((tool_name, arguments, tool_call_id))
        return ToolResult(tool_call_id=tool_call_id, output="unexpected")

    adapter.tool_registry = SimpleNamespace(execute=execute)
    original = _tool_call()
    completed_messages = [
        {"role": "assistant", "tool_calls": [original]},
        {"role": "tool", "tool_call_id": "tool-1", "content": "saved"},
    ]
    await adapter._run_tool_calls(
        [_tool_call()],
        completed_messages,
        None,
    )
    assert calls == []
    assert completed_messages[-1] == {
        "role": "tool",
        "tool_call_id": "tool-1",
        "content": "saved",
    }

    with pytest.raises(ProviderToolReplayError):
        await adapter._run_tool_calls(
            [_tool_call(path="changed.txt")],
            [
                {"role": "assistant", "tool_calls": [original]},
                {"role": "tool", "tool_call_id": "tool-1", "content": "saved"},
            ],
            None,
        )


@pytest.mark.asyncio
async def test_duplicate_tool_id_is_atomic_within_and_across_batches(
    monkeypatch, tmp_path
):
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(
        adapter,
        "_evaluate_tool_policy",
        lambda *args, **kwargs: SimpleNamespace(allowed=True),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def execute(tool_name, arguments, tool_call_id=""):
        calls.append((tool_name, arguments, tool_call_id))
        started.set()
        await release.wait()
        return ToolResult(tool_call_id=tool_call_id, output="saved")

    adapter.tool_registry = SimpleNamespace(execute=execute)
    first_messages = []
    first = asyncio.create_task(
        adapter._run_tool_calls([_tool_call("atomic")], first_messages, None)
    )
    await started.wait()
    with pytest.raises(ProviderToolReplayError):
        await adapter._run_tool_calls(
            [_tool_call("atomic")],
            [],
            None,
        )
    release.set()
    await first
    assert len(calls) == 1

    calls.clear()
    release.set()
    same_batch_messages = []
    await adapter._run_tool_calls(
        [_tool_call("delayed"), _tool_call("delayed")],
        same_batch_messages,
        None,
    )
    assert len(calls) == 1
    assert [item["tool_call_id"] for item in same_batch_messages] == [
        "delayed",
        "delayed",
    ]


class _StubManager:
    def __init__(self, options):
        self.privacy_level = int(PrivacyLevel.PROVIDER_TRUST)
        self._options = list(options)
        self.config = SimpleNamespace(
            allowed_backends=[
                {
                    "engine": "codex-cli",
                    "model": "gpt-6-sol",
                    "available_models": ["gpt-6-sol"],
                }
            ]
        )

    def get_her_v2_provider_options(self):
        return [dict(item) for item in self._options]


class _StubBridge:
    def __init__(self, runtime, manager):
        self.runtime_context = runtime
        self.backend_manager = manager
        self.budget = {}
        self.cost = {}
        self.routes = {}

    def _fallback_budget_key(self, request):
        return str(request.request_ref)

    def _fallback_budget_used(self, key):
        return int(self.budget.get(key, 0))

    def _fallback_cost_used(self, key):
        return self.cost.get(key, 0.0)

    def _fallback_routes_used(self, key):
        return frozenset(self.routes.get(key, set()))

    def _consume_fallback_budget(
        self,
        key,
        *,
        limit=None,
        route_key="",
        estimated_cost_usd=None,
    ):
        self.budget[key] = int(self.budget.get(key, 0)) + 1
        previous = self.cost.get(key, 0.0)
        self.cost[key] = (
            None
            if previous is None or estimated_cost_usd is None
            else float(previous) + float(estimated_cost_usd)
        )
        if route_key:
            self.routes.setdefault(key, set()).add(route_key)
        return self.budget[key]

    def _mark_fallback_route(self, key, route_key):
        self.routes.setdefault(key, set()).add(route_key)

    def _create_provider_backend(self, engine, *, target_model):
        return SimpleNamespace(
            capabilities=SimpleNamespace(
                supports_tool_use=True,
                function_calling=True,
                tool_request_mode="native",
                continuation_mode="reconstructed",
                reasoning_transport="visible_optional",
                input_modalities=frozenset({"text"}),
                output_modalities=frozenset({"text"}),
            ),
            continue_generation=lambda *args, **kwargs: None,
        )


def _runtime(tmp_path):
    return SimpleNamespace(
        global_config=SimpleNamespace(bridge_home=tmp_path),
    )


def _model_options(*, openrouter=True, codex=True, known_cost=None):
    options = []
    if openrouter:
        option = {
            "name": "openrouter",
            "engine": "openrouter-api",
            "label": "openrouter",
            "available": True,
            "models": [
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-pro",
            ],
        }
        if known_cost is not None:
            option["estimated_fallback_call_cost_usd"] = known_cost
        options.append(option)
    if codex:
        options.append(
            {
                "name": "codex",
                "engine": "codex-cli",
                "label": "codex",
                "available": True,
                "models": ["gpt-6-sol"],
            }
        )
    return options


def _save_policy(tmp_path, actor, policy):
    runtime = _runtime(tmp_path)
    _saved, document = read_policy(runtime, actor, for_update=True)
    write_policy(runtime, actor, policy, document=document)


@pytest.mark.parametrize("level", list(PrivacyLevel))
def test_fallback_facts_preserve_every_privacy_level(tmp_path, level):
    manager = _StubManager([])
    manager.privacy_level = int(level)
    request = _request("req-privacy")
    profile = SimpleNamespace(
        engine="deepseek-api",
        model="deepseek-v4-flash",
        reasoning=None,
    )
    port = _Herv3ProviderFallbackPort(
        _StubBridge(_runtime(tmp_path), manager),
        request,
        profile,
        purpose="continuation",
    )
    facts = port._facts(
        policy=FallbackPolicy(),
        source=policy_owner.SOURCE_HERV3,
        source_provider=profile.engine,
        source_model=profile.model,
        tool_activity=False,
        checkpoint_safe=True,
        visible_partial_answer=False,
    )
    assert facts.privacy_level == int(level)


@pytest.mark.asyncio
async def test_bridge_port_blocks_an_invalid_privacy_value(tmp_path, monkeypatch):
    from adapters import her_v2_provider as bridge_module

    monkeypatch.setattr(bridge_module, "_fallback_actor", lambda runtime: "tester")
    _save_policy(
        tmp_path,
        "tester",
        FallbackPolicy(
            mode=MODE_AUTO,
            routes=(HERV3_ROUTE,),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )
    manager = _StubManager(_model_options())
    manager.privacy_level = 99
    profile = SimpleNamespace(
        engine="deepseek-api",
        model="deepseek-v4-flash",
        reasoning=None,
    )
    port = _Herv3ProviderFallbackPort(
        _StubBridge(_runtime(tmp_path), manager),
        _request("req-invalid-privacy"),
        profile,
        purpose="continuation",
    )

    decision = await port.evaluate_provider_fallback(
        source_provider=profile.engine,
        source_model=profile.model,
        tool_activity=False,
        checkpoint_safe=True,
        visible_partial_answer=False,
        primary_failure={"code": "PROVIDER_SERVER_ERROR"},
    )

    assert decision["allowed"] is False
    assert decision["reason"] == policy_owner.REASON_PRIVACY_BLOCKED


@pytest.mark.asyncio
async def test_route_facts_fail_closed_on_real_target_capability(tmp_path, monkeypatch):
    manager = _StubManager(_model_options())
    bridge = _StubBridge(_runtime(tmp_path), manager)
    bridge._create_provider_backend = lambda *args, **kwargs: SimpleNamespace(
        capabilities=SimpleNamespace(
            supports_tool_use=False,
            function_calling=False,
            tool_request_mode="none",
            continuation_mode="none",
            reasoning_transport="absent",
            input_modalities=frozenset({"text"}),
            output_modalities=frozenset({"text"}),
        )
    )
    request = _request("req-capability")
    profile = SimpleNamespace(
        engine="deepseek-api",
        model="deepseek-v4-flash",
        reasoning=None,
    )
    _save_policy(
        tmp_path,
        "tester",
        FallbackPolicy(
            mode=MODE_AUTO,
            routes=(HERV3_ROUTE,),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )
    from adapters import her_v2_provider as bridge_module

    monkeypatch.setattr(bridge_module, "_fallback_actor", lambda runtime: "tester")
    port = _Herv3ProviderFallbackPort(
        bridge, request, profile, purpose="continuation"
    )
    decision = await port.evaluate_provider_fallback(
        source_provider=profile.engine,
        source_model=profile.model,
        tool_activity=False,
        checkpoint_safe=True,
        visible_partial_answer=False,
        primary_failure={"code": "PROVIDER_SERVER_ERROR"},
    )
    assert decision["allowed"] is False
    assert decision["reason"] == policy_owner.REASON_INCOMPATIBLE_CAPABILITY


@pytest.mark.asyncio
async def test_bridge_port_consumes_the_real_policy_and_defaults_off(
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
    decision = await port.evaluate_provider_fallback(
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
    decision = await port.evaluate_provider_fallback(
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
    decision = await locked.evaluate_provider_fallback(
        source_provider="deepseek-api",
        source_model="deepseek-v4-flash",
        tool_activity=True,
        checkpoint_safe=True,
        visible_partial_answer=False,
        primary_failure={"code": "PROVIDER_SERVER_ERROR"},
    )
    assert decision["allowed"] is False
    assert decision["reason"] == "missing_key_or_runtime"


@pytest.mark.asyncio
async def test_bridge_port_enforces_configured_route_cost_ceiling(
    tmp_path, monkeypatch
):
    from adapters import her_v2_provider as bridge_module

    monkeypatch.setattr(bridge_module, "_fallback_actor", lambda runtime: "tester")
    _save_policy(
        tmp_path,
        "tester",
        FallbackPolicy(
            mode=MODE_AUTO,
            routes=(HERV3_ROUTE,),
            max_additional_calls=1,
            max_extra_cost_usd=0.05,
            allow_unknown_cost=True,
        ),
    )
    request = _request("req-cost-ceiling")
    profile = SimpleNamespace(
        engine="deepseek-api",
        model="deepseek-v4-flash",
        reasoning=None,
    )
    port = _Herv3ProviderFallbackPort(
        _StubBridge(
            _runtime(tmp_path),
            _StubManager(_model_options(known_cost=0.20)),
        ),
        request,
        profile,
        purpose="continuation",
    )

    decision = await port.evaluate_provider_fallback(
        source_provider=profile.engine,
        source_model=profile.model,
        tool_activity=False,
        checkpoint_safe=True,
        visible_partial_answer=False,
        primary_failure={"code": "PROVIDER_SERVER_ERROR"},
    )

    assert decision["allowed"] is False
    assert decision["reason"] == policy_owner.REASON_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_bridge_port_accumulates_cost_across_fallback_routes(
    tmp_path,
    monkeypatch,
):
    from adapters import her_v2_provider as bridge_module

    monkeypatch.setattr(bridge_module, "_fallback_actor", lambda runtime: "tester")
    _save_policy(
        tmp_path,
        "tester",
        FallbackPolicy(
            mode=MODE_AUTO,
            routes=(HERV3_ROUTE, SECOND_HERV3_ROUTE),
            max_additional_calls=2,
            max_extra_cost_usd=0.05,
        ),
    )
    request = _request("req-cumulative-cost")
    bridge = _StubBridge(
        _runtime(tmp_path),
        _StubManager(_model_options(known_cost=0.03)),
    )
    profile = SimpleNamespace(
        engine="deepseek-api",
        model="deepseek-v4-flash",
        reasoning=None,
    )
    resolve = bridge_module.HashiStageProvider.resolve_stage_provider_fallback

    first = await resolve(
        bridge,
        request=request,
        selected=profile,
        tool_started=False,
        visible_partial_answer=False,
    )
    assert first["allowed"] is True
    assert first["route_key"] == HERV3_ROUTE.key()
    assert bridge.cost["req-cumulative-cost"] == pytest.approx(0.03)

    second = await resolve(
        bridge,
        request=request,
        selected=profile,
        tool_started=False,
        visible_partial_answer=False,
    )
    assert second["allowed"] is False
    assert second["reason"] == policy_owner.REASON_BUDGET_EXHAUSTED
    assert bridge.budget["req-cumulative-cost"] == 1


@pytest.mark.asyncio
async def test_herv3_internal_stage_defers_codex_route_to_pao(tmp_path, monkeypatch):
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

    result = await resolve(
        bridge,
        request=request,
        selected=profile,
        tool_started=False,
        visible_partial_answer=False,
    )
    assert result["allowed"] is False
    assert result["reason"] == policy_owner.REASON_INCOMPATIBLE_CAPABILITY
    assert result["target_label"] == ""
    assert bridge.budget.get("req-stage", 0) == 0

    # Keep the pre-existing safety coverage on an otherwise executable HERV3
    # Model Provider route. Codex is rejected earlier at the Engine boundary.
    _save_policy(
        tmp_path,
        "tester",
        FallbackPolicy(
            mode=MODE_AUTO,
            routes=(HERV3_ROUTE,),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )
    after_effect = await resolve(
        bridge,
        request=_request("req-stage-after-effect"),
        selected=profile,
        tool_started=True,
        visible_partial_answer=False,
    )
    assert after_effect["allowed"] is False
    assert after_effect["reason"] == policy_owner.REASON_UNSAFE_CHECKPOINT

    phone_request = _request("req-stage-phone")
    object.__setattr__(phone_request, "context", {"request_mode": "phone"})
    excluded = await resolve(
        bridge,
        request=phone_request,
        selected=profile,
        tool_started=False,
        visible_partial_answer=False,
    )
    assert excluded["allowed"] is False
    assert excluded["reason"] == policy_owner.REASON_MODALITY_EXCLUDED


@pytest.mark.asyncio
async def test_herv3_does_not_fallback_to_its_current_provider_and_model(
    tmp_path,
    monkeypatch,
):
    from adapters import her_v2_provider as bridge_module

    monkeypatch.setattr(bridge_module, "_fallback_actor", lambda runtime: "default")
    current_route = FallbackRoute(
        source=policy_owner.SOURCE_HERV3,
        target_kind=TARGET_KIND_HERV3_MODEL,
        provider="deepseek-api",
        model="deepseek-v4-flash",
    )
    _save_policy(
        tmp_path,
        "default",
        FallbackPolicy(
            mode=MODE_AUTO,
            routes=(current_route,),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )
    manager = _StubManager(
        [
            {
                "engine": "deepseek-api",
                "available": True,
                "models": ["deepseek-v4-flash"],
            }
        ]
    )
    profile = SimpleNamespace(
        engine="deepseek-api",
        model="deepseek-v4-flash",
        reasoning=None,
    )
    port = _Herv3ProviderFallbackPort(
        _StubBridge(_runtime(tmp_path), manager),
        _request("req-current-target"),
        profile,
        purpose="stage",
    )

    decision = await port.evaluate_provider_fallback(
        source_provider=profile.engine,
        source_model=profile.model,
        tool_activity=False,
        checkpoint_safe=False,
        visible_partial_answer=False,
        primary_failure={"code": "PROVIDER_CAPACITY_UNAVAILABLE"},
    )

    assert decision["allowed"] is False
    assert decision["reason"] == policy_owner.REASON_NO_ALLOWED_TARGET

@pytest.mark.asyncio
async def test_herv3_fallback_skips_permanent_failures(monkeypatch, tmp_path):
    """A permanent failure (400) must never consult the policy or spend a call."""
    import httpx

    target = _adapter(
        tmp_path, model="deepseek/deepseek-v4-flash", engine="openrouter-api"
    )
    port = _ScriptedPort(target)  # allowed=True: any consultation would fall back

    adapter = _adapter(tmp_path)
    adapter.TRANSIENT_PROVIDER_CALL_RETRIES = 1
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    request = _request("req-permanent")
    adapter.tool_registry = _checkpoint_registry(tmp_path, request)
    adapter.set_provider_fallback_port(port)

    def permanent_failure(payload, headers, on_stream_event):
        req = httpx.Request("POST", "https://openrouter.example/v1/chat/completions")
        raise httpx.HTTPStatusError(
            "400 Bad Request",
            request=req,
            response=httpx.Response(400, request=req),
        )

    monkeypatch.setattr(adapter, "_stream_api_once", permanent_failure)

    async def capture(_event):
        return None

    response = await adapter.generate_response(
        "hello", "req-permanent", on_stream_event=capture
    )

    assert response.is_success is False
    assert port.evaluations == [], (
        "a permanent failure must not consult the fallback policy"
    )
    assert port.target_calls == [], (
        "a permanent failure must not spend a paid fallback call"
    )
    record = response.stream_metadata["provider_fallback"]
    assert record["attempted"] is False
    assert record["disposition"] == "blocked"
    assert record["reason"] == "ineligible_failure"
    assert record["additional_calls"] == 0
