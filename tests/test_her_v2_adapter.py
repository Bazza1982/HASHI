from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse, TokenUsage
from adapters.her_habits import HERHabitStore
from adapters.her_v2 import (
    HERv2Adapter,
    HashiStageProvider,
    _backend_response_error,
)
from adapters.registry import get_backend_class
from adapters.stream_events import (
    DELIVERY_FINAL,
    DELIVERY_INTERNAL,
    DELIVERY_USER_COMMENTARY,
    KIND_INITIAL_RESOLUTION,
    KIND_COMMENTARY,
    KIND_THINKING,
    KIND_TOOL_START,
    StreamEvent,
)
from orchestrator.config import AgentConfig, GlobalConfig
from orchestrator.flexible_backend_registry import (
    BACKEND_REGISTRY,
    canonical_backend_engine,
)
from orchestrator.her_v2.config import ProviderProfile
from orchestrator.her_v2.commentary import PackagedCommentary
from orchestrator.her_v2.interfaces import ProviderFailureCode, StageInvocationError
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.ledger import ExecutionLedger, LedgerStore
from orchestrator.her_v2.models import (
    Effort,
    LifecycleState,
    Stage,
    StageRequest,
    StageResponse,
    ToolReceiptStatus,
    TriageClassification,
)
from orchestrator.her_v2.presentation import (
    RenderedRequiredMessage,
    RequiredUserMessage,
)
from orchestrator import runtime_her_dream, runtime_her_habits


def _profiles():
    return {
        name: {
            "engine": "openrouter-api",
            "model": f"configured/{name}",
            "reasoning": f"provider-{name}",
        }
        for name in ("lightweight", "triage", "premium", "reviewer", "orchestrator")
    }


@pytest.mark.parametrize(
    ("message", "code", "retryable"),
    [
        ("HTTP 400 Bad Request", ProviderFailureCode.PROVIDER_BAD_REQUEST, False),
        (
            "HTTP 401 Unauthorized",
            ProviderFailureCode.PROVIDER_AUTHENTICATION_FAILED,
            False,
        ),
        ("HTTP 403 Forbidden", ProviderFailureCode.PROVIDER_PERMISSION_DENIED, False),
        ("HTTP 408 Request Timeout", ProviderFailureCode.PROVIDER_REQUEST_TIMEOUT, True),
        ("HTTP 429 Rate Limited", ProviderFailureCode.PROVIDER_RATE_LIMITED, True),
        ("HTTP 503 Service Unavailable", ProviderFailureCode.PROVIDER_SERVER_ERROR, True),
        (
            "connection reset by peer",
            ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
            True,
        ),
        (
            "Gemini CLI was idle for 300s with no output",
            ProviderFailureCode.PROVIDER_REQUEST_TIMEOUT,
            True,
        ),
        (
            "Codex executable not found",
            ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
            False,
        ),
        (
            "TLS certificate verification failed",
            ProviderFailureCode.PROVIDER_TLS_ERROR,
            False,
        ),
        (
            "Grok CLI returned no answer text",
            ProviderFailureCode.PROVIDER_EMPTY_RESPONSE,
            True,
        ),
    ],
)
def test_untyped_legacy_backend_failures_receive_safe_provider_types(
    message,
    code,
    retryable,
):
    failure = _backend_response_error(
        BackendResponse(text="", duration_ms=1, error=message, is_success=False),
        fallback="provider failed",
    )

    assert failure.code is code
    assert failure.retryable is retryable
    assert failure.human_description


def _agent_config(tmp_path, *, her_v2=None, effort="low"):
    config = AgentConfig(
        name="agent",
        engine="her-v2",
        workspace_dir=tmp_path / "workspace",
        system_md=tmp_path / "SYSTEM.md",
        model="role-configured",
        is_active=True,
        extra={
            "effort": effort,
            "her_v2": her_v2 if her_v2 is not None else {"profiles": _profiles()},
        },
        project_root=tmp_path,
    )
    return config


def _global_config(tmp_path):
    return GlobalConfig(
        authorized_id=1,
        project_root=tmp_path,
        bridge_home=tmp_path,
        base_logs_dir=tmp_path / "logs",
    )


class _DirectProvider:
    def __init__(self):
        self.requests = []

    async def invoke(self, profile, request):
        self.requests.append((profile, request))
        if request.stage is Stage.IMMEDIATE_RESPONSE:
            data = {"message": "Hello from HER v2."}
        elif request.stage is Stage.TRIAGE:
            data = {"classification": "DIRECT_RESPONSE", "goal": request.goal}
        else:
            raise AssertionError(f"unexpected stage: {request.stage}")
        return StageResponse(
            text="",
            data=data,
            provider=profile.engine,
            model=profile.model,
            reasoning_trace=None,
        )


class _ImmediateFirstDirectProvider(_DirectProvider):
    async def invoke(self, profile, request):
        if request.stage is Stage.TRIAGE:
            await asyncio.sleep(0.02)
        return await super().invoke(profile, request)


class _DreamProvider(_DirectProvider):
    async def invoke(self, profile, request):
        self.requests.append((profile, request))
        if request.stage is Stage.DREAM:
            return StageResponse(
                text='{"groups":[]}',
                provider=profile.engine,
                model=profile.model,
                reasoning_trace="dream maintenance trace",
            )
        return await super().invoke(profile, request)


class _RetryingMaintenanceProvider:
    def __init__(self):
        self.requests = []

    async def invoke(self, profile, request):
        self.requests.append((profile, request))
        if len(self.requests) == 1:
            raise StageInvocationError(
                "temporary maintenance connection failure",
                code=ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
                human_description="The provider connection was interrupted.",
            )
        return StageResponse(
            text='{"groups":[]}' if request.stage is Stage.DREAM else '{"actions":[]}',
            provider=profile.engine,
            model=profile.model,
            reasoning_trace="recovered maintenance trace",
        )


class _WorkAndMeditationProvider(_DirectProvider):
    async def invoke(self, profile, request):
        self.requests.append((profile, request))
        payload = {
            Stage.IMMEDIATE_RESPONSE: {"message": "I have it."},
            Stage.TRIAGE: {
                "classification": "SIMPLE_TASK",
                "goal": request.goal,
            },
            Stage.EXECUTION: {
                "disposition": "COMPLETED",
                "summary": "Verified and completed the requested work.",
                "evidence_refs": ["receipt:v2"],
                "commentary": "The requested work is verified and complete.",
            },
            Stage.FINALISATION: {
                "execution_result": {
                    "disposition": "COMPLETED",
                    "summary": "Verified and completed the requested work.",
                    "evidence_refs": ["receipt:v2"],
                },
                "final_message": "Completed and verified.",
            },
        }.get(request.stage)
        if request.stage is Stage.MEDITATION:
            return StageResponse(
                text=json.dumps(
                    {
                        "actions": [
                            {
                                "operation": "create",
                                "title": "Verify before completion",
                                "metadata": "Use when completion depends on mutable state.",
                                "body": "Inspect current state and retain a concrete receipt.",
                            }
                        ]
                    }
                ),
                provider=profile.engine,
                model=profile.model,
                reasoning_trace="meditation trace",
            )
        if payload is None:
            raise AssertionError(f"unexpected stage: {request.stage}")
        return StageResponse(
            text="",
            data=payload,
            provider=profile.engine,
            model=profile.model,
            reasoning_trace=f"trace:{request.stage.value}",
        )


class _SideEffectFailureProvider(_DirectProvider):
    async def invoke(self, profile, request):
        self.requests.append((profile, request))
        if request.stage is Stage.IMMEDIATE_RESPONSE:
            payload = {"message": "I have it."}
        elif request.stage is Stage.TRIAGE:
            payload = {"classification": "SIMPLE_TASK", "goal": request.goal}
        elif request.stage is Stage.EXECUTION:
            request.provider_activity_callback(
                {
                    "kind": "shell_exec",
                    "content": "bash started",
                    "tool_name": "bash",
                    "tool_read_only": False,
                }
            )
            request.provider_activity_callback(
                {
                    "kind": "tool_end",
                    "content": "bash cleanup completed",
                    "tool_name": "bash",
                    "tool_read_only": False,
                    "tool_details": {
                        "foreground_cleanup": {
                            "status": "terminated",
                            "scope": "process_group",
                            "process_reaped": True,
                            "group_alive": False,
                            "errors": [],
                        }
                    },
                }
            )
            raise StageInvocationError(
                "provider stream ended before completion",
                code=ProviderFailureCode.PROVIDER_INCOMPLETE_STREAM_TIMEOUT,
                human_description=(
                    "The provider response began but did not complete."
                ),
            )
        elif request.stage is Stage.FINALISATION:
            payload = {
                "execution_result": None,
                "final_message": "The execution could not be completed safely.",
            }
        else:
            raise AssertionError(f"unexpected stage: {request.stage}")
        return StageResponse(
            text="",
            data=payload,
            provider=profile.engine,
            model=profile.model,
        )


