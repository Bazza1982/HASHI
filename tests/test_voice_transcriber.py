from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import voice_transcriber


def _fake_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"isolated-python")
    return path


def test_transcription_runtime_prefers_environment_override(tmp_path, monkeypatch):
    isolated_python = _fake_python(tmp_path / "speech runtime" / "python")
    monkeypatch.setenv("HASHI_TRANSCRIPTION_PYTHON", str(isolated_python))
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path / "instance"))

    resolved = voice_transcriber.resolve_transcription_python()

    assert resolved == isolated_python.resolve()


def test_transcription_runtime_uses_instance_platform_config(tmp_path, monkeypatch):
    isolated_python = _fake_python(tmp_path / "speech runtime" / "python")
    config = tmp_path / "state" / "platform" / "transcription.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"schema_version": 1, "python": str(isolated_python)}),
        encoding="utf-8",
    )
    monkeypatch.delenv("HASHI_TRANSCRIPTION_PYTHON", raising=False)
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))

    resolved = voice_transcriber.resolve_transcription_python()

    assert resolved == isolated_python.resolve()


def test_transcription_runtime_refuses_the_active_hashi_interpreter(monkeypatch):
    monkeypatch.setenv("HASHI_TRANSCRIPTION_PYTHON", sys.executable)

    with pytest.raises(voice_transcriber.TranscriptionRuntimeError, match="isolated"):
        voice_transcriber.resolve_transcription_python()


@pytest.mark.asyncio
async def test_transcription_runs_only_in_the_isolated_runtime(tmp_path, monkeypatch):
    isolated_python = _fake_python(tmp_path / "speech runtime" / "python")
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")
    captured: dict[str, object] = {}
    payload = {
        "status": "ok",
        "text": "hello from speech",
        "device": "cpu",
        "compute_type": "int8",
    }

    monkeypatch.setattr(
        voice_transcriber,
        "resolve_transcription_python",
        lambda: isolated_python,
    )

    def run(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "worker log\n"
                + voice_transcriber.TRANSCRIPTION_RESULT_PREFIX
                + json.dumps(payload)
                + "\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(voice_transcriber.subprocess, "run", run)
    transcriber = voice_transcriber.VoiceTranscriber(
        model_size="small",
        language="en",
    )

    result = await transcriber.transcribe(audio)

    assert result == "hello from speech"
    command = captured["command"]
    assert command[0] == str(isolated_python)
    assert command[1] == "-I"
    assert command[2].endswith("voice_transcription_worker.py")
    assert command[command.index("--audio") + 1] == str(audio.resolve())
    assert command[command.index("--model-size") + 1] == "small"
    assert command[command.index("--language") + 1] == "en"
    assert captured["kwargs"]["check"] is False
    assert transcriber._device == "cpu"
    assert transcriber._compute_type == "int8"


@pytest.mark.asyncio
async def test_missing_isolated_runtime_fails_closed(tmp_path, monkeypatch):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(voice_transcriber, "resolve_transcription_python", lambda: None)

    result = await voice_transcriber.VoiceTranscriber().transcribe(audio)

    assert result == (
        "[Transcription error] Isolated transcription runtime is unavailable"
    )


def test_voice_worker_is_part_of_the_function_generation_manifest():
    from orchestrator.function_generation import build_source_manifest

    root = Path(__file__).resolve().parents[1]
    manifest = build_source_manifest(
        ("orchestrator.voice_transcriber",),
        code_root=root,
    )

    assert "orchestrator.voice_transcription_worker" in manifest.module_names
