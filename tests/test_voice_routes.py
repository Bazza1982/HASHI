from __future__ import annotations

import json
from pathlib import Path
import pytest

from orchestrator import voice_manager as voice_manager_module
from orchestrator import voice_services as voice_services_module
from orchestrator.compute_services import (
    ComputeAudioFormats,
    ComputeCapacity,
    ComputeHealth,
    ComputeImageLimits,
    ComputeInterface,
    ComputeNode,
    ComputeNodeSnapshot,
    ComputeOffering,
)
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_services import (
    VoiceChoice,
    VoiceService,
    VoiceServiceCatalogue,
    discover_voice_services,
)
from orchestrator.voice_synthesizer import VoiceAsset


def _manager(tmp_path: Path, name: str = "agent") -> VoiceManager:
    return VoiceManager(
        tmp_path / name / "workspace",
        tmp_path / name / "media",
        project_root=tmp_path,
    )


def _route(
    service: str,
    *,
    voice: str,
    model: str | None = None,
    node_id: str | None = None,
    offering_id: str | None = None,
    speed: float | None = None,
    style: str | None = None,
) -> dict:
    options = {}
    if speed is not None:
        options["speed"] = speed
    if style is not None:
        options["style"] = style
    return {
        "service": service,
        "node_id": node_id,
        "offering_id": offering_id,
        "model": model,
        "voice": voice,
        "options": options,
    }


def test_legacy_voice_state_is_migrated_in_memory_without_read_side_effect(tmp_path):
    manager = _manager(tmp_path)
    manager.state_path.parent.mkdir(parents=True)
    legacy = {
        "enabled": True,
        "provider": "edge",
        "voice_name": "zh-CN-XiaoxiaoNeural",
        "rate": 2,
        "provider_options": {"rate_percent": 15},
        "future": {"keep": True},
    }
    manager.state_path.write_text(json.dumps(legacy), encoding="utf-8")
    before = manager.state_path.read_bytes()

    state = manager.get_state()

    assert state["schema_version"] == 2
    assert state["tts_routes"] == [
        {
            "service": "edge",
            "node_id": None,
            "offering_id": None,
            "model": None,
            "voice": "zh-CN-XiaoxiaoNeural",
            "options": {"rate_percent": 15},
        }
    ]
    assert state["external_fallback_enabled"] is False
    assert state["future"] == {"keep": True}
    assert manager.state_path.read_bytes() == before


def test_legacy_kokoro_runtime_paths_are_rederived_not_persisted_in_route(tmp_path):
    manager = _manager(tmp_path)
    manager.state_path.parent.mkdir(parents=True)
    legacy = {
        "enabled": True,
        "provider": "kokoro",
        "voice_name": "zf_xiaoxiao",
        "provider_options": {
            "lang_code": "z",
            "python_exe": "C:/runtime/python.exe",
            "model_cache": "C:/runtime/model-cache",
        },
    }
    manager.state_path.write_text(json.dumps(legacy), encoding="utf-8")

    state = manager.get_state()

    assert state["tts_routes"][0]["service"] == "kokoro"
    assert state["tts_routes"][0]["voice"] == "zf_xiaoxiao"
    assert state["tts_routes"][0]["options"] == {"lang_code": "z"}
    assert state["provider_options"]["python_exe"] == "C:/runtime/python.exe"


@pytest.mark.asyncio
async def test_kokoro_route_rehydrates_current_isolated_runtime(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    isolated_python = tmp_path / "tts-runtime" / "Scripts" / "python.exe"
    isolated_python.parent.mkdir(parents=True)
    isolated_python.write_bytes(b"runtime")
    monkeypatch.setattr(
        voice_manager_module,
        "resolve_tts_python",
        lambda: isolated_python,
    )
    manager.replace_tts_routes(
        [_route("kokoro", voice="zf_xiaoxiao") | {"options": {"lang_code": "z"}}]
    )
    manager.set_reply_mode("tts")
    captured = {}

    class Provider:
        async def synthesize(self, **kwargs):
            captured.update(kwargs)
            output = Path(kwargs["output_dir"]) / "kokoro.ogg"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"OggS-test")
            return VoiceAsset(
                provider="kokoro",
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=output,
            )

    monkeypatch.setattr(
        voice_manager_module,
        "build_provider",
        lambda *_args, **_kwargs: Provider(),
    )

    await manager.synthesize_reply("agent", "request", "语音测试", max_retries=0)

    assert captured["provider_options"]["lang_code"] == "z"
    assert captured["provider_options"]["python_exe"] == str(isolated_python)
    assert captured["provider_options"]["model_cache"] == str(
        isolated_python.parent.parent / "model-cache"
    )