class _PlannedWorkAndMeditationProvider(_WorkAndMeditationProvider):
    async def invoke(self, profile, request):
        if request.stage is Stage.PLANNING:
            self.requests.append((profile, request))
            return StageResponse(
                text="",
                data={"plan": ["Complete the current request safely"]},
                provider=profile.engine,
                model=profile.model,
                reasoning_trace="trace:planning",
            )
        return await super().invoke(profile, request)


class _EffortPolicyProvider:
    def __init__(self):
        self.requests = []

    async def invoke(self, profile, request):
        self.requests.append((profile, request))
        payload = {
            Stage.IMMEDIATE_RESPONSE: {"message": "I have it."},
            Stage.TRIAGE: {
                "classification": "SIMPLE_TASK",
                "goal": request.goal,
            },
            Stage.PLANNING: {"plan": ["Execute the scheduled specification"]},
            Stage.EXECUTION: {
                "disposition": "COMPLETED",
                "summary": "Completed.",
                "evidence_refs": ["receipt:effort-policy"],
            },
            Stage.REVIEW: {
                "outcome": "UNAVAILABLE",
                "summary": "Tool-backed Review is unavailable in this policy stub.",
            },
            Stage.VERIFICATION: {
                "outcome": "NOT_AI_VERIFIABLE",
                "summary": "This policy stub has no verification tools.",
                "checks": [
                    {
                        "claim": "The external result is correct",
                        "verifiability": "NOT_AI_VERIFIABLE",
                        "result": "NOT_AI_VERIFIABLE",
                        "method": "artifact_inspection",
                        "evidence_refs": [],
                        "observed": "No external artifact is attached to the stub.",
                    }
                ],
            },
            Stage.FINALISATION: {
                "execution_result": {
                    "disposition": "COMPLETED",
                    "summary": "Completed.",
                    "evidence_refs": ["receipt:effort-policy"],
                },
                "final_message": "Completed and verified.",
            },
        }.get(request.stage)
        if payload is None:
            raise AssertionError(f"unexpected stage: {request.stage}")
        return StageResponse(
            text="",
            data=payload,
            provider=profile.engine,
            model=profile.model,
            reasoning_trace=f"trace:{request.stage.value}",
        )


class _StaticPersonaPackager:
    def __init__(self):
        self.commentaries = []

    async def package(self, commentary):
        self.commentaries.append(commentary)
        return PackagedCommentary(
            source_event_id=commentary.event_id,
            stage=commentary.stage,
            text=f"Persona update: {commentary.text}",
            provenance="persona_packager",
        )


class _StaticRequiredPersonaRenderer:
    def __init__(self):
        self.messages: list[RequiredUserMessage] = []

    async def render(self, message):
        self.messages.append(message)
        return RenderedRequiredMessage(
            source_event_id=message.event_id,
            kind=message.kind,
            text=f"Persona {message.kind}: {message.text}",
            provenance="test_required_persona",
        )


class _LearningCommandRuntime:
    def __init__(self, adapter, global_config):
        self.name = "agent"
        self.workspace_dir = Path(adapter.config.workspace_dir)
        self.logger = logging.getLogger("test.her-v2-learning-command")
        self.error_logger = self.logger
        self.config = SimpleNamespace(
            active_backend="her-v2",
            system_md=adapter.config.system_md,
        )
        self.global_config = global_config
        self.backend_manager = SimpleNamespace(
            current_backend=adapter,
            get_habit_meditation_override=lambda: None,
        )
        self.skill_manager = None
        self.sys_prompt_manager = SimpleNamespace(get_active_texts=lambda: [])
        self.transcript_log_path = self.workspace_dir / "transcript.jsonl"
        self.replies = []

    def _is_authorized_user(self, user_id):
        return user_id == 1

    def _backend_busy(self):
        return False

    async def _reply_text(self, update, text, **kwargs):
        del update
        self.replies.append({"text": text, **kwargs})


def _command_update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=99),
        callback_query=None,
    )


def test_public_her_alias_resolves_forward_and_claw_id_is_removed():
    assert get_backend_class("her-v2") is HERv2Adapter
    assert get_backend_class("her") is HERv2Adapter
    with pytest.raises(ValueError, match="Unknown engine: claw-cli"):
        get_backend_class("claw-cli")
    assert canonical_backend_engine("her") == "her-v2"
    assert canonical_backend_engine("claw-cli") == "claw-cli"
    assert "her" not in BACKEND_REGISTRY
    assert "claw-cli" not in BACKEND_REGISTRY
    assert BACKEND_REGISTRY["her-v2"]["efforts"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert BACKEND_REGISTRY["her-v2"]["secret_keys"] == []


@pytest.mark.asyncio
async def test_documented_normal_configuration_stays_initializable(tmp_path):
    example_path = (
        Path(__file__).parents[1] / "docs" / "examples" / "her_v2_normal_backend.json"
    )
    example = json.loads(example_path.read_text(encoding="utf-8"))
    v2_entry = next(
        item for item in example["allowed_backends"] if item["engine"] == "her-v2"
    )
    config = _agent_config(
        tmp_path,
        her_v2=v2_entry["her_v2"],
        effort=v2_entry["effort"],
    )
    manager = SimpleNamespace(
        config=SimpleNamespace(allowed_backends=example["allowed_backends"])
    )
    setattr(config, "_hashi_runtime", SimpleNamespace(backend_manager=manager))
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    assert await adapter.initialize() is True
    assert adapter._v2_config.shadow_mode is False


@pytest.mark.asyncio
async def test_adapter_direct_response_uses_final_lane_once(tmp_path):
    provider = _DirectProvider()
    config = _agent_config(tmp_path)
    setattr(config, "_her_v2_stage_provider", provider)
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    events = []

    async def capture(event):
        events.append(event)

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Hello", "request-1", on_stream_event=capture
    )

    assert response.is_success is True
    assert response.text == "Hello from HER v2."
    assert response.stop_reason == "completed"
    assert response.stream_metadata["her_v2"]["classification"] == "DIRECT_RESPONSE"
    assert response.stream_metadata["her_v2"]["final_was_immediate"] is True
    final_events = [event for event in events if event.delivery_class == DELIVERY_FINAL]
    assert len(final_events) == 1
    assert final_events[0].summary == response.text
    audit_rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "agent" / "her_v2_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    delivery = next(row for row in audit_rows if row["event"] == "delivery_result")
    assert delivery["payload"] == {
        "kind": "final",
        "accepted": True,
        "delivered": False,
        "disposition": "deferred_to_final_boundary",
        "required": True,
        "delivery_id": response.stream_metadata["her_v2"]["delivery"][
            "delivery_id"
        ],
        "message_event_id": response.stream_metadata["her_v2"]["delivery"][
            "event_id"
        ],
    }


@pytest.mark.asyncio
async def test_scheduler_effort_is_request_scoped_and_provider_reasoning_is_unchanged(
    tmp_path,
):
    provider = _EffortPolicyProvider()
    runtime_context = SimpleNamespace(
        current_request_meta={
            "request_id": "request-scheduled-low",
            "scheduler_context": {
                "kind": "cron",
                "task_id": "nightly-report",
                "trigger": "scheduled",
            },
        }
    )
    config = _agent_config(tmp_path, effort="max")
    config._hashi_runtime = runtime_context
    config._her_v2_stage_provider = provider
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    assert await adapter.initialize() is True

    scheduled = await adapter.generate_response(
        "Run the nightly report",
        "request-scheduled-low",
    )

    assert scheduled.is_success is True
    scheduled_stages = [request.stage for _profile, request in provider.requests]
    assert Stage.PLANNING not in scheduled_stages
    assert Stage.REVIEW not in scheduled_stages
    assert Stage.EXECUTION in scheduled_stages
    assert Stage.FINALISATION in scheduled_stages
    assert scheduled.stream_metadata["her_v2"]["effort"] == {
        "configured": "max",
        "effective": "low",
        "reason": "scheduled_job_default",
        "scheduler_kind": "cron",
        "scheduler_task_id": "nightly-report",
        "scheduler_trigger": "scheduled",
    }
    scheduled_execution_profile = next(
        profile
        for profile, request in provider.requests
        if request.stage is Stage.EXECUTION
    )

    provider.requests.clear()
    runtime_context.current_request_meta = {
        "request_id": "request-ordinary-max",
        "source": "text",
    }
    ordinary = await adapter.generate_response(
        "Complete the ordinary request",
        "request-ordinary-max",
    )

    assert ordinary.is_success is True
    ordinary_stages = [request.stage for _profile, request in provider.requests]
    assert Stage.PLANNING in ordinary_stages
    assert Stage.REVIEW in ordinary_stages
    assert Stage.VERIFICATION in ordinary_stages
    assert ordinary.stream_metadata["her_v2"]["effort"] == {
        "configured": "max",
        "effective": "max",
        "reason": "agent_default",
    }
    ordinary_execution_profile = next(
        profile
        for profile, request in provider.requests
        if request.stage is Stage.EXECUTION
    )
    assert scheduled_execution_profile.model == ordinary_execution_profile.model
    assert (
        scheduled_execution_profile.reasoning
        == ordinary_execution_profile.reasoning
        == "provider-lightweight"
    )
    assert adapter.effort == "max"
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_adapter_correlates_ordinary_transport_receipt_with_stable_delivery_id(
    tmp_path,
):
    provider = _DirectProvider()
    config = _agent_config(tmp_path)
    setattr(config, "_her_v2_stage_provider", provider)
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    async def capture(_event):
        return None

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Hello", "request-delivery-receipt", on_stream_event=capture
    )
    delivery = response.stream_metadata["her_v2"]["delivery"]

    assert delivery["delivery_id"]
    assert delivery["event_id"]
    assert adapter.record_transport_delivery_receipt(
        request_id="request-delivery-receipt",
        delivery_id=delivery["delivery_id"],
        delivered=True,
        disposition="transport_delivered",
        chunk_count=1,
    ) is True
    # The same transport callback is idempotent and cannot duplicate audit truth.
    assert adapter.record_transport_delivery_receipt(
        request_id="request-delivery-receipt",
        delivery_id=delivery["delivery_id"],
        delivered=True,
        disposition="transport_delivered",
        chunk_count=1,
    ) is True

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "agent" / "her_v2_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    receipts = [
        row for row in rows if row["event"] == "transport_delivery_receipt"
    ]
    assert len(receipts) == 1
    assert receipts[0]["payload"]["delivery_id"] == delivery["delivery_id"]
    assert receipts[0]["payload"]["message_event_id"] == delivery["event_id"]
    assert receipts[0]["payload"]["delivered"] is True
    assert receipts[0]["payload"]["chunk_count"] == 1


