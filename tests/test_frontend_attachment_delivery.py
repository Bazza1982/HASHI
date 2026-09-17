from __future__ import annotations

import hashlib
import io
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from orchestrator.frontend_attachment_delivery import (
    send_telegram_run_attachments,
)
from orchestrator.session_store import SessionStore


class _Logger:
    def __init__(self):
        self.messages: list[str] = []

    def warning(self, message, *args):
        self.messages.append(message % args if args else message)


class _Bot:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_photo(self, **kwargs):
        self.calls.append(
            {**kwargs, "photo": kwargs["photo"].read(), "method": "send_photo"}
        )
        return SimpleNamespace(message_id=701)

    async def send_document(self, **kwargs):
        self.calls.append(
            {
                **kwargs,
                "document": kwargs["document"].read(),
                "method": "send_document",
            }
        )
        return SimpleNamespace(message_id=702)

    async def send_audio(self, **kwargs):
        self.calls.append(
            {**kwargs, "audio": kwargs["audio"].read(), "method": "send_audio"}
        )
        return SimpleNamespace(message_id=703)


class _FailingBot:
    def __init__(self):
        self.calls = 0

    async def send_document(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("deterministic transport rejection")


def _running_store(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    owner_id = "user:7"
    agent_id = "zelda"
    session = store.ensure_default_session(owner_id=owner_id, agent_id=agent_id)
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner_id,
        agent_id=agent_id,
        request_id="managed-output",
        text="create files",
        source="workbench_ui_chat",
        idempotency_key="managed-output",
    )
    store.mark_request_running(accepted.request_id, worker_id="HASHI3:zelda")
    return store, owner_id, agent_id, session, accepted


def _bind_file(
    store,
    *,
    session_id,
    owner_id,
    agent_id,
    request_id,
    filename,
    media_type,
    payload,
    index,
):
    staged = store.stage_attachment(
        session_id=session_id,
        owner_id=owner_id,
        filename=filename,
        media_type=media_type,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        semantic_role=("audio_attachment" if media_type.startswith("audio/") else ""),
    )
    store.upload_attachment_bytes(
        session_id=session_id,
        owner_id=owner_id,
        attachment_id=staged["attachment_id"],
        payload=payload,
        audio_direction="output",
    )
    store.commit_attachment(
        session_id=session_id,
        owner_id=owner_id,
        attachment_id=staged["attachment_id"],
    )
    store.bind_run_output_attachments(
        request_id=request_id,
        session_id=session_id,
        owner_id=owner_id,
        agent_id=agent_id,
        idempotency_key=f"group-{index}",
        request_digest=hashlib.sha256(payload).hexdigest(),
        attachments=[
            {
                "attachment_id": staged["attachment_id"],
                "caption": f"caption-{index}",
            }
        ],
    )
    return staged["attachment_id"]


def _runtime(store, bot):
    return SimpleNamespace(
        name="zelda",
        session_store=store,
        app=SimpleNamespace(bot=bot),
        logger=_Logger(),
    )


def _item(accepted, owner_id):
    return SimpleNamespace(
        request_id=accepted.request_id,
        run_id=accepted.run_id,
        owner_id=owner_id,
        chat_id=77,
    )


@pytest.mark.asyncio
async def test_telegram_projects_managed_output_bytes_and_returns_receipts(tmp_path):
    store, owner, agent, session, accepted = _running_store(tmp_path)
    image = b"\x89PNG\r\n\x1a\nmanaged-image"
    document = b"managed-document"
    image_id = _bind_file(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=agent,
        request_id=accepted.request_id,
        filename="proof.png",
        media_type="image/png",
        payload=image,
        index=1,
    )
    document_id = _bind_file(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=agent,
        request_id=accepted.request_id,
        filename="report.txt",
        media_type="text/plain",
        payload=document,
        index=2,
    )
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="Files attached.",
        assistant_source="test",
    )
    bot = _Bot()

    result = await send_telegram_run_attachments(
        _runtime(store, bot), _item(accepted, owner)
    )

    assert result["state"] == "delivered"
    assert result["complete"] is True
    assert result["attempted"] == 2
    assert result["delivered"] == 2
    assert [call["method"] for call in bot.calls] == [
        "send_photo",
        "send_document",
    ]
    assert bot.calls[0]["photo"] == image
    assert bot.calls[1]["document"] == document
    assert result["receipts"] == [
        {
            "attachment_id": image_id,
            "state": "delivered",
            "transport_message_id": "701",
        },
        {
            "attachment_id": document_id,
            "state": "delivered",
            "transport_message_id": "702",
        },
    ]

    store.record_assistant_delivery(
        accepted.request_id,
        delivered=True,
        surface="telegram",
        channel_key="77",
        transport="telegram",
        completion_path="foreground",
        disposition="transport_delivered_with_attachments",
        attachment_receipts=result["receipts"],
    )
    replay = await send_telegram_run_attachments(
        _runtime(store, bot), _item(accepted, owner)
    )
    assert replay["replayed"] is True
    assert replay["receipts"] == result["receipts"]
    assert len(bot.calls) == 2


@pytest.mark.asyncio
async def test_cancelled_run_never_projects_previously_bound_output(tmp_path):
    store, owner, agent, session, accepted = _running_store(tmp_path)
    _bind_file(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=agent,
        request_id=accepted.request_id,
        filename="late.txt",
        media_type="text/plain",
        payload=b"late",
        index=1,
    )
    store.cancel_run(accepted.run_id, owner_id=owner)
    bot = _Bot()

    result = await send_telegram_run_attachments(
        _runtime(store, bot), _item(accepted, owner)
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "run_not_completed"
    assert result["complete"] is False
    assert bot.calls == []


@pytest.mark.asyncio
async def test_recorded_attachment_failure_is_not_automatically_retried(tmp_path):
    store, owner, agent, session, accepted = _running_store(tmp_path)
    _bind_file(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=agent,
        request_id=accepted.request_id,
        filename="blocked.txt",
        media_type="text/plain",
        payload=b"blocked",
        index=1,
    )
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="File attached.",
        assistant_source="test",
    )
    bot = _FailingBot()
    runtime = _runtime(store, bot)
    item = _item(accepted, owner)

    failed = await send_telegram_run_attachments(runtime, item)
    assert failed["state"] == "failed"
    assert bot.calls == 1
    store.record_assistant_delivery(
        accepted.request_id,
        delivered=False,
        surface="telegram",
        channel_key="77",
        transport="telegram",
        completion_path="foreground",
        disposition="attachment_delivery_failed",
        attachment_receipts=failed["receipts"],
    )

    replay = await send_telegram_run_attachments(runtime, item)
    assert replay["replayed"] is True
    assert replay["state"] == "failed"
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_expired_managed_audio_is_not_reported_or_sent_as_delivered(tmp_path):
    store, owner, agent, session, accepted = _running_store(tmp_path)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x00" * 160)
    _bind_file(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=agent,
        request_id=accepted.request_id,
        filename="answer.wav",
        media_type="audio/wav",
        payload=stream.getvalue(),
        index=1,
    )
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="Audio attached.",
        assistant_source="test",
    )
    store.audio_assets.cleanup(
        now=datetime.now(timezone.utc) + timedelta(hours=2)
    )
    bot = _Bot()

    result = await send_telegram_run_attachments(
        _runtime(store, bot), _item(accepted, owner)
    )

    assert result["state"] == "unavailable"
    assert result["complete"] is False
    assert result["reason"] == "managed_attachment_unavailable"
    assert bot.calls == []
