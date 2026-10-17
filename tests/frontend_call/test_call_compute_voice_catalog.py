"""Hashi Compute is the source of Call voice choices and preview assets."""

import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import ui_language
from orchestrator.compute_services import (
    ComputeAudioFormats,
    ComputeCapacity,
    ComputeHealth,
    ComputeImageLimits,
    ComputeInterface,
    ComputeNode,
    ComputeNodeSnapshot,
    ComputeOffering,
    ComputeServiceError,
)
from orchestrator.config_json import new_config_json, write_config_json
from orchestrator.frontend_call.compute_voice_catalog import (
    ComputeVoiceDirectory,
    discover_compute_voice_directory,
    parse_speech_catalogue,
)
from orchestrator.frontend_call.defaults import default_call_configuration
from orchestrator.frontend_call.settings import CallSettings, callback


def _node():
    return ComputeNode(
        id="hashi-compute",
        label="Hashi Compute",
        location="lan",
        base_url="http://192.168.1.50:19446",
        manifest_path="/v1/manifest",
    ).validated()


def _audition(language, name):
    return {
        "language": language,
        "text": f"preview-{language}",
        "status": "available",
        "sample": {"url": f"/v1/speech/samples/{name}-{language}.wav"},
    }


def _catalogue_payload():
    return {
        "format": "hashi-compute.speech-catalogue",
        "version": 1,
        "base_url": "http://192.168.1.50:19446/v1",
        "configuration_revision": 38,
        "models": [
            {
                "id": "kokoro-82m",
                "voices": [
                    {
                        "id": "zf_001",
                        "label": "年轻4",
                        "gender": "female",
                        "language": "zh",
                        "language_name": "中文",
                        "note": "自然轻度沙哑感，约18岁",
                        "callable": True,
                        "availability": "voice_available",
                        "auditions": [_audition("zh", "zf_001")],
                    },
                    {
                        "id": "zm_009",
                        "label": "中文 · 男声",
                        "gender": None,
                        "language": "zh",
                        "language_name": None,
                        "note": None,
                        "callable": True,
                        "availability": "voice_available",
                        "auditions": [_audition("zh", "zm_009")],
                    },
                    {
                        "id": "am_michael",
                        "label": "成熟2",
                        "gender": "male",
                        "language": "en",
                        "language_name": "英语",
                        "note": "沙哑，约40岁",
                        "callable": True,
                        "availability": "voice_available",
                        "auditions": [
                            _audition("zh", "am_michael"),
                            _audition("en", "am_michael"),
                        ],
                    },
                    {
                        "id": "zf_hidden",
                        "label": "未开放",
                        "gender": "female",
                        "language": "fr",
                        "language_name": "法语",
                        "note": "仅供目录试听",
                        "callable": False,
                        "availability": "voice_not_exposed",
                        "auditions": [_audition("zh", "zf_hidden")],
                    },
                ],
            }
        ],
    }


def _wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x00\x00" * 2400)
    return stream.getvalue()


class _ResourceClient:
    def __init__(self):
        self.paths = []

    async def fetch_resource_bytes(self, node, path):
        assert node == _node()
        self.paths.append(path)
        return _wav_bytes(), "audio/wav"


class _DiscoveryClient(_ResourceClient):
    def __init__(self, snapshot, payload):
        super().__init__()
        self.snapshot = snapshot
        self.payload = payload
        self.json_paths = []

    async def fetch_manifest(self, node):
        assert node == self.snapshot.node
        return self.snapshot

    async def fetch_json_resource(self, node, path):
        assert node == self.snapshot.node
        self.json_paths.append(path)
        return self.payload


def _settings(tmp_path, catalogue, client):
    path = tmp_path / "call_profiles.json"
    doc = default_call_configuration()
    target = {
        "id": "hashicompute-kokoro",
        "kind": "tts",
        "adapter": "openai_compatible",
        "location": "lan",
        "base_url": "http://192.168.1.50:19446/v1",
        "model": "kokoro-82m",
        "label": "Hashi Compute Kokoro",
        "audio_format": "wav",
        "voices": ["zf_001"],
        "voice_styles": {"zf_001": "年轻4"},
        "options": {},
    }
    doc["targets"].append(target)
    doc["default_profile"]["tts"] = {
        "target_id": target["id"],
        "voice_id": "zf_001",
        "options": {},
    }
    write_config_json(path, new_config_json(path, doc))
    runtime = SimpleNamespace(
        name="call-voice-test",
        global_config=SimpleNamespace(bridge_home=tmp_path, authorized_id=7),
    )
    settings = CallSettings(runtime)
    settings.voice_directory = ComputeVoiceDirectory(
        target_id=target["id"],
        node=_node(),
        catalogue=catalogue,
        client=client,
    )
    return settings


