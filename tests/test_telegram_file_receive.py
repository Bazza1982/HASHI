"""User files reach a real Session/Run independently of format recognition."""
import errno
from dataclasses import replace
import hashlib
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import FileSizeLimit
from telegram.error import BadRequest

from orchestrator import runtime_long, runtime_media, runtime_session, session_store
from orchestrator.frontend_failures import frontend_failure
from tests.test_runtime_media import _runtime, _update
from tests.test_session_attachment_authorization import fixture


def _zip_bytes():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("example.txt", "received intact")
    return data.getvalue()


def _intake_runtime(tmp_path, payload):
    manager, backend, store, session, _, _, _ = fixture(
        tmp_path, agent="zelda", owner="user:1"
    )
    runtime = _runtime(tmp_path)
    runtime.session_store = store
    runtime.global_config = SimpleNamespace(authorized_id=1, ui_language="zh-CN")
    runtime.backend_manager = manager
    runtime._request_meta_by_id = {}
    manager.runtime = runtime
    backend.tool_registry = SimpleNamespace(is_allowed=lambda name: True)
    received = []

    async def download(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    async def enqueue(chat_id, prompt, source, summary, **kwargs):
        content = kwargs["request_content"]
        route, accepted, owner, _, _ = runtime_session.accept_request(
            runtime, request_id="req-file-intake", chat_id=chat_id,
            prompt=prompt, source=source, request_metadata=kwargs["request_metadata"],
            request_content=content, idempotency_key="file-intake",
        )
        fence = store.mark_request_running(accepted.request_id, worker_id="file-worker")
        runtime._request_meta_by_id[accepted.request_id] = {
            "request_id": accepted.request_id, "hashi_session_id": route["session_id"],
            "hashi_run_id": accepted.run_id, "hashi_message_id": accepted.message_id,
            "hashi_fencing_token": fence, "owner_id": owner,
            "context_generation": route["context_generation"],
        }
        response = await manager.generate_response(
            prompt, accepted.request_id, request_content=content
        )
        received.append((response, content, accepted))
        return accepted.request_id

    runtime.app.bot.file.download_to_drive = download
    runtime.enqueue_request = enqueue
    return runtime, received, store, session


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,mime,payload,batch", [
    ("archive.zip", "application/zip", _zip_bytes(), False),
    ("notes.txt", "text/plain", b"ordinary text", False),
    ("unrecognised", "application/x-user-format", b"\x00\xffopaque\x00bytes", False),
    ("empty", "", b"", False),
    ("picture.heic", "image/heic", b"opaque image format", False),
    ("archive.zip", "application/zip", _zip_bytes(), True),
], ids=["zip", "text", "unknown", "empty", "heic", "zip-batch"])
async def test_any_file_reaches_bound_message_and_backend(tmp_path, filename, mime, payload, batch):
    runtime, received, store, session = _intake_runtime(tmp_path, payload)
    update = _update(
        update_id=31, message_id=41, caption="keep this file",
        document=SimpleNamespace(
            file_id="incoming-file", file_name=filename, mime_type=mime,
            file_size=len(payload),
        ),
    )
    if batch:
        runtime_long.begin_batch(runtime, 123)
    await runtime_media.handle_document(runtime, update, SimpleNamespace())
    if batch:
        await runtime_long.cmd_end(runtime, update, SimpleNamespace())
        if runtime._long_finalize_task:
            await runtime._long_finalize_task
    assert received, runtime.error_logger.messages
    response, content, accepted = received[0]
    assert response.is_success, (response.error_code, response.error)
    part = next(p for p in content["parts"] if p["type"] == "media")
    assert Path(part["local_ref"]).read_bytes() == payload
    assert part["filename"] == filename
    assert part["size_bytes"] == len(payload)
    assert part["sha256"] == hashlib.sha256(payload).hexdigest()
    message = store.get_message(
        accepted.message_id, owner_id=session["owner_id"], session_id=session["session_id"]
    )
    assert part["attachment_id"] in {
        p.get("attachment_id") for p in message["content"]
    }
    assert not list(tmp_path.rglob("example.txt"))  # ZIP is saved, never extracted.