@pytest.mark.asyncio
async def test_semantic_profile_never_becomes_compute_performance_style(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    manager.set_voice_profile("warm_female")
    route = _route(
        "hashi_compute",
        node_id="hashi-compute",
        offering_id="advanced_tts",
        model="minicpm-o-4.5-zh-female",
        voice="zf_001",
    )
    manager.replace_tts_routes([route])
    manager.set_reply_mode("tts")
    captured = []

    class Provider:
        async def synthesize(self, **kwargs):
            captured.append(kwargs)
            output = Path(kwargs["output_dir"]) / f"voice-{len(captured)}.ogg"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"OggS-test")
            return VoiceAsset(
                provider="hashi_compute",
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=output,
                voice_name=kwargs["voice_name"],
            )

    monkeypatch.setattr(
        voice_manager_module,
        "build_provider",
        lambda *_args, **_kwargs: Provider(),
    )

    plain = await manager.synthesize_reply(
        "agent", "plain", "语音测试", max_retries=0
    )

    assert plain is not None
    assert plain.voice_name == "zf_001"
    assert plain.style_requested is None
    assert plain.style_applied is None
    assert "style" not in captured[0]["provider_options"]

    manager.replace_tts_routes(
        [{**route, "options": {"style": "温柔而自然"}}]
    )
    expressive = await manager.synthesize_reply(
        "agent", "expressive", "语音测试", max_retries=0
    )

    assert expressive is not None
    assert expressive.voice_name == "zf_001"
    assert expressive.style_requested == "温柔而自然"
    assert expressive.style_applied is True
    assert captured[1]["provider_options"]["style"] == "温柔而自然"


def test_voice_routes_are_ordered_revisioned_and_isolated_per_agent(tmp_path):
    first = _manager(tmp_path, "first")
    second = _manager(tmp_path, "second")
    routes = [
        _route(
            "hashi_compute",
            node_id="hashi-compute",
            offering_id="tts",
            model="kokoro-82m",
            voice="zf_001",
            speed=1.1,
        ),
        _route("windows", voice="Microsoft Huihui Desktop"),
        _route("edge", voice="zh-CN-XiaoxiaoNeural"),
    ]

    first.replace_tts_routes(routes)

    assert first.get_state()["tts_routes"] == routes
    assert second.get_state()["tts_routes"] != routes
    assert first.get_state()["external_fallback_enabled"] is False
    first.set_external_fallback_enabled(True)
    assert first.get_state()["external_fallback_enabled"] is True
    assert second.get_state()["external_fallback_enabled"] is False


def test_reply_trigger_is_independent_from_synthesis_mode(tmp_path):
    manager = _manager(tmp_path)
    manager.set_reply_mode("tts")

    assert manager.should_synthesize_reply(voice_origin=False) is True
    manager.set_reply_trigger("voice_message")
    assert manager.should_synthesize_reply(voice_origin=False) is False
    assert manager.should_synthesize_reply(voice_origin=True) is True

    manager.set_reply_mode("off")
    assert manager.should_synthesize_reply(voice_origin=True) is False


def test_reply_content_is_agent_wide_and_text_only_disables_tts(tmp_path):
    first = _manager(tmp_path, "first")
    second = _manager(tmp_path, "second")
    first.set_reply_mode("tts")
    second.set_reply_mode("tts")

    first.set_reply_content("text")

    assert first.get_reply_content() == "text_only"
    assert first.native_policy["reply_content"] == "text_only"
    assert first.should_synthesize_reply(voice_origin=True) is False
    assert second.get_reply_content() == "audio_and_text"
    assert second.should_synthesize_reply(voice_origin=True) is True


