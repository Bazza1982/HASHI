"""Tests for HChat message freshness gate and ingress confirmations.

Functional owner: PAO / Functions layer
Engineering layer: Functions
Verification: Red-green proof for HChat freshness gate across transmission and receiving endpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

from orchestrator.session_store import SessionStore
from orchestrator.ingress_confirmation import (
    ConfirmationConflictError,
    IngressConfirmationService,
)
from orchestrator.hchat_freshness_gate import (
    check_hchat_freshness_and_register,
    execute_confirmed_hchat,
)
from tools import hchat_send


def _make_utc_iso(seconds_ago: float = 0.0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat().replace("+00:00", "Z")


def test_hchat_send_generates_originated_at_and_message_id(monkeypatch):
    """Test that send_hchat generates message_id and originated_at once and passes them."""
    calls = []
    cfg = {
        "global": {"instance_id": "HASHI1", "workbench_port": 18800, "api_host": "127.0.0.1"},
        "agents": [{"name": "akane", "is_active": True}, {"name": "zelda", "is_active": True}],
    }
    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_build_reply_route", lambda _cfg: {"instance_id": "HASHI1"})

    def mock_send_via_local_workbench(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(hchat_send, "_send_via_local_workbench", mock_send_via_local_workbench)

    res = hchat_send.send_hchat("akane", "zelda", "hello from zelda")
    assert res is True
    assert len(calls) == 1
    args, kwargs = calls[0]
    # Check that message_id and originated_at were threaded through kwargs
    assert "originated_at" in kwargs
    assert "message_id" in kwargs
    orig = kwargs["originated_at"]
    assert isinstance(orig, str) and (orig.endswith("Z") or "+00:00" in orig)
    assert kwargs["message_id"].startswith("hchat-")


def test_hchat_transport_fallback_reuses_one_pending_confirmation(tmp_path: Path):
    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI3",
        attachment_root=tmp_path / "media" / "session_attachments",
    )
    store.resolve_primary_session(
        owner_id="user:default", agent_id="lily", establish=True
    )
    originated_at = _make_utc_iso(seconds_ago=90)
    message_id = "hchat-cross-route-one"
    raw_text = "execute this logical message once"

    first_fresh, first = check_hchat_freshness_and_register(
        store=store,
        originated_at=originated_at,
        to_agent="lily",
        from_agent="rain",
        from_instance="HASHI2",
        message_id=message_id,
        text=hchat_send.format_hchat_message("rain", "HASHI2", raw_text),
        is_reply=False,
        transport="workbench_api",
        connector_id="hchat_workbench",
        owner_id="user:default",
        idempotency_key=f"hchat:{message_id}",
        payload_to_execute={"runtime_payload": {"text": "workbench envelope"}},
    )
    second_fresh, second = check_hchat_freshness_and_register(
        store=store,
        originated_at=originated_at,
        to_agent="lily",
        from_agent="rain",
        from_instance="HASHI2",
        message_id=message_id,
        text=raw_text,
        is_reply=False,
        transport="protocol",
        connector_id="hchat_protocol",
        owner_id="user:default",
        idempotency_key=f"protocol:message:{message_id}",
        payload_to_execute={"prompt_text": "protocol envelope"},
    )

    assert first_fresh is False
    assert second_fresh is False
    assert second["confirmation_id"] == first["confirmation_id"]
    assert len(
        IngressConfirmationService(store).list(
            session_id=first["session_id"], owner_id="user:default"
        )
    ) == 1

    with pytest.raises(ConfirmationConflictError):
        check_hchat_freshness_and_register(
            store=store,
            originated_at=originated_at,
            to_agent="lily",
            from_agent="rain",
            from_instance="HASHI2",
            message_id=message_id,
            text="different content under the same message identity",
            is_reply=False,
            transport="protocol",
            connector_id="hchat_protocol",
            owner_id="user:default",
            idempotency_key=f"protocol:message:{message_id}",
            payload_to_execute={"prompt_text": "different protocol envelope"},
        )


@pytest.mark.asyncio
async def test_confirmed_hchat_request_executes_exactly_one_run(tmp_path: Path):
    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI3",
        attachment_root=tmp_path / "media" / "session_attachments",
    )
    session = store.resolve_primary_session(
        owner_id="user:default", agent_id="lily", establish=True
    )

    class Runtime:
        def __init__(self):
            self.calls = 0

        async def enqueue_api_text(self, text, **kwargs):
            self.calls += 1
            accepted = store.accept_run(
                session_id=session["session_id"],
                owner_id="user:default",
                agent_id="lily",
                request_id=f"req-hchat-confirm-{self.calls}",
                text=text,
                source=kwargs.get("source") or "hchat",
                idempotency_key=kwargs["idempotency_key"],
                content=[{"type": "text", "text": text}],
                expected_context_generation=session["context_generation"],
            )
            return accepted.request_id

    runtime = Runtime()
    is_fresh, confirmation = check_hchat_freshness_and_register(
        store=store,
        originated_at=_make_utc_iso(seconds_ago=90),
        to_agent="lily",
        from_agent="rain",
        from_instance="HASHI2",
        message_id="hchat-confirm-once",
        text="execute only once",
        is_reply=False,
        transport="protocol",
        connector_id="hchat_protocol",
        owner_id="user:default",
        idempotency_key="protocol:message:hchat-confirm-once",
        payload_to_execute={
            "runtime_payload": {
                "text": "execute only once",
                "source": "protocol:message",
                "request_metadata": {},
            }
        },
    )
    assert is_fresh is False
    assert confirmation is not None
    service = IngressConfirmationService(store)

    async def execute(record):
        return await execute_confirmed_hchat(
            record,
            runtime_map_fn=lambda: {"lily": runtime},
            session_store=store,
        )

    first = await service.decide_async(
        confirmation_id=confirmation["confirmation_id"],
        decision="confirm",
        owner_id="user:default",
        execute_fn=execute,
    )
    second = await service.decide_async(
        confirmation_id=confirmation["confirmation_id"],
        decision="confirm",
        owner_id="user:default",
        execute_fn=lambda _record: pytest.fail("HChat confirmation executed twice"),
    )

    assert first["state"] == "confirmed"
    assert second["run_id"] == first["run_id"]
    assert runtime.calls == 1
    assert len(
        store.recent_session_runs(session["session_id"], owner_id="user:default")
    ) == 1


@pytest.mark.asyncio
async def test_protocol_manager_stale_message_requires_confirmation(tmp_path: Path):
    """ProtocolManager.handle_protocol_message gates stale messages."""
    from remote.protocol_manager import ProtocolManager

    instance_info = {"instance_id": "HASHI1", "platform": "windows"}
    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI1",
        attachment_root=tmp_path / "media" / "session_attachments",
    )
    store.resolve_primary_session(owner_id="user:default", agent_id="lily", establish=True)

    pm = ProtocolManager(
        hashi_root=tmp_path,
        instance_info=instance_info,
        peer_registry=None,
        workbench_port=18800,
    )
    pm._inflight = {}
    pm.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    pm._frontend_session_store = lambda: store

    enqueue_called = []
    async def mock_enqueue(*args, **kwargs):
        enqueue_called.append((args, kwargs))
        return {"request_id": "req-1", "session_id": "ses-1", "run_id": "run-1"}

    pm._enqueue_local_prompt = mock_enqueue

    stale_originated = _make_utc_iso(seconds_ago=120)  # 2 minutes ago (> 60s)
    payload = {
        "message_type": "agent_message",
        "message_id": "msg-stale-001",
        "conversation_id": "conv-001",
        "from_instance": "HASHI2",
        "from_agent": "rain",
        "to_instance": "HASHI1",
        "to_agent": "lily",
        "originated_at": stale_originated,
        "body": {"text": "Delayed task command"},
        "ttl": 5,
        "route_trace": ["HASHI2"],
    }

    code, resp = await pm.handle_protocol_message(payload)
    assert code == 202
    assert resp.get("confirmation_required") is True
    assert resp.get("state") == "awaiting_confirmation"
    assert "confirmation_id" in resp
    # Ensure local queue was NOT enqueued
    assert len(enqueue_called) == 0

    confirmation = IngressConfirmationService(store).get(
        resp["confirmation_id"], owner_id="user:default"
    )
    IngressConfirmationService(store).decide(
        confirmation_id=confirmation["confirmation_id"],
        decision="discard",
        owner_id="user:default",
    )
    advanced = await pm._advance_inflight_item(
        pm._inflight[payload["message_id"]],
        now=datetime.now(timezone.utc).timestamp(),
    )
    assert advanced is True
    assert pm._inflight[payload["message_id"]]["state"] == "discarded"

    retry_code, retry_resp = await pm.handle_protocol_message(payload)
    assert retry_code == 200
    assert retry_resp["accepted"] is False
    assert retry_resp["state"] == "discarded"
    assert retry_resp["confirmation_id"] == resp["confirmation_id"]


@pytest.mark.asyncio
async def test_protocol_fallback_preserves_terminal_confirmation_from_another_route(
    tmp_path: Path,
):
    from remote.protocol_manager import ProtocolManager

    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI1",
        attachment_root=tmp_path / "media" / "session_attachments",
    )
    store.resolve_primary_session(
        owner_id="user:default", agent_id="lily", establish=True
    )
    originated_at = _make_utc_iso(seconds_ago=120)
    message_id = "msg-cross-route-discarded"
    _, confirmation = check_hchat_freshness_and_register(
        store=store,
        originated_at=originated_at,
        to_agent="lily",
        from_agent="rain",
        from_instance="HASHI2",
        message_id=message_id,
        text="Delayed task command",
        is_reply=False,
        transport="workbench_api",
        connector_id="hchat_workbench",
        owner_id="user:default",
        idempotency_key=f"hchat:{message_id}",
        payload_to_execute={"runtime_payload": {"text": "Delayed task command"}},
    )
    IngressConfirmationService(store).decide(
        confirmation_id=confirmation["confirmation_id"],
        decision="discard",
        owner_id="user:default",
    )

    pm = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI1", "platform": "windows"},
        peer_registry=None,
        workbench_port=18800,
    )
    pm._inflight = {}
    pm.get_local_agents_snapshot = lambda: [
        {"agent_name": "lily", "is_active": True}
    ]
    pm._frontend_session_store = lambda: store
    pm._enqueue_local_prompt = AsyncMock(
        side_effect=AssertionError("discarded fallback must not enqueue")
    )
    payload = {
        "message_type": "agent_message",
        "message_id": message_id,
        "conversation_id": "conv-cross-route",
        "from_instance": "HASHI2",
        "from_agent": "rain",
        "to_instance": "HASHI1",
        "to_agent": "lily",
        "originated_at": originated_at,
        "body": {"text": "Delayed task command"},
        "ttl": 5,
        "route_trace": ["HASHI2"],
    }

    code, receipt = await pm.handle_protocol_message(payload)

    assert code == 200
    assert receipt["accepted"] is False
    assert receipt["confirmation_required"] is False
    assert receipt["state"] == "discarded"
    assert receipt["confirmation_id"] == confirmation["confirmation_id"]
    assert pm._inflight[message_id]["state"] == "discarded"
    pm._enqueue_local_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_protocol_manager_stale_reply_requires_confirmation_to_display(tmp_path: Path):
    """ProtocolManager._handle_agent_reply gates stale terminal replies with action '仍然显示'."""
    from remote.protocol_manager import ProtocolManager
    from orchestrator.ingress_confirmation import IngressConfirmationService

    instance_info = {"instance_id": "HASHI1", "platform": "windows"}
    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI1",
        attachment_root=tmp_path / "media" / "session_attachments",
    )
    store.resolve_primary_session(owner_id="user:default", agent_id="lily", establish=True)

    pm = ProtocolManager(
        hashi_root=tmp_path,
        instance_info=instance_info,
        peer_registry=None,
        workbench_port=18800,
    )
    pm._inflight = {}
    pm.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    pm._frontend_session_store = lambda: store

    enqueue_called = []
    async def mock_enqueue(*args, **kwargs):
        enqueue_called.append((args, kwargs))
        return {"request_id": "req-1", "session_id": "ses-1", "run_id": "run-1"}

    pm._enqueue_local_prompt = mock_enqueue

    stale_originated = _make_utc_iso(seconds_ago=150)
    payload = {
        "message_type": "agent_reply",
        "message_id": "msg-reply-stale-001",
        "in_reply_to": "msg-orig-001",
        "conversation_id": "conv-001",
        "from_instance": "HASHI2",
        "from_agent": "rain",
        "to_instance": "HASHI1",
        "to_agent": "lily",
        "originated_at": stale_originated,
        "body": {"text": "Delayed response answer"},
        "ttl": 5,
        "route_trace": ["HASHI2"],
    }

    code, resp = await pm.handle_protocol_message(payload)
    assert code == 202
    assert resp.get("confirmation_required") is True
    assert resp.get("state") == "awaiting_confirmation"
    assert len(enqueue_called) == 0

    # Verify confirmation record has message_kind 'hchat_reply'
    service = IngressConfirmationService(store)
    conf = service.get(resp["confirmation_id"], owner_id="user:default")
    assert conf["message_kind"] == "hchat_reply"
    assert conf["display_metadata"].get("action_label") == "仍然显示"
