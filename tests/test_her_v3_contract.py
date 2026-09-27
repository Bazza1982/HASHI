from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.her_v2 import HERv2Adapter
from orchestrator.config import AgentConfig, GlobalConfig
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.models import Stage, StageResponse, parse_effort
from orchestrator.her_v2.turn_services import parse_health
from orchestrator.her_v2.v3_prompt import compile_main_prompt
from orchestrator.her_v2.wip_journal import WIPJournal
from orchestrator.pcm import render_pcm_document


def _legacy_config():
    return {
        "profiles": {
            "lightweight": {"engine": "deepseek-api", "model": "deepseek-flash", "reasoning": "high"},
            "premium": {"engine": "deepseek-api", "model": "deepseek-v4-pro", "reasoning": "high"},
        },
        "stage_roles": {"direct": "lightweight", "execution": "premium"},
    }


def test_legacy_config_collapses_to_one_main_foreground_model():
    config = HERv2Config.from_mapping(_legacy_config())
    assert set(config.profiles) == {"main", "auxiliary"}
    assert config.profile_for(Stage.DIRECT).model == "deepseek-v4-pro"
    assert config.profile_for(Stage.EXECUTION).model == "deepseek-v4-pro"


def test_effort_none_is_model_reasoning_wire_alias():
    assert parse_effort("none").value == "zero"
    assert parse_effort("xhigh").value == "xhigh"


def test_strategy_cards_are_optional_context_not_workflow():
    system, user = compile_main_prompt(
        pcm_input={"current_request": "do the task", "sections": [], "history": []},
        fallback_request="fallback",
        context={"strategy_playbook": {"cards": [{"id": "SAFE"}]}, "habit_catalogue": []},
    )
    assert "optional advice" in system
    assert "No mandatory selection" in system
    assert "do the task" in user


def test_agent_companion_only_intervenes_on_high_confidence_trouble():
    payload = {
        "answers": {
            "health": {
                "type": "choice",
                "choice": "trouble",
                "probabilities": {"continue": 0.05, "trouble": 0.9, "unknown": 0.05},
            }
        }
    }
    assert parse_health(payload)
    payload["answers"]["health"]["probabilities"] = {"continue": 0.2, "trouble": 0.7, "unknown": 0.1}
    assert not parse_health(payload)


class _MainProvider:
    def __init__(self):
        self.calls = []

    async def invoke(self, profile, request):
        self.calls.append((profile, request))
        return StageResponse(
            text="HER v3 main-model answer",
            provider=profile.engine,
            model=profile.model,
            reasoning_trace=None,
        )


def _adapter(tmp_path: Path, provider: _MainProvider, *, fixed: bool = False):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    agent_md = workspace / "agent.md"
    agent_md.write_text(
        render_pcm_document(persona="Be concise.", system="Follow HASHI policy."),
        encoding="utf-8",
    )
    config = AgentConfig(
        name="test-agent",
        engine="her-v2",
        workspace_dir=workspace,
        system_md=agent_md,
        model="role-configured",
        is_active=True,
        extra={"effort": "high", "her_v2": _legacy_config()},
        project_root=tmp_path,
    )
    config._her_v2_stage_provider = provider
    if fixed:
        config._hashi_runtime = SimpleNamespace(
            backend_manager=SimpleNamespace(agent_mode="fixed"),
            _request_meta_by_id={},
            current_request_meta={},
        )
    global_config = GlobalConfig(
        authorized_id=1,
        project_root=tmp_path,
        bridge_home=tmp_path,
        base_logs_dir=tmp_path / "logs",
    )
    return HERv2Adapter(config, global_config)


@pytest.mark.asyncio
async def test_high_effort_keeps_one_main_model_call(tmp_path):
    """Pre-v3 high effort routed through Triage; it must not do so here."""
    provider = _MainProvider()
    adapter = _adapter(tmp_path, provider)
    assert await adapter.initialize()

    response = await adapter.generate_response("Answer directly", "request-high")

    assert response.is_success
    assert response.text == "HER v3 main-model answer"
    assert len(provider.calls) == 1
    profile, request = provider.calls[0]
    assert request.stage is Stage.DIRECT
    assert request.context["her_v3"] is True
    assert profile.model == "deepseek-v4-pro"
    assert profile.reasoning == "high"


