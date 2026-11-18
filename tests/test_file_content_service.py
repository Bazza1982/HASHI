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
async def test_configured_service_uses_selected_route_unless_local_only_is_explicit():
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
            b"audio", filename="recording.wav", operation="transcribe", allow_compute=False
        )
    assert calls == []
    result = await service.process(
        b"audio", filename="recording.wav", operation="transcribe"
    )
    assert len(calls) == 1
    assert result["status"] == "partial"
    assert result["error_code"] == "fileshare_timestamps_unavailable"
    assert result["segments"][0]["locator"] == {"kind": "text"}


@pytest.mark.asyncio
async def test_local_only_is_checked_again_at_actual_transcriber_dispatch(tmp_path, monkeypatch):
    from orchestrator import voice_transcriber

    calls = []
    transcriber = voice_transcriber.VoiceTranscriber()
    async def must_not_send(*args, **kwargs):
        calls.append(args)
        pytest.fail("local-only input must not reach Compute")
    monkeypatch.setattr(transcriber, "_transcribe_compute", must_not_send)
    monkeypatch.setattr(voice_transcriber, "_transcription_configuration",
                        lambda: (tmp_path, {"provider": "compute"}))
    # The configured route changes after FileContentService read its local snapshot.
    service = FileContentService(transcriber=transcriber, configuration=lambda: {"provider": "local"})
    with pytest.raises(FileContentError, match="TRANSCRIPTION_ACCESS_DENIED"):
        await service.process(b"audio", filename="recording.wav", operation="transcribe",
                              allow_compute=False)
    assert calls == []


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
    app.router.add_get("/api/v1/file-content/capabilities", server.handle_v1_file_content_capabilities)
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
async def test_body_admission_queues_and_cancels_before_reading():
    entered, release = asyncio.Event(), asyncio.Event()
    reads = []

    class Request:
        content_type = "application/json"

        async def read(self):
            reads.append(True)
            if len(reads) == 2:
                entered.set()
            await release.wait()
            return json.dumps({"filename": "x.wav", "operation": "transcribe",
                               "payload": "eA=="}).encode()

    service = FileContentService(capacity={"queue_limit": 1})
    async def process(payload, **metadata):
        return {"payload": payload, "metadata": metadata}
    service.process = process
    first = [asyncio.create_task(service.process_request(Request())) for _ in range(2)]
    await entered.wait()
    third = asyncio.create_task(service.process_request(Request()))
    await asyncio.sleep(0)
    assert service.receiving == 2 and service.receiving_waiters == 1 and len(reads) == 2
    with pytest.raises(FileContentError, match="fileshare_content_busy"):
        await service.process_request(Request())
    third.cancel()
    with pytest.raises(asyncio.CancelledError):
        await third
    assert service.receiving_waiters == 0 and len(reads) == 2
    queued = asyncio.create_task(service.process_request(Request()))
    await asyncio.sleep(0)
    assert service.receiving_waiters == 1 and len(reads) == 2
    release.set()
    results = await asyncio.gather(*first, queued)
    assert [row["payload"] for row in results] == [b"x"] * 3
    assert len(reads) == 3
    assert service.receiving == service.active == service.receiving_waiters == 0


@pytest.mark.asyncio
async def test_configured_large_input_is_accepted_by_real_backend_api(tmp_path, monkeypatch):
    import hashlib
    size = 33 * 1024 * 1024
    payload = b"x" * size
    expected = hashlib.sha256(payload).hexdigest()
    seen = []

    class Transcriber:
        async def transcribe_result(self, filename, **kwargs):
            digest = hashlib.sha256()
            with Path(filename).open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            seen.append((Path(filename).stat().st_size, digest.hexdigest()))
            return TranscriptionResult(text="large synthetic media")

    service = FileContentService(
        transcriber=Transcriber(), configuration=lambda: {"provider": "local"},
        capacity={"max_input_bytes": 34 * 1024 * 1024},
    )
    async with content_api(tmp_path, monkeypatch, service) as (client, _):
        headers = {"X-Workbench-Token": "synthetic-admin"}
        response = await client.get("/api/v1/file-content/capabilities", headers=headers)
        capabilities = await response.json()
        assert capabilities["max_bytes"] == 34 * 1024 * 1024
        assert capabilities["instance_id"]
        response = await client.post(
            "/api/v1/file-content",
            data=content_body(metadata={"filename": "large.wav", "operation": "transcribe"},
                              payload=payload),
            headers=headers,
        )
        assert response.status == 200
        assert seen == [(size, expected)]
        assert (await response.json())["status"] == "partial"