def test_catalogue_keeps_callable_metadata_and_rejects_external_samples():
    catalogue = parse_speech_catalogue(
        _catalogue_payload(),
        model_id="kokoro-82m",
        expected_base_url="http://192.168.1.50:19446/v1",
    )
    assert catalogue.total == 4
    assert [voice.id for voice in catalogue.callable_voices] == [
        "zf_001",
        "zm_009",
        "am_michael",
    ]
    assert catalogue.voice("zf_001").description == "自然轻度沙哑感，约18岁"
    assert catalogue.voice("zf_hidden").callable is False

    invalid = _catalogue_payload()
    invalid["models"][0]["voices"][0]["auditions"][0]["sample"]["url"] = (
        "https://example.invalid/sample.wav"
    )
    with pytest.raises(ComputeServiceError, match="speech_catalogue_invalid"):
        parse_speech_catalogue(
            invalid,
            model_id="kokoro-82m",
            expected_base_url="http://192.168.1.50:19446/v1",
        )


@pytest.mark.asyncio
async def test_directory_matches_exact_call_endpoint_and_declared_resource(tmp_path):
    node = _node()
    offering = ComputeOffering(
        service_id="tts",
        kind="tts",
        model_id="kokoro-82m",
        selected=True,
        enabled=True,
        status="ready",
        input_modalities=frozenset({"text"}),
        output_modalities=frozenset({"audio"}),
        interfaces=(
            ComputeInterface(
                id="speech",
                protocol="openai.audio.speech",
                method="POST",
                path="/v1/audio/speech",
                streaming=(),
            ),
        ),
        image=ComputeImageLimits(),
        audio=ComputeAudioFormats(output_formats=("wav",)),
        tools=False,
        thinking=False,
        context_length=None,
        capacity=ComputeCapacity(max_concurrency=1, in_flight=0, busy=False),
    )
    snapshot = ComputeNodeSnapshot(
        node=node,
        schema_version=1,
        source_format="hashi_compute_manifest",
        name="Hashi Compute",
        service_version="0.2.0",
        config_revision="38",
        privacy_location="lan",
        health=ComputeHealth("ready", "/health"),
        capacity=ComputeCapacity(max_concurrency=4, in_flight=0, busy=False),
        offerings=(offering,),
        resources={"speech_catalogue": "/v1/speech/catalogue"},
    )
    config_path = tmp_path / "compute_services.json"
    write_config_json(
        config_path,
        new_config_json(
            config_path,
            {
                "version": 1,
                "nodes": [
                    {
                        "id": node.id,
                        "label": node.label,
                        "location": node.location,
                        "base_url": node.base_url,
                        "manifest_path": node.manifest_path,
                    }
                ],
            },
        ),
    )
    client = _DiscoveryClient(snapshot, _catalogue_payload())
    target = {
        "id": "hashicompute-kokoro",
        "base_url": "http://192.168.1.50:19446/v1",
        "model": "kokoro-82m",
    }

    directory = await discover_compute_voice_directory(
        tmp_path,
        target,
        client=client,
    )

    assert directory is not None
    assert directory.target_id == target["id"]
    assert directory.catalogue.total == 4
    assert client.json_paths == ["/v1/speech/catalogue"]
    assert (
        await discover_compute_voice_directory(
            tmp_path,
            {**target, "base_url": "http://192.168.1.50:19446/other"},
            client=client,
        )
        is None
    )


