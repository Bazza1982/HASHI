from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse, TokenUsage
from adapters.deepseek_api import DeepSeekAdapter
from adapters.hashi_api import HashiApiAdapter
from adapters.her_habits import HERHabitStore
from adapters.her_v2 import (
    HashiStageProvider,
    HERv2Adapter,
    _AdapterDelivery,
    _backend_response_error,
)
from adapters.ollama_api import OllamaAdapter
from adapters.openrouter_api import OpenRouterAdapter, _APIResult
from adapters.registry import get_backend_class
from adapters.stream_events import (
    DELIVERY_FINAL,
    DELIVERY_INTERNAL,
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    KIND_COMMENTARY,
    KIND_INITIAL_RESOLUTION,
    KIND_REVIEW,
    KIND_THINKING,
    KIND_TOOL_START,
    StreamEvent,
)
from adapters.xai_api import XaiApiAdapter
from orchestrator import runtime_her_dream, runtime_her_habits
from orchestrator.config import AgentConfig, GlobalConfig
from orchestrator.flexible_backend_registry import (
    BACKEND_REGISTRY,
    canonical_backend_engine,
)
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.checkpoint import (
    CheckpointSnapshot,
    CompulsoryReplanCoordinator,
    ReplanCompletionInterruption,
    ReplanDirective,
)
from orchestrator.her_v2.commentary import PackagedCommentary
from orchestrator.her_v2.config import ProviderProfile
from orchestrator.her_v2.interfaces import ProviderFailureCode, StageInvocationError
from orchestrator.her_v2.ledger import ExecutionLedger, LedgerStore
from orchestrator.her_v2.models import (
    Effort,
    LifecycleState,
    ReplanningOutcome,
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
from orchestrator.her_v2.wip_journal import CONTEXT_HEADER, WIPJournal
from orchestrator.pcm import render_pcm_document


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
        (
            "HTTP 408 Request Timeout",
            ProviderFailureCode.PROVIDER_REQUEST_TIMEOUT,
            True,
        ),
        ("HTTP 429 Rate Limited", ProviderFailureCode.PROVIDER_RATE_LIMITED, True),
        (
            "HTTP 503 Service Unavailable",
            ProviderFailureCode.PROVIDER_SERVER_ERROR,
            True,
        ),
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
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    agent_md = workspace / "agent.md"
    agent_md.write_text(
        render_pcm_document(
            persona="Use a concise, friendly reporting voice.",
            system="Follow HASHI policy.",
        ),
        encoding="utf-8",
    )
    config = AgentConfig(
        name="agent",
        engine="her-v2",
        workspace_dir=workspace,
        system_md=agent_md,
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
            data = {
                "classification": "DIRECT_RESPONSE",
                "real_goal": request.goal,
                "relevant_habits": [],
                "clarification": None,
            }
        else:
            raise AssertionError(f"unexpected stage: {request.stage}")
        return StageResponse(
            text="",
            data=data,
            provider=profile.engine,
            model=profile.model,
            reasoning_trace=None,
        )


class _ZeroProvider:
    def __init__(self):
        self.requests = []

    async def invoke(self, profile, request):
        self.requests.append((profile, request))
        if request.stage is not Stage.DIRECT:
            raise AssertionError(f"unexpected stage: {request.stage}")
        return StageResponse(
            text="Please provide the missing identifier.",
            provider=profile.engine,
            model=profile.model,
            reasoning_trace="direct trace",
        )


@pytest.mark.asyncio
async def test_adapter_injects_prior_wip_and_clears_after_completed_ledger(tmp_path):
    config = _agent_config(tmp_path)
    provider = _DirectProvider()
    setattr(config, "_her_v2_stage_provider", provider)
    journal = WIPJournal(
        config.workspace_dir / "backend_state" / "her_v2" / "wip_journal.jsonl"
    )
    journal.begin_turn(request_id="req-crashed", prompt="unfinished source check")
    journal.append_audit(
        {
            "event": "stage_completed",
            "stage": "execution",
            "payload": {"output": "partial observable result"},
        }
    )
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    assert await adapter.initialize() is True

    response = await adapter.generate_response(
        "What is the current status?", "req-next"
    )

    assert response.is_success is True
    injected_goals = [request.goal for _profile, request in provider.requests]
    assert any(CONTEXT_HEADER in goal for goal in injected_goals)
    assert any("partial observable result" in goal for goal in injected_goals)
    assert journal.records() == []
    audit_rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "agent" / "her_v2_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    lifecycle = {
        row["event"]: row["payload"]
        for row in audit_rows
        if row["stage"] == "wip_journal"
    }
    assert lifecycle["wip_journal_turn_started"]["context_injected"] is True
    assert lifecycle["wip_journal_context_injected"]["record_count"] == 2
    assert lifecycle["wip_journal_cleared"]["ledger_status"] == "COMPLETED"
    assert lifecycle["wip_journal_cleared"]["record_count"] > 2


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
                "real_goal": request.goal,
                "relevant_habits": [],
                "clarification": None,
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
            payload = {
                "classification": "SIMPLE_TASK",
                "real_goal": request.goal,
                "relevant_habits": [],
                "clarification": None,
            }
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
                human_description=("The provider response began but did not complete."),
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
                data={
                    "plan": ["Complete the current request safely"],
                    "success_criteria": ["The current request is completed safely"],
                },
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
        if request.stage is Stage.DIRECT:
            return StageResponse(
                text="Completed directly from the original instruction.",
                provider=profile.engine,
                model=profile.model,
                reasoning_trace="trace:direct",
            )
        payload = {
            Stage.IMMEDIATE_RESPONSE: {"message": "I have it."},
            Stage.TRIAGE: {
                "classification": "SIMPLE_TASK",
                "real_goal": request.goal,
                "relevant_habits": [],
                "clarification": None,
            },
            Stage.PLANNING: {
                "plan": ["Execute the scheduled specification"],
                "success_criteria": ["The scheduled specification is completed"],
            },
            Stage.EXECUTION: {
                "disposition": "COMPLETED",
                "summary": "Completed.",
                "evidence_refs": ["receipt:effort-policy"],
            },
            Stage.REVIEW: {
                "outcome": "UNAVAILABLE",
                "summary": "Tool-backed Review is unavailable in this policy stub.",
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
        "zero",
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
        "delivery_id": response.stream_metadata["her_v2"]["delivery"]["delivery_id"],
        "message_event_id": response.stream_metadata["her_v2"]["delivery"]["event_id"],
    }


@pytest.mark.asyncio
async def test_adapter_zero_effort_is_one_direct_call_and_question_is_completed(
    tmp_path,
):
    provider = _ZeroProvider()
    config = _agent_config(tmp_path, effort="zero")
    skill_manager = SimpleNamespace(
        RUNTIME_TOGGLE_IDS=frozenset({"debug", "recall"}),
        list_skills=lambda: [
            SimpleNamespace(
                id="reports",
                name="Reports",
                description="Build reports",
                skill_dir=tmp_path / "skills" / "reports",
                allowed_tools="Read Write",
            ),
            SimpleNamespace(
                id="debug",
                name="Debug",
                description="Runtime toggle",
                skill_dir=tmp_path / "skills" / "debug",
                allowed_tools=None,
            ),
        ],
        is_skill_enabled=lambda _workspace, skill_id: skill_id != "disabled",
    )
    config._hashi_runtime = SimpleNamespace(skill_manager=skill_manager)
    config._her_v2_stage_provider = provider
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Complete this difficult task directly",
        "request-zero",
    )

    assert response.is_success is True
    assert response.text == "Please provide the missing identifier."
    assert response.stop_reason == "completed"
    assert response.stream_metadata["her_v2"]["terminal_state"] == "COMPLETED"
    assert response.stream_metadata["her_v2"]["classification"] is None
    assert response.stream_metadata["her_v2"]["plan_id"] is None
    assert response.stream_metadata["her_v2"]["effort"] == {
        "configured": "zero",
        "effective": "zero",
        "reason": "agent_default",
    }
    assert len(provider.requests) == 1
    profile, request = provider.requests[0]
    assert request.stage is Stage.DIRECT
    assert request.allow_tools is True
    assert request.allow_side_effects is True
    assert [item["id"] for item in request.context["skills_catalogue"]] == ["reports"]
    assert profile.name == "lightweight"
    assert profile.reasoning == "high"
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_scheduler_direct_policy_is_request_scoped_and_preserves_instruction(
    tmp_path,
):
    provider = _EffortPolicyProvider()
    runtime_context = SimpleNamespace(
        current_request_meta={
            "request_id": "request-scheduled-direct",
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

    original_instruction = (
        "Run the nightly report with sections A, B, and C; preserve every detail."
    )
    scheduled = await adapter.generate_response(
        original_instruction,
        "request-scheduled-direct",
    )

    assert scheduled.is_success is True
    scheduled_stages = [request.stage for _profile, request in provider.requests]
    assert scheduled_stages == [Stage.DIRECT]
    scheduled_profile, scheduled_request = provider.requests[0]
    assert scheduled_request.goal == original_instruction
    assert scheduled_request.classification is None
    assert scheduled.stream_metadata["her_v2"]["effort"] == {
        "configured": "max",
        "effective": "zero",
        "reason": "scheduled_direct_policy",
        "scheduler_kind": "cron",
        "scheduler_task_id": "nightly-report",
        "scheduler_trigger": "scheduled",
    }
    assert scheduled_profile.model == "configured/lightweight"
    assert scheduled_profile.reasoning == "high"

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
    assert ordinary_execution_profile.model == "configured/lightweight"
    assert ordinary_execution_profile.reasoning == "provider-lightweight"
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
    assert (
        adapter.record_transport_delivery_receipt(
            request_id="request-delivery-receipt",
            delivery_id=delivery["delivery_id"],
            delivered=True,
            disposition="transport_delivered",
            chunk_count=1,
        )
        is True
    )
    # The same transport callback is idempotent and cannot duplicate audit truth.
    assert (
        adapter.record_transport_delivery_receipt(
            request_id="request-delivery-receipt",
            delivery_id=delivery["delivery_id"],
            delivered=True,
            disposition="transport_delivered",
            chunk_count=1,
        )
        is True
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "agent" / "her_v2_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    receipts = [row for row in rows if row["event"] == "transport_delivery_receipt"]
    assert len(receipts) == 1
    assert receipts[0]["payload"]["delivery_id"] == delivery["delivery_id"]
    assert receipts[0]["payload"]["message_event_id"] == delivery["event_id"]
    assert receipts[0]["payload"]["delivered"] is True
    assert receipts[0]["payload"]["chunk_count"] == 1


@pytest.mark.asyncio
async def test_adapter_accepts_primary_execution_natural_language_without_finalisation(
    tmp_path,
):
    class _MalformedExecutionProvider(_DirectProvider):
        async def invoke(self, profile, request):
            self.requests.append((profile, request))
            if request.stage is Stage.IMMEDIATE_RESPONSE:
                payload = {"message": "I have it."}
            elif request.stage is Stage.TRIAGE:
                payload = {
                    "classification": "SIMPLE_TASK",
                    "real_goal": request.goal,
                    "relevant_habits": [],
                    "clarification": None,
                }
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

    assert response.is_success is True
    assert response.stop_reason == "completed"
    assert response.error is None
    assert response.stream_metadata["her_v2"]["terminal_state"] == "COMPLETED"
    assert response.text == "execution reply without valid JSON"
    assert (
        sum(request.stage is Stage.EXECUTION for _profile, request in provider.requests)
        == 1
    )
    assert all(
        request.stage is not Stage.FINALISATION
        for _profile, request in provider.requests
    )


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
    assert response.text == "Verified and completed the requested work."
    assert renderer.messages == []
    final_events = [event for event in events if event.delivery_class == DELIVERY_FINAL]
    assert len(final_events) == 1
    assert final_events[0].summary == response.text
    assert final_events[0].provenance == "primary_execution_natural_language"
    assert final_events[0].detail == (
        "execution_workflow_completed=true; finalisation_invoked=false"
    )
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
    user_events = [
        event for event in events if event.delivery_class != DELIVERY_TECHNICAL
    ]
    assert [event.delivery_class for event in user_events] == [
        DELIVERY_USER_COMMENTARY,
        DELIVERY_INTERNAL,
    ]
    assert user_events[0].summary == response.text
    assert user_events[1].kind == KIND_INITIAL_RESOLUTION
    assert user_events[1].resolution == "final"
    assert user_events[1].target_event_id == user_events[0].event_id
    assert events[-1].metadata["lifecycle_state"] == "COMPLETED"


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
    user_events = [
        event for event in events if event.delivery_class != DELIVERY_TECHNICAL
    ]
    assert [event.delivery_class for event in user_events] == [DELIVERY_FINAL]
    assert all(event.kind != KIND_INITIAL_RESOLUTION for event in user_events)
    assert events[-1].metadata["lifecycle_state"] == "COMPLETED"


@pytest.mark.asyncio
async def test_adapter_draft_uses_commentary_lane_and_can_be_replaced() -> None:
    events = []

    async def capture(event):
        events.append(event)
        return True

    delivery = _AdapterDelivery(capture, allow_immediate_response=True)
    draft = await delivery.deliver_packaged_commentary(
        PackagedCommentary(
            source_event_id="turn-1:execution:draft",
            stage=Stage.EXECUTION,
            text="DRAFT RESPONSE\n\nWork completed.",
            provenance="primary_execution_draft",
            draft_response=True,
        )
    )
    resolved = await delivery.resolve_initial(
        resolution="final",
        text="Reviewed final response.",
        target_event_id="turn-1:execution:draft",
        event_id="turn-1:execution:draft:final",
    )

    assert draft is True
    assert events[0].kind == KIND_COMMENTARY
    assert events[0].delivery_class == DELIVERY_USER_COMMENTARY
    assert events[0].summary.startswith("DRAFT RESPONSE")
    assert events[0].provenance == "primary_execution_draft"
    assert events[0].required is True
    assert "exact_primary_execution_text=true" in events[0].detail
    assert resolved.accepted is True and resolved.delivered is True
    assert events[1].kind == KIND_INITIAL_RESOLUTION
    assert events[1].resolution == "final"
    assert events[1].target_event_id == events[0].event_id


@pytest.mark.asyncio
async def test_adapter_draft_is_independent_of_immediate_response_eligibility() -> None:
    events = []

    async def capture(event):
        events.append(event)
        return True

    delivery = _AdapterDelivery(capture, allow_immediate_response=False)
    accepted = await delivery.deliver_packaged_commentary(
        PackagedCommentary(
            source_event_id="turn-1:execution:draft",
            stage=Stage.EXECUTION,
            text="DRAFT RESPONSE\n\nWork completed.",
            provenance="primary_execution_draft",
            draft_response=True,
        )
    )

    assert accepted is True
    assert len(events) == 1
    assert events[0].kind == KIND_COMMENTARY
    assert events[0].required is True
    assert events[0].summary == "DRAFT RESPONSE\n\nWork completed."


@pytest.mark.asyncio
async def test_adapter_activity_uses_typed_technical_lane() -> None:
    events = []

    async def capture(event):
        events.append(event)
        return True

    delivery = _AdapterDelivery(capture, allow_immediate_response=False)
    accepted = await delivery.deliver_activity(
        kind=KIND_REVIEW,
        text="Review pass",
        event_id="turn-1:activity:review:1",
        phase="review",
        metadata={
            "activity_type": "review_result",
            "outcome": "PASS",
            "finding_count": 0,
        },
    )

    assert accepted is True
    assert len(events) == 1
    assert events[0].kind == KIND_REVIEW
    assert events[0].delivery_class == DELIVERY_TECHNICAL
    assert events[0].phase == "review"
    assert events[0].origin == "her_v2:runtime"
    assert events[0].metadata["outcome"] == "PASS"


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
    assert {profile.model for profile in profiles} == {"anthropic/claude-sonnet-4.6"}
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
    assert response.text == "Verified and completed the requested work."

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
    meditation_input = json.loads(meditation_request.context["meditation_input"])
    assert meditation_input["mode"] == "initial"
    assert meditation_input["agent_name"] == "agent"
    assert "maintenance_prompt" not in meditation_request.context
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
        render_pcm_document(
            persona="Use a concise, friendly reporting voice.",
            system="Follow HASHI policy.",
        ),
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
        request
        for _profile, request in provider.requests
        if request.stage is Stage.DREAM
    ]
    assert len(dream_requests) == 2
    assert any(":analysis:" in request.turn_id for request in dream_requests)
    assert any(":persona" in request.turn_id for request in dream_requests)
    analysis_request = next(
        request for request in dream_requests if ":analysis:" in request.turn_id
    )
    persona_request = next(
        request for request in dream_requests if ":persona" in request.turn_id
    )
    assert analysis_request.context["dream_role"] == "maintenance"
    assert json.loads(analysis_request.context["dream_input"])["mode"] == "initial"
    assert persona_request.context["dream_role"] == "report"
    assert json.loads(persona_request.context["dream_input"])["mode"] == (
        "persona_report"
    )
    assert "maintenance_prompt" not in analysis_request.context
    assert "maintenance_prompt" not in persona_request.context
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
        await on_stream_event(StreamEvent(kind=KIND_THINKING, summary="provider trace"))
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
        return SimpleNamespace(
            tool_call_id=tool_call_id, output="allowed", is_error=False
        )

    async def execute_with_audit_context(
        self, name, arguments, tool_call_id="", *, audit_context=None
    ):
        self.execution_contexts.append(dict(audit_context or {}))
        return await self.execute(name, arguments, tool_call_id)

    def record_delegated_denial(self, name, arguments, result, *, audit_context=None):
        self.denials.append((name, arguments, result, dict(audit_context or {})))


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


def _adapter_replan_outcome(*, completion_percent: int = 50) -> ReplanningOutcome:
    return ReplanningOutcome(
        plan={
            "plan": ["Continue from current evidence."],
            "success_criteria": ["done"],
        },
        completion_percent=completion_percent,
        completion_basis="Completed tool receipts were compared with the original goal.",
        plan_changed=False,
        change_reason="",
        next_step="Continue the current plan from the next safe boundary.",
        commentary=(
            f"Progress is {completion_percent}%. The plan is unchanged. "
            "Next, continue from the current evidence."
        ),
    )


def _adapter_replan_directive(
    snapshot: CheckpointSnapshot,
    *,
    completion_percent: int = 50,
) -> ReplanDirective:
    return ReplanDirective(
        checkpoint_id=snapshot.checkpoint_id,
        outcome=_adapter_replan_outcome(completion_percent=completion_percent),
        active_plan_id=f"{snapshot.cycle_id}:plan:v{snapshot.checkpoint_index + 1}",
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
        "idle_timeout_sec" not in backend.config.extra for backend in manager.backends
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
    assert (
        failed["payload"]["retry_invariant_hash"]
        == (completed["payload"]["retry_invariant_hash"])
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
    assert "triage classifier and context preparation agent" in backend.sys_prompt
    assert "configured agent persona" not in backend.sys_prompt
    assert "Original user request and context" in backend.sys_prompt
    assert "Earlier context already contains the result. Please check it." in (
        backend.sys_prompt
    )
    assert backend.prompt == request.goal
    assert "Choose one and only one classification" in backend.sys_prompt
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
    assert "Return exactly one valid JSON object" in backend.sys_prompt
    assert '"real_goal"' in backend.sys_prompt
    assert '"relevant_habits"' in backend.sys_prompt
    assert "checkpoint_policy" not in backend.sys_prompt
    assert "checkpoint_reason" not in backend.sys_prompt
    assert "For Planning, Execution, Replanning, and Review" not in backend.sys_prompt
    assert "her_effort" not in backend.sys_prompt
    assert "tools_authorised_for_this_stage" not in backend.sys_prompt
    assert "external_side_effects_authorised_for_this_stage" not in backend.sys_prompt
    assert "invocation_role" not in backend.sys_prompt
    assert "turn_id" not in backend.sys_prompt
    assert "request_ref" not in backend.sys_prompt

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

    retry_backend = manager.backends[-1]
    assert retry_backend.prompt == backend.prompt
    assert retry_backend.sys_prompt == backend.sys_prompt


@pytest.mark.asyncio
async def test_json_repair_uses_isolated_specialist_prompt_and_no_tools():
    manager = _FakeManager()
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=_BaseToolRegistry(),
    )
    profile = ProviderProfile(
        "triage",
        "openrouter-api",
        "configured/model",
        reasoning="provider-low",
    )
    repair_input = json.dumps(
        {
            "rejected_output": '{"classification":"simple-task"}',
            "required_schema": {
                "classification": (
                    "DIRECT_RESPONSE | SIMPLE_TASK | COMPLEX_TASK | "
                    "HIGH_VOLUME_TASK | CONFIRMATION_REQUIRED"
                ),
                "real_goal": "resolved operative goal or null",
                "relevant_habits": [],
                "clarification": "required question or null",
            },
            "validation_error": "classification is invalid",
        },
        sort_keys=True,
    )
    base = _stage_request(
        Stage.JSON_REPAIR,
        allow_tools=False,
        allow_side_effects=False,
    )
    request = StageRequest(
        **{
            **base.__dict__,
            "role": "json_repair_specialist",
            "goal": repair_input,
            "classification": None,
            "plan_id": None,
            "context": {"json_repair_input": repair_input},
            "request_content": None,
            "attachment_manifest": (),
        }
    )

    await provider.invoke(profile, request)

    backend = manager.backends[-1]
    assert backend.prompt == repair_input
    assert backend.tool_registry is None
    assert backend.sys_prompt.startswith("You are the JSON Repair Agent")
    assert "configured agent persona" not in backend.sys_prompt
    assert "Do the requested work" not in backend.prompt
    assert "Do not call tools" in backend.sys_prompt


@pytest.mark.asyncio
async def test_hashi_stage_provider_enforces_tool_gateway_and_provider_reasoning():
    manager = _FakeManager()
    registry = _BaseToolRegistry()
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
                "sub_agent_results": [
                    {
                        "plan_id": "plan-v1",
                        "assignment_id": "inspection-worker",
                        "disposition": "COMPLETED",
                        "summary": "The delegated inspection completed.",
                        "evidence_refs": ["receipt:inspection"],
                    }
                ],
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
    assert backend.sys_prompt.startswith("You are an execution agent")
    assert "RECENT TURN MESSAGES" in backend.prompt
    assert "Memory+ recent facts" in backend.prompt
    assert "Cross-session receipt evidence" in backend.prompt
    assert '"inspect"' in backend.sys_prompt
    assert '"assignment_id": "inspection-worker"' in backend.sys_prompt
    assert '"plan_id": "plan-v1"' in backend.sys_prompt
    assert "only that runtime-attached batch" in backend.sys_prompt
    assert "natural language" in backend.sys_prompt
    assert "Return exactly one JSON object" not in backend.sys_prompt
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
        "You are an independent reviewer agent for an agentic workflow"
    )
    assert "No review tools are available" in reviewer_backend.sys_prompt
    assert "Return exactly one valid JSON object" in reviewer_backend.sys_prompt
    assert reviewer_backend.prompt == "Do the requested work"

    for stage in (Stage.PLANNING, Stage.REPLANNING):
        await provider.invoke(
            profile,
            _stage_request(stage, allow_tools=False, allow_side_effects=False),
        )
        planning_backend = manager.backends[-1]
        assert planning_backend.prompt == "Do the requested work"
        assert "configured agent persona" not in planning_backend.sys_prompt
        assert '"name": "file_read"' in planning_backend.sys_prompt
        assert '"name": "file_write"' in planning_backend.sys_prompt
        assert '"hashi_read_only": true' in planning_backend.sys_prompt
        assert "Never invent a tool or capability name" in planning_backend.sys_prompt
        if stage is Stage.PLANNING:
            assert "planning agent for an agentic workflow" in (
                planning_backend.sys_prompt
            )
        else:
            assert "replanning agent in an agentic workflow" in (
                planning_backend.sys_prompt
            )
            assert '"completion_percent"' in planning_backend.sys_prompt

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
        "You are the finalisation agent in an agentic workflow"
    )
    assert "Return only the final user-facing response" in (
        finalisation_backend.sys_prompt
    )
    assert "Agent display name: agent" in finalisation_backend.sys_prompt
    assert "configured agent persona" not in finalisation_backend.sys_prompt
    assert "Return exactly one JSON object" not in finalisation_backend.sys_prompt
    assert finalisation_backend.prompt == "Do the requested work"


@pytest.mark.asyncio
async def test_review_system_prompt_receives_resolved_goal_evidence_and_real_tools():
    class ReviewToolRegistry(_BaseToolRegistry):
        def is_allowed(self, name):
            return name in {"file_read", "verification_run"}

        def allowed_tool_names(self):
            return ("file_read", "verification_run")

        def get_tool_definitions(self, tiers=None):
            del tiers
            return [
                {"type": "function", "function": {"name": "file_read"}},
                {
                    "type": "function",
                    "function": {"name": "verification_run"},
                },
            ]

    manager = _FakeManager()
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=ReviewToolRegistry(),
    )
    request = _stage_request(
        Stage.REVIEW,
        allow_tools=True,
        allow_side_effects=True,
    )
    request = StageRequest(
        **{
            **request.__dict__,
            "context": {
                "active_plan": {
                    "plan": ["implement"],
                    "success_criteria": ["tests pass"],
                },
                "draft_response": "Implemented the requested change.",
                "execution": {
                    "disposition": "COMPLETED",
                    "summary": "Implemented the requested change.",
                },
                "evidence_refs": ["receipt:execution:42"],
                "review_kind": "closure",
                "findings_to_close": ["The original test evidence was incomplete."],
                "delegated_tools": ["file_read", "verification_run"],
                "verification_run_policy": {
                    "workspace": "authoritative_current_workspace"
                },
                "execution_elapsed_s": 10.0,
            },
        }
    )

    await provider.invoke(
        ProviderProfile("reviewer", "openrouter-api", "configured/model"),
        request,
    )

    backend = manager.backends[-1]
    assert backend.prompt == request.goal
    assert "Implemented the requested change." in backend.sys_prompt
    assert "authoritative resolved goal" in backend.sys_prompt
    assert "original request" not in backend.sys_prompt
    assert '"tests pass"' not in backend.sys_prompt
    assert '"review_kind": "closure"' in backend.sys_prompt
    assert "The original test evidence was incomplete." in backend.sys_prompt
    assert '"disposition": "COMPLETED"' in backend.sys_prompt
    assert '"receipt:execution:42"' in backend.sys_prompt
    assert '"name": "file_read"' in backend.sys_prompt
    assert '"name": "verification_run"' in backend.sys_prompt
    assert "configured agent persona" not in backend.sys_prompt


@pytest.mark.asyncio
async def test_empty_delegated_catalogue_does_not_block_provider_invocation():
    manager = _FakeManager()
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=_BaseToolRegistry(),
    )
    request = _stage_request(
        Stage.EXECUTION,
        allow_tools=True,
        allow_side_effects=False,
    )
    request = StageRequest(
        **{
            **request.__dict__,
            "role": "sub_agent:unknown-tool",
            "context": {"delegated_tools": ["invented_search"]},
        }
    )

    response = await provider.invoke(
        ProviderProfile("worker", "openrouter-api", "configured/model"),
        request,
    )

    assert json.loads(response.text)["disposition"] == "COMPLETED"
    assert manager.backends[-1].shutdown_called is True
    assert manager.backends[-1].prompt
    assert manager.backends[-1].tool_registry.get_tool_definitions() == []


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
async def test_hashi_stage_provider_installs_full_direct_contract_and_tools():
    manager = _FakeManager()
    registry = _BaseToolRegistry()
    events = []

    async def capture(event):
        events.append(event)

    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=registry,
        on_stream_event=capture,
    )
    base = _stage_request(
        Stage.DIRECT,
        allow_tools=True,
        allow_side_effects=True,
    )
    request = StageRequest(
        **{
            **base.__dict__,
            "role": "lightweight",
            "effort": Effort.ZERO,
            "classification": None,
            "plan_id": None,
            "context": {
                "habit_catalogue": ["Verify before reporting success."],
                "skills_catalogue": [
                    {
                        "id": "reports",
                        "description": "Build reports",
                        "skill_md": "/skills/reports/SKILL.md",
                    }
                ],
            },
        }
    )

    response = await provider.invoke(
        ProviderProfile(
            "lightweight",
            "openrouter-api",
            "configured/model",
            reasoning="high",
        ),
        request,
    )

    backend = manager.backends[-1]
    assert response.text
    assert backend.prompt == "Do the requested work"
    assert "zero-orchestration Direct route" in backend.sys_prompt
    assert "Never hand the task off" in backend.sys_prompt
    assert '"name": "file_write"' in backend.sys_prompt
    assert '"id": "reports"' in backend.sys_prompt
    assert "Verify before reporting success." in backend.sys_prompt
    assert backend.tool_registry.is_allowed("file_write") is True
    assert backend.tool_registry.max_loops is None
    assert backend.config.extra["reasoning_effort"] == "high"
    assert backend.reasoning_enabled is True
    assert [event.kind for event in events] == [KIND_THINKING, KIND_TOOL_START]
    assert all(event.kind != KIND_COMMENTARY for event in events)