@pytest.mark.asyncio
async def test_processing_queue_is_cancellable_and_next_work_runs_after_release():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Transcriber:
        async def transcribe_result(self, filename, **kwargs):
            calls.append(Path(filename).read_bytes())
            if len(calls) == 2:
                entered.set()
            await release.wait()
            return TranscriptionResult(text="finished")

    service = FileContentService(
        transcriber=Transcriber(), configuration=lambda: {"provider": "local"},
        capacity={"processing_concurrency": 2, "queue_limit": 1},
    )
    async def process():
        return await service.process(b"audio", filename="x.wav", operation="transcribe")
    first = [asyncio.create_task(process()) for _ in range(2)]
    await entered.wait()
    third = asyncio.create_task(process())
    await asyncio.sleep(0)
    assert service.active == 2 and service.processing_waiters == 1 and len(calls) == 2
    with pytest.raises(FileContentError, match="fileshare_content_busy"):
        await process()
    third.cancel()
    with pytest.raises(asyncio.CancelledError):
        await third
    queued = asyncio.create_task(process())
    await asyncio.sleep(0)
    assert service.processing_waiters == 1
    release.set()
    assert len(await asyncio.gather(*first, queued)) == 3
    assert len(calls) == 3
    assert service.active == service.processing_waiters == 0


def test_capacity_is_configurable_and_rejects_wire_overcommit(monkeypatch):
    from orchestrator.file_content_service import CONTENT_WIRE_MAX_INPUT_BYTES
    monkeypatch.setenv("HASHI_FILE_CONTENT_MAX_INPUT_BYTES", str(40 * 1024 * 1024))
    service = FileContentService(capacity={"processing_concurrency": 3, "queue_limit": 2})
    advertised = service.capabilities()
    assert advertised["max_bytes"] == 40 * 1024 * 1024
    assert advertised["capacity"]["processing_concurrency"] == 3
    assert advertised["capacity"]["queue_limit"] == 2
    with pytest.raises(ValueError, match="transport"):
        FileContentService(capacity={"max_input_bytes": CONTENT_WIRE_MAX_INPUT_BYTES + 1})


@pytest.mark.asyncio
async def test_ocr_and_request_timeouts_follow_explicit_capacity(monkeypatch):
    from tools import ocr as ocr_owner

    seen = []
    monkeypatch.setenv("HASHI_FILE_CONTENT_OCR_TIMEOUT_SECONDS", "125")
    monkeypatch.setenv("HASHI_FILE_CONTENT_REQUEST_TIMEOUT_SECONDS", "350")
    monkeypatch.setattr(
        ocr_owner, "extract_image_bytes",
        lambda data, *, timeout_seconds: (
            seen.append(timeout_seconds) or SimpleNamespace(status="ok", text="word")
        ),
    )
    service = FileContentService()
    assert service.capabilities()["capacity"]["ocr_timeout_seconds"] == 125
    assert service.capabilities()["capacity"]["request_timeout_seconds"] == 350
    result = await service.process(image_bytes(), filename="image.png", operation="ocr")
    assert result["status"] == "ready"
    assert seen == [125]
    monkeypatch.setenv("HASHI_FILE_CONTENT_REQUEST_TIMEOUT_SECONDS", "invalid")
    with pytest.raises(ValueError, match="timeout"):
        service.capabilities()


@pytest.mark.asyncio
async def test_capabilities_and_dispatch_honor_selected_compute_audio_budget():
    calls = []
    class Transcriber:
        async def transcribe_result(self, *args, **kwargs):
            calls.append(True)
            return TranscriptionResult(text="ok")
    service = FileContentService(
        transcriber=Transcriber(),
        configuration=lambda: {"provider": "compute", "max_compute_input_bytes": 1024},
        capacity={"max_input_bytes": 2048},
    )
    assert service.capabilities()["transcription"]["max_bytes"] == 1024
    with pytest.raises(FileContentError, match="fileshare_content_limit"):
        await service.process(b"x" * 1025, filename="x.wav", operation="transcribe")
    assert calls == []
    assert (await service.process(b"x" * 1024, filename="x.wav",
                                  operation="transcribe"))["status"] == "partial"
    assert calls == [True]


@pytest.mark.asyncio
async def test_large_valid_image_is_downscaled_and_real_decode_budget_is_honored():
    source = io.BytesIO()
    Image.new("RGB", (5000, 4100), "white").save(source, format="JPEG")
    seen = []
    def ocr(data):
        with Image.open(io.BytesIO(data)) as image:
            seen.append(image.size)
        return SimpleNamespace(status="ok", text="large photo")
    service = FileContentService(ocr=ocr)
    result = await service.process(source.getvalue(), filename="large.jpg", operation="ocr")
    assert result["segments"][0]["text"] == "large photo"
    assert max(seen[0]) <= 2400
    restricted = FileContentService(ocr=lambda _: pytest.fail("over budget must not decode"),
                                    capacity={"max_decoded_bytes": 1024})
    with pytest.raises(FileContentError, match="fileshare_content_limit"):
        await restricted.process(source.getvalue(), filename="large.jpg", operation="ocr")


@pytest.mark.asyncio
async def test_body_local_only_is_respected_and_selected_route_needs_no_second_grant(
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
        metadata.pop("allow_compute")
        response = await client.post(
            "/api/v1/file-content",
            data=content_body(metadata=metadata, payload=b"audio"),
            headers={"X-Workbench-Token": "synthetic-admin"},
        )
        assert response.status == 200
        assert len(calls) == 1
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
        assert len(calls) == 2 and runtime.enqueue_request_calls == 0
