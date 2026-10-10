"""Call menu language and media publication are observable frontend behavior."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import ui_language
from orchestrator.frontend_call.settings import CallSettings, callback


def runtime_for(tmp_path):
    return SimpleNamespace(name="call-voice-test",
        global_config=SimpleNamespace(bridge_home=tmp_path, authorized_id=7),
        _is_authorized_user=lambda user_id: user_id == 7)


def test_chinese_voice_menu_shows_gender_and_translated_style(tmp_path):
    settings = CallSettings(runtime_for(tmp_path))
    with ui_language.language_scope(None, locale="zh-CN"):
        text, keyboard = settings.render("voice")
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "✓ 女声 · Achernar · 柔和" in labels
    assert "男声 · Achird · 亲切" in labels
    assert not any("Soft" in label or "Friendly" in label for label in labels)
    assert "试听当前音色" in labels
    assert "选中音色后" in text
    assert all(len(b.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for b in row)


def test_all_bundled_voices_have_matching_samples_in_both_menu_languages():
    from orchestrator.frontend_call.defaults import default_call_configuration
    from orchestrator.frontend_call.voice_catalog import GEMINI_VOICES, voice_label
    from orchestrator.frontend_call.voice_previews import get_call_preview
    from orchestrator.voice_preview_bundle import validate_voice_preview_bundle
    from orchestrator.frontend_call.voice_previews import PREVIEW_ROOT
    target = next(t for t in default_call_configuration()["targets"] if t["kind"] == "tts")
    indexed = validate_voice_preview_bundle(root=PREVIEW_ROOT, require_complete=False, allowed_formats=("ogg", "mp3"))
    assert set(indexed) == {(locale, voice, renderer) for locale in ("en", "zh-CN") for voice in GEMINI_VOICES for renderer in ("call", "call_mp3")}
    for locale in ("en", "zh-CN"):
        with ui_language.language_scope(None, locale=locale):
            for voice in target["voices"]:
                assert get_call_preview(target, voice) == (("call_mp3", indexed[(locale, voice, "call_mp3")]),)
                assert get_call_preview(target, voice, telegram=True) == (("call", indexed[(locale, voice, "call")]),)
                assert "call.voice." not in voice_label(target, voice)


def test_preview_never_substitutes_another_provider_model_or_voice():
    from orchestrator.frontend_call.defaults import default_call_configuration
    from orchestrator.frontend_call.voice_previews import get_call_preview
    from orchestrator.frontend_call.voice_catalog import voice_label
    target = next(t for t in default_call_configuration()["targets"] if t["kind"] == "tts")
    assert not get_call_preview({**target, "model": "another-model"}, "Achernar")
    assert not get_call_preview({**target, "base_url": "https://another-provider.invalid"}, "Achernar")
    assert not get_call_preview(target, "missing-voice")
    with ui_language.language_scope(None, locale="zh-CN"):
        assert voice_label({"model": "custom", "voice_styles": {"Achernar": "custom style"}}, "Achernar") == "Achernar · custom style"


def test_corrupt_preview_is_unavailable(tmp_path):
    import hashlib, json
    from orchestrator.frontend_call.voice_previews import get_call_preview
    target = {"model": "test", "adapter": "test", "base_url": "https://example.invalid"}
    sample = tmp_path / "v1/zh-CN/Test/call.ogg"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"OggScorrupt")
    manifest = {"schema_version": 1, "version": "v1", **target, "entries": [{
        "locale": "zh-CN", "profile": "Test", "renderer": "call", "path": "zh-CN/Test/call.ogg",
        "size_bytes": sample.stat().st_size, "sha256": hashlib.sha256(b"original").hexdigest()}]}
    (tmp_path / "v1/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert not get_call_preview(target, "Test", locale="zh-CN", root=tmp_path)


@pytest.mark.asyncio
async def test_call_selection_publishes_selected_preview_without_a_conversation_turn(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.session_store import SessionStore
    from orchestrator.frontend_call.voice_previews import get_call_preview

    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "call-voice-test"
    runtime.workspace_dir, runtime.media_dir = tmp_path / "workspace", tmp_path / "media"
    runtime.global_config = SimpleNamespace(authorized_id=7, instance_id="TEST",
        project_root=tmp_path, bridge_home=tmp_path)
    runtime.config = SimpleNamespace(active_backend="codex-cli", telegram_token_key="test",
        extra={"agent_lifecycle_id": "a" * 32})
    runtime.session_store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST")
    runtime._is_authorized_user = lambda user_id: user_id == 7
    runtime._reply_text = AsyncMock()
    runtime.app = SimpleNamespace(bot=SimpleNamespace(send_voice=AsyncMock()))
    runtime.error_logger = SimpleNamespace(error=lambda *_args: None)
    runtime.telegram_logger = SimpleNamespace(warning=lambda *_args: None)
    runtime.telegram_connected, runtime._notify_enabled, runtime.token = True, False, "test-token"
    owner = SessionStore.owner_id_for(runtime.global_config)
    session = runtime.session_store.create_session(owner_id=owner, agent_id=runtime.name, title="Call preview", is_default=True)
    runtime.default_session_id = session["session_id"]
    settings = CallSettings(runtime)
    revision = settings.config.context(owner, runtime.name)["revision"][:12]
    query = SimpleNamespace(id="selection-1", data=f"call:voice:1:{revision}",
        from_user=SimpleNamespace(id=7), message=SimpleNamespace(chat_id=7),
        edit_message_text=AsyncMock(), answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, _hashi_session_surface="workbench",
        _hashi_session_channel_key="default", _hashi_session_owner_id=owner,
        _hashi_session_id=session["session_id"],
        _hashi_session_context_generation=session["context_generation"])
    with ui_language.language_scope(None, locale="zh-CN"):
        await callback(runtime, update, SimpleNamespace())
    ctx = CallSettings(runtime).config.context(owner, runtime.name)
    assert ctx["profile"]["tts"]["voice_id"] == "Achird"
    previews = runtime.session_store.messages(session["session_id"], owner_id=owner)
    assert len(previews) == 1
    assert previews[0]["history_eligible"] is False
    assert "男声 · Achird · 亲切" in str(previews[0]["content"])
    part = previews[0]["content"][0]
    assert part["mime_type"] == "audio/mpeg"
    assert part["filename"].endswith(".mp3")
    assert part["semantic_role"] == "audio_attachment"
    assert part["presentation_role"] == "audio"
    _, payload = runtime.session_store.attachment_bytes(session_id=session["session_id"],
        owner_id=owner, attachment_id=part["attachment_id"])
    _, targets = settings.config.read()
    assert payload == get_call_preview(targets[ctx["profile"]["tts"]["target_id"]], "Achird", locale="zh-CN")[0][1].read_bytes()
    runtime.app.bot.send_voice.assert_not_awaited()

    # Explicit replay creates a new card; retrying that exact callback is idempotent.
    before = (tmp_path / "call_profiles.json").read_bytes()
    query.id, query.data = "preview-2", f"call:preview::{ctx['revision'][:12]}"
    with ui_language.language_scope(None, locale="zh-CN"):
        await callback(runtime, update, SimpleNamespace())
        await callback(runtime, update, SimpleNamespace())
    assert (tmp_path / "call_profiles.json").read_bytes() == before
    assert len(runtime.session_store.messages(session["session_id"], owner_id=owner)) == 2

    query.data = f"call:voice:0:{revision}"
    await callback(runtime, update, SimpleNamespace())
    assert query.answer.call_args.kwargs["show_alert"] is True
    assert len(runtime.session_store.messages(session["session_id"], owner_id=owner)) == 2
