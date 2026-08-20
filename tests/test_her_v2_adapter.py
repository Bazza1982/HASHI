from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse, TokenUsage
from adapters.her_habits import HERHabitStore
from adapters.her_v2 import HERv2Adapter, HashiStageProvider
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
from orchestrator.her_v2.interfaces import StageInvocationError
from orchestrator.her_v2.ledger import ExecutionLedger, LedgerStore
from orchestrator.her_v2.models import (
    Effort,
    LifecycleState,
    Stage,
    StageRequest,
    StageResponse,
    TriageClassification,
)
from orchestrator import runtime_her_dream, runtime_her_habits


def _profiles():
    return {
        name: {
            "engine": "openrouter-api",
            "model": f"configured/{name}",
            "reasoning": f"provider-{name}",
            "max_attempts": 1,
        }
        for name in ("lightweight", "triage", "premium", "reviewer", "orchestrator")
    }


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
            Stage.FINALISATION: {"report": "Completed and verified."},
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


def test_retired_her_ids_resolve_forward_to_the_only_her_backend():
    assert get_backend_class("her-v2") is HERv2Adapter
    assert get_backend_class("her") is HERv2Adapter
    assert get_backend_class("claw-cli") is HERv2Adapter
    assert canonical_backend_engine("her") == "her-v2"
    assert canonical_backend_engine("claw-cli") == "her-v2"
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
async def test_adapter_surfaces_reconciliation_required_as_non_success(tmp_path):
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
            elif request.stage is Stage.STRUCTURE_REPAIR:
                return StageResponse(
                    text="repair reply without valid JSON",
                    provider=profile.engine,
                    model=profile.model,
                )
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
        her_v2={"profiles": _profiles(), "structured_repair_attempts": 2},
    )
    setattr(config, "_her_v2_stage_provider", provider)
    adapter = HERv2Adapter(config, _global_config(tmp_path))

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "Perform the action once", "request-adapter-reconciliation"
    )

    assert response.is_success is False
    assert response.stop_reason == "reconciliation_required"
    assert "was not replayed" in response.error
    assert response.stream_metadata["her_v2"]["terminal_state"] == (
        "RECONCILIATION_REQUIRED"
    )
    assert sum(
        request.stage is Stage.EXECUTION
        for _profile, request in provider.requests
    ) == 1
    assert sum(
        request.stage is Stage.STRUCTURE_REPAIR
        for _profile, request in provider.requests
    ) == 2


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
    assert len(dream_requests) == 1
    assert dream_requests[0].allow_tools is False
    assert dream_requests[0].allow_side_effects is False
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
    def __init__(self):
        self.config = SimpleNamespace(extra={})
        self.tool_registry = "not-set"
        self.privacy_level = None
        self.reasoning_enabled = None
        self.shutdown_called = False
        self.prompt = ""
        self.sys_prompt = "configured agent persona"

    def set_reasoning_enabled(self, enabled):
        self.reasoning_enabled = enabled

    async def initialize(self):
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
    def __init__(self):
        self.backends = []
        self.privacy_level = 1

    def create_ephemeral_backend(self, engine, target_model=None):
        assert engine == "openrouter-api"
        assert target_model == "configured/model"
        backend = _FakeBackend()
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
        backend = _CommentaryFakeBackend()
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

    result = await provider.invoke(
        profile,
        _stage_request(Stage.EXECUTION, allow_tools=True, allow_side_effects=True),
    )

    backend = manager.backends[-1]
    assert backend.tool_registry is registry
    assert backend.reasoning_enabled is True
    assert backend.config.extra["reasoning_effort"] == "provider-high"
    assert '"her_effort": "xhigh"' in backend.prompt
    assert "provider-high" not in backend.prompt
    assert result.reasoning_trace == "provider trace"
    assert result.evidence_refs == ("hashi-tools:turn-1:1",)
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
    assert "independent advisory reviewer" in reviewer_backend.prompt
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

    repair_request = _stage_request(
        Stage.STRUCTURE_REPAIR,
        allow_tools=False,
        allow_side_effects=False,
    )
    repair_request = StageRequest(
        **{
            **repair_request.__dict__,
            "context": {
                "repair_target_stage": Stage.EXECUTION.value,
                "original_provider_response": {"text": "not json"},
                "original_execution_must_not_be_replayed": True,
            },
        }
    )
    await provider.invoke(profile, repair_request)
    repair_backend = manager.backends[-1]
    assert repair_backend.tool_registry is None
    assert repair_backend.sys_prompt.startswith(
        "You are the isolated HER v2 structure repair role"
    )
    assert '"tools_authorised_for_this_stage": false' in repair_backend.prompt
    assert '"external_side_effects_authorised_for_this_stage": false' in (
        repair_backend.prompt
    )


@pytest.mark.asyncio
async def test_persona_packaging_is_tool_free_and_receives_only_block_and_neutral_text():
    manager = _FakeManager()
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
    assert "Address the user as Captain" not in backend.prompt
    assert "The old endpoint was removed" in backend.prompt
    assert "authoritative_user_goal" not in backend.prompt
    assert "plan_steps" not in backend.prompt
    assert provider.tool_call_count == 0


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
async def test_hashi_stage_provider_rejects_non_gateway_cli_before_invocation():
    manager = _FakeManager()
    provider = HashiStageProvider(backend_manager=manager)
    profile = ProviderProfile("premium", "codex-cli", "gpt-configured")

    with pytest.raises(StageInvocationError, match="not certified"):
        await provider.invoke(
            profile,
            _stage_request(Stage.EXECUTION, allow_tools=True),
        )
    assert manager.backends == []


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
async def test_shadow_execution_registry_exposes_read_only_tools_only():
    manager = _FakeManager()
    base_registry = _BaseToolRegistry()
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
    names = {
        item["function"]["name"] for item in shadow_registry.get_tool_definitions()
    }
    assert names == {"file_read"}
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