@pytest.mark.asyncio
async def test_review_verification_tool_injects_timeout_and_workspace_authority():
    class VerificationToolRegistry(_BaseToolRegistry):
        def is_allowed(self, name):
            return name == "verification_run" or super().is_allowed(name)

        def allowed_tool_names(self):
            return (*super().allowed_tool_names(), "verification_run")

        def get_tool_definitions(self, tiers=None):
            return [
                *super().get_tool_definitions(tiers=tiers),
                {"type": "function", "function": {"name": "verification_run"}},
            ]

    class CallingVerificationBackend(_FakeBackend):
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
            await self.tool_registry.execute(
                "verification_run",
                {
                    "operation": "run",
                    "argv": ["python", "-V"],
                    "timeout_s": 60,
                    "_hashi_verification_policy": {"execution_elapsed_s": 1},
                },
                "verification-call",
            )
            return BackendResponse(
                text=('{"status":"PASS","reason":"policy captured","conditions":null}'),
                duration_ms=1,
                tool_call_count=1,
                tool_loop_count=1,
            )

    class CallingVerificationManager(_FakeManager):
        def create_ephemeral_backend(self, engine, target_model=None):
            backend = CallingVerificationBackend(self.system_md)
            self.backends.append(backend)
            return backend

    manager = CallingVerificationManager()
    registry = VerificationToolRegistry()
    provider = HashiStageProvider(backend_manager=manager, tool_registry=registry)
    request = _stage_request(
        Stage.REVIEW,
        allow_tools=True,
        allow_side_effects=True,
    )
    request = StageRequest(
        **{
            **request.__dict__,
            "context": {
                "delegated_tools": ["verification_run"],
                "execution_elapsed_s": 3600,
                "verification_run_policy": {
                    "workspace": "authoritative_current_workspace",
                    "environment": "inherited",
                    "network": "inherited",
                },
            },
        }
    )

    await provider.invoke(
        ProviderProfile("reviewer", "openrouter-api", "configured/model"),
        request,
    )

    _name, arguments, _call_id = registry.executed[-1]
    assert arguments["timeout_s"] == 60
    assert arguments["_hashi_verification_policy"] == {
        "workspace": "authoritative_current_workspace",
        "environment": "inherited",
        "network": "inherited",
        "execution_elapsed_s": 3600,
    }
    assert registry.execution_contexts[-1]["safety_mode"] == ("workspace_verification")
    assert registry.execution_contexts[-1]["authority_mode"] == (
        "her_v2_review_verification"
    )
    assert registry.execution_contexts[-1]["verification_execution_elapsed_s"] == 3600