def test_store_file_limit_exposes_safe_size_facts(tmp_path, monkeypatch):
    store = session_store.SessionStore(tmp_path / "limits.sqlite3", instance_id="NP")
    session = store.ensure_default_session(owner_id="user:1", agent_id="qa")
    monkeypatch.setattr(session_store, "MAX_SESSION_ATTACHMENT_BYTES", 4)
    accepted = store.stage_attachment(
        session_id=session["session_id"], owner_id=session["owner_id"],
        filename="exact.zip", media_type="application/zip", size_bytes=4,
        sha256=hashlib.sha256(b"1234").hexdigest(),
    )
    store.upload_attachment_bytes(
        session_id=session["session_id"], owner_id=session["owner_id"],
        attachment_id=accepted["attachment_id"], payload=b"1234",
    )
    store.commit_attachment(
        session_id=session["session_id"], owner_id=session["owner_id"],
        attachment_id=accepted["attachment_id"],
    )
    with pytest.raises(ValueError) as caught:
        store.stage_attachment(
            session_id=session["session_id"], owner_id=session["owner_id"],
            filename="private-name.zip", media_type="application/zip", size_bytes=5,
            sha256=hashlib.sha256(b"12345").hexdigest(),
        )
    failure = frontend_failure(caught.value, phase="store", submission_state="not_submitted")
    assert failure.reason == "attachment_too_large"
    assert failure.diagnostic()["actual_bytes"] == 5
    assert failure.diagnostic()["maximum_bytes"] == 4
    for locale in ("en", "zh-CN"):
        assert "5" in failure.render(locale=locale) and "4" in failure.render(locale=locale)
        assert "private-name" not in failure.render(locale=locale)


@pytest.mark.asyncio
async def test_telegram_oversize_is_explained_before_download(tmp_path):
    runtime = _runtime(tmp_path)
    maximum = int(FileSizeLimit.FILESIZE_DOWNLOAD)
    update = _update(
        document=SimpleNamespace(file_name="large.zip", file_id="large",
                                 mime_type="application/zip", file_size=maximum + 1),
        caption="",
    )
    await runtime_media.handle_document(runtime, update, SimpleNamespace())
    assert runtime.app.bot.get_file_calls == 0
    assert not runtime.enqueued
    text = runtime.replies[-1]["text"]
    assert "Telegram" in text and str(maximum) in text and str(maximum + 1) in text