@pytest.mark.asyncio
async def test_adapter_finalises_unusable_execution_as_runtime_error(tmp_path):
    class _MalformedExecutionProvider(_DirectProvider):
        async def invoke(self, profile, request):
            self.requests.append((profile, request))
            if request.stage is Stage.IMMEDIATE_RESPONSE:
                payload = {"message": "I have it."}
            elif request.stage is Stage.TRIAGE:
                payload = {"classification": "SIMPLE_TASK", "goal": request.goal}
            elif request.stage is Stage.EXECUTION:
                return StageResponse(
                    text="execution reply without valid JSON",
                    reasoning_trace="execution trace",
                    provider=profile.engine,
                    model=profile.model,
                    evidence_refs=("hashi-tools:uncertain",),
                )
            elif request.stage is Stage.FINALISATION:
                payload = {
                    "execution_result": None,
                    "final_message": "The execution result could not be interpreted.",
                }
            else:
                raise AssertionError(f"unexpected stage: {request.stage}")
            return StageResponse(
                text="",
                data=payload,
                provider=profile.engine,
                model=profile.model,
            )

    provider = _MalformedExecutionProvider()
    config = _agent_config(
        tmp_path,
        her_v2={"profiles": _profiles()},
    )
    setattr(config, "_her_v2_stage_provider", provider)
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Perform the action once", "request-adapter-plan-b-error"
    )

    assert response.is_success is False
    assert response.stop_reason == "error"
    assert response.error == "execution_result_unusable"
    assert response.stream_metadata["her_v2"]["terminal_state"] == "ERROR"
    assert response.text == "The execution result could not be interpreted."
    assert sum(
        request.stage is Stage.EXECUTION
        for _profile, request in provider.requests
    ) == 1
    assert sum(
        request.stage is Stage.FINALISATION
        for _profile, request in provider.requests
    ) == 1


@pytest.mark.asyncio
async def test_adapter_exposes_primary_failure_recovery_decision_and_cleanup(tmp_path):
    provider = _SideEffectFailureProvider()
    config = _agent_config(tmp_path)
    setattr(config, "_her_v2_stage_provider", provider)
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Run the shell operation once",
        "request-complete-failure-chain",
    )

    primary_code = ProviderFailureCode.PROVIDER_INCOMPLETE_STREAM_TIMEOUT.value
    recovery_code = ProviderFailureCode.SIDE_EFFECT_REPLAY_BLOCKED.value
    assert response.is_success is False
    assert response.error_code == primary_code
    assert primary_code in response.error
    assert primary_code in response.text
    assert recovery_code in response.text
    assert "Foreground cleanup:" in response.text
    chain = response.stream_metadata["her_v2"]["failure_chain"]
    assert chain["primary_failure"]["code"] == primary_code
    assert chain["recovery_decision"]["code"] == recovery_code
    assert chain["recovery_decision"]["automatic_replay_attempted"] is False
    assert chain["foreground_cleanup"]["status"] == "terminated"
    assert chain["foreground_cleanup"]["process_reaped"] is True
    assert response.stream_metadata["her_v2"]["error"]["code"] == primary_code

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "agent" / "her_v2_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failed = next(
        row
        for row in rows
        if row["event"] == "stage_attempt_failed"
        and row["stage"] == Stage.EXECUTION.value
    )
    assert failed["payload"]["error_code"] == primary_code
    assert failed["payload"]["recovery_decision"]["code"] == recovery_code


@pytest.mark.asyncio
async def test_adapter_packages_only_structured_stage_commentary_before_delivery(
    tmp_path,
):
    provider = _WorkAndMeditationProvider()
    packager = _StaticPersonaPackager()
    config = _agent_config(tmp_path, effort="low")
    setattr(config, "_her_v2_stage_provider", provider)
    setattr(config, "_her_v2_persona_packager", packager)
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    events = []

    async def capture(event):
        events.append(event)

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Complete and verify it", "request-commentary-events", on_stream_event=capture
    )

    assert response.is_success is True
    commentary = [event for event in events if event.kind == KIND_COMMENTARY]
    assert [(event.phase, event.provenance) for event in commentary] == [
        ("execution", "persona_packager"),
    ]
    assert commentary[0].detail == "persona_packaging_fallback=false"
    assert [(item.stage, item.text) for item in packager.commentaries] == [
        (Stage.EXECUTION, "The requested work is verified and complete."),
    ]


@pytest.mark.asyncio
async def test_adapter_uses_combined_finalisation_without_required_persona_renderer(
    tmp_path,
):
    provider = _WorkAndMeditationProvider()
    renderer = _StaticRequiredPersonaRenderer()
    config = _agent_config(tmp_path, effort="low")
    setattr(config, "_her_v2_stage_provider", provider)
    setattr(config, "_her_v2_required_persona_renderer", renderer)
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    events = []

    async def capture(event):
        events.append(event)

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Complete and verify it",
        "request-required-persona-final",
        on_stream_event=capture,
    )

    assert response.is_success is True
    assert response.text == "Completed and verified."
    assert renderer.messages == []
    final_events = [event for event in events if event.delivery_class == DELIVERY_FINAL]
    assert len(final_events) == 1
    assert final_events[0].summary == response.text
    assert final_events[0].provenance == "her_v2_combined_finalisation"
    assert final_events[0].detail == "persona_rendered_in_finalisation=true"
    assert response.stream_metadata["her_v2"]["delivery"]["delivery_id"]


@pytest.mark.asyncio
async def test_adapter_marks_direct_answer_delivered_when_immediate_lane_is_accepted(
    tmp_path,
):
    provider = _ImmediateFirstDirectProvider()
    config = _agent_config(tmp_path)
    setattr(config, "_her_v2_stage_provider", provider)
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    events = []

    async def capture(event):
        events.append(event)
        return True

    capture.supports_initial_resolution = True

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Hello", "request-early", on_stream_event=capture
    )

    assert response.is_success is True
    assert response.stream_metadata["her_v2"]["final_already_delivered"] is True
    assert [event.delivery_class for event in events] == [
        DELIVERY_USER_COMMENTARY,
        DELIVERY_INTERNAL,
    ]
    assert events[0].summary == response.text
    assert events[1].kind == KIND_INITIAL_RESOLUTION
    assert events[1].resolution == "final"
    assert events[1].target_event_id == events[0].event_id


@pytest.mark.asyncio
async def test_adapter_does_not_send_early_without_resolution_capability(tmp_path):
    provider = _ImmediateFirstDirectProvider()
    config = _agent_config(tmp_path)
    setattr(config, "_her_v2_stage_provider", provider)
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    events = []

    async def capture(event):
        events.append(event)
        return True

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Hello", "request-no-resolution-capability", on_stream_event=capture
    )

    assert response.is_success is True
    assert response.stream_metadata["her_v2"]["final_already_delivered"] is False
    assert [event.delivery_class for event in events] == [DELIVERY_FINAL]
    assert all(event.kind != KIND_INITIAL_RESOLUTION for event in events)


@pytest.mark.asyncio
async def test_adapter_requires_explicit_role_profiles(tmp_path):
    config = _agent_config(tmp_path, her_v2={})
    setattr(config, "_her_v2_stage_provider", _DirectProvider())
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    assert await adapter.initialize() is False


