from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import voice_manager as voice_manager_module
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.session_store import SessionStore
from orchestrator.tts_voice_previews import TTS_VOICE_PREVIEW_TEXT
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_synthesizer import VoiceAsset


def _manager(tmp_path: Path) -> VoiceManager:
    return VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        native_capabilities=[
            {
                "engine": "openrouter-api",
                "model": "openai/gpt-audio-mini",
                "input_modalities": ["text", "audio"],
                "input_transports": {"audio": ["inline"]},
                "input_formats": {"audio": ["wav"]},
                "output_modalities": ["text", "audio"],
                "output_formats": {"audio": ["pcm16"]},
                "output_streaming": "sse",
                "api_surface": "chat_completions",
                "supported_voices": [
                    "alloy",
                    "ash",
                    "ballad",
                    "coral",
                    "echo",
                    "sage",
                    "shimmer",
                    "verse",
                ],
            }
        ],
    )


def _prepare_runtime_for_fc(runtime, tmp_path: Path) -> None:
    runtime.name = "voice-test"
    runtime.workspace_dir = tmp_path / "workspace"
    runtime.media_dir = tmp_path / "media"
    runtime.global_config = SimpleNamespace(
        authorized_id=7,
        instance_id="HASHI1",
        project_root=tmp_path,
    )
    runtime.config = SimpleNamespace(
        active_backend="codex-cli",
        telegram_token_key="voice-test",
        extra={"agent_lifecycle_id": "a" * 32},
    )
    runtime.session_store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI1",
    )
    runtime.telegram_connected = True
    runtime._notify_enabled = False
    runtime.token = "test-token"


