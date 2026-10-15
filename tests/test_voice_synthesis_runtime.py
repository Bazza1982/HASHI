from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from orchestrator import voice_synthesis_runtime
from orchestrator.tts_providers import edge
from orchestrator.tts_providers import windows


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


def test_resolve_tts_python_returns_none_when_instance_has_no_tts_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))
    monkeypatch.delenv("HASHI_TTS_PYTHON", raising=False)

    assert voice_synthesis_runtime.resolve_tts_python() is None
    assert voice_synthesis_runtime.isolated_tts_supports("kokoro") is False


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


def test_isolated_worker_error_keeps_stable_code_and_retry_policy():
    completed = subprocess.CompletedProcess(
        ["python", "worker.py"],
        1,
        stdout=voice_synthesis_runtime.TTS_RESULT_PREFIX
        + json.dumps(
            {
                "status": "error",
                "error_code": "tts_protocol_rejected",
                "error": "Edge TTS rejected the speech request (HTTP 403).",
                "retryable": False,
            }
        ),
        stderr="",
    )

    with pytest.raises(voice_synthesis_runtime.TTSRuntimeError) as raised:
        voice_synthesis_runtime._result_payload(completed)

    assert raised.value.code == "tts_protocol_rejected"
    assert raised.value.retryable is False
    assert "HTTP 403" in str(raised.value)


def test_isolated_worker_debug_log_removes_service_url_and_signature(
    tmp_path, monkeypatch, caplog
):
    helper = tmp_path / "isolated" / "python"
    helper.parent.mkdir()
    helper.write_bytes(b"python")
    output = tmp_path / "voice.mp3"
    monkeypatch.setattr(voice_synthesis_runtime, "resolve_tts_python", lambda: helper)

    def fake_run(command, **_kwargs):
        output.write_bytes(b"ID3" + b"a" * 128)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=voice_synthesis_runtime.TTS_RESULT_PREFIX
            + json.dumps({"status": "ok", "size_bytes": 131}),
            stderr=(
                "warning wss://speech.example.test/path?token=private "
                "sec-ms-gec=private-signature"
            ),
        )

    monkeypatch.setattr(voice_synthesis_runtime.subprocess, "run", fake_run)
    caplog.set_level(logging.DEBUG, logger="VoiceSynthesisRuntime")

    voice_synthesis_runtime.synthesize_edge_to_mp3(
        "private words",
        output_path=output,
        voice="en-US-EmmaNeural",
        rate="+0%",
    )

    assert "speech.example.test" not in caplog.text
    assert "private-signature" not in caplog.text
    assert "[service URL omitted]" in caplog.text


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


@pytest.mark.asyncio
async def test_windows_empty_audio_is_rejected_before_conversion(tmp_path, monkeypatch):
    import wave
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    # SAPI can report success for Chinese passed to an English-only voice.
    with wave.open(str(tmp_path / "reply.wav"), "wb") as audio:
        audio.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
    monkeypatch.setattr(windows.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    convert = AsyncMock()
    monkeypatch.setattr(windows, "convert_wav_to_ogg", convert)

    with pytest.raises(RuntimeError, match="empty audio"):
        await windows.WindowsSapiProvider().synthesize("中文回复", tmp_path, "reply")
    convert.assert_not_called()
    assert not (tmp_path / "reply.ogg").exists()