@pytest.mark.asyncio
async def test_hashi_stage_provider_makes_every_effort_tool_loop_unbounded():
    manager = _FakeManager()
    registry = _BaseToolRegistry()
    registry.max_loops = 8
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=registry,
    )
    profile = ProviderProfile("premium", "openrouter-api", "configured/model")
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
async def test_hashi_stage_provider_replans_exact_receipts_without_capping_loop():
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return _adapter_replan_directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="turn-1:execution-cycle:1",
        evaluator=evaluator,
        clock=lambda: 0.0,
    )

    class ElevenToolBackend(_FakeBackend):
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
            self.results = []
            for index in range(1, 12):
                self.results.append(
                    await self.tool_registry.execute(
                        "file_read",
                        {"path": f"evidence-{index}.txt"},
                        f"provider-call-{index}",
                    )
                )
            return BackendResponse(
                text='{"disposition":"COMPLETED","summary":"done"}',
                duration_ms=1,
                tool_call_count=11,
                tool_loop_count=11,
            )

    class ElevenToolManager(_FakeManager):
        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == (
                "openrouter-api",
                "configured/model",
            )
            backend = ElevenToolBackend(self.system_md)
            self.backends.append(backend)
            return backend

    manager = ElevenToolManager()
    registry = _BaseToolRegistry()
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=registry,
    )
    request = _stage_request(
        Stage.EXECUTION,
        allow_tools=True,
        allow_side_effects=True,
    )
    request = StageRequest(
        **{
            **request.__dict__,
            "invocation_id": "turn-1:execution:invocation:1",
            "checkpoint_coordinator": coordinator,
        }
    )

    response = await provider.invoke(
        ProviderProfile("premium", "openrouter-api", "configured/model"),
        request,
    )
    await coordinator.close()

    assert len(snapshots) == 1
    assert snapshots[0].completed_result_count == 10
    assert len(response.tool_receipts) == 11
    assert len(registry.executed) == 11
    assert coordinator.checkpoint_count == 1
    assert manager.backends[0].tool_registry.max_loops is None
    assert all(
        "HASHI_EVIDENCE_RECEIPT" in result.output
        for result in manager.backends[0].results
    )
    assert "HASHI_COMPULSORY_REPLAN" in manager.backends[0].results[9].output
    assert "HASHI_COMPULSORY_REPLAN" not in manager.backends[0].results[10].output


