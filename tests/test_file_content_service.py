import asyncio
import base64
import io
import json
from contextlib import asynccontextmanager
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
async def test_ocr_processing_failure_keeps_the_native_cause_in_diagnostics(caplog):
    service = FileContentService(
        ocr=lambda _: SimpleNamespace(
            status="error",
            engine="paddleocr",
            error="isolated helper did not return a result",
        )
    )
    with caplog.at_level("ERROR", logger="BridgeU.FileContent"):
        with pytest.raises(FileContentError, match="fileshare_ocr_error"):
            await service.process(image_bytes(), filename="test.png", operation="ocr")
    assert "isolated helper did not return a result" in caplog.text


def content_body(*, metadata=None, payload=None, extra=False):
    from aiohttp.payload import BytesPayload

    value = (
        metadata
        if metadata is not None
        else {
            "filename": "テスト.png",
            "operation": "ocr",
            "allow_compute": False,
        }
    )
    if isinstance(value, dict):
        value = dict(value)
        value.setdefault(
            "payload",
            base64.b64encode(payload if payload is not None else image_bytes()).decode(
                "ascii"
            ),
        )
        if extra:
            value["unexpected"] = "not accepted"
    return BytesPayload(
        json.dumps(value).encode("utf-8"), content_type="application/json"
    )


@asynccontextmanager
async def content_api(tmp_path, monkeypatch, service):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from tests.test_session_api import _server

    server, runtime = _server(tmp_path)
    server._file_content_service = service
    monkeypatch.setattr(server, "_v1_owner_id", lambda _: "synthetic-owner")
    monkeypatch.setattr(
        server,
        "_check_admin_auth",
        lambda request: request.headers.get("X-Workbench-Token") == "synthetic-admin",
    )
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/api/v1/file-content", server.handle_v1_file_content)
    async with TestClient(TestServer(app)) as client:
        yield client, runtime


@pytest.mark.asyncio
async def test_file_processing_api_requires_authority_and_reads_real_signed_json(
    tmp_path, monkeypatch
):
    service = FileContentService(
        ocr=lambda _: SimpleNamespace(status="ok", text="word")
    )
    async with content_api(tmp_path, monkeypatch, service) as (client, runtime):
        response = await client.post(
            "/api/v1/file-content",
            data=content_body(),
            headers={"X-Workbench-Token": "synthetic-admin"},
        )
        assert response.status == 200
        assert (await response.json())["segments"][0]["text"] == "word"
        response = await client.post("/api/v1/file-content", data=content_body())
        assert response.status == 403
        assert runtime.enqueue_request_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata,extra",
    [
        ([], False),
        ({"filename": "test.png", "operation": "ocr", "allow_compute": "false"}, False),
        (
            {
                "filename": "test.png",
                "operation": "ocr",
                "allow_compute": False,
                "unexpected": 1,
            },
            False,
        ),
        (
            {
                "filename": "test.png",
                "operation": "ocr",
                "allow_compute": False,
                "payload": "%%%",
            },
            False,
        ),
        (
            {
                "filename": "test.png",
                "operation": "ocr",
                "allow_compute": False,
                "payload": "eB==",
            },
            False,
        ),
        (None, True),
    ],
)
async def test_signed_content_metadata_and_fields_are_strict(
    tmp_path, monkeypatch, metadata, extra
):
    service = FileContentService(
        ocr=lambda _: pytest.fail("invalid input must not run OCR")
    )
    async with content_api(tmp_path, monkeypatch, service) as (client, _):
        response = await client.post(
            "/api/v1/file-content",
            data=content_body(metadata=metadata, extra=extra),
            headers={"X-Workbench-Token": "synthetic-admin"},
        )
        assert response.status == 400
        assert (await response.json())["error_code"] == "fileshare_content_invalid"


@pytest.mark.asyncio
async def test_signed_content_limits_and_retired_header_protocol_do_not_start_processing(
    tmp_path, monkeypatch
):
    service = FileContentService(
        ocr=lambda _: pytest.fail("invalid input must not run OCR")
    )
    service.MAX_BYTES = 16
    async with content_api(tmp_path, monkeypatch, service) as (client, _):
        for form in [
            content_body(payload=b"x" * 17),
            content_body(metadata={"filename": "x" * 4100}),
        ]:
            response = await client.post(
                "/api/v1/file-content",
                data=form,
                headers={"X-Workbench-Token": "synthetic-admin"},
            )
            assert response.status == 413
        response = await client.post(
            "/api/v1/file-content",
            data=b"input",
            headers={
                "X-Workbench-Token": "synthetic-admin",
                "Content-Type": "application/octet-stream",
                "X-File-Operation": "ocr",
                "X-File-Name": "test.png",
            },
        )
        assert response.status == 415


@pytest.mark.asyncio
async def test_body_admission_is_bounded_before_reading_and_cancellation_releases_it():
    entered, release = asyncio.Event(), asyncio.Event()
    reads = []

    class Request:
        content_type = "application/json"

        async def read(self):
            reads.append(True)
            if len(reads) == 2:
                entered.set()
            await release.wait()
            raise AssertionError("cancelled reads must not reach parsing")

    service = FileContentService()
    pending = [
        asyncio.create_task(service.process_request(Request())) for _ in range(2)
    ]
    await entered.wait()
    try:
        with pytest.raises(FileContentError, match="fileshare_content_busy"):
            await service.process_request(Request())
        assert len(reads) == 2
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    assert service.receiving == 0 and service.active == 0


@pytest.mark.asyncio
async def test_only_body_bound_compute_opt_in_can_authorize_the_configured_service(
    tmp_path, monkeypatch
):
    calls = []

    class Transcriber:
        async def transcribe_result(self, *args, **kwargs):
            calls.append(args)
            return TranscriptionResult(text="synthetic service words")

    service = FileContentService(
        transcriber=Transcriber(), configuration=lambda: {"provider": "compute"}
    )
    async with content_api(tmp_path, monkeypatch, service) as (client, runtime):
        metadata = {
            "filename": "test.wav",
            "operation": "transcribe",
            "allow_compute": False,
        }
        response = await client.post(
            "/api/v1/file-content",
            data=content_body(metadata=metadata, payload=b"audio"),
            headers={
                "X-Workbench-Token": "synthetic-admin",
                "X-Allow-Configured-Compute": "1",
            },
        )
        assert response.status == 403
        assert not calls
        metadata["allow_compute"] = True
        response = await client.post(
            "/api/v1/file-content",
            data=content_body(metadata=metadata, payload=b"audio"),
            headers={
                "X-Workbench-Token": "synthetic-admin",
                "X-Allow-Configured-Compute": "0",
            },
        )
        assert response.status == 200
        assert len(calls) == 1 and runtime.enqueue_request_calls == 0
