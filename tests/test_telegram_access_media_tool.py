from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator import runtime_session
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.telegram_actor_scope import _delegated_owner, runtime_snapshot
from tests.test_telegram_actor_scope import instance
from tools.builtins import execute_telegram_send_file


@pytest.mark.asyncio
async def test_scoped_media_is_published_to_the_callers_session(tmp_path, monkeypatch):
    runtime, _observations, _config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    file_path = tmp_path / "picture.png"
    file_path.write_bytes(b"image")
    publications = []

    def publish(runtime, **kwargs):
        publications.append(kwargs)
        return {"owner_id": owner, "session_id": "actor-session",
                "delivery_event_id": "event", "attachment_id": "attachment"}

    store = SimpleNamespace(
        claim_delivery_outbox=lambda **kwargs: [],
        frontend_delivery_receipts=lambda **kwargs: [{"status": "delivered"}],
    )
    monkeypatch.setattr(runtime_session, "publish_frontend_media_notification", publish)
    monkeypatch.setattr(runtime_session, "ensure_store", lambda _runtime: store)
    result = await execute_telegram_send_file(
        {"path": str(file_path)}, {}, access_root=tmp_path, workspace_dir=tmp_path,
        audit_context={"_runtime": runtime, "owner_id": owner, "hashi_session_id": "actor-session",
                       "chat_id": 100},
    )

    assert json.loads(result)["ok"]
    assert publications[0]["explicit_owner_id"] == owner
    assert publications[0]["explicit_session_id"] == "actor-session"


@pytest.mark.asyncio
async def test_global_user_can_explicitly_send_to_another_chat(tmp_path, monkeypatch):
    runtime, _observations, _config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    file_path = tmp_path / "picture.png"
    file_path.write_bytes(b"image")
    publications = []

    def publish(runtime, **kwargs):
        publications.append(kwargs)
        return {"owner_id": owner, "session_id": "actor-session",
                "delivery_event_id": "event", "attachment_id": "attachment"}

    store = SimpleNamespace(
        claim_delivery_outbox=lambda **kwargs: [],
        frontend_delivery_receipts=lambda **kwargs: [{"status": "delivered"}],
    )
    monkeypatch.setattr(runtime_session, "publish_frontend_media_notification", publish)
    monkeypatch.setattr(runtime_session, "ensure_store", lambda _runtime: store)
    result = await execute_telegram_send_file(
        {"path": str(file_path), "chat_id": 101}, {}, access_root=tmp_path,
        workspace_dir=tmp_path,
        audit_context={"_runtime": runtime, "owner_id": owner,
                       "hashi_session_id": "actor-session", "chat_id": 100},
    )
    assert json.loads(result)["ok"]
    assert publications[0]["channel_key"] == "101"
    assert publications[0]["explicit_owner_id"] == owner


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["revoked", "different_chat", "missing_session"])
async def test_scoped_media_rejects_unsafe_destination(tmp_path, monkeypatch, reason):
    runtime, _observations, config_path = instance(tmp_path)
    actor_id = 200 if reason == "different_chat" else 100
    owner = _delegated_owner(runtime_snapshot(runtime), actor_id)
    file_path = tmp_path / "picture.png"
    file_path.write_bytes(b"image")
    publications = []
    monkeypatch.setattr(runtime_session, "publish_frontend_media_notification",
                        lambda *args, **kwargs: publications.append(kwargs))
    if reason == "revoked":
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)
    context = {"_runtime": runtime, "owner_id": owner, "hashi_session_id": "actor-session",
               "chat_id": actor_id}
    if reason == "missing_session":
        context.pop("hashi_session_id")
    args = {"path": str(file_path)}
    if reason == "different_chat":
        args["chat_id"] = 101
    result = await execute_telegram_send_file(
        args, {}, access_root=tmp_path, workspace_dir=tmp_path, audit_context=context,
    )

    assert result.startswith("Error:")
    assert publications == []
