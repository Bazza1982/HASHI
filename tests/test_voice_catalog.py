from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.voice_catalog import VoiceCatalog, VoiceCatalogError
from orchestrator.voice_synthesizer import VoiceAsset
from orchestrator.voice_manager import VoiceManager
import orchestrator.voice_manager as voice_manager_module


def _write_catalog(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _openai_compatible_catalog() -> dict:
    return {
        "version": 1,
        "services": [
            {
                "id": "lan-kokoro",
                "adapter": "openai_compatible",
                "label": "LAN Kokoro",
                "location": "lan",
                "base_url": "http://192.168.10.22:19446/v1",
                "model": "kokoro-82m",
                "audio_format": "wav",
                "style_support": "unsupported",
                "fallbacks": ["edge"],
            }
        ],
        "voices": [
            {
                "id": "lan_amy",
                "service": "lan-kokoro",
                "voice_name": "af_amy",
                "label": "Amy",
                "display_names": {"zh-CN": "艾米"},
                "language": "English",
                "language_labels": {
                    "en": "English (US)",
                    "zh-CN": "美式英语",
                },
                "locale": "en-US",
                "gender": "female",
                "provider_options": {"speed": 1.05},
            }
        ],
    }


def test_instance_catalog_adds_a_compatible_service_without_python_registration(
    tmp_path,
):
    path = tmp_path / "voice_catalog.json"
    _write_catalog(path, _openai_compatible_catalog())

    catalog = VoiceCatalog.load(
        instance_path=path,
        project_root=tmp_path,
        platform="win32",
    )

    assert "lan-kokoro" in catalog.service_ids
    assert catalog.service("lan-kokoro")["adapter"] == "openai_compatible"
    assert catalog.fallbacks("lan-kokoro") == ("edge",)
    assert catalog.presets["lan_amy"] == {
        "provider": "lan-kokoro",
        "voice_name": "af_amy",
        "label": "Amy",
        "display_names": {"zh-CN": "艾米"},
        "language": "English",
        "language_labels": {
            "en": "English (US)",
            "zh-CN": "美式英语",
        },
        "locale": "en-US",
        "gender": "female",
        "provider_options": {"speed": 1.05},
        "other_languages_unsupported": True,
    }


def test_published_instance_catalog_example_is_valid():
    root = Path(__file__).resolve().parents[1]

    catalog = VoiceCatalog.load(
        instance_path=root / "examples" / "voice_catalog.example.json",
        project_root=root,
        platform="win32",
    )

    assert {"lan-kokoro", "cloud-speech"} <= set(catalog.service_ids)
    assert {"lan_amy", "cloud_rachel"} <= set(catalog.presets)


def test_voice_manager_menu_and_selection_use_the_instance_catalog(tmp_path):
    path = tmp_path / "voice_catalog.json"
    _write_catalog(path, _openai_compatible_catalog())
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        catalog_path=path,
        project_root=tmp_path,
    )

    rows = {alias: availability for alias, _preset, availability in manager.get_voice_presets()}
    assert rows["lan_amy"] == "ready"
    assert "艾米（美式英语" in manager.voice_preset_menu_label(
        "lan_amy", locale="zh-CN"
    )

    manager.apply_voice_preset("lan_amy")
    state = manager.get_state()
    assert state["provider"] == "lan-kokoro"
    assert state["voice_name"] == "af_amy"
    assert state["provider_options"] == {"speed": 1.05}


def test_voice_manager_refreshes_a_valid_catalog_revision_without_reboot(tmp_path):
    path = tmp_path / "voice_catalog.json"
    payload = _openai_compatible_catalog()
    _write_catalog(path, payload)
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        catalog_path=path,
        project_root=tmp_path,
    )

    assert {alias for alias, _preset, _status in manager.get_voice_presets()} >= {
        "lan_amy"
    }
    payload["voices"].append(
        {
            **payload["voices"][0],
            "id": "lan_bella",
            "voice_name": "af_bella",
            "label": "Bella",
        }
    )
    _write_catalog(path, payload)

    assert {alias for alias, _preset, _status in manager.get_voice_presets()} >= {
        "lan_amy",
        "lan_bella",
    }


