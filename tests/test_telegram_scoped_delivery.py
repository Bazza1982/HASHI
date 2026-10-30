from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_delivery, runtime_session
from orchestrator.telegram_actor_scope import _delegated_owner, runtime_snapshot
from tests.test_telegram_actor_scope import instance


def test_queued_run_claim_uses_its_frozen_owner(tmp_path, monkeypatch):
    runtime, _observations, _ = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime._request_meta_by_id = {
        "one": {"request_id": "one", "owner_id": owner},
        "other": {"request_id": "other", "owner_id": "user:other"},
    }
    runtime.current_request_meta = runtime._request_meta_by_id["other"]
    calls = []
    store = SimpleNamespace(claim_run_delivery_outbox=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(runtime_session, "ensure_store", lambda runtime: store)
    runtime_session.claim_run_delivery_outbox(
        runtime, request_id="one", surface="telegram", channel_key="100", worker_id="test",
    )
    assert calls[0]["owner_id"] == owner
    runtime_session.claim_run_delivery_outbox(
        runtime, request_id="unknown", surface="telegram", channel_key="42", worker_id="test",
    )
    assert calls[1]["owner_id"] == "user:42"
    runtime_session.claim_run_delivery_outbox(
        runtime, request_id="unknown", surface="telegram", channel_key="100",
        worker_id="test", explicit_owner_id=owner,
    )
    assert calls[2]["owner_id"] == owner


@pytest.mark.asyncio
async def test_final_delivery_uses_run_owner_not_active_primary(tmp_path, monkeypatch):
    import orchestrator.admin_local_testing as local_testing

    runtime, _observations, _ = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime._request_meta_by_id = {"one": {"request_id": "one", "owner_id": owner}}
    monkeypatch.setattr(local_testing, "capture_local_command_output", AsyncMock(return_value=False))
    monkeypatch.setattr(runtime_session, "claim_run_delivery_outbox",
                        lambda *args, **kwargs: {"state": "claimed", "claim": {"id": "claim"}})
    monkeypatch.setattr(runtime_session, "ensure_store", lambda runtime: object())
    dispatch = AsyncMock(return_value=(0.1, 1))
    monkeypatch.setattr(runtime_delivery, "dispatch_claimed_telegram_event", dispatch)
    result = await runtime_delivery.send_long_message(
        runtime, chat_id=100, text="response", request_id="one", frontend_outbox=True,
    )
    assert result == (0.1, 1)
    assert dispatch.await_args.kwargs["frontend_owner_id"] == owner

@pytest.mark.asyncio
async def test_explicit_owner_is_used_for_claim_when_registry_is_unavailable(tmp_path, monkeypatch):
    import orchestrator.admin_local_testing as local_testing

    runtime, _observations, _ = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime._request_meta_by_id = {}
    monkeypatch.setattr(local_testing, "capture_local_command_output", AsyncMock(return_value=False))
    seen = []
    def claim(*args, **kwargs):
        seen.append(kwargs)
        return {"state": "claimed", "claim": {"id": "claim"}}
    monkeypatch.setattr(runtime_session, "claim_run_delivery_outbox", claim)
    monkeypatch.setattr(runtime_session, "ensure_store", lambda runtime: object())
    dispatch = AsyncMock(return_value=(0.1, 1))
    monkeypatch.setattr(runtime_delivery, "dispatch_claimed_telegram_event", dispatch)
    await runtime_delivery.send_long_message(
        runtime, chat_id=100, text="response", request_id="one",
        frontend_outbox=True, frontend_owner_id=owner,
    )
    assert seen[0]["explicit_owner_id"] == owner
    assert dispatch.await_args.kwargs["frontend_owner_id"] == owner

@pytest.mark.asyncio
async def test_missing_delegated_outbox_cannot_fall_back_to_raw_send(tmp_path, monkeypatch):
    import orchestrator.admin_local_testing as local_testing

    runtime, _observations, _ = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime._request_meta_by_id = {"one": {"request_id": "one", "owner_id": owner}}
    monkeypatch.setattr(local_testing, "capture_local_command_output", AsyncMock(return_value=False))
    monkeypatch.setattr(runtime_session, "claim_run_delivery_outbox", lambda *args, **kwargs: None)
    raw_send = AsyncMock(return_value=(0.1, 1))
    monkeypatch.setattr(runtime_delivery, "_send_long_message_transport", raw_send)
    result = await runtime_delivery.send_long_message(
        runtime, chat_id=100, text="response", request_id="one", frontend_outbox=True,
    )
    assert result == (0.0, 0)
    assert raw_send.await_count == 0

@pytest.mark.asyncio
async def test_progress_publication_uses_the_run_scope(tmp_path, monkeypatch):
    import orchestrator.admin_local_testing as local_testing

    runtime, _observations, _ = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime._request_meta_by_id = {
        "one": {"request_id": "one", "owner_id": owner, "hashi_session_id": "session-one"}
    }
    runtime.session_store = SimpleNamespace(
        claim_delivery_outbox=lambda **kwargs: [{"outbox_id": "ob", "lease_token": "lease"}],
        complete_delivery_outbox=lambda **kwargs: None,
    )
    captured = []
    monkeypatch.setattr(local_testing, "capture_local_command_output", AsyncMock(return_value=False))
    monkeypatch.setattr(runtime_session, "publish_frontend_message",
                        lambda *args, **kwargs: captured.append(kwargs) or {
                            "session_id": "s", "owner_id": kwargs.get("explicit_owner_id") or "user:42",
                            "delivery_event_id": "ev"})
    monkeypatch.setattr(runtime_delivery, "_send_long_message_transport", AsyncMock(return_value=(0.1, 1)))
    result = await runtime_delivery.send_long_message(
        runtime, chat_id=100, text="progress", request_id="one", purpose="status",
    )
    assert result == (0.1, 1)
    assert captured[0]["explicit_owner_id"] == owner
    assert captured[0]["explicit_session_id"] == "session-one"

@pytest.mark.asyncio
async def test_revoked_progress_is_not_published_to_any_session(tmp_path, monkeypatch):
    import orchestrator.admin_local_testing as local_testing
    from orchestrator.config_json import read_config_json, write_config_json

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime._request_meta_by_id = {
        "one": {"request_id": "one", "owner_id": owner, "hashi_session_id": "session-one"}
    }
    runtime.session_store = object()
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    monkeypatch.setattr(local_testing, "capture_local_command_output", AsyncMock(return_value=False))
    published = []
    monkeypatch.setattr(runtime_session, "publish_frontend_message",
                        lambda *args, **kwargs: published.append(kwargs))
    result = await runtime_delivery.send_long_message(
        runtime, chat_id=100, text="private progress", request_id="one", purpose="status",
    )
    assert result == (0.0, 0)
    assert published == []

@pytest.mark.asyncio
async def test_revoked_progress_cannot_use_direct_send_path(tmp_path, monkeypatch):
    from orchestrator.config_json import read_config_json, write_config_json
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    import orchestrator.flexible_agent_runtime as agent_runtime

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime._request_meta_by_id = {
        "one": {"request_id": "one", "owner_id": owner, "hashi_session_id": "session-one"}
    }
    runtime.app.bot = SimpleNamespace(send_message=AsyncMock())
    runtime.session_store = object()
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    monkeypatch.setattr(agent_runtime, "apply_disable_notification_default", lambda *args: None)
    monkeypatch.setattr(agent_runtime.telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    published = []
    monkeypatch.setattr(runtime_session, "ensure_store", lambda runtime: runtime.session_store)
    monkeypatch.setattr(runtime_session, "publish_frontend_message",
                        lambda *args, **kwargs: published.append(kwargs))
    result = await FlexibleAgentRuntime._send_text(
        runtime, 100, "private progress", _request_id="one", _purpose="status",
    )
    assert result is None
    assert published == []
    assert runtime.app.bot.send_message.await_count == 0

@pytest.mark.asyncio
async def test_direct_progress_uses_frozen_session(tmp_path, monkeypatch):
    import orchestrator.flexible_agent_runtime as agent_runtime
    import orchestrator.frontend_connector_registry as connector
    import orchestrator.frontend_dispatch as dispatch
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime, _observations, _ = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime._request_meta_by_id = {
        "one": {"request_id": "one", "owner_id": owner, "hashi_session_id": "session-one"}
    }
    runtime.app.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    claim = {"outbox_id": "ob", "lease_token": "lease", "endpoint_id": "ep"}
    runtime.session_store = SimpleNamespace(
        claim_delivery_outbox=lambda **kwargs: [claim],
        complete_delivery_outbox=lambda **kwargs: None,
        record_frontend_delivery_receipt=lambda **kwargs: None,
    )
    monkeypatch.setattr(agent_runtime, "apply_disable_notification_default", lambda *args: None)
    monkeypatch.setattr(agent_runtime.telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    monkeypatch.setattr(runtime_session, "ensure_store", lambda runtime: runtime.session_store)
    published = []
    monkeypatch.setattr(runtime_session, "publish_frontend_message",
                        lambda *args, **kw: published.append(kw) or {
                            "session_id": "session-one", "owner_id": owner, "delivery_event_id": "ev"})
    monkeypatch.setattr(connector, "require_connector_event", lambda *args: None)
    monkeypatch.setattr(dispatch, "project_claimed_frontend_event", lambda *args, **kw: {})
    monkeypatch.setattr(runtime_delivery, "require_matching_telegram_rendition", lambda *args, **kw: None)
    monkeypatch.setattr(runtime_delivery, "telegram_reply_markup_for_event", lambda *args: None)
    result = await FlexibleAgentRuntime._send_text(
        runtime, 100, "progress", _request_id="one", _purpose="status",
    )
    assert result.message_id == 1
    assert published[0]["explicit_owner_id"] == owner
    assert published[0]["explicit_session_id"] == "session-one"
    assert runtime.app.bot.send_message.await_count == 1

@pytest.mark.asyncio
async def test_command_reply_stops_if_actor_is_revoked_during_handler(tmp_path, monkeypatch):
    import orchestrator.admin_local_testing as local_testing
    import orchestrator.flexible_agent_runtime as agent_runtime
    from orchestrator.config_json import read_config_json, write_config_json
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.telegram_actor_scope import process_telegram_update
    from tests.test_telegram_actor_scope import update

    runtime, _observations, config_path = instance(tmp_path)
    reply = AsyncMock()
    incoming = update(100)
    incoming.message = SimpleNamespace(reply_text=reply)
    monkeypatch.setattr(local_testing, "local_command_capture", lambda *args: None)
    monkeypatch.setattr(agent_runtime, "apply_disable_notification_default", lambda *args: None)
    monkeypatch.setattr(agent_runtime.telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    published = []
    monkeypatch.setattr(runtime_session, "publish_frontend_message_for_update",
                        lambda *args, **kwargs: published.append(kwargs))
    async def handler(_update):
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)
        await FlexibleAgentRuntime._reply_text(runtime, incoming, "private command output")
    runtime.app.process_update = handler
    assert await process_telegram_update(runtime, incoming)
    assert reply.await_count == 0
    assert published == []
