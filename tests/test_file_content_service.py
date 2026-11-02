import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from orchestrator.file_content_service import FileContentService, FileContentError
from orchestrator.voice_transcription_result import (
    TranscriptionResult,
    TranscriptionSegment,
)


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_ocr_normalizes_supplied_bytes_without_publishing_local_paths():
    seen = []

    def ocr(data):
        seen.append(data)
        return SimpleNamespace(status="ok", text="synthetic image words")

    result = await FileContentService(ocr=ocr).process(
        image_bytes(), filename="image.png", operation="ocr"
    )
    assert result["segments"] == [
        {"text": "synthetic image words", "locator": {"kind": "image"}}
    ]
    assert seen[0].startswith(b"\xff\xd8")
    assert result["status"] == "ready"


@pytest.mark.asyncio
async def test_timed_transcription_uses_the_shared_owner_and_removes_temporary_input():
    paths = []

    class Transcriber:
        async def transcribe_result(self, filename, *, include_segments=False):
            paths.append(Path(filename))
            assert paths[-1].read_bytes() == b"audio bytes"
            assert include_segments
            return TranscriptionResult(
                text="target phrase",
                segments=(TranscriptionSegment(222.0, 225.5, "target phrase"),),
            )

    service = FileContentService(
        transcriber=Transcriber(), configuration=lambda: {"provider": "local"}
    )
    result = await service.process(
        b"audio bytes", filename="recording.wav", operation="transcribe"
    )
    assert result["segments"][0]["locator"] == {
        "kind": "time",
        "start": 222.0,
        "end": 225.5,
    }
    assert not paths[0].exists()


@pytest.mark.asyncio
async def test_configured_service_requires_an_explicit_opt_in_before_any_media_leaves_the_local_processor():
    calls = []

    class Transcriber:
        async def transcribe_result(self, *args, **kwargs):
            calls.append(args)
            return TranscriptionResult(text="untimed service text")

    service = FileContentService(
        transcriber=Transcriber(), configuration=lambda: {"provider": "compute"}
    )
    with pytest.raises(FileContentError, match="opt_in"):
        await service.process(
            b"audio", filename="recording.wav", operation="transcribe"
        )
    assert calls == []
    result = await service.process(
        b"audio", filename="recording.wav", operation="transcribe", allow_compute=True
    )
    assert result["status"] == "partial"
    assert result["error_code"] == "fileshare_timestamps_unavailable"
    assert result["segments"][0]["locator"] == {"kind": "text"}


@pytest.mark.asyncio
async def test_cancelling_native_processing_cleans_up_without_publishing_a_result():
    paths = []
    started = asyncio.Event()

    class Transcriber:
        async def transcribe_result(self, filename, **kwargs):
            paths.append(Path(filename))
            started.set()
            await asyncio.Event().wait()

    service = FileContentService(
        transcriber=Transcriber(), configuration=lambda: {"provider": "local"}
    )
    task = asyncio.create_task(
        service.process(b"audio", filename="recording.wav", operation="transcribe")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not paths[0].exists()


@pytest.mark.asyncio
async def test_file_processing_api_requires_authority_and_returns_safe_results(
    tmp_path, monkeypatch
):
    from tests.test_session_api import _server, _Request

    server, _ = _server(tmp_path)
    server._file_content_service = FileContentService(
        ocr=lambda _: SimpleNamespace(status="ok", text="word")
    )
    headers = {
        "Content-Type": "application/octet-stream",
        "X-File-Name": "test.png",
        "X-File-Operation": "ocr",
    }
    result = await server.handle_v1_file_content(
        _Request(headers=headers, body=image_bytes())
    )
    assert result.status == 200 and b"word" in result.body
    monkeypatch.setattr(server, "_check_admin_auth", lambda _: False)
    rejected = await server.handle_v1_file_content(
        _Request(headers=headers, body=image_bytes())
    )
    assert rejected.status == 403