def _write_tts_voice_preview_bundle(
    root: Path,
    *,
    alias: str,
    preset: dict,
    locale: str,
) -> dict[str, Path]:
    version_root = root / "v1"
    voice = str(preset["voice_name"])
    if preset["provider"] == "piper":
        voice = Path(voice).name
    paths = {
        "tts": version_root / locale / alias / "tts.ogg",
        "tts_mp3": version_root / locale / alias / "tts_mp3.mp3",
    }
    payloads = {
        "tts": b"OggS-" + alias.encode("ascii"),
        "tts_mp3": b"ID3-" + alias.encode("ascii"),
    }
    entries = []
    for renderer, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads[renderer])
        audio_format = "mp3" if renderer == "tts_mp3" else "ogg"
        entries.append(
            {
                "locale": locale,
                "profile": alias,
                "renderer": renderer,
                "format": audio_format,
                "path": path.relative_to(version_root).as_posix(),
                "size_bytes": len(payloads[renderer]),
                "sha256": hashlib.sha256(payloads[renderer]).hexdigest(),
                "provider": preset["provider"],
                "voice": voice,
                "script": TTS_VOICE_PREVIEW_TEXT[locale],
            }
        )
    (version_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "v1",
                "kind": "tts_voice_presets",
                "text": TTS_VOICE_PREVIEW_TEXT,
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return paths


def test_piper_preset_uses_standalone_executable_without_python_module(
    tmp_path, monkeypatch
):
    python_exe = tmp_path / "python.exe"
    piper_exe = tmp_path / "piper.exe"
    python_exe.touch()
    piper_exe.touch()
    monkeypatch.setattr("orchestrator.voice_manager.sys.executable", str(python_exe))
    monkeypatch.setattr(
        "orchestrator.voice_manager.importlib.util.find_spec", lambda _name: None
    )

    manager = _manager(tmp_path)
    preset = manager._preset_payload("pcn")

    assert preset is not None
    assert preset["provider_options"]["exe"] == str(piper_exe)
    assert preset["provider_options"]["module_mode"] is False
    assert manager._provider_status("piper") == "installed"


def test_voice_profiles_resolve_supported_native_voice_and_tts_fallback(tmp_path):
    manager = _manager(tmp_path)
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")
    manager.apply_voice_preset("ecn")

    expected = {
        "warm_female": ("shimmer", "en-US-EmmaMultilingualNeural"),
        "clear_female": ("coral", "en-US-AriaNeural"),
        "warm_male": ("verse", "en-US-GuyNeural"),
        "calm_male": ("echo", "en-US-ChristopherNeural"),
    }
    for profile_id, (native_voice, tts_voice) in expected.items():
        manager.set_voice_profile(profile_id)
        state = manager.get_state()
        assert state["voice_profile"] == profile_id
        assert state["native"]["voice"] == native_voice
        assert state["provider"] == "edge"
        # TTS voice and style are independent preferences. Changing style must
        # not silently overwrite the user's selected Xiaoxiao voice.
        assert state["voice_name"] == "zh-CN-XiaoxiaoNeural"

    assert manager._tts_voice_for_profile("warm_male", "任务完成。") == (
        "zh-CN-YunxiNeural"
    )
    assert manager._tts_voice_for_profile("calm_male", "終わりました。") == (
        "ja-JP-NaokiNeural"
    )


def test_voice_preview_assets_fall_back_to_the_versioned_product_bundle(tmp_path):
    bundle_root = tmp_path / "voice_preview_assets"
    bundled = bundle_root / "v1" / "en" / "warm_female" / "tts.ogg"
    bundled.parent.mkdir(parents=True)
    payload = b"OggSbundled-preview"
    bundled.write_bytes(payload)
    (bundle_root / "v1" / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "v1",
                "entries": [
                    {
                        "locale": "en",
                        "profile": "warm_female",
                        "renderer": "tts",
                        "path": "en/warm_female/tts.ogg",
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        preview_asset_root=bundle_root,
    )

    assert manager.get_voice_preview_assets(
        "warm_female", renderers=("tts",), locale="en"
    ) == (("tts", bundled),)

    local = manager.voice_preview_path("warm_female", "tts", locale="en")
    local.parent.mkdir(parents=True)
    local.write_bytes(b"OggSlocal-preview")
    assert manager.get_voice_preview_assets(
        "warm_female", renderers=("tts",), locale="en"
    ) == (("tts", local),)


def test_tts_voice_preview_assets_are_prerecorded_and_match_preset_provenance(
    tmp_path,
):
    bundle_root = tmp_path / "tts_voice_preview_assets"
    preset = VoiceManager.VOICE_PRESETS["ecn"]
    paths = _write_tts_voice_preview_bundle(
        bundle_root,
        alias="ecn",
        preset=preset,
        locale="zh-CN",
    )
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        tts_preview_asset_root=bundle_root,
    )

    assert manager.get_tts_voice_preview_assets("ecn", telegram=True) == (
        ("tts", paths["tts"]),
    )
    assert manager.get_tts_voice_preview_assets("ecn", telegram=False) == (
        ("tts_mp3", paths["tts_mp3"]),
    )

    manifest_path = bundle_root / "v1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["voice"] = "zh-CN-NotXiaoxiaoNeural"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert manager.get_tts_voice_preview_assets("ecn", telegram=True) == ()


def test_tts_voice_catalog_declares_locale_and_gender_for_every_preset():
    presets = {**VoiceManager.VOICE_PRESETS, **VoiceManager.MACOS_VOICE_PRESETS}

    assert presets
    assert all(preset.get("locale") in TTS_VOICE_PREVIEW_TEXT for preset in presets.values())
    assert all(preset.get("gender") in {"female", "male"} for preset in presets.values())
    assert VoiceManager.VOICE_PRESETS["us"]["gender"] == "female"
    assert VoiceManager.VOICE_PRESETS["uk"]["gender"] == "female"
    assert VoiceManager.VOICE_PRESETS["pcn"]["gender"] == "female"


def test_free_voice_catalog_has_balanced_gender_coverage_for_supported_locales():
    coverage = {
        (str(preset["provider"]), str(preset["locale"]), str(preset["gender"]))
        for preset in VoiceManager.VOICE_PRESETS.values()
    }

    assert {
        ("edge", locale, gender)
        for locale in ("en-US", "en-GB", "zh-CN", "zh-TW", "zh-HK", "ja-JP")
        for gender in ("female", "male")
    } <= coverage
    assert {
        ("kokoro", locale, gender)
        for locale in ("en-US", "en-GB", "zh-CN", "ja-JP")
        for gender in ("female", "male")
    } <= coverage
    assert {
        ("windows", "en-US", "female"),
        ("windows", "en-US", "male"),
        ("windows", "zh-CN", "female"),
    } <= coverage


def test_kokoro_preset_uses_the_isolated_tts_runtime(tmp_path, monkeypatch):
    isolated_python = tmp_path / "tts-runtime" / "Scripts" / "python.exe"
    isolated_python.parent.mkdir(parents=True)
    isolated_python.write_bytes(b"isolated")
    monkeypatch.setattr(
        voice_manager_module,
        "resolve_tts_python",
        lambda: isolated_python,
        raising=False,
    )
    manager = _manager(tmp_path)

    preset = manager._preset_payload("ko_bella")

    assert preset["provider_options"]["python_exe"] == str(isolated_python)
    assert preset["provider_options"]["model_cache"] == str(
        isolated_python.parent.parent / "model-cache"
    )


def test_windows_voice_presets_are_ready_only_when_the_exact_sapi_voice_exists(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        manager,
        "_available_windows_voice_names",
        lambda: frozenset({"Microsoft David Desktop"}),
        raising=False,
    )

    availability = {
        alias: status for alias, _preset, status in manager.get_voice_presets()
    }

    assert availability["wusm"] == "ready"
    assert availability["wus"] == "missing system voice"
    assert availability["cn"] == "missing system voice"


def test_packaged_tts_voice_preview_bundle_covers_non_macos_presets(tmp_path):
    manager = _manager(tmp_path)
    aliases = [
        alias
        for alias, preset in VoiceManager.VOICE_PRESETS.items()
        if preset["provider"] != "macos"
    ]

    assert len(aliases) == 31
    for alias in aliases:
        assert manager.get_tts_voice_preview_assets(alias, telegram=True)
        assert manager.get_tts_voice_preview_assets(alias, telegram=False)


def test_voice_state_writer_normalizes_legacy_bytes_and_preserves_unknown_fields(tmp_path):
    manager = _manager(tmp_path)
    manager.workspace_dir.mkdir(parents=True)
    manager.state_path.write_bytes(
        b'\xef\xbb\xbf{\r\n  "enabled": false,\r\n  "future": {"keep": 1}\r\n}\r\n'
    )

    manager.set_voice_profile("clear_female")

    raw = manager.state_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert json.loads(raw)["future"] == {"keep": 1}


def test_voice_state_writer_rejects_corruption_without_overwrite(tmp_path):
    manager = _manager(tmp_path)
    manager.workspace_dir.mkdir(parents=True)
    manager.state_path.write_bytes(b'{"enabled":')
    before = manager.state_path.read_bytes()

    with pytest.raises(RuntimeError, match="unreadable"):
        manager.set_voice_profile("warm_female")

    assert manager.state_path.read_bytes() == before


def test_voice_state_conflict_does_not_replay_setting_change(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.workspace_dir.mkdir(parents=True)
    manager.state_path.write_text('{"voice_profile": "warm_female"}', encoding="utf-8")
    actual_write = write_config_json
    injected = False

    def interleaved(path, payload, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            winner = read_config_json(path)
            winner["voice_profile"] = "calm_male"
            actual_write(path, winner)
        return actual_write(path, payload, **kwargs)

    monkeypatch.setattr(voice_manager_module, "write_config_json", interleaved)

    with pytest.raises(RuntimeError, match="changed since"):
        manager.set_voice_profile("clear_female")

    assert read_config_json(manager.state_path)["voice_profile"] == "calm_male"


def test_voice_modes_coordinate_native_and_legacy_tts_state(tmp_path):
    manager = _manager(tmp_path)

    assert manager.get_reply_mode() == "off"
    assert manager.is_enabled() is False
    assert not manager.state_path.exists()

    manager.set_reply_mode("tts")
    manager = _manager(tmp_path)
    assert manager.get_reply_mode() == "tts"
    assert manager.is_enabled() is True
    assert manager.native_policy["mode"] == "tts"

    manager.set_reply_mode("auto")
    manager = _manager(tmp_path)
    assert manager.get_reply_mode() == "native"
    assert manager.is_enabled() is False
    assert manager.native_audio_enabled() is True
    assert manager.native_policy["provider"] == "openrouter-api"
    assert manager.native_policy["model"] == "openai/gpt-audio-mini"
    assert manager.native_policy["voice"] == "shimmer"
    assert manager.get_voice_profile_id() == "warm_female"

    manager.set_reply_mode("off")
    manager = _manager(tmp_path)
    assert manager.get_reply_mode() == "off"
    assert manager.is_enabled() is False
    assert manager.native_audio_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [None, "{}", "{invalid json"])
async def test_unconfigured_voice_never_synthesizes_or_writes_state(tmp_path, monkeypatch, saved):
    manager = _manager(tmp_path)
    if saved is not None:
        manager.state_path.parent.mkdir(parents=True, exist_ok=True)
        manager.state_path.write_text(saved, encoding="utf-8")
    provider = SimpleNamespace(synthesize=AsyncMock())
    monkeypatch.setattr(voice_manager_module, "build_provider", lambda *a, **k: provider)

    assert await manager.synthesize_reply("agent", "request", "普通文字回复") is None
    assert manager.get_reply_mode() == "off"
    provider.synthesize.assert_not_called()
    assert (manager.state_path.read_text(encoding="utf-8") if manager.state_path.exists() else None) == saved


def test_native_mode_fails_closed_without_a_complete_audio_capability(tmp_path):
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        native_capabilities=[
            {
                "engine": "text-api",
                "model": "text-only",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            }
        ],
    )

    with pytest.raises(RuntimeError, match="no compatible audio model"):
        manager.set_reply_mode("native")

    assert manager.get_reply_mode() == "off"
    assert manager.state_path.exists() is False


def test_voice_menu_separates_rules_routes_and_semantic_style(tmp_path):
    manager = _manager(tmp_path)
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")
    manager.set_reply_mode("auto")
    manager.set_voice_profile("clear_female")
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager

    menu = manager.voice_menu_text()
    keyboard = runtime._voice_keyboard().inline_keyboard
    rules = manager.voice_rules_menu_text()
    rules_keyboard = runtime._voice_keyboard("rules").inline_keyboard

    assert "<b>Current</b> · <b>NATIVE</b>" in menu
    assert "<b>Reply rules</b>" in menu
    assert "<b>Voice route</b>" in menu
    assert "<b>Voice style</b> · 👩 Clear" in menu
    assert "<b>When to speak</b> · All replies" in rules
    assert "<b>How to generate voice</b> · Native audio model" in rules
    assert (
        "<b>Reply format</b> · Audio + text · "
        "applies to every voice path for this Agent"
    ) in rules
    assert "Native target" not in menu
    assert "Retention" not in menu
    assert "Audio-model tools" not in menu

    assert len(keyboard) == 3
    assert [[button.text for button in row] for row in keyboard] == [
        ["📋 Reply rules"],
        ["🧭 TTS services and fallbacks"],
        ["🎨 Semantic voice style"],
    ]
    assert [[button.callback_data for button in row] for row in keyboard] == [
        ["voice:view:rules"],
        ["voice:view:routes"],
        ["voice:view:profiles"],
    ]
    assert [len(row) for row in rules_keyboard] == [2, 1, 2, 2, 1, 1]
    assert rules_keyboard[-2][0].callback_data == "voice:content:text"


@pytest.mark.asyncio
async def test_voice_profile_callback_updates_both_renderers_without_growing_menu(
    tmp_path,
):
    manager = _manager(tmp_path)
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager
    runtime._is_authorized_user = lambda _user_id: True
    _prepare_runtime_for_fc(runtime, tmp_path)
    sent_previews = []

    for renderer in ("native", "tts"):
        preview = manager.voice_preview_path("calm_male", renderer)
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"OggS" + renderer.encode("ascii"))

    class Bot:
        async def send_voice(self, **kwargs):
            sent_previews.append(
                {
                    **kwargs,
                    "payload": kwargs["voice"].read(),
                }
            )

    runtime.app = SimpleNamespace(bot=Bot())
    runtime.telegram_logger = SimpleNamespace(warning=lambda *_args: None)
    runtime.error_logger = SimpleNamespace(error=lambda *_args: None)

    class Query:
        data = "voice:profile:calm_male"
        from_user = SimpleNamespace(id=7)
        message = SimpleNamespace(chat_id=99)

        def __init__(self):
            self.edits = []
            self.answers = []

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

    query = Query()
    await runtime.callback_voice(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    state = manager.get_state()
    assert state["voice_profile"] == "calm_male"
    assert state["native"]["voice"] == "echo"
    assert state["provider"] == "windows"
    assert state["voice_name"] is None
    assert len(query.edits) == 1
    assert "native echo" not in query.edits[0][0]
    assert len(query.edits[0][1]["reply_markup"].inline_keyboard) == 3
    assert query.answers[0][0].startswith("Voice set to 👨 Calm")
    assert [call["chat_id"] for call in sent_previews] == [99, 99]
    assert [call["caption"] for call in sent_previews] == [
        "🎧 Native audio model · 👨 Calm",
        "🎧 Text model + TTS · 👨 Calm",
    ]
    assert [call["payload"] for call in sent_previews] == [
        b"OggSnative",
        b"OggStts",
    ]


@pytest.mark.asyncio
async def test_tts_voice_selection_saves_and_publishes_matching_prerecorded_preview(
    tmp_path,
):
    bundle_root = tmp_path / "tts_voice_preview_assets"
    preset = VoiceManager.VOICE_PRESETS["ecn"]
    paths = _write_tts_voice_preview_bundle(
        bundle_root,
        alias="ecn",
        preset=preset,
        locale="zh-CN",
    )
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        tts_preview_asset_root=bundle_root,
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager
    runtime._is_authorized_user = lambda _user_id: True
    _prepare_runtime_for_fc(runtime, tmp_path)
    sent_previews = []

    class Bot:
        async def send_voice(self, **kwargs):
            sent_previews.append(
                {
                    **kwargs,
                    "payload": kwargs["voice"].read(),
                }
            )

    runtime.app = SimpleNamespace(bot=Bot())
    runtime.telegram_logger = SimpleNamespace(warning=lambda *_args: None)
    preview_errors = []
    runtime.error_logger = SimpleNamespace(
        error=lambda *args: preview_errors.append(args)
    )
    runtime._reply_text = AsyncMock()

    class Query:
        id = "tts-voice-selection"
        data = "voice:use:ecn"
        from_user = SimpleNamespace(id=7)
        message = SimpleNamespace(chat_id=99)

        def __init__(self):
            self.answers = []

        async def edit_message_text(self, *_args, **_kwargs):
            return None

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

    query = Query()
    await runtime.callback_voice(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert manager.get_active_preset_alias() == "ecn"
    assert query.answers[0][1].get("show_alert") is not True
    assert len(sent_previews) == 1, preview_errors
    assert sent_previews[0]["payload"] == paths["tts"].read_bytes()
    assert sent_previews[0]["caption"] == (
        "🎧 TTS voice preview · Xiaoxiao "
        "(Simplified Chinese · Female · Edge)"
    )


@pytest.mark.asyncio
async def test_workbench_tts_voice_selection_publishes_browser_mp3_preview(tmp_path):
    bundle_root = tmp_path / "tts_voice_preview_assets"
    preset = VoiceManager.VOICE_PRESETS["ecn"]
    paths = _write_tts_voice_preview_bundle(
        bundle_root,
        alias="ecn",
        preset=preset,
        locale="zh-CN",
    )
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        tts_preview_asset_root=bundle_root,
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager
    runtime._is_authorized_user = lambda _user_id: True
    _prepare_runtime_for_fc(runtime, tmp_path)
    owner_id = SessionStore.owner_id_for(runtime.global_config)
    session = runtime.session_store.create_session(
        owner_id=owner_id,
        agent_id=runtime.name,
        title="Workbench TTS voice preview",
        is_default=True,
    )
    runtime.default_session_id = session["session_id"]
    runtime.error_logger = SimpleNamespace(error=lambda *_args: None)

    class Query:
        id = "workbench-tts-voice-selection"
        data = "voice:use:ecn"
        from_user = SimpleNamespace(id=7)
        message = SimpleNamespace(chat_id=7)

        async def edit_message_text(self, *_args, **_kwargs):
            return None

        async def answer(self, *_args, **_kwargs):
            return None

    update = SimpleNamespace(
        callback_query=Query(),
        _hashi_session_surface="workbench",
        _hashi_session_channel_key="default",
        _hashi_session_owner_id=owner_id,
        _hashi_session_id=session["session_id"],
        _hashi_session_context_generation=session["context_generation"],
    )

    await runtime.callback_voice(update, SimpleNamespace())

    previews = [
        message
        for message in runtime.session_store.messages(
            session["session_id"], owner_id=owner_id
        )
        if message["source"] == "workbench.explicit-media"
    ]
    assert len(previews) == 1
    part = previews[0]["content"][0]
    assert part["mime_type"] == "audio/mpeg"
    assert part["filename"] == "ecn.mp3"
    assert part["semantic_role"] == "audio_attachment"
    _attachment, payload = runtime.session_store.attachment_bytes(
        session_id=session["session_id"],
        owner_id=owner_id,
        attachment_id=part["attachment_id"],
    )
    assert payload == paths["tts_mp3"].read_bytes()


@pytest.mark.asyncio
async def test_missing_tts_voice_preview_does_not_rollback_saved_selection(tmp_path):
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        tts_preview_asset_root=tmp_path / "missing-preview-bundle",
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager
    runtime._is_authorized_user = lambda _user_id: True
    _prepare_runtime_for_fc(runtime, tmp_path)
    send_voice = AsyncMock()
    runtime.app = SimpleNamespace(bot=SimpleNamespace(send_voice=send_voice))
    runtime.telegram_logger = SimpleNamespace(warning=lambda *_args: None)
    runtime.error_logger = SimpleNamespace(error=lambda *_args: None)

    class Query:
        id = "missing-tts-preview"
        data = "voice:use:eja"
        from_user = SimpleNamespace(id=7)
        message = SimpleNamespace(chat_id=99)

        def __init__(self):
            self.answers = []

        async def edit_message_text(self, *_args, **_kwargs):
            return None

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

    query = Query()
    await runtime.callback_voice(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert manager.get_active_preset_alias() == "eja"
    send_voice.assert_not_awaited()
    assert query.answers == [
        (
            "Voice saved, but its prerecorded preview is unavailable.",
            {"show_alert": True},
        )
    ]


@pytest.mark.asyncio
async def test_workbench_voice_preview_uses_bound_session_without_telegram(
    tmp_path,
):
    manager = _manager(tmp_path)
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager
    runtime._is_authorized_user = lambda _user_id: True
    _prepare_runtime_for_fc(runtime, tmp_path)
    owner_id = SessionStore.owner_id_for(runtime.global_config)
    session = runtime.session_store.create_session(
        owner_id=owner_id,
        agent_id=runtime.name,
        title="Workbench voice preview",
        is_default=True,
    )
    runtime.default_session_id = session["session_id"]

    expected_payloads = {}
    for renderer in ("native", "tts"):
        preview = manager.voice_preview_path("calm_male", renderer, locale="en")
        preview.parent.mkdir(parents=True, exist_ok=True)
        payload = b"OggS" + renderer.encode("ascii")
        preview.write_bytes(payload)
        expected_payloads[renderer] = payload

    telegram_send = AsyncMock()
    runtime.app = SimpleNamespace(
        bot=SimpleNamespace(send_voice=telegram_send),
    )
    runtime.telegram_logger = SimpleNamespace(warning=lambda *_args: None)
    runtime.error_logger = SimpleNamespace(error=lambda *_args: None)

    class Query:
        data = "voice:profile:calm_male"
        from_user = SimpleNamespace(id=7)
        message = SimpleNamespace(chat_id=7)

        def __init__(self):
            self.answers = []

        async def edit_message_text(self, *_args, **_kwargs):
            return None

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

    query = Query()
    update = SimpleNamespace(
        callback_query=query,
        _hashi_session_surface="workbench",
        _hashi_session_channel_key="default",
        _hashi_session_owner_id=owner_id,
        _hashi_session_id=session["session_id"],
        _hashi_session_context_generation=session["context_generation"],
    )

    await runtime.callback_voice(update, SimpleNamespace())

    telegram_send.assert_not_awaited()
    messages = runtime.session_store.messages(
        session["session_id"], owner_id=owner_id
    )
    previews = [
        message
        for message in messages
        if message["source"] == "workbench.explicit-media"
    ]
    assert len(previews) == 2
    assert {message["context_generation"] for message in previews} == {
        session["context_generation"]
    }
    assert all(message["history_eligible"] is False for message in previews)
    assert [message["content"][0]["semantic_role"] for message in previews] == [
        "audio_attachment",
        "audio_attachment",
    ]
    assert [message["content"][0]["presentation_role"] for message in previews] == [
        "audio",
        "audio",
    ]
    stored_payloads = []
    for message in previews:
        part = message["content"][0]
        _attachment, payload = runtime.session_store.attachment_bytes(
            session_id=session["session_id"],
            owner_id=owner_id,
            attachment_id=part["attachment_id"],
        )
        stored_payloads.append(payload)
    assert stored_payloads == [
        expected_payloads["native"],
        expected_payloads["tts"],
    ]


@pytest.mark.asyncio
async def test_voice_mode_callback_sends_only_matching_current_profile_preview(
    tmp_path,
):
    manager = _manager(tmp_path)
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")
    manager.set_voice_profile("warm_female")
    for renderer in ("native", "tts"):
        preview = manager.voice_preview_path("warm_female", renderer)
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"OggS" + renderer.encode("ascii"))

    sent = []

    class Bot:
        async def send_voice(self, **kwargs):
            sent.append(kwargs["caption"])

    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager
    runtime._is_authorized_user = lambda _user_id: True
    _prepare_runtime_for_fc(runtime, tmp_path)
    runtime.app = SimpleNamespace(bot=Bot())
    runtime.telegram_logger = SimpleNamespace(warning=lambda *_args: None)
    runtime.error_logger = SimpleNamespace(error=lambda *_args: None)

    class Query:
        data = "voice:mode:tts"
        from_user = SimpleNamespace(id=7)
        message = SimpleNamespace(chat_id=99)

        async def edit_message_text(self, *_args, **_kwargs):
            return None

        async def answer(self, *_args, **_kwargs):
            return None

    await runtime.callback_voice(
        SimpleNamespace(callback_query=Query()), SimpleNamespace()
    )

    assert manager.get_reply_mode() == "tts"
    assert sent == ["🎧 Text model + TTS · 👩 Warm"]


@pytest.mark.asyncio
async def test_semantic_profile_drives_language_aware_tts_synthesis(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    manager.set_provider("edge")
    manager.set_voice_profile("warm_male")
    manager.set_reply_mode("tts")
    calls = []

    class Provider:
        async def synthesize(self, **kwargs):
            calls.append(kwargs)
            output = Path(kwargs["output_dir"]) / "voice.ogg"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"ogg")
            return VoiceAsset(
                provider="edge",
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=output,
            )

    monkeypatch.setattr(
        "orchestrator.voice_manager.build_provider",
        lambda *_args, **_kwargs: Provider(),
    )

    await manager.synthesize_reply("arale", "req-voice", "任务完成。")

    assert calls[0]["voice_name"] == "zh-CN-YunxiNeural"


def test_voice_picker_uses_clear_localized_language_annotations(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setattr(manager, "_provider_status", lambda _provider: "installed")
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager

    with voice_manager_module.ui_language.language_scope(manager, locale="zh-CN"):
        keyboard = runtime._voice_keyboard("voices").inline_keyboard
        labels = [row[0].text for row in keyboard[:-1]]
        main_menu = manager.voice_menu_text()

    assert "晓晓（简中 · 女声 · Edge）" in labels
    assert "七海（日语 · 女声 · Edge）" in labels
    assert "晓晓（简中 · 女声 · Kokoro）" in labels
    assert "<b>语音风格</b> · 未设置" in main_menu
    assert keyboard[-1][0].callback_data == "voice:back:main"


@pytest.mark.asyncio
async def test_japanese_adaptation_is_temporary_audited_and_user_visible(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    manager.apply_voice_preset("ecn")
    manager.set_reply_mode("tts")
    calls = []

    class Provider:
        async def synthesize(self, **kwargs):
            calls.append(kwargs)
            output = Path(kwargs["output_dir"]) / "voice.ogg"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"OggS-test")
            return VoiceAsset(
                provider="edge",
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=output,
            )

    monkeypatch.setattr(voice_manager_module, "build_provider", lambda *_a, **_k: Provider())

    text = "これは日本語の文章です。すぐに読み上げてください。"
    asset = await manager.synthesize_reply(
        "arale",
        "req-japanese",
        text,
        force=True,
        source_language="zh-CN",
        source_surface="workbench",
    )

    assert calls[0]["voice_name"] == "ja-JP-NanamiNeural"
    assert asset.voice_name == "ja-JP-NanamiNeural"
    assert asset.adapted_from_voice == "zh-CN-XiaoxiaoNeural"
    assert asset.language == "ja"
    assert "临时" in asset.notice
    assert "默认语音未改变" in asset.notice
    assert manager.get_state()["voice_name"] == "zh-CN-XiaoxiaoNeural"

    audit = json.loads(manager.voice_audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert audit["source_surface"] == "workbench"
    assert audit["language"]["language"] == "ja"
    assert audit["routing"]["primary"]["adapted_from_voice"] == "zh-CN-XiaoxiaoNeural"
    assert audit["attempts"][0]["status"] == "succeeded"
    assert audit["result"]["voice_name"] == "ja-JP-NanamiNeural"
    assert text not in manager.voice_audit_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_edge_protocol_rejection_falls_back_to_windows_without_reusing_edge_voice(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    state = {
        **VoiceManager.DEFAULT_STATE,
        "enabled": True,
        "provider": "edge",
        "voice_name": "zh-CN-XiaoxiaoNeural",
        "provider_options": {"voice": "zh-CN-XiaoxiaoNeural"},
    }
    monkeypatch.setattr(manager, "_load", lambda: state)
    monkeypatch.setattr(voice_manager_module, "_default_tts_provider", lambda: "windows")
    built = []
    edge_calls = []
    windows_calls = []
    fallback_asset = VoiceAsset(
        provider="windows",
        text="任务完成。",
        spoken_text="任务完成。",
        wav_path=tmp_path / "voice.wav",
        ogg_path=tmp_path / "voice.ogg",
    )

    class EdgeProvider:
        async def synthesize(self, **kwargs):
            edge_calls.append(kwargs)
            raise RuntimeError(
                "WSServerHandshakeError: 403, url='wss://speech.invalid/path?token=not-a-real-secret'"
            )

    class WindowsProvider:
        async def synthesize(self, **kwargs):
            windows_calls.append(kwargs)
            return fallback_asset

    def provider(name, **_kwargs):
        built.append(name)
        return EdgeProvider() if name == "edge" else WindowsProvider()

    monkeypatch.setattr(voice_manager_module, "build_provider", provider)

    asset = await manager.synthesize_reply(
        "arale", "req-fallback", "任务完成。", max_retries=2, force=True
    )

    assert built == ["edge", "windows"]
    assert len(edge_calls) == 1
    assert len(windows_calls) == 1
    assert windows_calls[0]["voice_name"] is None
    assert windows_calls[0]["provider_options"] == {"language_hint": "zh"}
    assert asset.provider == "windows"
    assert asset.fallback_from == "edge"
    assert asset.fallback_reason_code == "tts_protocol_rejected"
    assert "HTTP 403" in asset.fallback_reason
    assert "token=" not in asset.fallback_reason
    assert "downgraded to Windows TTS" in asset.notice

    second = await manager.synthesize_reply(
        "arale", "req-cooldown", "任务完成。", max_retries=2, force=True
    )
    assert built == ["edge", "windows", "windows"]
    assert len(edge_calls) == 1
    assert len(windows_calls) == 2
    assert second.fallback_reason_code == "tts_protocol_rejected"
    assert "temporarily bypassed" in second.fallback_reason


@pytest.mark.asyncio
async def test_voice_failure_reports_primary_and_windows_fallback_without_secrets(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    state = {
        **VoiceManager.DEFAULT_STATE,
        "enabled": True,
        "provider": "edge",
        "voice_name": "ja-JP-NanamiNeural",
        "provider_options": {},
    }
    monkeypatch.setattr(manager, "_load", lambda: state)
    monkeypatch.setattr(voice_manager_module, "_default_tts_provider", lambda: "windows")

    class Provider:
        def __init__(self, name):
            self.name = name

        async def synthesize(self, **_kwargs):
            if self.name == "edge":
                raise RuntimeError(
                    "WSServerHandshakeError: 403, url='wss://speech.invalid/path?token=not-a-real-secret'"
                )
            raise RuntimeError("No installed Windows TTS voice for language: ja.")

    monkeypatch.setattr(
        voice_manager_module,
        "build_provider",
        lambda name, **_kwargs: Provider(name),
    )

    with pytest.raises(RuntimeError) as raised:
        await manager.synthesize_reply(
            "arale", "req-all-failed", "終わりました。", max_retries=2, force=True
        )

    error = raised.value
    assert getattr(error, "code", None) == "tts_all_providers_failed"
    assert "Edge TTS" in str(error)
    assert "HTTP 403" in str(error)