@pytest.mark.asyncio
async def test_shared_tts_uses_declared_same_node_fallback_and_can_lock_it(
    tmp_path, monkeypatch
):
    path = tmp_path / "voice_catalog.json"
    payload = _openai_compatible_catalog()
    payload["services"][0]["id"] = "compute-premium"
    payload["services"][0]["label"] = "Compute Premium"
    payload["services"][0]["model"] = "premium-voice"
    payload["services"][0]["fallbacks"] = ["compute-kokoro", "edge", "windows"]
    payload["services"].append(
        {
            **payload["services"][0],
            "id": "compute-kokoro",
            "label": "Compute Kokoro",
            "model": "kokoro-82m",
            "fallbacks": ["edge", "windows"],
        }
    )
    payload["voices"][0].update(
        {
            "id": "premium_one",
            "service": "compute-premium",
            "voice_name": "premium-1",
            "language": "Chinese",
            "locale": "zh-CN",
        }
    )
    payload["voices"].append(
        {
            **payload["voices"][0],
            "id": "kokoro_one",
            "service": "compute-kokoro",
            "voice_name": "zf_001",
        }
    )
    _write_catalog(path, payload)
    catalog = VoiceCatalog.load(
        instance_path=path, project_root=tmp_path, platform="win32"
    )
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        catalog_path=path,
        project_root=tmp_path,
    )
    built = []

    class Provider:
        def __init__(self, name):
            self.name = name

        async def synthesize(self, **kwargs):
            built.append(self.name)
            if self.name == "compute-premium":
                error = RuntimeError("premium backend unavailable")
                error.code = "tts_provider_unavailable"
                error.retryable = False
                raise error
            output = Path(kwargs["output_dir"]) / f"{self.name}.ogg"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"OggS-test")
            return VoiceAsset(
                provider=self.name,
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=output,
            )

    monkeypatch.setattr(
        voice_manager_module,
        "build_provider",
        lambda name, **_kwargs: Provider(name),
    )

    first = await manager.synthesize_configured(
        agent_name="call",
        request_id="segment-1",
        text="您好。",
        provider="compute-premium",
        voice_name="premium-1",
        catalog=catalog,
        max_retries=0,
        source_language="zh-CN",
        source_surface="call",
    )
    second = await manager.synthesize_configured(
        agent_name="call",
        request_id="segment-2",
        text="请讲。",
        provider=first.effective_provider,
        voice_name=first.effective_voice_name,
        provider_options=first.effective_provider_options,
        catalog=catalog,
        max_retries=0,
        source_language="zh-CN",
        source_surface="call",
    )

    assert built == ["compute-premium", "compute-kokoro", "compute-kokoro"]
    assert first.asset.fallback_from == "compute-premium"
    assert first.effective_provider == "compute-kokoro"
    assert second.effective_provider == "compute-kokoro"


@pytest.mark.asyncio
async def test_shared_tts_skips_cloud_candidates_outside_the_privacy_boundary(
    tmp_path, monkeypatch
):
    path = tmp_path / "voice_catalog.json"
    payload = _openai_compatible_catalog()
    service = payload["services"][0]
    service.update(
        {
            "id": "cloud-premium",
            "label": "Cloud Premium",
            "location": "cloud",
            "base_url": "https://speech.example/v1",
            "fallbacks": ["edge", "windows"],
        }
    )
    payload["voices"][0].update(
        {"id": "cloud_one", "service": "cloud-premium", "voice_name": "cloud-1"}
    )
    _write_catalog(path, payload)
    catalog = VoiceCatalog.load(
        instance_path=path, project_root=tmp_path, platform="win32"
    )
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        catalog_path=path,
        project_root=tmp_path,
    )
    built = []

    class Provider:
        async def synthesize(self, **kwargs):
            output = Path(kwargs["output_dir"]) / "windows.ogg"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"OggS-test")
            return VoiceAsset(
                provider="windows",
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=output,
            )

    def provider(name, **_kwargs):
        built.append(name)
        return Provider()

    monkeypatch.setattr(voice_manager_module, "build_provider", provider)
    result = await manager.synthesize_configured(
        agent_name="call",
        request_id="private-segment",
        text="隐私通话。",
        provider="cloud-premium",
        voice_name="cloud-1",
        catalog=catalog,
        max_retries=0,
        source_language="zh-CN",
        source_surface="call",
        provider_allowed=lambda _service_id, row: row["location"] != "cloud",
    )

    assert built == ["windows"]
    assert result.effective_provider == "windows"
    assert result.asset.fallback_reason_code == "tts_privacy_blocked"


def test_invalid_instance_catalog_fails_closed_without_rewriting_it(tmp_path):
    path = tmp_path / "voice_catalog.json"
    original = b'{"version":1,"services":['
    path.write_bytes(original)

    with pytest.raises(VoiceCatalogError, match="unreadable"):
        VoiceCatalog.load(instance_path=path, project_root=tmp_path)

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "mutation, error",
    [
        (lambda payload: payload.update({"unexpected": True}), "unsupported fields"),
        (
            lambda payload: payload["voices"][0].update({"id": "v" * 49}),
            "too long for menu callbacks",
        ),
    ],
)
def test_catalog_rejects_unsafe_schema_without_rewriting_it(
    tmp_path, mutation, error
):
    path = tmp_path / "voice_catalog.json"
    payload = _openai_compatible_catalog()
    mutation(payload)
    _write_catalog(path, payload)
    original = path.read_bytes()

    with pytest.raises(VoiceCatalogError, match=error):
        VoiceCatalog.load(instance_path=path, project_root=tmp_path)

    assert path.read_bytes() == original
