from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from adapters.her_persona import HERPersonaPackagingSource
from adapters.her_v2 import HashiStageProvider, _ConfiguredFinalStyleRenderer
from orchestrator import command_interaction_bridge
from orchestrator.command_specs import COMMAND_SPEC_BY_NAME
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.final_style_policy import get_enabled, get_policy
from orchestrator.her_v2.config import ProviderProfile
from orchestrator.her_v2.presentation import (
    FinalStyleRequest,
    PresentationRequirement,
)
from orchestrator.her_v2.prompts import render_final_style_system_prompt
from orchestrator.runtime_command_binding import get_flexible_bot_commands
from tools.meter_cost import PerCallUsageLineItem, UsageReceipt, format_cost_tail


def _style_request() -> FinalStyleRequest:
    return FinalStyleRequest(
        event_id="turn-1:final-style",
        turn_id="turn-1",
        draft_text="A needlessly long technical report.",
        current_request="Tell me the result briefly.",
        requirements=(
            PresentationRequirement(
                key="global-1",
                title="Global system",
                authority="global_system",
                text="Use concise plain language.",
            ),
            PresentationRequirement(
                key="persona",
                title="Persona",
                authority="persona",
                text="Address the user warmly.",
            ),
        ),
    )


class _FinalStyleProvider:
    def __init__(self, response: str):
        self.response = response
        self.calls = []
        self.bindings = []
        self.decisions = []

    def bind_persona_audit_context(self, request_id, **kwargs):
        self.bindings.append((request_id, kwargs))

    async def package_final_style(self, profile, **kwargs):
        self.calls.append((profile, kwargs))
        return self.response

    def mark_final_style_decision(self, request_id, decision):
        self.decisions.append((request_id, decision))


def _source() -> HERPersonaPackagingSource:
    return HERPersonaPackagingSource(
        guidance="Speak warmly and briefly.",
        display_name="Guide",
        usable=True,
        unavailable_reason=None,
        content_sha256="digest",
    )


def _renderer(provider: _FinalStyleProvider) -> _ConfiguredFinalStyleRenderer:
    return _ConfiguredFinalStyleRenderer(
        provider=provider,
        profile=ProviderProfile(
            "auxiliary", "deepseek-api", "deepseek-v4-flash"
        ),
        source=_source(),
        request_id="request-1",
        logger=logging.getLogger("test.final-style"),
    )


@pytest.mark.asyncio
async def test_final_style_renderer_keeps_compliant_main_answer_without_echoing_it():
    provider = _FinalStyleProvider('{"decision":"keep"}')

    result = await _renderer(provider).render(_style_request())

    assert result.decision == "keep"
    assert result.text == "A needlessly long technical report."
    assert result.fallback is False
    assert len(provider.calls) == 1
    profile, kwargs = provider.calls[0]
    assert profile.model == "deepseek-v4-flash"
    assert kwargs["request"].current_request == "Tell me the result briefly."
    assert provider.decisions[-1][1] == "keep"


@pytest.mark.asyncio
async def test_final_style_renderer_accepts_expression_only_rewrite():
    provider = _FinalStyleProvider(
        '{"decision":"rewrite","text":"The result is ready."}'
    )

    result = await _renderer(provider).render(_style_request())

    assert result.decision == "rewrite"
    assert result.text == "The result is ready."
    assert result.fallback is False
    assert provider.decisions[-1][1] == "rewrite"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"decision":"rewrite"}',
        '{"decision":"keep","text":"silently changed"}',
        '{"decision":"delete","text":"bad"}',
    ],
)
async def test_final_style_renderer_fails_open_to_exact_main_answer(response):
    result = await _renderer(_FinalStyleProvider(response)).render(_style_request())

    assert result.decision == "keep"
    assert result.text == "A needlessly long technical report."
    assert result.fallback is True


def test_final_style_prompt_forbids_factual_or_authority_changes():
    prompt = render_final_style_system_prompt()

    assert "presentation" in prompt.casefold()
    assert "Do not add, alter, contradict, or infer facts" in prompt
    assert "Preserve every fact that affects the user's understanding" in prompt
    assert '"decision":"keep"' in prompt
    assert '"decision":"rewrite"' in prompt
    assert "Do not call tools" in prompt


def test_style_command_and_frontend_catalogue_share_canonical_spec(tmp_path):
    spec = COMMAND_SPEC_BY_NAME["style"]
    assert spec.method_name == "cmd_style"
    assert spec.guide.usage == "/style [on|off|status]"
    assert any(command.command == "style" for command in get_flexible_bot_commands(
        SimpleNamespace(
            global_config=SimpleNamespace(project_root=tmp_path, instance_id="TEST")
        ),
        locale="en",
    ))

    runtime = SimpleNamespace(
        name="agent",
        global_config=SimpleNamespace(project_root=tmp_path, instance_id="TEST"),
        _is_command_allowed=lambda _name: True,
    )
    style_command = SimpleNamespace(command="style", description="Final style check")
    with (
        patch(
            "orchestrator.admin_local_testing.supported_commands",
            return_value=["style"],
        ),
        patch(
            "orchestrator.runtime_command_binding.get_flexible_bot_commands",
            return_value=[style_command],
        ),
        patch("orchestrator.command_registry.runtime_command_map", return_value={}),
    ):
        catalogue = command_interaction_bridge._catalogue(runtime, "en")
    assert catalogue["commands"] == [
        {
            "name": "style",
            "description": "Final style check",
            "usage": "/style [on|off|status]",
            "enabled": True,
            "reason": None,
        }
    ]


