import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orchestrator.voice_catalog import VoiceCatalog
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_synthesizer import VoiceAsset
import orchestrator.voice_manager as voice_module


def manager(tmp_path):
    payload = {"version": 1, "services": [{"id": "advanced", "adapter": "openai_compatible",
        "label": "Advanced", "location": "local", "base_url": "http://127.0.0.1:19446/v1",
        "model": "advanced-model", "audio_format": "wav"}],
        "voices": [{"id": "advanced_voice", "service": "advanced", "voice_name": "chosen",
            "label": "Chosen", "language": "Chinese", "locale": "zh-CN", "gender": "female"}]}
    catalog_file = tmp_path / "voice_catalog.json"
    catalog_file.write_text(json.dumps(payload), encoding="utf-8")
    catalog = VoiceCatalog.load(instance_path=catalog_file, project_root=tmp_path, platform="win32")
    value = VoiceManager(tmp_path / "agent", tmp_path / "media", catalog_path=catalog_file, project_root=tmp_path)
    value.apply_voice_preset("advanced_voice")
    return value


@pytest.mark.asyncio
async def test_default_off_then_one_plan_reused_and_off_has_no_old_plan(tmp_path, monkeypatch):
    value = manager(tmp_path)
    assert value.get_performance() is False
    assert (await value.prepare_performance("完整正文")) == {"enabled": False}
    plan = {"version": 1, "model": "advanced-model", "plan_id": "a"*32, "applied": True,
            "instructions": "温柔亲近，转折处自然停顿。", "reason": None}
    prepare = AsyncMock(return_value=plan)
    synthesis = AsyncMock(return_value=VoiceAsset("advanced", "段", "段", None, Path("test.ogg")))
    provider = type("Provider", (), {"prepare_performance": prepare, "synthesize": synthesis})()
    monkeypatch.setattr(voice_module, "build_provider", lambda *a, **kw: provider)
    value.set_performance(True)
    context = await value.prepare_performance("完整正文")
    assert context["plan"] == plan and prepare.await_count == 1
    for segment in ("第一段", "第二段"):
        result = await value.synthesize_reply("agent1", "request", segment, force=True,
            max_retries=0, performance_context=context)
        sent = synthesis.await_args.kwargs
        assert sent["text"] == segment and sent["voice_name"] == "chosen"
        assert sent["provider_options"]["performance_plan"] == plan
        assert result.performance["applied"] is True
    assert prepare.await_count == 1
    value.set_performance(False)
    await value.synthesize_reply("agent1", "next", "普通正文", force=True,
        max_retries=0, performance_context=context)
    assert "performance_plan" not in synthesis.await_args.kwargs["provider_options"]
    assert prepare.await_count == 1
    saved = json.loads(value.state_path.read_text(encoding="utf-8"))
    assert saved["performance"] is False
    assert "plan" not in saved and "instructions" not in saved
    assert saved["voice_name"] == "chosen"


@pytest.mark.asyncio
async def test_failed_preparation_is_not_repeated_or_reported_as_applied(tmp_path, monkeypatch):
    value = manager(tmp_path)
    prepare = AsyncMock(side_effect=TimeoutError())
    synthesis = AsyncMock(return_value=VoiceAsset("advanced", "正文", "正文", None, Path("test.ogg")))
    provider = type("Provider", (), {"prepare_performance": prepare, "synthesize": synthesis})()
    monkeypatch.setattr(voice_module, "build_provider", lambda *a, **kw: provider)
    value.set_performance(True)
    context = await value.prepare_performance("整条朗读")
    for segment in ("第一段", "第二段"):
        asset = await value.synthesize_reply("agent1", "request", segment, force=True,
            max_retries=0, performance_context=context)
        assert asset.performance["requested"] is True
        assert asset.performance["applied"] is False and asset.notice
        assert "performance_plan" not in synthesis.await_args.kwargs["provider_options"]
    assert prepare.await_count == 1


@pytest.mark.asyncio
async def test_preparation_cancellation_propagates_and_never_synthesizes(tmp_path, monkeypatch):
    import asyncio
    value = manager(tmp_path)
    prepare = AsyncMock(side_effect=asyncio.CancelledError())
    synthesis = AsyncMock()
    provider = type("Provider", (), {"prepare_performance": prepare, "synthesize": synthesis})()
    monkeypatch.setattr(voice_module, "build_provider", lambda *a, **kw: provider)
    value.set_performance(True)
    with pytest.raises(asyncio.CancelledError):
        await value.synthesize_reply("agent1", "cancelled", "正文", force=True)
    assert synthesis.await_count == 0


@pytest.mark.asyncio
async def test_voice_command_and_button_persist_without_generating_a_preview(tmp_path):
    from types import SimpleNamespace
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    value = manager(tmp_path)
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = value
    runtime._is_authorized_user = lambda _: True
    runtime._reply_text = AsyncMock()
    await runtime.cmd_voice(SimpleNamespace(effective_user=SimpleNamespace(id=7)),
        SimpleNamespace(args=["performance", "on"]))
    assert value.get_performance() is True
    query = SimpleNamespace(from_user=SimpleNamespace(id=7), data="voice:performance:off",
        edit_message_text=AsyncMock(), answer=AsyncMock())
    await runtime.callback_voice(SimpleNamespace(callback_query=query), SimpleNamespace())
    assert value.get_performance() is False
    assert query.edit_message_text.await_count == 1