@pytest.mark.asyncio
async def test_hashi_stage_provider_preserves_typed_replan_completion():
    async def evaluator(snapshot):
        return _adapter_replan_directive(
            snapshot,
            completion_percent=100,
        )

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="turn-1:execution-cycle:1",
        evaluator=evaluator,
        clock=lambda: 0.0,
    )

    class InterruptingBackend(_FakeBackend):
        async def generate_response(
            self,
            prompt,
            request_id,
            is_retry=False,
            silent=False,
            on_stream_event=None,
        ):
            del prompt, request_id, is_retry, silent, on_stream_event
            for index in range(1, 12):
                await self.tool_registry.execute(
                    "file_write",
                    {"path": f"record-{index}"},
                    f"provider-call-{index}",
                )
            raise AssertionError("an eleventh tool must not start")

    class InterruptingManager(_FakeManager):
        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == (
                "openrouter-api",
                "configured/model",
            )
            backend = InterruptingBackend(self.system_md)
            self.backends.append(backend)
            return backend

    manager = InterruptingManager()
    registry = _BaseToolRegistry()
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=registry,
    )
    request = _stage_request(
        Stage.EXECUTION,
        allow_tools=True,
        allow_side_effects=True,
    )
    request = StageRequest(
        **{
            **request.__dict__,
            "invocation_id": "turn-1:execution:invocation:1",
            "checkpoint_coordinator": coordinator,
        }
    )

    with pytest.raises(ReplanCompletionInterruption) as raised:
        await provider.invoke(
            ProviderProfile("premium", "openrouter-api", "configured/model"),
            request,
        )
    await coordinator.close()

    assert raised.value.directive.outcome.completion_percent == 100
    assert raised.value.snapshot.completed_result_count == 10
    assert len(registry.executed) == 10
    assert manager.backends[0].shutdown_called is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_type",
    [
        OpenRouterAdapter,
        DeepSeekAdapter,
        OllamaAdapter,
        XaiApiAdapter,
        HashiApiAdapter,
    ],
)
async def test_tool_capable_api_families_do_not_flatten_replan_control(
    adapter_type,
):
    snapshot = CheckpointSnapshot(
        cycle_id="turn-1:execution-cycle:1",
        checkpoint_id="turn-1:execution-cycle:1:checkpoint:1",
        checkpoint_index=1,
        trigger_reasons=("completed_result_count",),
        completed_result_count=10,
        elapsed_s=1.0,
        receipt_summaries=(),
        receipt_set_sha256="sha256",
        boundary_kind="completed_tool_result",
    )
    interruption = ReplanCompletionInterruption(
        _adapter_replan_directive(snapshot, completion_percent=100),
        snapshot,
        (),
    )
    adapter = object.__new__(adapter_type)
    adapter.sys_prompt = ""
    adapter.api_key = "test-key"
    adapter.tool_registry = object()
    adapter.config = SimpleNamespace(model="test-model", name="test-agent")
    adapter.global_config = SimpleNamespace(openrouter_url="https://invalid.test")
    adapter.logger = SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    adapter._ensure_client = lambda: None
    adapter._touch_activity = lambda: None
    adapter._build_payload = lambda *_args, **_kwargs: {}
    adapter._deepseek_headers = lambda: {}
    adapter._ollama_headers = lambda: {}
    adapter._xai_headers = lambda: {}
    adapter._hashi_headers = lambda **_kwargs: {}
    adapter._use_responses_api = lambda: False

    async def resolve_bearer(*_args, **_kwargs):
        return None

    async def call_api_once(*_args, **_kwargs):
        return _APIResult(
            text="",
            tool_calls=[
                {
                    "id": "call-10",
                    "type": "function",
                    "function": {"name": "file_write", "arguments": "{}"},
                }
            ],
            finish_reason="tool_calls",
        )

    async def interrupt_tool_boundary(*_args, **_kwargs):
        raise interruption

    adapter._resolve_bearer = resolve_bearer
    adapter._call_api_once = call_api_once
    adapter._run_tool_calls = interrupt_tool_boundary

    with pytest.raises(ReplanCompletionInterruption) as raised:
        await adapter_type.generate_response(adapter, "continue", "request-1")
    assert raised.value is interruption