@pytest.mark.asyncio
async def test_adapter_supplies_concrete_meditation_service_when_enabled(tmp_path):
    raw = {"profiles": _profiles(), "meditation_enabled": True}
    config = _agent_config(tmp_path, her_v2=raw)
    setattr(config, "_her_v2_stage_provider", _DirectProvider())
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    assert await adapter.initialize() is True
    assert adapter._learning is not None
    assert adapter._habit_meditation_config().enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [Stage.MEDITATION, Stage.DREAM])
async def test_background_learning_provider_retries_once_with_same_invariants(
    tmp_path,
    stage,
):
    provider = _RetryingMaintenanceProvider()
    config = _agent_config(tmp_path, her_v2={"profiles": _profiles()})
    config._her_v2_stage_provider = provider
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    assert await adapter.initialize() is True

    response = await adapter._invoke_maintenance_model(
        stage,
        "Maintain the local catalogue.",
        f"turn-{stage.value}",
        f"request-{stage.value}",
        None,
    )

    assert response.provider_attempt == 2
    requests = [request for _profile, request in provider.requests]
    assert [request.attempt for request in requests] == [1, 2]
    assert len({request.retry_invariant_hash for request in requests}) == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "agent" / "her_v2_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        row["event"] == "maintenance_provider_retry_scheduled"
        and row["stage"] == stage.value
        and row["payload"]["fresh_connection"] is True
        for row in rows
    )
    assert all(
        "retry_tier" not in row["payload"]
        and "attempt_timeout_s" not in row["payload"]
        and "next_attempt_timeout_s" not in row["payload"]
        for row in rows
        if row["event"].startswith("maintenance_provider_")
    )
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_meditation_uses_turn_frozen_provider_target(tmp_path):
    provider = _RetryingMaintenanceProvider()
    config = _agent_config(tmp_path, her_v2={"profiles": _profiles()})
    config._her_v2_stage_provider = provider
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    assert await adapter.initialize() is True
    job_id = "a" * 32
    adapter._her_meditation_journal().enqueue(
        job_id=job_id,
        request_id="turn-frozen-routing",
        prompt="Maintain the local catalogue.",
        max_actions=3,
        routing_target={
            "provider": "openrouter-api",
            "model": "anthropic/claude-sonnet-4.6",
            "reasoning": "low",
        },
    )

    await adapter._invoke_maintenance_model(
        Stage.MEDITATION,
        "Maintain the local catalogue.",
        "turn-frozen-routing",
        f"{job_id}:attempt:1",
        None,
    )

    profiles = [profile for profile, _request in provider.requests]
    assert {profile.engine for profile in profiles} == {"openrouter-api"}
    assert {profile.model for profile in profiles} == {
        "anthropic/claude-sonnet-4.6"
    }
    assert {profile.reasoning for profile in profiles} == {"low"}
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_adapter_runs_durable_meditation_after_completed_turn(tmp_path):
    provider = _WorkAndMeditationProvider()
    raw = {
        "profiles": _profiles(),
        "meditation_enabled": True,
        "shadow_mode": False,
    }
    config = _agent_config(tmp_path, her_v2=raw, effort="low")
    setattr(config, "_her_v2_stage_provider", provider)
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    assert await adapter.initialize() is True

    response = await adapter.generate_response("Complete safely", "request-learning")
    assert response.is_success is True
    assert response.text == "Completed and verified."

    for _ in range(200):
        jobs = list(adapter._her_meditation_journal().root.glob("*.json"))
        if jobs:
            payload = json.loads(jobs[0].read_text(encoding="utf-8"))
            if payload["status"] in {"completed", "no_change", "failed"}:
                break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError("Meditation did not reach a durable terminal state")

    assert payload["status"] == "completed"
    assert payload["routing_target"] == {
        "provider": "openrouter-api",
        "model": "configured/lightweight",
        "reasoning": "provider-lightweight",
    }
    assert [habit.title for habit in adapter._her_habit_store().load()] == [
        "Verify before completion"
    ]
    meditation_profile, meditation_request = next(
        (profile, request)
        for profile, request in provider.requests
        if request.stage is Stage.MEDITATION
    )
    premium_profile = adapter._v2_config.profiles["premium"]
    assert meditation_profile.name == "lightweight"
    assert meditation_profile.engine == premium_profile.engine
    assert meditation_profile.model == "configured/lightweight"
    assert meditation_profile.model != premium_profile.model
    assert meditation_request.allow_tools is False
    assert meditation_request.allow_side_effects is False
    assert "HER HABIT MEDITATION" in meditation_request.context["maintenance_prompt"]
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_request_scoped_ineligibility_disables_planning_and_meditation(
    tmp_path, monkeypatch
):
    provider = _PlannedWorkAndMeditationProvider()
    raw = {"profiles": _profiles(), "meditation_enabled": True}
    config = _agent_config(tmp_path, her_v2=raw, effort="medium")
    config._hashi_runtime = SimpleNamespace(
        current_request_meta={
            "request_id": "request-ineligible-learning",
            "habit_learning_eligible": False,
        }
    )
    setattr(config, "_her_v2_stage_provider", provider)
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    assert await adapter.initialize() is True

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("ineligible request must not inspect Habit files")

    monkeypatch.setattr(adapter._her_habit_store(), "retrieve", forbidden_read)
    response = await adapter.generate_response(
        "Complete safely", "request-ineligible-learning"
    )
    await asyncio.sleep(0)

    assert response.is_success is True
    planning_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.PLANNING
    )
    assert "habits" not in planning_request.context
    assert "habits_are_advisory" not in planning_request.context
    assert not any(
        request.stage is Stage.MEDITATION for _profile, request in provider.requests
    )
    assert not adapter._her_meditation_journal().root.exists()
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_habit_command_uses_her_v2_persistent_store(tmp_path):
    config = _agent_config(tmp_path)
    setattr(config, "_her_v2_stage_provider", _DirectProvider())
    global_config = _global_config(tmp_path)
    adapter = HERv2Adapter(config, global_config)
    assert await adapter.initialize() is True
    adapter._her_habit_store().apply_actions(
        [
            {
                "operation": "create",
                "title": "Verify V2 state",
                "metadata": "Use when HER v2 state may have changed.",
                "body": "Inspect current HER v2 state before another write.",
            }
        ],
        max_actions=1,
    )
    runtime = _LearningCommandRuntime(adapter, global_config)

    await runtime_her_habits.cmd_habit(
        runtime,
        _command_update(),
        SimpleNamespace(args=[]),
    )

    assert runtime.replies
    assert "Verify V2 state" in runtime.replies[-1]["text"]
    assert "her-v2" in runtime.replies[-1]["text"]
    assert "UNAVAILABLE" not in runtime.replies[-1]["text"]
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_dream_command_path_runs_outside_live_turn_with_v2_audit(tmp_path):
    provider = _DreamProvider()
    config = _agent_config(tmp_path)
    config.system_md.write_text(
        "[persona]\nUse a concise, friendly reporting voice.\n[persona_end]\n",
        encoding="utf-8",
    )
    setattr(config, "_her_v2_stage_provider", provider)
    global_config = _global_config(tmp_path)
    adapter = HERv2Adapter(config, global_config)
    assert await adapter.initialize() is True
    store: HERHabitStore = adapter._her_habit_store()
    store.apply_actions(
        [
            {
                "operation": "create",
                "title": "Review stable guidance",
                "metadata": "Use during periodic Habit catalogue maintenance.",
                "body": "Keep verified guidance compact and current.",
            }
        ],
        max_actions=1,
    )
    runtime = _LearningCommandRuntime(adapter, global_config)

    ok, report, manifest = await runtime_her_dream.execute_dream(
        runtime, origin="manual:test"
    )

    assert ok is True
    assert report
    assert manifest is not None
    dream_requests = [
        request for _profile, request in provider.requests if request.stage is Stage.DREAM
    ]
    assert len(dream_requests) == 2
    assert any(":analysis:" in request.turn_id for request in dream_requests)
    assert any(":persona" in request.turn_id for request in dream_requests)
    assert all(request.allow_tools is False for request in dream_requests)
    assert all(request.allow_side_effects is False for request in dream_requests)
    audit_rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "agent" / "her_v2_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    events = {row["event"] for row in audit_rows}
    assert "dream_write_authorised" in events
    assert "dream_commit_completed" in events
    assert "reasoning_trace" in events
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_adapter_rejects_profile_model_without_exact_agent_grant(tmp_path):
    config = _agent_config(tmp_path)
    manager = SimpleNamespace(
        config=SimpleNamespace(
            allowed_backends=[
                {
                    "engine": "openrouter-api",
                    "model": "configured/a-different-model",
                }
            ]
        ),
        # This deliberately models the manager's historical engine fallback.
        _select_backend_cfg=lambda _engine, target_model=None: {
            "engine": "openrouter-api",
            "model": "configured/a-different-model",
        },
    )
    setattr(config, "_hashi_runtime", SimpleNamespace(backend_manager=manager))
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    assert await adapter.initialize() is False


