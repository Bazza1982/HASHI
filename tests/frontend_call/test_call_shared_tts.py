import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.frontend_call.config import CallConfig
from orchestrator.frontend_call.service import Call, CallService
from orchestrator.frontend_call.settings import CallSettings
from orchestrator.frontend_call.tts import CatalogCallTTS
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_synthesizer import VoiceAsset
import orchestrator.voice_manager as voice_manager_module


def _voice_catalog():
    return {
        "version": 1,
        "services": [
            {
                "id": "compute-premium",
                "adapter": "openai_compatible",
                "label": "Compute Premium",
                "menu_label": "Premium",
                "location": "lan",
                "base_url": "http://192.168.10.22:19446/v1",
                "model": "premium-voice",
                "audio_format": "wav",
                "style_support": "supported",
                "fallbacks": ["compute-kokoro", "edge", "windows"],
            },
            {
                "id": "compute-kokoro",
                "adapter": "openai_compatible",
                "label": "Compute Kokoro",
                "menu_label": "Kokoro",
                "location": "lan",
                "base_url": "http://192.168.10.22:19446/v1",
                "model": "kokoro-82m",
                "audio_format": "wav",
                "style_support": "unsupported",
                "fallbacks": ["edge", "windows"],
            },
        ],
        "voices": [
            {
                "id": "premium_one",
                "service": "compute-premium",
                "voice_name": "premium-1",
                "label": "Premium One",
                "language": "Chinese",
                "locale": "zh-CN",
                "gender": "female",
            },
            {
                "id": "kokoro_one",
                "service": "compute-kokoro",
                "voice_name": "zf_001",
                "label": "Kokoro One",
                "language": "Chinese",
                "locale": "zh-CN",
                "gender": "female",
            },
        ],
    }


def _call_document():
    common = {
        "adapter": "openai_compatible",
        "location": "local",
        "base_url": "http://127.0.0.1:19000/v1",
        "model": "fixture-model",
    }
    return {
        "version": 1,
        "enabled": True,
        "targets": [
            {**common, "id": "ears", "kind": "stt"},
            {**common, "id": "eyes", "kind": "vision"},
        ],
        "default_profile": {
            "stt": {"target_id": "ears", "options": {}},
            "tts": {
                "target_id": "compute-premium",
                "voice_id": "premium_one",
                "options": {},
            },
            "vision": {"target_id": "eyes", "options": {}},
        },
        "profiles": {},
    }


def test_agent_default_voice_is_scoped_and_owner_selection_still_wins(tmp_path):
    config = _shared_config(tmp_path)
    doc = json.loads(config.path.read_text(encoding="utf-8"))
    chosen = {**doc["default_profile"], "tts": {
        "target_id": "compute-kokoro", "voice_id": "kokoro_one", "options": {}}}
    doc["agent_defaults"] = {"character": chosen}
    config.path.write_text(json.dumps(doc), encoding="utf-8")
    context = config.context("owner-a", "character")
    assert context["profile"]["tts"]["voice_id"] == "kokoro_one"
    assert config.context("owner-b", "character")["profile"] == context["profile"]
    assert config.context("owner-a", "other")["profile"]["tts"]["voice_id"] == "premium_one"
    override = {**context["profile"], "tts": doc["default_profile"]["tts"]}
    config.save("owner-a", "character", context["revision"], override)
    assert config.context("owner-a", "character")["profile"]["tts"]["voice_id"] == "premium_one"
    assert config.context("owner-b", "character")["profile"]["tts"]["voice_id"] == "kokoro_one"


def test_call_derives_tts_services_and_voices_from_the_shared_catalog(tmp_path):
    call_path = tmp_path / "call_profiles.json"
    catalog_path = tmp_path / "voice_catalog.json"
    call_path.write_text(json.dumps(_call_document()), encoding="utf-8")
    catalog_path.write_text(json.dumps(_voice_catalog()), encoding="utf-8")
    config = CallConfig(
        call_path,
        voice_catalog_path=catalog_path,
        project_root=tmp_path,
        platform="win32",
    )

    first = config.context("owner", "agent")
    tts = {row["id"]: row for row in first["targets"] if row["kind"] == "tts"}
    assert tts["compute-premium"]["voices"] == ["premium_one"]
    assert tts["compute-kokoro"]["voices"] == ["kokoro_one"]
    assert tts["compute-premium"]["fallbacks"][:2] == [
        "compute-kokoro",
        "edge",
    ]

    payload = _voice_catalog()
    payload["voices"].append(
        {
            **payload["voices"][0],
            "id": "premium_two",
            "voice_name": "premium-2",
            "label": "Premium Two",
        }
    )
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
    second = config.context("owner", "agent")

    assert second["revision"] != first["revision"]
    refreshed = {row["id"]: row for row in second["targets"] if row["kind"] == "tts"}
    assert refreshed["compute-premium"]["voices"] == [
        "premium_one",
        "premium_two",
    ]