def test_legacy_native_reply_content_becomes_agent_wide_default(tmp_path):
    manager = _manager(tmp_path)
    manager.state_path.parent.mkdir(parents=True)
    manager.state_path.write_text(
        json.dumps({"native": {"reply_content": "audio_only"}}),
        encoding="utf-8",
    )

    assert manager.get_reply_content() == "audio_only"
    assert manager.get_state()["reply_content"] == "audio_only"


@pytest.mark.asyncio
async def test_catalogue_status_does_not_lock_an_authorized_voice(tmp_path):
    manager = _manager(tmp_path)
    route = _route(
        "hashi_compute",
        node_id="hashi-compute",
        offering_id="tts",
        model="kokoro-82m",
        voice="zf_001",
    )
    manager.replace_tts_routes([route])
    service = VoiceService(
        id="compute-ordinary",
        label="Hashi Compute TTS",
        service="hashi_compute",
        provider="hashi_compute",
        privacy="local_private",
        location="lan",
        available=True,
        availability="ready",
        node_id="hashi-compute",
        offering_id="tts",
        model="kokoro-82m",
        voices=(
            VoiceChoice("zf_001", "年轻4", options={"lang_code": "a"}),
            VoiceChoice(
                "zf_002",
                "年轻5",
                callable=False,
                options={"lang_code": "z"},
            ),
        ),
    )
    catalogue = VoiceServiceCatalogue((service,))

    await manager.set_catalogue_tts_route_voice(
        0,
        service.id,
        "zf_002",
        catalogue=catalogue,
    )

    assert manager.get_tts_routes()[0]["voice"] == "zf_002"
    assert manager.get_tts_routes()[0]["options"] == {"lang_code": "z"}