@pytest.mark.asyncio
async def test_policy_denial_returns_before_due_replan_gates_next_admission():
    timeline = []
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        assert timeline[-1] == "denial_returned"
        timeline.append("checkpoint")
        return _adapter_replan_directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="turn-1:execution-cycle:1",
        evaluator=evaluator,
        clock=lambda: 0.0,
    )

    class DenyingRegistry(_BaseToolRegistry):
        async def execute(self, name, arguments, tool_call_id=""):
            self.executed.append((name, arguments, tool_call_id))
            if len(self.executed) == 10:
                return SimpleNamespace(
                    tool_call_id=tool_call_id,
                    output="Error: approval required",
                    is_error=True,
                    details={"control_disposition": "approval_required"},
                )
            return SimpleNamespace(
                tool_call_id=tool_call_id,
                output="allowed",
                is_error=False,
            )

    class DenialBackend(_FakeBackend):
        async def generate_response(
            self,
            prompt,
            request_id,
            is_retry=False,
            silent=False,
            on_stream_event=None,
        ):
            del prompt, request_id, is_retry, silent, on_stream_event
            for index in range(1, 10):
                await self.tool_registry.execute(
                    "file_read", {}, f"provider-call-{index}"
                )
            denied = await self.tool_registry.execute(
                "file_write", {}, "provider-call-10"
            )
            assert denied.is_error is True
            timeline.append("denial_returned")
            replan_control = await self.tool_registry.execute(
                "file_read", {}, "provider-call-11"
            )
            assert replan_control.is_error is True
            assert replan_control.details["control_disposition"] == "compulsory_replan"
            timeline.append("replan_control_returned")
            allowed = await self.tool_registry.execute(
                "file_read", {}, "provider-call-12"
            )
            assert allowed.is_error is False
            timeline.append("next_tool_completed")
            return BackendResponse(
                text='{"disposition":"COMPLETED","summary":"done"}',
                duration_ms=1,
                tool_call_count=12,
                tool_loop_count=12,
            )

    class DenialManager(_FakeManager):
        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == (
                "openrouter-api",
                "configured/model",
            )
            backend = DenialBackend(self.system_md)
            self.backends.append(backend)
            return backend

    provider = HashiStageProvider(
        backend_manager=DenialManager(),
        tool_registry=DenyingRegistry(),
    )
    request = _stage_request(
        Stage.EXECUTION,
        allow_tools=True,
        allow_side_effects=True,
    )
    request = StageRequest(
        **{
            **request.__dict__,
            "checkpoint_coordinator": coordinator,
        }
    )

    response = await provider.invoke(
        ProviderProfile("premium", "openrouter-api", "configured/model"),
        request,
    )
    await coordinator.close()

    assert timeline == [
        "denial_returned",
        "checkpoint",
        "replan_control_returned",
        "next_tool_completed",
    ]
    assert snapshots[0].completed_result_count == 10
    assert snapshots[0].receipt_summaries[-1]["status"] == "FAILED"
    assert len(response.tool_receipts) == 11


