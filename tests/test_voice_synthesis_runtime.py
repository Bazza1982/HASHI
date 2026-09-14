from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator import voice_synthesis_runtime
from orchestrator.tts_providers import edge


def test_resolve_tts_python_uses_instance_platform_config(tmp_path, monkeypatch):
    helper = tmp_path / "tts-runtime" / "bin" / "python"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"python")
    config = tmp_path / "state" / "platform" / "tts.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"schema_version": 1, "python": str(helper)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))
    monkeypatch.delenv("HASHI_TTS_PYTHON", raising=False)

    assert voice_synthesis_runtime.resolve_tts_python() == helper


def test_resolve_tts_python_rejects_active_runtime(monkeypatch):
    monkeypatch.setenv("HASHI_TTS_PYTHON", str(Path(voice_synthesis_runtime.sys.executable)))

    with pytest.raises(voice_synthesis_runtime.TTSRuntimeError, match="isolated"):
        voice_synthesis_runtime.resolve_tts_python()


def test_synthesize_edge_to_mp3_uses_isolated_worker_stdin(tmp_path, monkeypatch):
    helper = tmp_path / "isolated" / "python"
    helper.parent.mkdir()
    helper.write_bytes(b"python")
    output = tmp_path / "voice.mp3"
    captured = {}

    monkeypatch.setattr(voice_synthesis_runtime, "resolve_tts_python", lambda: helper)

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        Path(command[command.index("--output") + 1]).write_bytes(b"ID3" + b"a" * 128)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=voice_synthesis_runtime.TTS_RESULT_PREFIX
            + json.dumps({"status": "ok", "size_bytes": 131}),
            stderr="",
        )

    monkeypatch.setattr(voice_synthesis_runtime.subprocess, "run", fake_run)

    voice_synthesis_runtime.synthesize_edge_to_mp3(
        "private words",
        output_path=output,
        voice="en-US-EmmaNeural",
        rate="+10%",
    )

    assert output.is_file()
    assert captured["input"] == "private words"
    assert "private words" not in captured["command"]
    assert captured["command"][0] == str(helper)
    assert "-I" in captured["command"]


@pytest.mark.asyncio
async def test_edge_provider_prefers_isolated_runtime(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(edge, "resolve_tts_python", lambda: tmp_path / "helper-python")

    def synthesize(text, *, output_path, voice, rate):
        calls.update(text=text, voice=voice, rate=rate)
        output_path.write_bytes(b"ID3" + b"a" * 128)

    async def convert(_ffmpeg, _input, output):
        output.write_bytes(b"OggS-test")

    monkeypatch.setattr(edge, "synthesize_edge_to_mp3", synthesize)
    monkeypatch.setattr(edge, "convert_audio_to_ogg", convert)
    provider = edge.EdgeTTSProvider(ffmpeg_cmd="ffmpeg")

    asset = await provider.synthesize(
        "Read this",
        tmp_path,
        "reply",
        voice_name="en-US-EmmaNeural",
        rate=1,
    )

    assert asset.ogg_path.read_bytes().startswith(b"OggS")
    assert calls == {
        "text": "Read this",
        "voice": "en-US-EmmaNeural",
        "rate": "+10%",
    }