@pytest.mark.asyncio
async def test_adapter_accepts_exact_profile_grants_from_models_lists(tmp_path):
    config = _agent_config(tmp_path)
    manager = SimpleNamespace(
        config=SimpleNamespace(
            allowed_backends=[
                {
                    "engine": "openrouter-api",
                    "models": [profile["model"] for profile in _profiles().values()],
                }
            ]
        )
    )
    setattr(config, "_hashi_runtime", SimpleNamespace(backend_manager=manager))
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    assert await adapter.initialize() is True


@pytest.mark.asyncio
async def test_adapter_reconciles_old_inflight_ledger_without_resuming_it(tmp_path):
    config = _agent_config(tmp_path)
    provider = _DirectProvider()
    setattr(config, "_her_v2_stage_provider", provider)
    store = LedgerStore(config.workspace_dir / "backend_state" / "her_v2" / "ledgers")
    ledger = ExecutionLedger("interrupted", "request:old", "goal:old")
    ledger.record_triage(TriageClassification.COMPLEX_TASK)
    ledger.transition(LifecycleState.EXECUTING)
    store.save(ledger)
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    assert await adapter.initialize() is True

    recovered = store.load("interrupted")
    assert recovered.status is LifecycleState.ERROR
    assert recovered.terminal_reason == "unexpected_process_interruption"
    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "agent" / "her_v2_audit.jsonl")
        .read_text()
        .splitlines()
    ]
    assert rows[-1]["event"] == "interrupted_turn_reconciled"
    assert rows[-1]["payload"]["execution_resumed"] is False
    assert provider.requests == []

    continued = await adapter.generate_response("Continue safely", "request-new")

    assert continued.is_success is True
    assert continued.stream_metadata["her_v2"]["turn_id"] != "interrupted"
    assert store.load("interrupted").status is LifecycleState.ERROR


class _FakeBackend:
    def __init__(self, system_md=None):
        self.config = SimpleNamespace(
            extra={},
            name="agent",
            system_md=system_md,
        )
        self.tool_registry = "not-set"
        self.privacy_level = None
        self.reasoning_enabled = None
        self.shutdown_called = False
        self.prompt = ""
        self.sys_prompt = "configured agent persona"
        self.capabilities = SimpleNamespace(supports_tool_use=True)

    def set_reasoning_enabled(self, enabled):
        self.reasoning_enabled = enabled

    async def initialize(self):
        if self.config.system_md:
            self.sys_prompt = Path(self.config.system_md).read_text(encoding="utf-8")
        return True

    async def generate_response(
        self, prompt, request_id, is_retry=False, silent=False, on_stream_event=None
    ):
        del request_id, is_retry, silent
        self.prompt = prompt
        await on_stream_event(
            StreamEvent(kind=KIND_THINKING, summary="provider trace")
        )
        await on_stream_event(
            StreamEvent(kind=KIND_TOOL_START, summary="tool activity", tool_name="bash")
        )
        return BackendResponse(
            text='{"disposition":"COMPLETED","summary":"done"}',
            duration_ms=1,
            usage=TokenUsage(input_tokens=3, output_tokens=4, thinking_tokens=2),
            cost_usd=0.25,
            tool_call_count=1 if self.tool_registry else 0,
            tool_loop_count=1 if self.tool_registry else 0,
        )

    async def shutdown(self):
        self.shutdown_called = True


class _FakeManager:
    def __init__(self, system_md=None):
        self.backends = []
        self.privacy_level = 1
        self.system_md = system_md

    def create_ephemeral_backend(self, engine, target_model=None):
        assert engine == "openrouter-api"
        assert target_model == "configured/model"
        backend = _FakeBackend(self.system_md)
        self.backends.append(backend)
        return backend


class _CommentaryFakeBackend(_FakeBackend):
    async def generate_response(
        self, prompt, request_id, is_retry=False, silent=False, on_stream_event=None
    ):
        del request_id, is_retry, silent
        self.prompt = prompt
        await on_stream_event(
            StreamEvent(
                kind=KIND_COMMENTARY,
                summary="Raw provider progress",
                delivery_class=DELIVERY_USER_COMMENTARY,
            )
        )
        return BackendResponse(
            text='{"plan":["Use the replacement route"]}',
            duration_ms=1,
        )


class _CommentaryManager(_FakeManager):
    def create_ephemeral_backend(self, engine, target_model=None):
        assert engine == "openrouter-api"
        assert target_model == "configured/model"
        backend = _CommentaryFakeBackend(self.system_md)
        self.backends.append(backend)
        return backend


class _FlakyPersonaBackend(_FakeBackend):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.retry_flag = None
        self.request_id = ""

    async def generate_response(
        self, prompt, request_id, is_retry=False, silent=False, on_stream_event=None
    ):
        del silent, on_stream_event
        self.prompt = prompt
        self.request_id = request_id
        self.retry_flag = is_retry
        if len(self.manager.backends) == 1:
            return BackendResponse(
                text="",
                duration_ms=1,
                error="connection reset",
                is_success=False,
                error_code=ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value,
                error_retryable=True,
                stream_metadata={
                    "provider_failure_description": (
                        "The provider connection was interrupted."
                    )
                },
            )
        return BackendResponse(
            text="Captain, the verified result is ready.",
            duration_ms=1,
        )


class _FlakyPersonaManager(_FakeManager):
    def create_ephemeral_backend(self, engine, target_model=None):
        assert engine == "openrouter-api"
        assert target_model == "configured/model"
        backend = _FlakyPersonaBackend(self)
        self.backends.append(backend)
        return backend


class _BaseToolRegistry:
    def __init__(self):
        self.max_loops = 4
        self.audit_context = {
            "request_id": "request-1",
            "safety_mode": "read_write",
        }
        self.executed = []
        self.execution_contexts = []
        self.denials = []

    def is_allowed(self, name):
        return name in {"file_read", "file_write", "bash", "http_request"}

    def allowed_tool_names(self):
        return ("file_read", "file_write", "bash", "http_request")

    def is_read_only(self, name):
        return name == "file_read"

    def get_tool_definitions(self, tiers=None):
        del tiers
        return [
            {"type": "function", "function": {"name": "file_read"}},
            {"type": "function", "function": {"name": "file_write"}},
            {"type": "function", "function": {"name": "bash"}},
            {"type": "function", "function": {"name": "http_request"}},
        ]

    async def execute(self, name, arguments, tool_call_id=""):
        self.executed.append((name, arguments, tool_call_id))
        return SimpleNamespace(tool_call_id=tool_call_id, output="allowed", is_error=False)

    async def execute_with_audit_context(
        self, name, arguments, tool_call_id="", *, audit_context=None
    ):
        self.execution_contexts.append(dict(audit_context or {}))
        return await self.execute(name, arguments, tool_call_id)

    def record_delegated_denial(
        self, name, arguments, result, *, audit_context=None
    ):
        self.denials.append(
            (name, arguments, result, dict(audit_context or {}))
        )


def _stage_request(stage, *, allow_tools, allow_side_effects=False):
    return StageRequest(
        turn_id="turn-1",
        request_ref="request-1",
        stage=stage,
        role=stage.value,
        attempt=1,
        goal="Do the requested work",
        classification=TriageClassification.COMPLEX_TASK,
        effort=Effort.XHIGH,
        plan_id="plan-v1",
        context={},
        allow_tools=allow_tools,
        allow_side_effects=allow_side_effects,
    )