@pytest.mark.asyncio
async def test_tool_registry_denial_precedes_time_due_replan():
    from tools.registry import ToolResult

    now = [0.0]
    timeline = []

    async def evaluator(snapshot):
        timeline.append("checkpoint")
        return _adapter_replan_directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="turn-1:execution-cycle:1",
        evaluator=evaluator,
        clock=lambda: now[0],
    )

    class PreflightRegistry(_BaseToolRegistry):
        def evaluate_admission(self, name, arguments, tool_call_id=""):
            del arguments
            if name == "file_write":
                return ToolResult(
                    tool_call_id=tool_call_id,
                    output="Error: path is outside the authorised workzone",
                    is_error=True,
                    details={"control_disposition": "denied"},
                )
            return None

    class PreflightBackend(_FakeBackend):
        async def generate_response(
            self,
            prompt,
            request_id,
            is_retry=False,
            silent=False,
            on_stream_event=None,
        ):
            del prompt, request_id, is_retry, silent, on_stream_event
            now[0] = 300.0
            denied = await self.tool_registry.execute(
                "file_write", {"path": "outside"}, "provider-denied"
            )
            assert denied.is_error is True
            timeline.append("denial_returned")
            replan_control = await self.tool_registry.execute(
                "file_read", {"path": "inside"}, "provider-allowed"
            )
            assert replan_control.is_error is True
            assert replan_control.details["control_disposition"] == "compulsory_replan"
            timeline.append("replan_control_returned")
            allowed = await self.tool_registry.execute(
                "file_read", {"path": "inside"}, "provider-allowed-after-replan"
            )
            assert allowed.is_error is False
            timeline.append("allowed_returned")
            return BackendResponse(
                text='{"disposition":"COMPLETED","summary":"done"}',
                duration_ms=1,
                tool_call_count=3,
                tool_loop_count=3,
            )

    class PreflightManager(_FakeManager):
        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == (
                "openrouter-api",
                "configured/model",
            )
            backend = PreflightBackend(self.system_md)
            self.backends.append(backend)
            return backend

    registry = PreflightRegistry()
    provider = HashiStageProvider(
        backend_manager=PreflightManager(),
        tool_registry=registry,
    )
    request = _stage_request(
        Stage.EXECUTION,
        allow_tools=True,
        allow_side_effects=True,
    )
    request = StageRequest(
        **{
            **request.__dict__,
            "checkpoint_coordinator": coordinator,
        }
    )

    response = await provider.invoke(
        ProviderProfile("premium", "openrouter-api", "configured/model"),
        request,
    )
    await coordinator.close()

    assert timeline == [
        "denial_returned",
        "checkpoint",
        "replan_control_returned",
        "allowed_returned",
    ]
    assert registry.executed == [
        (
            "file_read",
            {"path": "inside"},
            "provider-allowed-after-replan",
        )
    ]
    assert len(registry.denials) == 1
    assert len(response.tool_receipts) == 2