class _StyleCommandRuntime:
    def __init__(self, workspace_dir):
        self.workspace_dir = workspace_dir
        self.replies = []

    def _is_authorized_user(self, _user_id):
        return True

    def _style_enabled(self):
        return get_enabled(self)

    def _style_menu_text(self):
        return "style=on" if self._style_enabled() else "style=off"

    def _style_keyboard(self):
        return None

    async def _reply_text(self, _update, text, **_kwargs):
        self.replies.append(text)


@pytest.mark.asyncio
async def test_style_command_status_is_read_only_and_only_on_off_mutate(tmp_path):
    runtime = _StyleCommandRuntime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_style(
        runtime, update, SimpleNamespace(args=[])
    )
    assert runtime.replies[-1] == "style=off"

    await FlexibleAgentRuntime.cmd_style(
        runtime, update, SimpleNamespace(args=["on"])
    )
    assert runtime._style_enabled() is True

    await FlexibleAgentRuntime.cmd_style(
        runtime, update, SimpleNamespace(args=["status"])
    )
    assert runtime._style_enabled() is True

    await FlexibleAgentRuntime.cmd_style(
        runtime, update, SimpleNamespace(args=["off"])
    )
    assert runtime._style_enabled() is False


@pytest.mark.asyncio
async def test_style_command_rejects_invalid_value_without_mutating(tmp_path):
    runtime = _StyleCommandRuntime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_style(
        runtime, update, SimpleNamespace(args=["banana"])
    )

    assert runtime._style_enabled() is False
    assert "on|off|status" in runtime.replies[-1]


def test_style_policy_uses_config_default_without_writing_fallback(tmp_path):
    runtime = SimpleNamespace(
        workspace_dir=tmp_path,
        config=SimpleNamespace(extra={"final_style": {"enabled": True}}),
    )

    policy = get_policy(runtime)

    assert policy.enabled is True
    assert policy.source == "config default"
    assert not (tmp_path / "state" / "final_style.json").exists()


def test_meter_reports_final_style_model_as_its_own_work():
    receipt = UsageReceipt(
        line_items=[
            PerCallUsageLineItem(
                phase="direct",
                engine="deepseek-api",
                model="deepseek-v4-pro",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=0.02,
                cost_source="provider",
            ),
            PerCallUsageLineItem(
                phase="final_style_rewrite",
                engine="deepseek-api",
                model="deepseek-v4-flash",
                input_tokens=1200,
                output_tokens=20,
                cost_usd=0.001,
                cost_source="provider",
            ),
        ]
    )

    rendered = format_cost_tail(receipt, locale="zh-CN")

    assert "最终表达检查：deepseek-v4-flash" in rendered
    assert "最终答复：deepseek-v4-flash（已进行额外模型改写）" in rendered


def test_style_physical_call_is_observed_only_after_decision():
    class _PhysicalCallBackend:
        def set_provider_invocation_context(self, context):
            self.context = context

        def set_provider_call_observer(self, callback):
            self.observe = callback

    observed = []
    backend = _PhysicalCallBackend()
    provider = object.__new__(HashiStageProvider)
    provider.audit_log = None
    provider.usage_observer = observed.append
    provider.usage_line_items = []
    provider._observed_provider_request_ids = set()
    provider._bind_provider_call_observer(
        backend,
        request_id="request-1:final-style:1",
        phase="final_style",
        engine="deepseek-api",
        model="deepseek-v4-flash",
    )

    backend.observe(
        {
            "provider_request_id": "provider-call-1",
            "status": "completed",
            "input": 100,
            "output": 5,
            "token_source": "provider",
        }
    )

    assert observed == []
    assert provider.usage_line_items[0].phase == "final_style"

    provider.mark_final_style_decision("request-1:final-style:1", "rewrite")

    assert [item.phase for item in observed] == ["final_style_rewrite"]


@pytest.mark.parametrize(
    ("decision", "expected_phase"),
    [("keep", "final_style_check"), ("rewrite", "final_style_rewrite")],
)
def test_style_decision_refines_existing_physical_call_receipt(
    decision, expected_phase
):
    provider = object.__new__(HashiStageProvider)
    provider.usage_line_items = [
        PerCallUsageLineItem(
            request_id="request-1:final-style:1",
            phase="final_style",
            engine="deepseek-api",
            model="deepseek-v4-flash",
        )
    ]

    provider.mark_final_style_decision("request-1:final-style:1", decision)

    assert len(provider.usage_line_items) == 1
    assert provider.usage_line_items[0].phase == expected_phase


def test_meter_keeps_main_model_as_final_author_when_style_check_keeps_draft():
    receipt = UsageReceipt(
        line_items=[
            PerCallUsageLineItem(
                phase="direct",
                engine="deepseek-api",
                model="deepseek-v4-pro",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=0.02,
                cost_source="provider",
            ),
            PerCallUsageLineItem(
                phase="final_style_check",
                engine="deepseek-api",
                model="deepseek-v4-flash",
                input_tokens=1200,
                output_tokens=5,
                cost_usd=0.001,
                cost_source="provider",
            ),
        ]
    )

    rendered = format_cost_tail(receipt, locale="zh-CN")

    assert "最终表达检查：deepseek-v4-flash" in rendered
    assert "最终答复：deepseek-v4-pro（无额外模型改写）" in rendered
