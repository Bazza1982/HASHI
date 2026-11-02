from __future__ import annotations

import json
from argparse import Namespace
from types import SimpleNamespace

import pytest

from orchestrator.tts_routing import resolve_tts_route
from orchestrator.tts_voice_previews import get_tts_voice_preview_assets
from tools import generate_tts_voice_previews as generator


def test_language_default_keeps_an_explicit_native_voice_and_its_options():
    presets = {
        "default_zh": {
            "provider": "advanced-example", "voice_name": "zf_default",
            "language": "zh", "provider_options": {},
        },
        "selected_zh": {
            "provider": "advanced-example", "voice_name": "zf_selected",
            "language": "zh", "provider_options": {"speed": 1.1},
        },
    }
    route = resolve_tts_route(
        provider="advanced-example", voice_name="zf_selected",
        provider_options={"speed": 1.1}, requested_style=None,
        text="您好，请用我选择的声音朗读。", source_language="zh-CN",
        presets=presets, profiles={},
        provider_profiles={"advanced-example": {
            "style_support": "unsupported", "fallbacks": (),
            "language_voices": {"zh": "default_zh"},
        }},
    )
    assert route.voice_name == "zf_selected"
    assert route.provider_options == {"speed": 1.1}
    assert route.adapted_from_voice is None
    assert route.adaptation_reason is None


@pytest.mark.asyncio
async def test_generator_uses_instance_service_and_resumes_only_exact_previews(tmp_path, monkeypatch):
    home = tmp_path / "instance"
    home.mkdir()
    (home / "voice_catalog.json").write_text(json.dumps({
        "version": 1,
        "services": [{
            "id": "advanced-example", "adapter": "openai_compatible",
            "label": "Example advanced TTS", "location": "local",
            "base_url": "http://127.0.0.1:12345/v1",
            "model": "example-advanced", "audio_format": "wav",
            "fallbacks": [],
        }],
        "voices": [{
            "id": "example_selected", "service": "advanced-example",
            "voice_name": "zf_selected", "label": "Selected voice",
            "language": "zh", "locale": "zh-CN", "gender": "female",
        }],
    }), encoding="utf-8")
    calls = []

    class Provider:
        async def synthesize(self, **kwargs):
            calls.append(kwargs)
            path = kwargs["output_dir"] / "actual.ogg"
            path.write_bytes(b"OggS-provider-fixture")
            return SimpleNamespace(ogg_path=path)

    def build(name, **kwargs):
        assert name == "advanced-example"
        assert kwargs["provider_config"]["adapter"] == "openai_compatible"
        assert kwargs["provider_config"]["model"] == "example-advanced"
        assert kwargs["bridge_home"] == home
        return Provider()

    async def transcode(_ffmpeg, _source, destination):
        destination.write_bytes(b"ID3-browser-fixture")

    monkeypatch.setenv("BRIDGE_HOME", str(home))
    monkeypatch.setattr(generator, "configured_media_executable", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(generator, "build_provider", build)
    monkeypatch.setattr(generator, "_transcode_mp3", transcode)
    args = Namespace(bridge_home=home, output=tmp_path / "bundle",
                     aliases=["example_selected"], force=False)
    manifest_path = await generator.generate(args)
    assert len(calls) == 1 and calls[0]["voice_name"] == "zf_selected"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {entry["renderer"] for entry in manifest["entries"]} == {"tts", "tts_mp3"}
    assert all(entry["provider"] == "advanced-example" and
               entry["voice"] == "zf_selected" for entry in manifest["entries"])
    preset = {"provider": "advanced-example", "voice_name": "zf_selected", "locale": "zh-CN"}
    assert get_tts_voice_preview_assets("example_selected", preset, telegram=False, root=args.output)
    assert get_tts_voice_preview_assets("example_selected", preset, telegram=True, root=args.output)

    await generator.generate(args)
    assert len(calls) == 1

    # The files still validate, but a changed provider/voice must not be reused.
    manifest["entries"][0]["voice"] = "zf_other"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    await generator.generate(args)
    assert len(calls) == 2
    assert get_tts_voice_preview_assets("example_selected", preset, telegram=True, root=args.output)