@pytest.mark.asyncio
async def test_boolean_provider_reasoning_is_preserved_for_her_v3(tmp_path):
    provider = _MainProvider()
    adapter = _adapter(tmp_path, provider)
    adapter.config.extra["effort"] = "enabled"
    adapter.config.extra["her_v2"] = {
        "main": {
            "provider": "openrouter-api",
            "model": "deepseek/deepseek-v3.2-exp",
            "reasoning": "enabled",
        }
    }

    assert await adapter.initialize()
    response = await adapter.generate_response("Answer directly", "request-enabled")

    assert response.is_success
    assert len(provider.calls) == 1
    profile, request = provider.calls[0]
    assert request.stage is Stage.DIRECT
    assert profile.reasoning == "enabled"


@pytest.mark.asyncio
async def test_fixed_session_pcm_reaches_single_main_call(tmp_path):
    """Current user text must survive fixed-Session materialisation."""
    provider = _MainProvider()
    adapter = _adapter(tmp_path, provider, fixed=True)
    assert await adapter.initialize()
    runtime = adapter.config._hashi_runtime
    request_meta = {
        "request_id": "request-fixed",
        "hashi_session_id": "session-v3",
        "hashi_message_id": "message-v3",
        "context_generation": 1,
        "session_workspace": str(tmp_path / "session-v3"),
    }
    runtime._request_meta_by_id["request-fixed"] = request_meta
    runtime.current_request_meta = request_meta
    sections = [
        {
            "key": "permanent_system",
            "text": "Follow the permanent policy.",
            "authority": "permanent_system",
            "order": 0,
        },
        {
            "key": "current_user_request",
            "text": "placeholder",
            "authority": "current_user",
            "order": 1,
        },
    ]
    transport, _audit = adapter.prepare_fixed_turn_input(
        prompt_payload={"transport_snapshot": {"version": 1, "sections": sections}},
        user_message="Preserve this request",
        request_id="request-fixed",
        request_meta=request_meta,
    )

    response = await adapter.generate_response(transport, "request-fixed")

    assert response.is_success
    assert len(provider.calls) == 1
    request = provider.calls[0][1]
    assert request.stage is Stage.DIRECT
    assert request.context["pcm_input"]["current_request"] == "Preserve this request"
    system, user = compile_main_prompt(
        pcm_input=request.context["pcm_input"],
        fallback_request=request.goal,
        context=request.context,
    )
    assert "Follow the permanent policy." in system
    assert user.endswith("Current user request:\nPreserve this request")


@pytest.mark.asyncio
async def test_recovered_work_is_context_not_a_new_request(tmp_path):
    """A pre-v3 WIP summary must not disappear behind typed PCM projection."""
    provider = _MainProvider()
    adapter = _adapter(tmp_path, provider)
    journal = WIPJournal(
        adapter.config.workspace_dir / "backend_state" / "her_v2" / "wip_journal.jsonl"
    )
    journal.begin_turn(request_id="interrupted", prompt="unfinished source check")
    journal.append_audit(
        {
            "event": "stage_completed",
            "stage": "execution",
            "payload": {"output": "side effect may already have happened"},
        }
    )
    assert await adapter.initialize()

    response = await adapter.generate_response("Check before continuing", "resumed")

    assert response.is_success
    request = provider.calls[0][1]
    pcm_input = request.context["pcm_input"]
    assert pcm_input["current_request"] == "Check before continuing"
    assert pcm_input["runtime_context"]["prior_wip"]
    system, user = compile_main_prompt(
        pcm_input=pcm_input,
        fallback_request=request.goal,
        context=request.context,
    )
    assert "never replay them automatically" in system
    assert "side effect may already have happened" in user
    assert user.endswith("Current user request:\nCheck before continuing")