@pytest.mark.asyncio
async def test_persona_presentation_lanes_receive_only_block_and_minimal_inputs(
    tmp_path,
):
    system_md = tmp_path / "agent.md"
    system_md.write_text(
        render_pcm_document(
            persona="Use a warm voice and address the user as Captain.",
            system=("FULL AGENT OPERATIONAL CONTENT\nPRIVATE WORKFLOW INSTRUCTIONS"),
        ),
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
        "You are an immediate response agent"
    )
    assert "Use a warm voice and address the user as Captain." in (
        immediate_backend.sys_prompt
    )
    assert "Choose exactly one of the following two response modes" in (
        immediate_backend.sys_prompt
    )
    assert "When this mode applies, answer the user's request directly" in (
        immediate_backend.sys_prompt
    )
    assert "requires checking, execution, new evidence, tool use" in (
        immediate_backend.sys_prompt
    )
    assert "Do not perform or simulate the work" in immediate_backend.sys_prompt
    assert "Do not imply that the work has already been completed" in (
        immediate_backend.sys_prompt
    )
    assert "FULL AGENT OPERATIONAL CONTENT" not in immediate_backend.sys_prompt
    assert "PRIVATE WORKFLOW INSTRUCTIONS" not in immediate_backend.sys_prompt
    assert "GLOBAL SYS CONTENT" not in immediate_backend.prompt
    assert "FULL AGENT OPERATIONAL CONTENT" not in immediate_backend.prompt
    assert "Earlier context remains available" in immediate_backend.prompt
    assert "Please scan Outlook" in immediate_backend.prompt
    assert "Please scan Outlook" in immediate_backend.sys_prompt
    assert "$goal" not in immediate_backend.sys_prompt
    assert "Return exactly one JSON object" not in immediate_backend.prompt
    assert "Return exactly one JSON object" not in immediate_backend.sys_prompt
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
                "draft_response": "The requested work completed.",
                "reviewer_findings": {
                    "status": "PASS",
                    "reason": "The completed work satisfies the request.",
                    "conditions": None,
                },
                "completion_evidence": {
                    "evidence_refs": ["receipt:42"],
                    "limitations": [],
                },
            },
        }
    )

    await provider.invoke(profile, finalisation_request)

    finalisation_backend = manager.backends[-1]
    assert "Use a warm voice and address the user as Captain." in (
        finalisation_backend.sys_prompt
    )
    assert "FULL AGENT OPERATIONAL CONTENT" not in (finalisation_backend.sys_prompt)
    assert "PRIVATE WORKFLOW INSTRUCTIONS" not in (finalisation_backend.sys_prompt)
    assert "The requested work completed." in finalisation_backend.sys_prompt
    assert '"status": "PASS"' in finalisation_backend.sys_prompt
    assert '"evidence_refs"' in finalisation_backend.sys_prompt
    assert finalisation_backend.prompt == finalisation_request.goal
    assert "Return exactly one JSON object" not in finalisation_backend.sys_prompt

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
    assert (
        "Agent display name: agent. Use the configured default language and a "
        "respectful tone." in fallback_backend.sys_prompt
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
    profile = ProviderProfile("premium", "openrouter-api", "configured/model")

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
            self.sys_prompt = ""
            self.shutdown_called = False

        def set_system_prompt(self, prompt):
            self.sys_prompt = prompt

        async def initialize(self):
            return True

        async def generate_response(self, prompt, *_args, **_kwargs):
            self.prompt = prompt
            return BackendResponse(
                text="",
                duration_ms=1,
                structured_data={
                    "status": "PASS",
                    "reason": "verified",
                    "conditions": None,
                },
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
    assert result.data == {
        "status": "PASS",
        "reason": "verified",
        "conditions": None,
    }
    assert "independent reviewer agent" in manager.backends[0].sys_prompt
    assert manager.backends[0].prompt == "Do the requested work"
    assert manager.backends[0].shutdown_called is True


@pytest.mark.asyncio
async def test_subagent_receives_only_explicitly_delegated_tools():
    manager = _FakeManager()
    base_registry = _BaseToolRegistry()
    provider = HashiStageProvider(backend_manager=manager, tool_registry=base_registry)
    profile = ProviderProfile("lightweight", "openrouter-api", "configured/model")
    request = _stage_request(Stage.EXECUTION, allow_tools=True)
    request = StageRequest(
        **{
            **request.__dict__,
            "role": "sub_agent:bounded",
            "context": {
                "assignment_id": "bounded",
                "assigned_task": "Inspect one file",
                "delegated_tools": ["file_read"],
                "authorized_attachment_ids": [],
                "authority": {
                    "scope": "bounded_execution_only",
                    "may_replan": False,
                    "may_contact_user": False,
                    "may_finalise": False,
                    "may_create_subagents": False,
                },
            },
        }
    )

    await provider.invoke(profile, request)

    delegated = manager.backends[-1].tool_registry
    assert manager.backends[-1].sys_prompt.startswith(
        "You are a bounded HER v2 sub-agent."
    )
    assert "Bounded assignment and authority envelope" in (
        manager.backends[-1].sys_prompt
    )
    assert '"plan_id": "plan-v1"' in manager.backends[-1].sys_prompt
    assert '"may_replan": false' in manager.backends[-1].sys_prompt
    assert manager.backends[-1].prompt == request.goal
    assert delegated.max_loops is None
    names = {item["function"]["name"] for item in delegated.get_tool_definitions()}
    assert names == {"file_read"}
    allowed = await delegated.execute("file_read", {"path": "a"}, "call-1")
    denied = await delegated.execute("bash", {"command": "whoami"}, "call-2")
    assert allowed.is_error is False
    assert denied.is_error is True
    assert "outside this sub-agent's delegated authority" in denied.output
    assert [item[0] for item in base_registry.executed] == ["file_read"]


@pytest.mark.asyncio
async def test_superseded_subagent_plan_cannot_start_another_tool():
    manager = _FakeManager()
    base_registry = _BaseToolRegistry()
    provider = HashiStageProvider(backend_manager=manager, tool_registry=base_registry)
    profile = ProviderProfile("lightweight", "openrouter-api", "configured/model")
    directive = ReplanDirective(
        checkpoint_id="checkpoint-1",
        outcome=_adapter_replan_outcome(),
        active_plan_id="plan-v2",
    )
    request = _stage_request(Stage.EXECUTION, allow_tools=True)
    request = StageRequest(
        **{
            **request.__dict__,
            "role": "sub_agent:bounded",
            "context": {
                "assignment_id": "bounded",
                "assigned_task": "Inspect one file",
                "delegated_tools": ["file_read"],
                "authorized_attachment_ids": [],
            },
            "checkpoint_coordinator": SimpleNamespace(latest_directive=directive),
        }
    )

    await provider.invoke(profile, request)
    denied = await manager.backends[-1].tool_registry.execute(
        "file_read", {"path": "a"}, "call-after-replan"
    )

    assert denied.is_error is True
    assert "HASHI_PLAN_SUPERSEDED" in denied.output
    assert "bound_plan_id: plan-v1" in denied.output
    assert "active_plan_id: plan-v2" in denied.output
    assert base_registry.executed == []
    assert [
        item[2].details["control_disposition"] for item in base_registry.denials
    ] == ["plan_superseded"]


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
    provider = HashiStageProvider(backend_manager=manager, tool_registry=base_registry)
    profile = ProviderProfile("lightweight", "openrouter-api", "configured/model")

    await provider.invoke(
        profile,
        _stage_request(Stage.EXECUTION, allow_tools=True, allow_side_effects=False),
    )

    shadow_registry = manager.backends[-1].tool_registry
    assert shadow_registry.max_loops is None
    names = {
        item["function"]["name"] for item in shadow_registry.get_tool_definitions()
    }
    assert names == {"file_read", "custom_inspector"}
    allowed = await shadow_registry.execute("file_read", {"path": "a"}, "call-read")
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
