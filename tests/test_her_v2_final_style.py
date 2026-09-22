"""Focused, offline tests for the opt-in presentation-only experiment."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.her_v2_style import capture_style_context, make_final_style_pass
from orchestrator.her_v2.audit import AuditPersistenceError
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.final_style import FinalStyleConfig, FinalStylePass
from orchestrator.her_v2.interfaces import TurnStopped
from orchestrator.her_v2.models import Effort, Route, Stage, StageResponse, TerminalState
from orchestrator.her_v2.runtime_configuration import (
    apply_her_v2_runtime_configuration, resolve_her_v2_configuration,
)
from tests.test_her_v2_runtime import ScriptedProvider, _initial, _runtime


def _answer(choice="rewrite"):
    return {"model": "jev-latest", "usage": {"input_tokens": 100, "output_tokens": 1},
            "answers": {"style": {"type": "choice", "choice": choice,
                "probabilities": {key: 0.9 if key == choice else 0.05
                                  for key in ("keep", "rewrite", "uncertain")}}}}


def _pass(choice="rewrite", *, enabled=True, **options):
    check = AsyncMock(return_value=_answer(choice))
    rewrite = AsyncMock(return_value="Done. Here is the result.")
    events = []
    gate = FinalStylePass(
        config=FinalStyleConfig(enabled=enabled, **options),
        context={"instruction_sources": [{"authority": "local_system", "text": "Be brief."}],
                 "current_request": "Make the change."},
        check=check, rewrite=rewrite,
        observe=lambda event, payload, turn: events.append((event, payload)),
    )
    return gate, check, rewrite, events


@pytest.mark.parametrize("choice,enabled", [("rewrite", False), ("keep", True), ("uncertain", True)])
@pytest.mark.asyncio
async def test_disabled_or_no_clear_mismatch_preserves_original(choice, enabled):
    gate, check, rewrite, _ = _pass(choice, enabled=enabled)
    assert await gate.render(" Original text. ", "turn") == " Original text. "
    assert check.await_count == int(enabled)
    rewrite.assert_not_awaited()


@pytest.mark.parametrize("failure", ["check", "rewrite", "malformed"])
@pytest.mark.asyncio
async def test_optional_failure_preserves_original_without_retry(failure):
    gate, check, rewrite, events = _pass()
    if failure == "check":
        check.side_effect = RuntimeError("private data must not reach audit")
    elif failure == "rewrite":
        rewrite.side_effect = TimeoutError("service unavailable")
    else:
        check.return_value = {"answers": []}
    assert await gate.render("Original", "turn") == "Original"
    assert check.await_count == 1
    assert rewrite.await_count == int(failure == "rewrite")
    assert events[-1][0] == "degraded"
    assert "private data" not in json.dumps(events)


@pytest.mark.parametrize("error", [asyncio.CancelledError(), TurnStopped("USER_STOP"), AuditPersistenceError("disk")])
@pytest.mark.asyncio
async def test_stop_and_required_audit_failure_are_not_suppressed(error):
    gate, check, rewrite, _ = _pass()
    check.side_effect = error
    with pytest.raises(type(error)):
        await gate.render("Original", "turn")
    rewrite.assert_not_awaited()


@pytest.mark.parametrize("effort", [Effort.ZERO, Effort.LOW, Effort.MEDIUM])
@pytest.mark.asyncio
async def test_final_text_is_rewritten_once_without_altering_execution(tmp_path, effort):
    original = "Implementation report: the requested change was completed."
    scripts = {Stage.DIRECT: [StageResponse(text=original)]} if effort is Effort.ZERO else {
        **_initial("SIMPLE_TASK"), Stage.PLANNING: [{"plan": ["Make change"]}],
        Stage.EXECUTION: [StageResponse(text=original)],
    }
    provider = ScriptedProvider(scripts)
    runtime = _runtime(tmp_path, provider)
    gate, check, rewrite, _ = _pass()
    runtime.final_style = gate
    result = await runtime.run_turn("Make the change; be brief.", "request", effort=effort)
    assert result.terminal_state is TerminalState.COMPLETED
    assert result.text == "Done. Here is the result."
    finals = [r for r in runtime.delivery.records if r.kind == "final"]
    assert len(finals) == 1 and finals[0].text == result.text
    check.assert_awaited_once()
    rewrite.assert_awaited_once()
    assert check.call_args.args[0] == rewrite.call_args.args[0]
    stages = [r.stage for _, r in provider.requests]
    assert Stage.FINALISATION not in stages
    assert stages.count(Stage.DIRECT if effort is Effort.ZERO else Stage.EXECUTION) == 1
    assert check.call_args.args[0]["draft_response"] == original
    assert len(runtime.delivery.records) <= 2  # original acknowledgment + final only


@pytest.mark.asyncio
async def test_parallel_immediate_answer_path_is_unchanged(tmp_path):
    provider = ScriptedProvider(_initial("DIRECT_RESPONSE"))
    runtime = _runtime(tmp_path, provider)
    gate, check, rewrite, _ = _pass()
    runtime.final_style = gate
    result = await runtime.run_turn("Hi", "r", effort=Effort.LOW)
    assert result.final_was_immediate
    check.assert_not_awaited()
    rewrite.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_prevents_second_pass_and_skips_rich_output(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider({}))
    gate, check, rewrite, _ = _pass()
    runtime.final_style = gate
    from orchestrator.her_v2.interfaces import TurnControl
    state = SimpleNamespace(style_finalisation_done=False, ledger=SimpleNamespace(turn_id="turn"),
                            control=TurnControl("turn"))
    assert await runtime._final_style_text(state, "Audio", content=({"type": "audio"},)) == "Audio"
    first = await runtime._final_style_text(state, "Original")
    assert await runtime._final_style_text(state, first) == first
    check.assert_awaited_once()
    rewrite.assert_awaited_once()


def _raw_config():
    return {"profiles": {name: {"engine": "openrouter-api", "model": model}
            for name, model in (("lightweight", "quick-test"), ("triage", "quick-test"),
                               ("premium", "pro-test"), ("reviewer", "pro-test"),
                               ("orchestrator", "pro-test"))}}


def test_setting_roundtrip_preserves_route_targets_and_rejects_string_bool():
    raw = _raw_config()
    selected = resolve_her_v2_configuration(raw)
    assert not selected.style_finalisation_enabled
    on = replace(selected, style_finalisation_enabled=True)
    restored = resolve_her_v2_configuration(raw, on.to_dict())
    assert restored.style_finalisation_enabled
    assert restored.all_targets() == selected.all_targets()
    from orchestrator.her_v2.config import HERv2Config
    effective = HERv2Config.from_mapping(apply_her_v2_runtime_configuration(raw, restored))
    assert effective.style_finalisation.enabled
    assert effective.profile_for_route(Route.DIRECT).model == "quick-test"
    with pytest.raises(ValueError):
        resolve_her_v2_configuration(raw, {"style_finalisation_enabled": "false"})


def test_snapshot_uses_typed_sys_and_persona_but_not_history():
    pcm = {str(i): {"key": str(i), "authority": auth, "text": auth}
           for i, auth in enumerate(("permanent_system", "global_system", "local_system", "persona", "history", "memory"))}
    coordinator = SimpleNamespace(store=SimpleNamespace(session=lambda _: {"pcm_revision": 3, "pcm": pcm}))
    adapter = SimpleNamespace(_session_coordinator=coordinator)
    turn = SimpleNamespace(session_id="session", pcm_revision=3)
    encoded = HerBackendSessionCoordinator.encode({"protocol": "hashi.her-fixed-backend.v1", "turn": {"user_message": "This request"}})
    snapshot = capture_style_context(adapter, encoded, turn)
    assert snapshot["current_request"] == "This request"
    assert {s["authority"] for s in snapshot["instruction_sources"]} == {
        "permanent_system", "global_system", "local_system", "persona"}
    with pytest.raises(ValueError):
        capture_style_context(adapter, encoded, SimpleNamespace(session_id="session", pcm_revision=2))


@pytest.mark.asyncio
async def test_typesafe_wire_and_quick_route_use_one_silent_rewrite(monkeypatch):
    from adapters import her_v2_style as module
    from orchestrator.her_v2.config import HERv2Config
    sent = []
    real_client = httpx.AsyncClient
    def transport(request):
        sent.append(request)
        return httpx.Response(200, json=_answer(), headers={"x-typesafe-request-id": "test-jev"})
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(transport), **kwargs))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-key")
    provider = SimpleNamespace(
        audit_log=None,
        backend_manager=SimpleNamespace(
            privacy_level=0, secrets={"typesafe_api_key": "should-not-win"}
        ),
        _accumulate_usage=lambda _: None,
        _record_usage_line_item=lambda **kwargs: None,
        bind_persona_audit_context=lambda *args, **kwargs: None,
        _package_persona_text_once=AsyncMock(return_value="Brief result."),
    )
    raw = _raw_config(); raw["style_finalisation"] = {"enabled": True}
    gate = make_final_style_pass(provider=provider, config=HERv2Config.from_mapping(raw),
                                context={"current_request": "Be brief", "instruction_sources": []},
                                request_id="req")
    assert await gate.render("Long report", "turn") == "Brief result."
    assert len(sent) == 1
    assert str(sent[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert sent[0].headers["Authorization"] == "Bearer test-only-key"
    body = json.loads(sent[0].content)
    assert set(body["questions"]) == {"style"}
    assert body["questions"]["style"]["type"] == "choice"
    profile = provider._package_persona_text_once.call_args.args[0]
    assert profile.engine == "openrouter-api" and profile.model == "quick-test"
    assert profile.reasoning == "off"
    assert provider._package_persona_text_once.call_args.kwargs["metering_phase"] == "style_rewrite"
    provider._package_persona_text_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_typesafe_key_falls_back_to_hashi_secret(monkeypatch):
    from adapters import her_v2_style as module
    from orchestrator.her_v2.config import HERv2Config

    sent = []
    real_client = httpx.AsyncClient

    def transport(request):
        sent.append(request)
        return httpx.Response(200, json=_answer("keep"))

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(transport), **kwargs))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    provider = SimpleNamespace(
        audit_log=None,
        backend_manager=SimpleNamespace(
            privacy_level=0, secrets={"typesafe_api_key": "local-test-key"}
        ),
        _accumulate_usage=lambda _: None,
        _record_usage_line_item=lambda **kwargs: None,
        bind_persona_audit_context=lambda *args, **kwargs: None,
        _package_persona_text_once=AsyncMock(return_value="Unused"),
    )
    raw = _raw_config()
    raw["style_finalisation"] = {"enabled": True}
    gate = make_final_style_pass(
        provider=provider,
        config=HERv2Config.from_mapping(raw),
        context={"current_request": "Be brief", "instruction_sources": []},
        request_id="req",
    )

    assert await gate.render("Already brief", "turn") == "Already brief"
    assert len(sent) == 1
    assert sent[0].headers["Authorization"] == "Bearer local-test-key"
    provider._package_persona_text_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_ui_model_style_saved_and_hybrid_drafted(tmp_path, monkeypatch):
    from orchestrator import runtime_model_selection as ui
    from tests.test_her_v2_runtime_configuration import _manager
    manager = _manager(tmp_path)
    runtime = SimpleNamespace(config=manager.config, backend_manager=manager, _reply_text=AsyncMock())
    monkeypatch.setattr(ui, "_schedule_her_v2_pricing_prewarm", lambda *args: None)
    monkeypatch.setattr(ui, "_schedule_her_v2_capability_prewarm", lambda *args: None)
    original = manager.get_her_v2_configuration()
    await ui._cmd_her_v2_model(runtime, None, ["style", "on"])
    assert manager.get_her_v2_configuration().style_finalisation_enabled
    assert _manager(tmp_path).get_her_v2_configuration().style_finalisation_enabled
    assert manager.get_her_v2_configuration().all_targets() == original.all_targets()
    assert any(button.callback_data == "her_model_style" for row in ui.her_v2_model_keyboard(runtime).inline_keyboard for button in row)
    hybrid = replace(manager.get_her_v2_configuration(), routing_mode="hybrid")
    manager.apply_her_v2_configuration(hybrid)
    await ui._cmd_her_v2_model(runtime, None, ["style", "off"])
    assert manager.get_her_v2_configuration().style_finalisation_enabled  # unchanged until Apply
    assert not manager.get_her_v2_draft_configuration().style_finalisation_enabled
    manager.apply_her_v2_configuration_draft()
    assert not manager.get_her_v2_configuration().style_finalisation_enabled


def test_ui_locale_keys_and_default_render():
    from orchestrator import ui_language
    from orchestrator.runtime_menu_views import her_v2_model_menu_text
    assert ui_language.validate_catalogs() == []
    rendered = her_v2_model_menu_text(provider="x<y", fast_model="quick", pro_model="pro")
    assert "x&lt;y" in rendered and "Style finalisation" in rendered


@pytest.mark.asyncio
async def test_light_renderer_is_tool_free_silent_and_metered():
    from adapters.base import BackendResponse, TokenUsage
    from adapters.her_v2_provider import HashiStageProvider
    from adapters.stream_events import KIND_COMMENTARY, StreamEvent
    from orchestrator.her_v2.config import ProviderProfile
    from orchestrator.her_v2.progress import ProviderActivityTracker
    calls, forwarded = [], []

    class Backend:
        config = SimpleNamespace(extra={})
        capabilities = SimpleNamespace(supports_tool_use=True)
        tool_registry = object()
        sys_prompt = ""
        async def initialize(self):
            return True
        def set_reasoning_enabled(self, value):
            assert value is False
        async def generate_response(self, prompt, request_id, **kwargs):
            calls.append((prompt, kwargs))
            assert self.tool_registry is None
            assert self.sys_prompt == "Style only"
            assert kwargs["silent"] and not kwargs["is_retry"]
            await kwargs["on_stream_event"](StreamEvent(kind=KIND_COMMENTARY, summary="Do not publish"))
            return BackendResponse(text="Reworded", duration_ms=1, usage=TokenUsage(10, 3))
        async def shutdown(self):
            pass

    backend = Backend()
    manager = SimpleNamespace(privacy_level=0,
                              create_ephemeral_backend=lambda *args, **kwargs: backend)
    provider = HashiStageProvider(backend_manager=manager,
                                  on_stream_event=AsyncMock(side_effect=forwarded.append))
    profile = ProviderProfile(name="lightweight", engine="openrouter-api", model="quick-test", reasoning="off")
    text = await provider._package_persona_text_once(
        profile, prompt="Original", system_prompt="Style only", request_id="style", message_label="style",
        max_chars=100, attempt=1, activity=ProviderActivityTracker(), metering_phase="style_rewrite")
    assert text == "Reworded" and len(calls) == 1 and not forwarded
    assert len(provider.usage_line_items) == 1
    assert provider.usage_line_items[0].phase == "style_rewrite"