def _shared_config(tmp_path, *, catalog_payload=None):
    call_path = tmp_path / "call_profiles.json"
    catalog_path = tmp_path / "voice_catalog.json"
    call_path.write_text(json.dumps(_call_document()), encoding="utf-8")
    catalog_path.write_text(
        json.dumps(catalog_payload or _voice_catalog()), encoding="utf-8"
    )
    return CallConfig(
        call_path,
        voice_catalog_path=catalog_path,
        project_root=tmp_path,
        platform="win32",
    )


@pytest.mark.asyncio
async def test_call_uses_shared_fallback_and_locks_it_for_later_segments(
    tmp_path, monkeypatch
):
    config = _shared_config(tmp_path)
    context = config.context("owner", "agent")
    profile, targets, catalog = config.freeze_runtime(
        "owner", "agent", context["revision"]
    )
    manager = VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        catalog_path=tmp_path / "voice_catalog.json",
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
            output.write_bytes(b"OggS-call-test")
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
    synthesizer = CatalogCallTTS(
        manager,
        temp_root=tmp_path / "call-media",
    )
    call = Call(
        owner="owner",
        binding={"call_id": "call-1", "agent_id": "agent"},
        profile=profile,
        targets=targets,
        fingerprint="fingerprint",
        expires=100,
        started=0,
        locale="zh-CN",
        tts_catalog=catalog,
    )

    first = await synthesizer.synthesize(
        call, "您好。", privacy_level=0, request_id="segment-1"
    )
    second = await synthesizer.synthesize(
        call, "请讲。", privacy_level=0, request_id="segment-2"
    )

    assert first["_tts_route"]["degraded"] is True
    assert first["_billing_target"]["base_url"] == "http://192.168.10.22:19446/v1"
    assert second["_tts_route"]["service_id"] == "compute-kokoro"
    assert built == ["compute-premium", "compute-kokoro", "compute-kokoro"]
    assert CallService._tts_view(call) == {
        "selected_service_id": "compute-premium",
        "selected_service_label": "Compute Premium",
        "effective_service_id": "compute-kokoro",
        "effective_service_label": "Compute Kokoro",
        "degraded": True,
        "reason_code": "tts_provider_unavailable",
        "error_code": None,
    }


def test_call_voice_menu_pages_every_shared_catalog_voice(tmp_path):
    payload = _voice_catalog()
    template = payload["voices"][0]
    payload["voices"] = [
        {
            **template,
            "id": f"premium_{index:02d}",
            "voice_name": f"premium-{index:02d}",
            "label": f"Premium {index:02d}",
        }
        for index in range(25)
    ] + [payload["voices"][1]]
    document = _call_document()
    document["default_profile"]["tts"]["voice_id"] = "premium_00"
    (tmp_path / "call_profiles.json").write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / "voice_catalog.json").write_text(json.dumps(payload), encoding="utf-8")
    settings = CallSettings.__new__(CallSettings)
    settings.runtime = SimpleNamespace()
    settings.owner, settings.agent = "owner", "agent"
    settings.config = CallConfig(
        tmp_path / "call_profiles.json",
        voice_catalog_path=tmp_path / "voice_catalog.json",
        project_root=tmp_path,
        platform="win32",
    )

    callbacks = []
    for page in ("voice", "voice.1", "voice.2"):
        _text, keyboard = settings.render(page)
        callbacks.extend(
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data.startswith("call:voice:")
        )

    assert sorted(int(value.split(":")[2]) for value in callbacks) == list(range(25))
    assert all(len(value.encode()) <= 64 for value in callbacks)
