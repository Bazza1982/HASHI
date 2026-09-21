from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from orchestrator import voice_transcriber, voice_transcription_worker


def test_isolated_worker_probe_uses_its_required_distribution_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module_name in ("av", "ctranslate2", "faster_whisper"):
        monkeypatch.setitem(sys.modules, module_name, ModuleType(module_name))
    versions = {
        "faster-whisper": "1.2.1",
        "ctranslate2": "4.7.1",
        "av": "17.0.0",
    }
    monkeypatch.setattr(
        voice_transcription_worker.importlib.metadata,
        "version",
        versions.__getitem__,
    )

    assert voice_transcription_worker.probe_runtime()["packages"] == versions


def test_isolated_worker_protocol_uses_utf8_bytes_independent_of_console_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "语音.wav"
    audio.write_bytes(b"audio")
    request = {
        "version": 1,
        "id": "request-utf8",
        "audio_path": str(audio),
        "model_size": "small",
        "language": None,
    }
    input_bytes = io.BytesIO(
        (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
    )
    output_bytes = io.BytesIO()
    console_stdin = io.TextIOWrapper(input_bytes, encoding="cp1252")
    console_stdout = io.TextIOWrapper(output_bytes, encoding="cp1252")
    calls: list[Path] = []

    class _Runtime:
        def transcribe(
            self,
            audio_path: Path,
            *,
            model_size: str,
            language: str | None,
        ) -> dict[str, object]:
            calls.append(audio_path)
            assert model_size == "small"
            assert language is None
            return {
                "text": "中文转写成功",
                "device": "cpu",
                "compute_type": "int8",
                "language": "zh",
                "language_probability": 1.0,
                "duration": 1.0,
            }

    monkeypatch.setattr(voice_transcription_worker, "_ModelRuntime", _Runtime)
    monkeypatch.setattr(voice_transcription_worker.sys, "stdin", console_stdin)
    monkeypatch.setattr(voice_transcription_worker.sys, "stdout", console_stdout)

    assert voice_transcription_worker._serve() == 0

    record = output_bytes.getvalue().decode("utf-8").strip()
    assert record.startswith(voice_transcription_worker.RESULT_PREFIX)
    payload = json.loads(record.removeprefix(voice_transcription_worker.RESULT_PREFIX))
    assert payload["ok"] is True
    assert payload["text"] == "中文转写成功"
    assert calls == [audio.resolve()]


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = list(lines)

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class _FakeProcess:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.returncode = None

    def terminate(self) -> None:
        self.returncode = 1

    async def wait(self) -> int:
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_platform_transcription_runtime_keeps_native_dependencies_out_of_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_home = tmp_path / "instance"
    config_path = bridge_home / "state" / "platform" / "transcription.json"
    config_path.parent.mkdir(parents=True)
    external_python = tmp_path / "transcription runtime" / "python"
    config_path.write_text(
        json.dumps({"python": str(external_python)}),
        encoding="utf-8",
    )
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setenv("BRIDGE_HOME", str(bridge_home))
    monkeypatch.delenv("HASHI_TRANSCRIPTION_PYTHON", raising=False)

    result_line = (
        "HASHI_VOICE_TRANSCRIPTION_RESULT="
        + json.dumps(
            {
                "version": 1,
                "id": "1",
                "ok": True,
                "text": "isolated transcript",
                "device": "cpu",
                "compute_type": "int8",
                "language": "en",
                "language_probability": 0.99,
                "duration": 1.25,
            }
        )
        + "\n"
    ).encode()
    process = _FakeProcess([result_line])
    spawn_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def create_subprocess_exec(*args, **kwargs):
        spawn_calls.append((args, kwargs))
        return process

    monkeypatch.setattr(
        voice_transcriber.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )
    transcriber = voice_transcriber.VoiceTranscriber()
    monkeypatch.setattr(
        transcriber,
        "_load_model",
        lambda: pytest.fail("native model must stay outside the Function Worker"),
    )

    result = await transcriber.transcribe(audio)

    assert result == "isolated transcript"
    assert len(spawn_calls) == 1
    command = spawn_calls[0][0]
    assert command[0] == str(external_python)
    assert command[1] == "-I"
    assert Path(str(command[2])).name == "voice_transcription_worker.py"
    assert command[3] == "--serve"
    request = json.loads(process.stdin.writes[0])
    assert request == {
        "version": 1,
        "id": "1",
        "audio_path": str(audio.resolve()),
        "model_size": "small",
        "language": None,
    }
    assert transcriber._model is None
    await transcriber.aclose()


def test_transcription_python_environment_override_precedes_instance_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_home = tmp_path / "instance"
    config_path = bridge_home / "state" / "platform" / "transcription.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"python": "configured-python"}), encoding="utf-8")
    monkeypatch.setenv("BRIDGE_HOME", str(bridge_home))
    monkeypatch.setenv("HASHI_TRANSCRIPTION_PYTHON", "environment-python")

    assert voice_transcriber._external_python() == "environment-python"


def test_no_external_runtime_preserves_direct_transcription_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))
    monkeypatch.delenv("HASHI_TRANSCRIPTION_PYTHON", raising=False)

    assert voice_transcriber._external_python() == ""


def test_isolated_worker_returns_bounded_typed_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    runtime = voice_transcription_worker._ModelRuntime()
    calls = []

    class _Model:
        def transcribe(self, path, **kwargs):
            calls.append((path, kwargs))
            return (
                [SimpleNamespace(text="  hello "), SimpleNamespace(text="world\x00 ")],
                SimpleNamespace(
                    language="en",
                    language_probability=0.98,
                    duration=2.5,
                ),
            )

    monkeypatch.setattr(
        runtime,
        "_model",
        lambda _model_size, _language: (_Model(), "cpu", "int8"),
    )

    result = runtime.transcribe(audio, model_size="small", language=None)

    assert result == {
        "text": "hello world",
        "device": "cpu",
        "compute_type": "int8",
        "language": "en",
        "language_probability": 0.98,
        "duration": 2.5,
    }
    assert calls == [
        (
            str(audio),
            {"language": None, "beam_size": 5, "vad_filter": True},
        )
    ]


def test_isolated_worker_rejects_missing_audio_before_model_load(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="audio file is unavailable"):
        voice_transcription_worker._request_payload(
            json.dumps(
                {
                    "version": 1,
                    "id": "request-1",
                    "audio_path": str(tmp_path / "missing.wav"),
                    "model_size": "small",
                    "language": None,
                }
            )
        )
