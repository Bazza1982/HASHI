from types import SimpleNamespace
import json

import pytest
from orchestrator import voice_transcriber, voice_transcription_worker
from tests.test_voice_transcriber import _FakeProcess


@pytest.mark.asyncio
async def test_public_transcriber_preserves_real_segment_times_without_changing_legacy_text(
    tmp_path, monkeypatch
):
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(
        voice_transcriber,
        "_transcription_configuration",
        lambda: (tmp_path, {"provider": "local"}),
    )
    monkeypatch.setattr(voice_transcriber, "_external_python", lambda: "")
    rows = [
        SimpleNamespace(start=1.25, end=2.5, text="first"),
        SimpleNamespace(start=222.0, end=225.5, text="target phrase"),
    ]
    info = SimpleNamespace(language="en", language_probability=1.0, duration=226.0)
    transcriber = voice_transcriber.VoiceTranscriber()
    transcriber._model = SimpleNamespace(transcribe=lambda *a, **kw: (iter(rows), info))
    result = await transcriber.transcribe_result(audio, include_segments=True)
    assert result.ok
    assert result.text == "first target phrase"
    assert result.segments[1].start == 222.0
    assert result.segments[1].end == 225.5
    assert result.segments[1].text == "target phrase"
    assert await transcriber.transcribe(audio) == "first target phrase"


def test_isolated_model_and_wire_request_retain_timing_only_when_requested(
    tmp_path, monkeypatch
):
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"fixture")
    runtime = voice_transcription_worker._ModelRuntime()
    model = SimpleNamespace(
        transcribe=lambda *a, **kw: (
            iter([SimpleNamespace(start=3.0, end=4.0, text="sentence")]),
            SimpleNamespace(language="en", language_probability=1.0, duration=4.0),
        )
    )
    monkeypatch.setattr(runtime, "_model", lambda *a: (model, "cpu", "int8"))
    result = runtime.transcribe(
        audio, model_size="small", language=None, include_segments=True
    )
    assert result["segments"] == [{"start": 3.0, "end": 4.0, "text": "sentence"}]
    assert "segments" not in runtime.transcribe(
        audio, model_size="small", language=None
    )
    request = {
        "version": 1,
        "id": "test",
        "audio_path": str(audio),
        "model_size": "small",
        "language": None,
        "include_segments": True,
    }
    assert (
        voice_transcription_worker._request_payload(request)["include_segments"] is True
    )
    with pytest.raises(ValueError):
        voice_transcription_worker._request_payload(
            {**request, "include_segments": "yes"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segments",
    [
        [{"start": -1, "end": 2, "text": "bad"}],
        [{"start": 2, "end": 1, "text": "bad"}],
        [{"start": "1", "end": 2, "text": "bad"}],
    ],
)
async def test_malformed_isolated_timing_is_not_published_as_a_seek_point(
    tmp_path, monkeypatch, segments
):
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(
        voice_transcriber,
        "_transcription_configuration",
        lambda: (tmp_path, {"provider": "local"}),
    )
    monkeypatch.setattr(
        voice_transcriber, "_external_python", lambda: "isolated-python"
    )
    transcriber = voice_transcriber.VoiceTranscriber()
    payload = {
        "version": 1,
        "id": "1",
        "ok": True,
        "text": "bad timing",
        "segments": segments,
    }
    process = _FakeProcess(
        [
            (
                voice_transcription_worker.RESULT_PREFIX + json.dumps(payload) + "\n"
            ).encode()
        ]
    )

    async def ready(_):
        return process

    monkeypatch.setattr(transcriber, "_ensure_external_process", ready)
    result = await transcriber.transcribe_result(audio, include_segments=True)
    assert not result.ok
    assert result.failure.code == "TRANSCRIPTION_PROTOCOL_ERROR"
    assert result.segments == ()


@pytest.mark.asyncio
async def test_configured_compute_text_remains_searchable_without_inventing_timestamps(
    tmp_path, monkeypatch
):
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(
        voice_transcriber,
        "_transcription_configuration",
        lambda: (tmp_path, {"provider": "compute"}),
    )
    transcriber = voice_transcriber.VoiceTranscriber()

    async def compute(*_):
        return "actual service transcript"

    monkeypatch.setattr(transcriber, "_transcribe_compute", compute)
    result = await transcriber.transcribe_result(audio, include_segments=True)
    assert result.ok and result.text == "actual service transcript"
    assert result.segments == ()
