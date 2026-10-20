from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.voice_catalog import VoiceCatalog, VoiceCatalogError
from orchestrator.voice_manager import VoiceManager


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
