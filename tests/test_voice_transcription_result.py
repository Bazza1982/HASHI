from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import voice_transcriber
from orchestrator.voice_transcription_result import (
    TranscriptionFailed, TranscriptionFailure,
    transcribe_audio, transcription_failure,
)


@pytest.mark.parametrize("field,value", [
    ("code", ["private-secret"]), ("phase", {}), ("dependency", ["private-secret"]),
    ("reference", "private-secret"), ("error_type", "private secret"),
])
def test_worker_failure_payload_is_strict_and_never_copies_private_fields(field, value):
    original = transcription_failure(ImportError(name="faster_whisper"), phase="model")
    payload = {**original.as_dict(), field: value, "error": "private-secret"}
    assert TranscriptionFailure.from_dict(payload) is None
    good = TranscriptionFailure.from_dict({**original.as_dict(), "error": "private-secret"})
    assert good == original and "private-secret" not in str(good.as_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code", [
    (ImportError("private-library-path", name="faster_whisper"), "TRANSCRIPTION_DEPENDENCY_MISSING"),
    (PermissionError("private-path"), "TRANSCRIPTION_ACCESS_DENIED"),
    (OSError("private-model-path"), "TRANSCRIPTION_MODEL_LOAD_FAILED"),
    (TimeoutError("private-service-url"), "TRANSCRIPTION_TIMEOUT"),
])
async def test_model_errors_are_typed_safe_and_legacy_text_compatible(tmp_path, monkeypatch, error, code):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(voice_transcriber, "_transcription_configuration", lambda: (tmp_path, {}))
    monkeypatch.setattr(voice_transcriber, "_external_python", lambda: "")
    transcriber = voice_transcriber.VoiceTranscriber()
    def load():
        raise error
    monkeypatch.setattr(transcriber, "_load_model", load)
    monkeypatch.setattr(transcriber, "_transcribe_sync", lambda _: pytest.fail("Failed load cannot transcribe"))
    result = await transcribe_audio(transcriber, audio)
    assert not result.ok and result.failure.code == code
    assert "private" not in result.legacy_text()
    assert result.failure.reference.startswith("stterr_")
    assert result.attachment_result("att-test")["failure"] == result.failure.as_dict()
    legacy = await transcriber.transcribe(audio)
    assert legacy.startswith("[Transcription error]") and code in legacy and "private" not in legacy


@pytest.mark.asyncio
async def test_empty_audio_text_and_literal_error_prefix_are_different_typed_results(tmp_path, monkeypatch):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(voice_transcriber, "_transcription_configuration", lambda: (tmp_path, {}))
    monkeypatch.setattr(voice_transcriber, "_external_python", lambda: "")
    transcriber = voice_transcriber.VoiceTranscriber()
    transcriber._model = object()
    monkeypatch.setattr(transcriber, "_transcribe_sync", lambda _: " \n ")
    empty = await transcribe_audio(transcriber, audio)
    assert not empty.ok and empty.failure.code == "TRANSCRIPTION_EMPTY"
    literal = "[Transcription error] is the literal phrase I said"
    monkeypatch.setattr(transcriber, "_transcribe_sync", lambda _: literal)
    result = await transcribe_audio(transcriber, audio)
    assert result.ok and result.text == literal and result.failure is None


@pytest.mark.asyncio
async def test_simultaneous_calls_keep_distinct_outcomes_without_shared_last_error(tmp_path, monkeypatch):
    for name in ("ok.wav", "failed.wav"):
        (tmp_path / name).write_bytes(b"audio")
    monkeypatch.setattr(voice_transcriber, "_transcription_configuration",
                        lambda: (tmp_path, {"provider": "compute"}))
    transcriber = voice_transcriber.VoiceTranscriber()
    async def compute(audio, *args):
        await asyncio.sleep(0)
        if audio.name == "failed.wav":
            raise RuntimeError("private-provider-body")
        return "已转写"
    monkeypatch.setattr(transcriber, "_transcribe_compute", compute)
    monkeypatch.setattr(transcriber, "_load_model", lambda: pytest.fail("No silent local fallback"))
    success, failed = await asyncio.gather(
        transcribe_audio(transcriber, tmp_path / "ok.wav"),
        transcribe_audio(transcriber, tmp_path / "failed.wav"))
    assert success.ok and success.text == "已转写" and success.failure is None
    assert failed.failure.code == "TRANSCRIPTION_SERVICE_UNAVAILABLE"
    assert "private" not in failed.legacy_text()


@pytest.mark.asyncio
async def test_embedded_legacy_transcriber_is_supported_without_echoing_error_text():
    success = await transcribe_audio(SimpleNamespace(transcribe=AsyncMock(return_value="中文")), Path("ignored"))
    assert success.ok and success.text == "中文"
    failure = await transcribe_audio(SimpleNamespace(transcribe=AsyncMock(
        return_value="[Transcription error] private-secret")), Path("ignored"))
    assert failure.failure.code == "TRANSCRIPTION_FAILED"
    assert "private" not in failure.legacy_text()


@pytest.mark.asyncio
async def test_isolated_worker_failure_keeps_the_original_typed_reference(tmp_path, monkeypatch):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(voice_transcriber, "_transcription_configuration", lambda: (tmp_path, {}))
    monkeypatch.setattr(voice_transcriber, "_external_python", lambda: "selected-python")
    transcriber = voice_transcriber.VoiceTranscriber()
    failure = transcription_failure(ImportError(name="av"), phase="model")
    process = SimpleNamespace(stdin=SimpleNamespace(write=lambda _: None, drain=AsyncMock()))
    monkeypatch.setattr(transcriber, "_ensure_external_process", AsyncMock(return_value=process))
    monkeypatch.setattr(transcriber, "_read_external_result", AsyncMock(
        return_value={"ok": False, "failure": failure.as_dict(), "error": "private-worker-body"}))
    result = await transcribe_audio(transcriber, audio)
    assert result.failure == failure
    assert "private" not in str(TranscriptionFailed(result.failure))


@pytest.mark.asyncio
async def test_cancelled_external_exchange_is_closed_before_another_recording_reuses_it(tmp_path, monkeypatch):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    transcriber = voice_transcriber.VoiceTranscriber()
    process = SimpleNamespace(stdin=SimpleNamespace(write=lambda _: None, drain=AsyncMock()))
    entered = asyncio.Event()
    async def pending(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(transcriber, "_ensure_external_process", AsyncMock(return_value=process))
    monkeypatch.setattr(transcriber, "_read_external_result", pending)
    close = AsyncMock()
    monkeypatch.setattr(transcriber, "aclose", close)
    task = asyncio.create_task(transcriber._transcribe_external(audio, "selected-python"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    close.assert_awaited_once()
    assert not transcriber._lock.locked()