@pytest.mark.asyncio
async def test_persona_packaging_retries_once_with_a_fresh_backend(tmp_path):
    manager = _FlakyPersonaManager()
    audit = DurableAuditLog(
        tmp_path / "persona-audit.jsonl",
        tmp_path / "persona-audit-fallback.jsonl",
    )
    provider = HashiStageProvider(
        backend_manager=manager,
        audit_log=audit,
        workzone_ref=str(tmp_path / "workspace"),
    )
    profile = ProviderProfile(
        "lightweight",
        "openrouter-api",
        "configured/model",
        reasoning="provider-low",
    )
    provider.bind_persona_audit_context(
        "request-persona-retry",
        turn_id="turn-persona-retry",
        request_ref="hashi-request:request-persona-retry",
    )

    rendered = await provider.package_persona_commentary(
        profile,
        persona_block="Address the user as Captain.",
        neutral_commentary="The verified result is ready.",
        request_id="request-persona-retry",
    )

    assert rendered == "Captain, the verified result is ready."
    assert len(manager.backends) == 2
    assert [backend.retry_flag for backend in manager.backends] == [False, True]
    assert {backend.request_id for backend in manager.backends} == {
        "request-persona-retry"
    }
    assert all(backend.shutdown_called for backend in manager.backends)
    assert all(
        "idle_timeout_sec" not in backend.config.extra
        for backend in manager.backends
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "persona-audit.jsonl").read_text().splitlines()
    ]
    failed = next(
        row for row in rows if row["event"] == "persona_provider_attempt_failed"
    )
    retry = next(
        row for row in rows if row["event"] == "persona_provider_retry_scheduled"
    )
    completed = next(
        row for row in rows if row["event"] == "persona_provider_attempt_completed"
    )
    assert failed["payload"]["error_code"] == (
        ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value
    )
    assert failed["turn_id"] == "turn-persona-retry"
    assert failed["request_ref"] == "hashi-request:request-persona-retry"
    assert failed["payload"]["will_retry"] is True
    assert "retry_tier" not in failed["payload"]
    assert "attempt_timeout_s" not in failed["payload"]
    assert "next_attempt_timeout_s" not in retry["payload"]
    assert retry["payload"]["fresh_connection"] is True
    assert retry["payload"]["same_provider"] is True
    assert retry["payload"]["same_model"] is True
    assert retry["payload"]["same_goal"] is True
    assert retry["payload"]["same_classification"] is True
    assert retry["payload"]["same_permissions"] is True
    assert retry["payload"]["same_workzone"] is True
    assert failed["payload"]["retry_invariant_hash"] == (
        completed["payload"]["retry_invariant_hash"]
    )
    assert "retry_tier" not in completed["payload"]
    assert "attempt_timeout_s" not in completed["payload"]


@pytest.mark.asyncio
async def test_triage_receives_complete_policy_and_minimal_turn_prompt():
    manager = _FakeManager()
    provider = HashiStageProvider(backend_manager=manager)
    profile = ProviderProfile(
        "triage",
        "openrouter-api",
        "configured/model",
        reasoning="provider-low",
    )
    request = _stage_request(
        Stage.TRIAGE,
        allow_tools=False,
        allow_side_effects=False,
    )
    request = StageRequest(
        **{
            **request.__dict__,
            "goal": "Earlier context already contains the result. Please check it.",
        }
    )

    await provider.invoke(profile, request)

    backend = manager.backends[-1]
    assert backend.sys_prompt.startswith(
        "You are the authoritative HER v2 Triage classifier."
    )
    assert "Do not answer the request, acknowledge it, plan it, execute it" in (
        backend.sys_prompt
    )
    assert "supplied context already contains a reliable result" in backend.sys_prompt
    assert 'merely because the user says "check"' in backend.sys_prompt
    assert "current, recent, live, or externally stored information" in (
        backend.sys_prompt
    )
    assert "Choose exactly one classification" in backend.sys_prompt
    for classification in (
        "DIRECT_RESPONSE",
        "SIMPLE_TASK",
        "COMPLEX_TASK",
        "HIGH_VOLUME_TASK",
        "CONFIRMATION_REQUIRED",
    ):
        assert classification in backend.sys_prompt
    for decision_boundary in (
        "A bounded and straightforward execution step is required",
        "multiple dependent steps, discovery, comparison, validation",
        "substantial execution volume or many independent items",
        "goal, target, scope, required choice, or authority is materially unclear",
    ):
        assert decision_boundary in backend.sys_prompt
    assert "conservatively choose SIMPLE_TASK" in backend.sys_prompt
    assert "Return only the required JSON object" in backend.sys_prompt

    assert backend.prompt == """Authoritative user request and supplied context:

Earlier context already contains the result. Please check it.

Return exactly one JSON object matching this shape:

{
  "classification": "DIRECT_RESPONSE | SIMPLE_TASK | COMPLEX_TASK | HIGH_VOLUME_TASK | CONFIRMATION_REQUIRED",
  "goal": "optional concise interpretation of the current request, or null when unnecessary",
  "clarification": "a concrete question required only for CONFIRMATION_REQUIRED; otherwise null"
}"""
    assert "For Planning, Execution, Replanning, and Review" not in backend.prompt
    assert "her_effort" not in backend.prompt
    assert "tools_authorised_for_this_stage" not in backend.prompt
    assert "external_side_effects_authorised_for_this_stage" not in backend.prompt
    assert "invocation_role" not in backend.prompt
    assert "turn_id" not in backend.prompt
    assert "request_ref" not in backend.prompt

    retry_request = StageRequest(
        **{
            **request.__dict__,
            "attempt": 2,
            "context": {
                "previous_structure_error": {
                    "attempt": 1,
                    "error_type": "StructuredOutputError",
                    "error": "provider returned an empty structured response",
                },
                "retry_instruction": "legacy generic instruction",
            },
        }
    )

    await provider.invoke(profile, retry_request)

    retry_prompt = manager.backends[-1].prompt
    assert retry_prompt.startswith(backend.prompt)
    assert "The previous output was rejected" in retry_prompt
    assert '"attempt": 1' in retry_prompt
    assert "provider returned an empty structured response" in retry_prompt
    assert "legacy generic instruction" not in retry_prompt


@pytest.mark.asyncio
async def test_hashi_stage_provider_enforces_tool_gateway_and_provider_reasoning():
    manager = _FakeManager()
    registry = object()
    events = []

    async def capture(event):
        events.append(event)

    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=registry,
        on_stream_event=capture,
    )
    profile = ProviderProfile(
        "premium",
        "openrouter-api",
        "configured/model",
        reasoning="provider-high",
    )

    execution_request = _stage_request(
        Stage.EXECUTION, allow_tools=True, allow_side_effects=True
    )
    execution_request = StageRequest(
        **{
            **execution_request.__dict__,
            "goal": (
                "RECENT TURN MESSAGES\nMemory+ recent facts\n"
                "Cross-session receipt evidence\nCURRENT REQUEST"
            ),
            "context": {
                "active_plan": {"plan": ["inspect", "change", "verify"]},
                "sub_agent_results": [],
            },
        }
    )
    result = await provider.invoke(
        profile,
        execution_request,
    )

    backend = manager.backends[-1]
    assert backend.tool_registry.base is registry
    assert backend.tool_registry.max_loops is None
    assert backend.reasoning_enabled is True
    assert backend.config.extra["reasoning_effort"] == "provider-high"
    assert backend.sys_prompt.startswith("You are the HER v2 Execution stage.")
    assert "RECENT TURN MESSAGES" in backend.prompt
    assert "Memory+ recent facts" in backend.prompt
    assert "Cross-session receipt evidence" in backend.prompt
    assert '"inspect"' in backend.prompt
    assert "COMPLETED_WITH_LIMITATIONS" in backend.sys_prompt
    assert "USER_INPUT_REQUIRED" in backend.sys_prompt
    assert "REPLAN_REQUIRED" not in backend.sys_prompt
    assert "ABANDONED" not in backend.sys_prompt
    assert "ERROR" not in backend.sys_prompt
    assert "configured agent persona" not in backend.sys_prompt
    assert "her_effort" not in backend.prompt
    assert "provider-high" not in backend.prompt
    assert result.reasoning_trace == "provider trace"
    # Provider telemetry alone is not evidence; no registry call completed.
    assert result.evidence_refs == ()
    assert result.tool_receipts == ()
    assert provider.usage.input_tokens == 3
    assert provider.tool_call_count == 1
    assert backend.shutdown_called is True
    assert any(event.kind == KIND_TOOL_START for event in events)

    await provider.invoke(
        profile,
        _stage_request(Stage.REVIEW, allow_tools=False, allow_side_effects=False),
    )
    reviewer_backend = manager.backends[-1]
    assert reviewer_backend.tool_registry is None
    assert reviewer_backend.sys_prompt.startswith(
        "You are the independent strict HER v2 Reviewer"
    )
    assert "independent read-only assessor" in reviewer_backend.prompt
    assert '"tools_authorised_for_this_stage": false' in reviewer_backend.prompt

    for stage in (Stage.PLANNING, Stage.REPLANNING):
        await provider.invoke(
            profile,
            _stage_request(stage, allow_tools=False, allow_side_effects=False),
        )
        planning_backend = manager.backends[-1]
        assert "configured agent persona" not in planning_backend.sys_prompt
        assert (
            "HER v2 Planner" in planning_backend.sys_prompt
            or "HER v2 Replanner" in planning_backend.sys_prompt
        )

    await provider.invoke(
        profile,
        _stage_request(
            Stage.FINALISATION,
            allow_tools=False,
            allow_side_effects=False,
        ),
    )
    finalisation_backend = manager.backends[-1]
    assert finalisation_backend.tool_registry is None
    assert finalisation_backend.sys_prompt.startswith(
        "You are the HER v2 Finalisation stage"
    )
    assert "normalisation, ledger-payload, and user-message" in (
        finalisation_backend.sys_prompt
    )
    assert "Agent display name: agent" in finalisation_backend.sys_prompt
    assert "configured agent persona" not in finalisation_backend.sys_prompt
    assert '"execution_result"' in finalisation_backend.prompt
    assert '"final_message"' in finalisation_backend.prompt