@pytest.mark.asyncio
async def test_telegram_cloud_rejection_is_not_a_network_error(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.app.bot.get_file = AsyncMock(side_effect=BadRequest("File is too big"))
    update = _update(
        document=SimpleNamespace(file_name="large.zip", file_id="large",
                                 mime_type="application/zip"),
        caption="",
    )
    await runtime_media.handle_document(runtime, update, SimpleNamespace())
    assert not runtime.enqueued
    assert runtime.app.bot.get_file.await_count == 1
    text = runtime.replies[-1]["text"]
    assert "Telegram" in text and str(int(FileSizeLimit.FILESIZE_DOWNLOAD)) in text
    assert "无法连接" not in text and "无法确认附件的实际格式" not in text


@pytest.mark.asyncio
async def test_session_storage_disk_full_reports_actual_cause(tmp_path, monkeypatch):
    payload = b"%PDF-1.4\n"
    runtime, received, store, _ = _intake_runtime(tmp_path, payload)
    original = Path.write_bytes

    def write(path, payload):
        if path.parent == store.attachment_files_root and path.suffix == ".partial":
            raise OSError(errno.ENOSPC, "private-disk-path")
        return original(path, payload)

    monkeypatch.setattr(Path, "write_bytes", write)
    update = _update(
        document=SimpleNamespace(file_name="file.pdf", file_id="file",
                                 mime_type="application/pdf", file_size=len(payload)),
        caption="",
    )
    await runtime_media.handle_document(runtime, update, SimpleNamespace())
    assert not received
    text = runtime.replies[-1]["text"]
    assert "磁盘" in text or "disk" in text.lower()
    assert "保存附件" in text or "Save attachment" in text
    assert "private" not in text and "fcerr_" in text
# HASHI-Fingerprint: hfp_b3ff38e882b949bab34094baf61169b0


@pytest.mark.asyncio
async def test_actual_download_size_is_checked_when_metadata_is_missing(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        runtime_media, "SESSION_ATTACHMENT_LIMITS",
        replace(runtime_media.SESSION_ATTACHMENT_LIMITS, attachment_bytes=4),
    )
    update = _update(
        document=SimpleNamespace(file_name="file.bin", file_id="file",
                                 mime_type="application/octet-stream"),
        caption="",
    )
    await runtime_media.handle_document(runtime, update, SimpleNamespace())
    assert not runtime.enqueued
    assert not runtime.app.bot.file.downloaded_to.exists()
    facts = __import__("json").loads(runtime.error_logger.messages[-1][1].split(": ", 1)[1])
    assert facts["reason"] == "attachment_too_large"
    assert facts["actual_bytes"] == 5 and facts["maximum_bytes"] == 4


@pytest.mark.asyncio
async def test_local_bot_mode_retains_instance_budget_without_cloud_ceiling(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    cloud_maximum = int(FileSizeLimit.FILESIZE_DOWNLOAD)
    maximum = cloud_maximum + 2
    runtime.app.bot.local_mode = True
    runtime.app.bot.file.file_size = cloud_maximum + 1
    monkeypatch.setattr(
        runtime_media, "SESSION_ATTACHMENT_LIMITS",
        replace(runtime_media.SESSION_ATTACHMENT_LIMITS, attachment_bytes=maximum),
    )
    saved = await runtime_media.download_media(runtime, "file", "local.bin")
    assert saved.read_bytes() == b"media"
    runtime.app.bot.file.file_size = maximum + 1
    with pytest.raises(ValueError) as caught:
        await runtime_media.download_media(runtime, "other-file", "other.bin")
    failure = frontend_failure(caught.value, phase="download", submission_state="not_submitted")
    assert failure.reason == "attachment_too_large"
    assert failure.diagnostic()["maximum_bytes"] == maximum


def test_total_size_failure_retains_rollback_and_byte_facts(tmp_path, monkeypatch):
    store = session_store.SessionStore(tmp_path / "total.sqlite3", instance_id="NP")
    session = store.ensure_default_session(owner_id="user:1", agent_id="qa")
    scope = {"session_id": session["session_id"], "owner_id": session["owner_id"]}
    content = []
    for filename in ("one.zip", "two.unknown"):
        attachment = store.stage_attachment(
            **scope, filename=filename, media_type="application/octet-stream",
            size_bytes=4, sha256=hashlib.sha256(b"1234").hexdigest(),
        )
        store.upload_attachment_bytes(**scope, attachment_id=attachment["attachment_id"], payload=b"1234")
        store.commit_attachment(**scope, attachment_id=attachment["attachment_id"])
        content.append({"type": "attachment", "attachment_id": attachment["attachment_id"]})
    monkeypatch.setattr(session_store, "MAX_SESSION_ATTACHMENT_TOTAL_BYTES", 7)
    with pytest.raises(session_store.SessionConflict) as caught:
        store.accept_run(
            **scope, agent_id="qa", request_id="req-over-total", text="files",
            source="telegram", idempotency_key="over-total", content=content,
        )
    failure = frontend_failure(caught.value, phase="admit", submission_state="unconfirmed")
    assert failure.reason == "attachments_too_large"
    assert failure.diagnostic()["actual_bytes"] == 8
    assert failure.diagnostic()["maximum_bytes"] == 7
    with pytest.raises(session_store.SessionNotFound):
        store.get_run_by_request("req-over-total")


@pytest.mark.asyncio
async def test_invalid_telegram_file_request_is_not_reported_as_network_loss(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.app.bot.get_file = AsyncMock(side_effect=BadRequest("Wrong file identifier: private-token"))
    update = _update(
        document=SimpleNamespace(file_name="file.zip", file_id="file"),
        caption="",
    )
    await runtime_media.handle_document(runtime, update, SimpleNamespace())
    assert not runtime.enqueued and runtime.app.bot.get_file.await_count == 1
    facts = __import__("json").loads(runtime.error_logger.messages[-1][1].split(": ", 1)[1])
    assert facts["reason"] == "request_rejected"
    assert "private-token" not in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_document_reception_is_independent_of_model_format_support(tmp_path, monkeypatch):
    runtime, received, _, _ = _intake_runtime(tmp_path, _zip_bytes())
    monkeypatch.setattr(
        runtime_media, "_backend_media_acceptance",
        lambda *args: runtime_media.MediaBridgeAcceptance(
            False, code="MODEL_MODALITY_UNSUPPORTED", reason="model_modality_unsupported"
        ),
    )
    update = _update(
        document=SimpleNamespace(file_name="archive.zip", file_id="file",
                                 mime_type="application/zip"),
        caption="",
    )
    await runtime_media.handle_document(runtime, update, SimpleNamespace())
    assert received and received[0][0].is_success, runtime.error_logger.messages