@pytest.mark.asyncio
async def test_recommended_routes_are_private_and_ordered(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    voices = (VoiceChoice("zf_001", "年轻4"),)
    ordinary = VoiceService(
        id="ordinary",
        label="Hashi Compute TTS",
        service="hashi_compute",
        provider="hashi_compute",
        privacy="local_private",
        location="lan",
        available=True,
        availability="ready",
        node_id="hashi-compute",
        offering_id="tts",
        model="kokoro-82m",
        supports_speed=True,
        speed_min=0.5,
        speed_max=2.0,
        voices=voices,
    )
    advanced = VoiceService(
        id="advanced",
        label="Hashi Compute advanced TTS",
        service="hashi_compute",
        provider="hashi_compute",
        privacy="local_private",
        location="lan",
        available=True,
        availability="ready",
        node_id="hashi-compute",
        offering_id="advanced_tts",
        model="minicpm-o-4.5-zh-female",
        supports_speed=True,
        speed_min=0.5,
        speed_max=2.0,
        supports_style=True,
        voices=voices,
    )
    windows = VoiceService(
        id="windows",
        label="Windows TTS",
        service="windows",
        provider="windows",
        privacy="local_private",
        location="device",
        available=True,
        availability="ready",
        supports_speed=True,
        speed_min=0.5,
        speed_max=2.0,
        voices=(VoiceChoice("Microsoft Huihui Desktop", "Huihui"),),
    )
    catalogue = VoiceServiceCatalogue((ordinary, advanced, windows))

    async def get_catalogue(**_kwargs):
        return catalogue

    monkeypatch.setattr(manager, "get_voice_service_catalogue", get_catalogue)

    await manager.apply_recommended_local_routes(advanced=False)
    assert [route["service"] for route in manager.get_tts_routes()] == [
        "hashi_compute",
        "windows",
    ]

    await manager.apply_recommended_local_routes(advanced=True)
    assert [route["offering_id"] for route in manager.get_tts_routes()] == [
        "advanced_tts",
        "tts",
        None,
    ]
    assert manager.get_state()["external_fallback_enabled"] is False


@pytest.mark.asyncio
async def test_local_route_does_not_cross_external_privacy_boundary_without_opt_in(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    manager.replace_tts_routes(
        [
            _route("windows", voice="Microsoft Huihui Desktop"),
            _route("edge", voice="zh-CN-XiaoxiaoNeural"),
        ]
    )
    manager.set_reply_mode("tts")
    built: list[str] = []

    class Provider:
        def __init__(self, name: str):
            self.name = name

        async def synthesize(self, **kwargs):
            if self.name == "windows":
                raise RuntimeError("local provider failed")
            output = Path(kwargs["output_dir"]) / "voice.ogg"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"OggS-test")
            return VoiceAsset(
                provider=self.name,
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=output,
            )

    def build(name, **_kwargs):
        built.append(name)
        return Provider(name)

    monkeypatch.setattr(voice_manager_module, "build_provider", build)

    with pytest.raises(RuntimeError):
        await manager.synthesize_reply("agent", "request", "隐私测试", max_retries=0)
    assert built == ["windows"]

    manager.set_external_fallback_enabled(True)
    asset = await manager.synthesize_reply(
        "agent", "request-2", "隐私测试", max_retries=0
    )
    assert built == ["windows", "edge"]
    assert asset is not None
    assert asset.provider == "edge"


@pytest.mark.asyncio
async def test_compute_offerings_have_independent_failure_cooldowns(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    manager.replace_tts_routes(
        [
            _route(
                "hashi_compute",
                node_id="hashi-compute",
                offering_id="advanced_tts",
                model="minicpm-o-4.5-zh-female",
                voice="zf_001",
            ),
            _route(
                "hashi_compute",
                node_id="hashi-compute",
                offering_id="tts",
                model="kokoro-82m",
                voice="zf_001",
            ),
        ]
    )
    manager.set_reply_mode("tts")
    attempts = []

    class Provider:
        async def synthesize(self, **kwargs):
            offering = kwargs["provider_options"]["offering_id"]
            attempts.append(offering)
            if offering == "advanced_tts":
                raise RuntimeError("advanced route unavailable")
            output = Path(kwargs["output_dir"]) / "ordinary.ogg"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"OggS-test")
            return VoiceAsset(
                provider="hashi_compute",
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=output,
            )

    monkeypatch.setattr(
        voice_manager_module,
        "build_provider",
        lambda *_args, **_kwargs: Provider(),
    )

    asset = await manager.synthesize_reply(
        "agent",
        "request",
        "语音测试",
        max_retries=0,
    )

    assert attempts == ["advanced_tts", "tts"]
    assert asset is not None
    assert asset.provider == "hashi_compute"


def test_voice_main_menu_is_hierarchical_and_reports_route_privacy(tmp_path):
    manager = _manager(tmp_path)
    (tmp_path / "compute_services.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "hashi-compute",
                        "label": "Hashi Compute",
                        "location": "lan",
                        "base_url": "http://192.168.1.50:19446",
                        "manifest_path": "/v1/manifest",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager.replace_tts_routes(
        [
            _route(
                "hashi_compute",
                node_id="hashi-compute",
                offering_id="tts",
                model="kokoro-82m",
                voice="zf_001",
            ),
            _route("windows", voice="Microsoft Huihui Desktop"),
        ]
    )

    text = manager.voice_menu_text()

    assert "Reply rules" in text
    assert "Voice route" in text
    assert "Hashi Compute TTS" in text
    assert "Windows TTS" in text
    assert "Local private · LAN" in text
    assert "Local private · device" in text
    assert "Provider" not in text


def test_compute_route_privacy_is_derived_and_unknown_fails_closed(tmp_path):
    manager = _manager(tmp_path)
    route = _route(
        "hashi_compute",
        node_id="remote-compute",
        offering_id="tts",
        model="kokoro-82m",
        voice="zf_001",
    )

    assert manager.tts_route_privacy(route) == ("unknown", "unknown")
    assert "Privacy unavailable" in manager.tts_route_privacy_label(route)

    (tmp_path / "compute_services.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "remote-compute",
                        "location": "internet",
                        "base_url": "https://compute.example.com",
                        "manifest_path": "/v1/manifest",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert manager.tts_route_privacy(route) == ("external", "network")


def _compute_offering(kind: str, model: str) -> ComputeOffering:
    interfaces = [
        ComputeInterface(
            id="speech",
            protocol="openai.audio.speech",
            method="POST",
            path="/v1/audio/speech",
            streaming=(),
        )
    ]
    if kind == "advanced_tts":
        interfaces.append(
            ComputeInterface(
                id="expressive",
                protocol="hashi.audio.speech.expressive",
                method="POST",
                path="/v1/audio/speech/expressive",
                streaming=(),
            )
        )
    return ComputeOffering(
        service_id=kind,
        kind=kind,
        model_id=model,
        selected=kind == "tts",
        enabled=True,
        status="ready",
        input_modalities=frozenset({"text"}),
        output_modalities=frozenset({"audio"}),
        interfaces=tuple(interfaces),
        image=ComputeImageLimits(),
        audio=ComputeAudioFormats(output_formats=("wav",)),
        tools=False,
        thinking=False,
        context_length=None,
        capacity=ComputeCapacity(max_concurrency=1, in_flight=0, busy=False),
    )


@pytest.mark.asyncio
async def test_compute_directory_becomes_two_services_with_one_voice_authority(
    tmp_path, monkeypatch
):
    node = ComputeNode(
        id="hashi-compute",
        label="Hashi Compute",
        location="lan",
        base_url="http://192.168.1.50:19446",
        manifest_path="/v1/manifest",
    ).validated()
    snapshot = ComputeNodeSnapshot(
        node=node,
        schema_version=1,
        source_format="hashi_compute_manifest",
        name="Hashi Compute",
        service_version="1.0",
        config_revision="1",
        privacy_location="lan",
        health=ComputeHealth("ready", "/health"),
        capacity=ComputeCapacity(max_concurrency=2, in_flight=0, busy=False),
        offerings=(
            _compute_offering("tts", "kokoro-82m"),
            _compute_offering("advanced_tts", "minicpm-o-4.5-zh-female"),
        ),
        resources={"speech_catalogue": "/v1/speech/catalogue"},
    )
    payload = {
        "format": "hashi-compute.speech-catalogue",
        "version": 1,
        "base_url": "http://192.168.1.50:19446/v1",
        "configuration_revision": 1,
        "models": [
            {
                "id": model,
                "voices": [
                    {
                        "id": "zf_001",
                        "label": "年轻4",
                        "gender": "female",
                        "language": "zh",
                        "language_name": "中文",
                        "note": "自然",
                        "callable": True,
                        "availability": "voice_available",
                        "auditions": [],
                    }
                ],
            }
            for model in ("kokoro-82m", "minicpm-o-4.5-zh-female")
        ],
    }

    class Client:
        async def fetch_json_resource(self, selected_node, path):
            assert selected_node == node
            assert path == "/v1/speech/catalogue"
            return payload

    class Directory:
        def __init__(self, _config, *, client):
            assert isinstance(client, Client)

        async def refresh(self):
            return type(
                "Refresh",
                (),
                {
                    "catalogue": type("Catalogue", (), {"snapshots": (snapshot,)})(),
                    "errors": {},
                },
            )()

    monkeypatch.setattr(voice_services_module, "ComputeServiceDirectory", Directory)
    catalogue = await discover_voice_services(
        tmp_path,
        client=Client(),
        platform_name="win32",
        built_in_voices={},
    )

    compute = [service for service in catalogue.services if service.service == "hashi_compute"]
    assert [service.label for service in compute] == [
        "Hashi Compute TTS",
        "Hashi Compute advanced TTS",
    ]
    assert [service.supports_style for service in compute] == [False, True]
    assert all(service.privacy_label == "Local private · LAN" for service in compute)
    assert all([voice.id for voice in service.voices] == ["zf_001"] for service in compute)