@pytest.mark.asyncio
async def test_hashi_stage_provider_records_exact_completed_tool_evidence_receipts():
    class CallingToolBackend(_FakeBackend):
        async def generate_response(
            self,
            prompt,
            request_id,
            is_retry=False,
            silent=False,
            on_stream_event=None,
        ):
            del request_id, is_retry, silent, on_stream_event
            self.prompt = prompt
            self.tool_result = await self.tool_registry.execute(
                "file_read", {"path": "evidence.txt"}, "provider-call-42"
            )
            return BackendResponse(
                text='{"outcome":"UNAVAILABLE","summary":"receipt captured"}',
                duration_ms=1,
                tool_call_count=1,
                tool_loop_count=1,
            )

    class CallingToolManager(_FakeManager):
        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == (
                "openrouter-api",
                "configured/model",
            )
            backend = CallingToolBackend(self.system_md)
            self.backends.append(backend)
            return backend

    manager = CallingToolManager()
    registry = _BaseToolRegistry()
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=registry,
    )
    request = _stage_request(
        Stage.REVIEW,
        allow_tools=True,
        allow_side_effects=False,
    )
    request = StageRequest(
        **{
            **request.__dict__,
            "invocation_id": "turn-1:review:invocation:7",
            "context": {"delegated_tools": ["file_read"]},
        }
    )

    response = await provider.invoke(
        ProviderProfile("reviewer", "openrouter-api", "configured/model"),
        request,
    )

    assert len(response.tool_receipts) == 1
    receipt = response.tool_receipts[0]
    assert receipt.stage is Stage.REVIEW
    assert receipt.invocation_id == "turn-1:review:invocation:7"
    assert receipt.attempt == 1
    assert receipt.tool_call_id == "provider-call-42"
    assert receipt.tool_name == "file_read"
    assert receipt.status is ToolReceiptStatus.SUCCESS
    assert receipt.read_only is True
    assert receipt.completed is True
    assert receipt.output_sha256
    assert response.evidence_refs == (receipt.evidence_ref,)
    assert receipt.evidence_ref in manager.backends[0].tool_result.output
    assert "HASHI_EVIDENCE_RECEIPT" in manager.backends[0].tool_result.output
    assert registry.execution_contexts[-1]["safety_mode"] == "read_only"


@pytest.mark.asyncio
async def test_hashi_stage_provider_makes_every_effort_tool_loop_unbounded():
    manager = _FakeManager()
    registry = _BaseToolRegistry()
    registry.max_loops = 8
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=registry,
    )
    profile = ProviderProfile(
        "premium", "openrouter-api", "configured/model"
    )
    for effort in Effort:
        request = _stage_request(
            Stage.EXECUTION,
            allow_tools=True,
            allow_side_effects=True,
        )
        request = StageRequest(**{**request.__dict__, "effort": effort})

        await provider.invoke(profile, request)

        request_registry = manager.backends[-1].tool_registry
        assert request_registry.base is registry
        assert request_registry.max_loops is None

    assert registry.max_loops == 8


@pytest.mark.asyncio
async def test_persona_presentation_lanes_receive_only_block_and_minimal_inputs(
    tmp_path,
):
    system_md = tmp_path / "agent.md"
    system_md.write_text(
        """FULL AGENT OPERATIONAL CONTENT
[persona]
Use a warm voice and address the user as Captain.
[persona_end]
PRIVATE WORKFLOW INSTRUCTIONS""",
        encoding="utf-8",
    )
    manager = _FakeManager(system_md)
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=_BaseToolRegistry(),
    )
    profile = ProviderProfile(
        "lightweight",
        "openrouter-api",
        "configured/model",
        reasoning="provider-low",
    )
    immediate_request = _stage_request(
        Stage.IMMEDIATE_RESPONSE,
        allow_tools=False,
        allow_side_effects=False,
    )
    immediate_request = StageRequest(
        **{
            **immediate_request.__dict__,
            "goal": """Bridge-managed context follows.

--- ADDITIONAL SYSTEM CONTEXT ---

GLOBAL SYS CONTENT

--- SYSTEM IDENTITY ---

FULL AGENT OPERATIONAL CONTENT

--- RECENT CONTEXT ---

USER: Earlier context remains available.

--- CURRENT USER REQUEST — AUTHORITATIVE ---
[FYI: received now]

Please scan Outlook.""",
        }
    )

    await provider.invoke(profile, immediate_request)

    immediate_backend = manager.backends[-1]
    assert immediate_backend.sys_prompt.startswith(
        "[persona]\nUse a warm voice and address the user as Captain.\n[persona_end]"
    )
    assert "For an obviously direct conversational request" in (
        immediate_backend.sys_prompt
    )
    assert "has no tool access or tool authority" in immediate_backend.sys_prompt
    assert "it does not need tools" in immediate_backend.sys_prompt
    assert "only that stage may determine actual tool availability" in (
        immediate_backend.sys_prompt
    )
    assert "from real invocation results" in immediate_backend.sys_prompt
    assert "private control information for your behaviour only" in (
        immediate_backend.sys_prompt
    )
    assert "never repeat or explain tool availability" in (
        immediate_backend.sys_prompt
    )
    assert "Never call a tool or emit a tool call, tool-control envelope" in (
        immediate_backend.sys_prompt
    )
    assert "requires checking, execution, or new evidence" in (
        immediate_backend.sys_prompt
    )
    assert "Even if the user's request says what to report" in (
        immediate_backend.sys_prompt
    )
    assert "do not make, infer, or repeat that judgement" in (
        immediate_backend.sys_prompt
    )
    assert "return only a short receipt acknowledgement" in (
        immediate_backend.sys_prompt
    )
    assert "Do not execute, plan, assess feasibility, discuss capability" in (
        immediate_backend.sys_prompt
    )
    assert "claim an execution result" in immediate_backend.sys_prompt
    assert "narrate a concrete execution attempt" in immediate_backend.sys_prompt
    assert "FULL AGENT OPERATIONAL CONTENT" not in immediate_backend.sys_prompt
    assert "PRIVATE WORKFLOW INSTRUCTIONS" not in immediate_backend.sys_prompt
    assert "GLOBAL SYS CONTENT" not in immediate_backend.prompt
    assert "FULL AGENT OPERATIONAL CONTENT" not in immediate_backend.prompt
    assert "Earlier context remains available" in immediate_backend.prompt
    assert "Please scan Outlook" in immediate_backend.prompt
    assert "tools_authorised_for_this_stage" not in immediate_backend.prompt
    assert "invocation_role" not in immediate_backend.prompt

    finalisation_request = _stage_request(
        Stage.FINALISATION,
        allow_tools=False,
        allow_side_effects=False,
    )
    finalisation_request = StageRequest(
        **{
            **finalisation_request.__dict__,
            "context": {
                "raw_execution_output": {
                    "text": "The requested work completed.",
                    "data": {},
                    "evidence_refs": ["receipt:42"],
                },
                "parsed_execution_result": {
                    "disposition": "COMPLETED",
                    "summary": "The requested work completed.",
                    "evidence_refs": ["receipt:42"],
                    "limitations": [],
                    "clarification": None,
                },
                "execution_json_valid": True,
            },
        }
    )

    await provider.invoke(profile, finalisation_request)

    finalisation_backend = manager.backends[-1]
    assert "Use a warm voice and address the user as Captain." in (
        finalisation_backend.sys_prompt
    )
    assert "FULL AGENT OPERATIONAL CONTENT" not in (
        finalisation_backend.sys_prompt
    )
    assert "PRIVATE WORKFLOW INSTRUCTIONS" not in (
        finalisation_backend.sys_prompt
    )
    assert "The requested work completed." in finalisation_backend.prompt
    assert '"execution_result"' in finalisation_backend.prompt
    assert '"final_message"' in finalisation_backend.prompt

    rendered = await provider.package_persona_commentary(
        profile,
        persona_block="Address the user as Captain and use a warm voice.",
        neutral_commentary="The old endpoint was removed; a supported route is ready.",
        request_id="request-1:commentary:1",
    )

    backend = manager.backends[-1]
    assert rendered
    assert backend.tool_registry is None
    assert backend.shutdown_called is True
    assert "Address the user as Captain" in backend.sys_prompt
    assert "[persona]" in backend.sys_prompt
    assert "[persona_end]" in backend.sys_prompt
    assert "FULL AGENT OPERATIONAL CONTENT" not in backend.sys_prompt
    assert "PRIVATE WORKFLOW INSTRUCTIONS" not in backend.sys_prompt
    assert "Address the user as Captain" not in backend.prompt
    assert "The old endpoint was removed" in backend.prompt
    assert "authoritative_user_goal" not in backend.prompt
    assert "plan_steps" not in backend.prompt
    assert provider.tool_call_count == 0

    final_report = (
        "## Verified result\n\n"
        "- Path: `C:\\\\Work\\\\report.md`\n"
        "- Receipt: `job-42`"
    )
    required_rendered = await provider.package_persona_required_message(
        profile,
        persona_block="Address the user as Captain and use a warm voice.",
        neutral_message=final_report,
        message_kind="final",
        request_id="request-1:persona-package:final:1",
    )

    required_backend = manager.backends[-1]
    assert required_rendered
    assert required_backend.tool_registry is None
    assert required_backend.shutdown_called is True
    assert "HER V2 REQUIRED MESSAGE PERSONA RENDERING" in (
        required_backend.sys_prompt
    )
    assert "Address the user as Captain" in required_backend.sys_prompt
    assert "Preserve the original Markdown structure" in required_backend.sys_prompt
    assert "Do not add a question, invitation, next step" in (
        required_backend.sys_prompt
    )
    assert "FULL AGENT OPERATIONAL CONTENT" not in required_backend.sys_prompt
    assert "PRIVATE WORKFLOW INSTRUCTIONS" not in required_backend.sys_prompt
    assert required_backend.prompt.startswith(
        "VALIDATED FINAL REPORT (quoted, read-only)"
    )
    assert final_report in required_backend.prompt
    assert "Address the user as Captain" not in required_backend.prompt
    assert provider.tool_call_count == 0

    await provider.package_persona_required_message(
        profile,
        persona_block="Address the user as Captain and use a warm voice.",
        neutral_message="Which account should be changed?",
        message_kind="clarification",
        request_id="request-1:persona-package:clarification:2",
    )

    clarification_backend = manager.backends[-1]
    assert "Keep it as the same clarification question" in (
        clarification_backend.sys_prompt
    )
    assert clarification_backend.prompt.startswith(
        "VALIDATED CLARIFICATION QUESTION (quoted, read-only)"
    )
    assert clarification_backend.tool_registry is None
    assert provider.tool_call_count == 0

    system_md.write_text(
        "FULL AGENT CONTENT WITHOUT A PERSONA BLOCK",
        encoding="utf-8",
    )
    await provider.invoke(
        profile,
        _stage_request(
            Stage.IMMEDIATE_RESPONSE,
            allow_tools=False,
            allow_side_effects=False,
        ),
    )

    fallback_backend = manager.backends[-1]
    assert fallback_backend.sys_prompt.startswith(
        "[persona]\n"
        "Agent display name: agent. Use a polite tone and address the user as 您.\n"
        "[persona_end]"
    )
    assert "FULL AGENT CONTENT WITHOUT A PERSONA BLOCK" not in (
        fallback_backend.sys_prompt
    )