@pytest.mark.asyncio
async def test_compute_voice_menu_groups_metadata_and_downloads_catalogue_preview(
    tmp_path,
):
    catalogue = parse_speech_catalogue(
        _catalogue_payload(),
        model_id="kokoro-82m",
        expected_base_url="http://192.168.1.50:19446/v1",
    )
    client = _ResourceClient()
    settings = _settings(tmp_path, catalogue, client)

    with ui_language.language_scope(None, locale="zh-CN"):
        text, keyboard = settings.render("voice")
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        assert "中文 · 女声 · 1" in labels
        assert "中文 · 性别未标注 · 1" in labels
        assert "英语 · 男声 · 1" in labels
        assert "法语 · 女声 · 1" in labels
        assert "年轻4" in text and "自然轻度沙哑感，约18岁" in text
        assert "3 / 4" in text

        group_pages = [
            button.callback_data.split(":", 3)[2]
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data.startswith("call:view:voice.")
        ]
        selection = None
        selection_label = ""
        locked_preview = None
        locked_label = ""
        group_buttons = []
        for page in group_pages:
            _group_text, group_keyboard = settings.render(page)
            for row in group_keyboard.inline_keyboard:
                for button in row:
                    group_buttons.append(button)
                    if button.callback_data.startswith("call:cvoice:am_michael:"):
                        selection = button.callback_data
                        selection_label = button.text
                    if button.callback_data.startswith("call:vpreview:zf_hidden:"):
                        locked_preview = button.callback_data
                        locked_label = button.text
        assert selection is not None
        assert "成熟2" in selection_label and "沙哑，约40岁" in selection_label
        assert locked_preview is not None
        assert "未开放" in locked_label and "仅试听" in locked_label
        assert all(
            len(button.callback_data.encode()) <= 64
            for button in [
                *(button for row in keyboard.inline_keyboard for button in row),
                *group_buttons,
            ]
        )

        _, action, value, revision = locked_preview.split(":", 3)
        assert settings.apply(action, value, revision).startswith("voice.")
        locked_delivery = await settings.preview(
            telegram=False,
            voice_id=value,
        )
        unchanged = settings.config.context(settings.owner, settings.agent)
        assert unchanged["profile"]["tts"]["voice_id"] == "zf_001"

        _, action, value, revision = selection.split(":", 3)
        assert settings.apply(action, value, revision).startswith("voice.")
        preview = await settings.preview(telegram=False)

    context = settings.config.context(settings.owner, settings.agent)
    assert context["profile"]["tts"]["voice_id"] == "am_michael"
    _doc, targets = settings.config.read()
    assert "am_michael" in targets["hashicompute-kokoro"]["voices"]
    assert targets["hashicompute-kokoro"]["voice_styles"]["am_michael"] == "成熟2"
    assert locked_delivery.payload == _wav_bytes()
    assert locked_delivery.media_type == "audio/wav"
    assert preview.payload == _wav_bytes()
    assert preview.media_type == "audio/wav"
    assert client.paths == [
        "/v1/speech/samples/zf_hidden-zh.wav",
        "/v1/speech/samples/am_michael-zh.wav",
    ]


@pytest.mark.asyncio
async def test_preview_only_callback_publishes_catalogue_audio_without_saving(
    tmp_path,
    monkeypatch,
):
    catalogue = parse_speech_catalogue(
        _catalogue_payload(),
        model_id="kokoro-82m",
        expected_base_url="http://192.168.1.50:19446/v1",
    )
    client = _ResourceClient()
    prepared = _settings(tmp_path, catalogue, client)
    directory = prepared.voice_directory
    captured = {}

    async def discover(root, target, *, secret_resolver=None):
        assert root == tmp_path
        assert target["id"] == directory.target_id
        assert secret_resolver is None
        return directory

    async def send_preview(
        update,
        profile_id,
        assets,
        *,
        caption_override,
        media_type_override,
        filename_override,
        preview_id,
    ):
        captured.update(
            profile_id=profile_id,
            payload=assets[0][1].read_bytes(),
            caption=caption_override,
            media_type=media_type_override,
            filename=filename_override,
            preview_id=preview_id,
        )
        return True

    monkeypatch.setattr(
        "orchestrator.frontend_call.settings.discover_compute_voice_directory",
        discover,
    )
    runtime = SimpleNamespace(
        name="call-voice-test",
        global_config=SimpleNamespace(
            bridge_home=tmp_path,
            authorized_id=7,
        ),
        _is_authorized_user=lambda user_id: user_id == 7,
        _send_voice_profile_previews=send_preview,
        _reply_text=AsyncMock(),
    )
    settings = CallSettings(runtime)
    before = (tmp_path / "call_profiles.json").read_bytes()
    revision = settings.config.context(settings.owner, settings.agent)["revision"][:12]
    query = SimpleNamespace(
        id="locked-preview-1",
        data=f"call:vpreview:zf_hidden:{revision}",
        from_user=SimpleNamespace(id=7),
        edit_message_text=AsyncMock(),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        _hashi_session_surface="telegram",
    )

    with ui_language.language_scope(None, locale="zh-CN"):
        await callback(runtime, update, SimpleNamespace())

    assert (tmp_path / "call_profiles.json").read_bytes() == before
    assert captured["profile_id"] == "call:kokoro-82m:zf_hidden"
    assert captured["payload"] == _wav_bytes()
    assert captured["media_type"] == "audio/wav"
    assert captured["filename"] == "zf_hidden-zh.wav"
    assert captured["preview_id"] == query.id
    assert "未开放" in captured["caption"]
    assert client.paths == ["/v1/speech/samples/zf_hidden-zh.wav"]
    query.edit_message_text.assert_awaited_once()
    query.answer.assert_awaited_once_with(None, show_alert=False)
    runtime._reply_text.assert_not_awaited()