@pytest.mark.asyncio
async def test_raw_provider_commentary_cannot_bypass_persona_packaging():
    manager = _CommentaryManager()
    events = []

    async def capture(event):
        events.append(event)

    provider = HashiStageProvider(
        backend_manager=manager,
        on_stream_event=capture,
    )
    profile = ProviderProfile(
        "premium", "openrouter-api", "configured/model"
    )

    await provider.invoke(
        profile,
        _stage_request(Stage.REPLANNING, allow_tools=False),
    )
    assert events[-1].summary == "Raw provider progress"
    assert events[-1].delivery_class == DELIVERY_INTERNAL

    await provider.invoke(
        profile,
        _stage_request(Stage.EXECUTION, allow_tools=False),
    )
    assert events[-1].delivery_class == DELIVERY_INTERNAL


@pytest.mark.asyncio
async def test_hashi_stage_provider_rejects_tool_backend_without_isolation_capability():
    class UnisolatedBackend:
        def __init__(self):
            self.config = SimpleNamespace(extra={})
            self.capabilities = SimpleNamespace(supports_tool_use=True)
            self.shutdown_called = False

        async def shutdown(self):
            self.shutdown_called = True

    class CapabilityManager:
        privacy_level = 1

        def __init__(self):
            self.backends = []

        def create_ephemeral_backend(self, _engine, target_model=None):
            del target_model
            backend = UnisolatedBackend()
            self.backends.append(backend)
            return backend

    manager = CapabilityManager()
    provider = HashiStageProvider(backend_manager=manager)
    profile = ProviderProfile("premium", "codex-cli", "gpt-configured")

    with pytest.raises(StageInvocationError, match="cannot prove HASHI tool isolation"):
        await provider.invoke(
            profile,
            _stage_request(Stage.EXECUTION, allow_tools=True),
        )
    assert manager.backends[0].shutdown_called is True


@pytest.mark.asyncio
async def test_hashi_stage_provider_accepts_unknown_engine_by_safe_capability():
    class ToolFreeBackend:
        def __init__(self):
            self.config = SimpleNamespace(extra={}, system_md=None, name="safe")
            self.capabilities = SimpleNamespace(supports_tool_use=False)
            self.prompt = ""
            self.shutdown_called = False

        async def initialize(self):
            return True

        async def generate_response(self, prompt, *_args, **_kwargs):
            self.prompt = prompt
            return BackendResponse(
                text="",
                duration_ms=1,
                structured_data={"outcome": "PASS", "summary": "verified"},
            )

        async def shutdown(self):
            self.shutdown_called = True

    class CapabilityManager:
        privacy_level = 1

        def __init__(self):
            self.backends = []

        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == ("new-provider", "model-safe")
            backend = ToolFreeBackend()
            self.backends.append(backend)
            return backend

    manager = CapabilityManager()
    provider = HashiStageProvider(backend_manager=manager)

    result = await provider.invoke(
        ProviderProfile("reviewer", "new-provider", "model-safe"),
        _stage_request(Stage.REVIEW, allow_tools=False),
    )

    assert result.text == ""
    assert result.data == {"outcome": "PASS", "summary": "verified"}
    assert "independent strict HER v2 Reviewer" in manager.backends[0].prompt
    assert manager.backends[0].shutdown_called is True


@pytest.mark.asyncio
async def test_subagent_receives_only_explicitly_delegated_tools():
    manager = _FakeManager()
    base_registry = _BaseToolRegistry()
    provider = HashiStageProvider(
        backend_manager=manager, tool_registry=base_registry
    )
    profile = ProviderProfile(
        "lightweight", "openrouter-api", "configured/model"
    )
    request = _stage_request(Stage.EXECUTION, allow_tools=True)
    request = StageRequest(
        **{
            **request.__dict__,
            "role": "sub_agent:bounded",
            "context": {"delegated_tools": ["file_read"]},
        }
    )

    await provider.invoke(profile, request)

    delegated = manager.backends[-1].tool_registry
    assert delegated.max_loops is None
    names = {
        item["function"]["name"] for item in delegated.get_tool_definitions()
    }
    assert names == {"file_read"}
    allowed = await delegated.execute("file_read", {"path": "a"}, "call-1")
    denied = await delegated.execute("bash", {"command": "whoami"}, "call-2")
    assert allowed.is_error is False
    assert denied.is_error is True
    assert "outside this sub-agent's delegated authority" in denied.output
    assert [item[0] for item in base_registry.executed] == ["file_read"]


@pytest.mark.asyncio
async def test_read_only_delegation_uses_registry_capability_not_her_name_list():
    class RegistryWithCustomReadOnlyTool(_BaseToolRegistry):
        def is_allowed(self, name):
            return name == "custom_inspector" or super().is_allowed(name)

        def allowed_tool_names(self):
            return (*super().allowed_tool_names(), "custom_inspector")

        def is_read_only(self, name):
            return name == "custom_inspector" or super().is_read_only(name)

        def get_tool_definitions(self, tiers=None):
            return [
                *super().get_tool_definitions(tiers=tiers),
                {"type": "function", "function": {"name": "custom_inspector"}},
            ]

    manager = _FakeManager()
    base_registry = RegistryWithCustomReadOnlyTool()
    provider = HashiStageProvider(
        backend_manager=manager, tool_registry=base_registry
    )
    profile = ProviderProfile(
        "lightweight", "openrouter-api", "configured/model"
    )

    await provider.invoke(
        profile,
        _stage_request(
            Stage.EXECUTION, allow_tools=True, allow_side_effects=False
        ),
    )

    shadow_registry = manager.backends[-1].tool_registry
    assert shadow_registry.max_loops is None
    names = {
        item["function"]["name"] for item in shadow_registry.get_tool_definitions()
    }
    assert names == {"file_read", "custom_inspector"}
    allowed = await shadow_registry.execute(
        "file_read", {"path": "a"}, "call-read"
    )
    assert allowed.is_error is False
    assert base_registry.execution_contexts[-1]["safety_mode"] == "read_only"
    assert base_registry.execution_contexts[-1]["authority_mode"] == "her_v2_shadow"
    assert base_registry.audit_context["safety_mode"] == "read_write"
    denied = await shadow_registry.execute(
        "file_write", {"path": "a", "content": "x"}, "call-write"
    )
    assert denied.is_error is True
    assert [item[0] for item in base_registry.executed] == ["file_read"]
    assert base_registry.denials[0][0] == "file_write"
    assert base_registry.denials[0][3]["safety_mode"] == "read_only"
