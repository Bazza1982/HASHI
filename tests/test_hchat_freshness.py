"""Tests for HChat message freshness gate and ingress confirmations.

Functional owner: PAO / Functions layer
Engineering layer: Functions
Verification: Red-green proof for HChat freshness gate across transmission and receiving endpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
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


def test_protocol_state_home_is_disposable(tmp_path):
    assert Path.home() == tmp_path / "isolated-protocol-home"


@pytest.mark.parametrize("originated_at", [_make_utc_iso(seconds_ago=3600), None, "not-a-time"])
def test_default_hchat_request_age_is_informational(tmp_path, originated_at):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    accepted, confirmation = check_hchat_freshness_and_register(
        store=store, originated_at=originated_at, to_agent="lily", from_agent="rain",
        from_instance="HASHI2", message_id="old-request", text="hello", is_reply=False,
        transport="protocol", owner_id="user:default",
    )
    assert accepted is True
    assert confirmation is None


def test_existing_discarded_hchat_decision_survives_default_policy_change(tmp_path):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    store.resolve_primary_session(owner_id="user:default", agent_id="lily", establish=True)
    arguments = dict(store=store, originated_at=_make_utc_iso(seconds_ago=120), to_agent="lily",
        from_agent="rain", from_instance="HASHI2", message_id="old-request", text="hello",
        is_reply=False, transport="protocol", owner_id="user:default")
    _, original = check_hchat_freshness_and_register(
        **arguments, age_policy={"mode": "confirm", "max_age_seconds": 60})
    IngressConfirmationService(store).decide(
        confirmation_id=original["confirmation_id"], decision="discard", owner_id="user:default")
    allowed, retry = check_hchat_freshness_and_register(**arguments)
    assert allowed is False
    assert retry["state"] == "discarded"
    assert retry["confirmation_id"] == original["confirmation_id"]


def test_hchat_request_can_use_explicit_expiry_policy(tmp_path):
    from orchestrator.hchat_freshness_gate import HChatMessageExpired

    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    arguments = dict(store=store, to_agent="lily", from_agent="rain", from_instance="HASHI2",
        message_id="expiring-request", text="hello", is_reply=False, transport="protocol",
        owner_id="user:default", age_policy={"mode": "expire", "max_age_seconds": 60})
    with pytest.raises(HChatMessageExpired):
        check_hchat_freshness_and_register(**arguments, originated_at=_make_utc_iso(seconds_ago=120))
    assert check_hchat_freshness_and_register(**arguments, originated_at=_make_utc_iso())[0] is True


@pytest.mark.asyncio
async def test_protocol_delayed_request_is_accepted_once_by_default(tmp_path):
    from remote.protocol_manager import ProtocolManager

    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    pm = ProtocolManager(hashi_root=tmp_path, instance_info={"instance_id": "HASHI1"},
                         peer_registry=None, workbench_port=18800)
    pm.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    pm._frontend_session_store = lambda: store
    pm._enqueue_local_prompt = AsyncMock(return_value={"request_id": "req-1", "session_id": "ses-1", "run_id": "run-1"})
    payload = {"message_type": "agent_message", "message_id": "delayed-request",
        "idempotency_key": "hchat:delayed-request", "conversation_id": "conversation-1",
        "from_instance": "HASHI2", "from_agent": "rain", "to_instance": "HASHI1",
        "to_agent": "lily", "originated_at": _make_utc_iso(seconds_ago=3600),
        "body": {"text": "hello"}, "ttl": 5, "route_trace": ["HASHI2"]}
    first_code, first = await pm.handle_protocol_message(payload)
    retry_code, retry = await pm.handle_protocol_message(payload)
    assert first_code == retry_code == 202
    assert first["accepted"] is True and retry["request_id"] == first["request_id"]
    assert pm._enqueue_local_prompt.await_count == 1


@pytest.mark.asyncio
async def test_protocol_uses_explicit_hchat_request_expiry_config(tmp_path):
    import json
    from remote.protocol_manager import ProtocolManager

    (tmp_path / "agents.json").write_text(json.dumps({"global": {
        "hchat_request_age_policy": {"mode": "expire", "max_age_seconds": 60}}, "agents": []}), encoding="utf-8")
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    pm = ProtocolManager(hashi_root=tmp_path, instance_info={"instance_id": "HASHI1"},
                         peer_registry=None, workbench_port=18800)
    pm.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    pm._frontend_session_store = lambda: store
    pm._enqueue_local_prompt = AsyncMock()
    payload = {"message_type": "agent_message", "message_id": "delayed-request",
        "idempotency_key": "hchat:delayed-request", "conversation_id": "conversation-1",
        "from_instance": "HASHI2", "from_agent": "rain", "to_instance": "HASHI1",
        "to_agent": "lily", "originated_at": _make_utc_iso(seconds_ago=120),
        "body": {"text": "hello"}, "ttl": 5, "route_trace": ["HASHI2"]}
    code, result = await pm.handle_protocol_message(payload)
    assert code == 409 and result["body"]["code"] == "hchat_message_expired"
    pm._enqueue_local_prompt.assert_not_awaited()


def test_hchat_send_generates_originated_at_and_message_id(monkeypatch):
    """Test that send_hchat generates message_id and originated_at once and passes them."""
    calls = []
    cfg = {
        "global": {"instance_id": "HASHI1", "workbench_port": 18800, "api_host": "127.0.0.1"},
        "agents": [{"name": "akane", "is_active": True}, {"name": "zelda", "is_active": True}],
    }
    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_build_reply_route", lambda _cfg: {"instance_id": "HASHI1"})
    monkeypatch.setattr(
        hchat_send,
        "_register_hchat_outbound_correlation",
        lambda *_args, **_kwargs: True,
    )

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
    assert kwargs["conversation_id"].startswith("conv-")


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
        age_policy={"mode": "confirm"},
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
        idempotency_key=f"hchat:{message_id}",
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
            idempotency_key=f"hchat:{message_id}",
            payload_to_execute={"prompt_text": "different protocol envelope"},
        )

    with pytest.raises(ConfirmationConflictError):
        check_hchat_freshness_and_register(
            store=store,
            originated_at=originated_at,
            to_agent="lily",
            from_agent="rain",
            from_instance="HASHI9",
            message_id=message_id,
            text=raw_text,
            is_reply=False,
            transport="protocol",
            connector_id="hchat_protocol",
            owner_id="user:default",
            idempotency_key=f"hchat:{message_id}",
            payload_to_execute={"prompt_text": "different source instance"},
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
        age_policy={"mode": "confirm"},
        to_agent="lily",
        from_agent="rain",
        from_instance="HASHI2",
        message_id="hchat-confirm-once",
        text="execute only once",
        is_reply=False,
        transport="protocol",
        connector_id="hchat_protocol",
        owner_id="user:default",
        idempotency_key="hchat:hchat-confirm-once",
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
    pm._hchat_request_age_policy = lambda: {"mode": "confirm"}

    enqueue_called = []
    async def mock_enqueue(*args, **kwargs):
        enqueue_called.append((args, kwargs))
        return {"request_id": "req-1", "session_id": "ses-1", "run_id": "run-1"}

    pm._enqueue_local_prompt = mock_enqueue
    pm._send_delivery_status = AsyncMock(return_value=True)

    stale_originated = _make_utc_iso(seconds_ago=120)  # 2 minutes ago (> 60s)
    payload = {
        "message_type": "agent_message",
        "message_id": "msg-stale-001",
        "idempotency_key": "hchat:msg-stale-001",
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
    assert (
        pm._inflight[payload["message_id"]]["delivery_status_notified_state"]
        == "discarded"
    )
    pm._send_delivery_status.assert_awaited_once()

    retry_code, retry_resp = await pm.handle_protocol_message(payload)
    assert retry_code == 200
    assert retry_resp["accepted"] is False
    assert retry_resp["state"] == "discarded"
    assert retry_resp["confirmation_id"] == resp["confirmation_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["text", "conversation_id", "from_instance", "from_agent", "to_agent", "to_instance", "originated_at"])
async def test_protocol_retry_binds_content_and_route_before_returning_old_receipt(tmp_path: Path, field):
    from copy import deepcopy
    from remote.protocol_manager import ProtocolManager

    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    pm = ProtocolManager(hashi_root=tmp_path, instance_info={"instance_id": "HASHI1"},
                         peer_registry=None, workbench_port=18800)
    pm.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    pm._frontend_session_store = lambda: store
    pm._enqueue_local_prompt = AsyncMock(return_value={"request_id": "req-1", "session_id": "ses-1", "run_id": "run-1"})
    payload = {"message_type": "agent_message", "message_id": "msg-retry-binding",
        "idempotency_key": "hchat:msg-retry-binding", "conversation_id": "conversation-1",
        "from_instance": "HASHI2", "from_agent": "rain", "to_instance": "HASHI1",
        "to_agent": "lily", "originated_at": _make_utc_iso(), "body": {"text": "original"},
        "ttl": 5, "route_trace": ["HASHI2"]}
    first_code, first = await pm.handle_protocol_message(payload)
    assert first_code == 202 and first["accepted"] is True
    retry_code, retry = await pm.handle_protocol_message(deepcopy(payload))
    assert retry_code == 202 and retry["request_id"] == first["request_id"]
    pm._inflight = pm._load_json(pm._inflight_path)["messages"]
    changed_hop = deepcopy(payload)
    changed_hop["route_trace"] = ["HASHI2", "HASHI_RELAY"]
    assert (await pm.handle_protocol_message(changed_hop))[0] == 202
    equivalent_time = deepcopy(payload)
    equivalent_time["originated_at"] = datetime.fromisoformat(payload["originated_at"].replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=10))).isoformat()
    assert (await pm.handle_protocol_message(equivalent_time))[0] == 202
    changed = deepcopy(payload)
    if field == "text":
        changed["body"]["text"] = "different"
    else:
        changed[field] = "different"
    code, result = await pm.handle_protocol_message(changed)
    assert code == 409 and result["ok"] is False
    assert pm._enqueue_local_prompt.await_count == 1


@pytest.mark.asyncio
async def test_protocol_restart_rebinds_exact_accepted_run_without_reenqueue(tmp_path: Path):
    from remote.protocol_manager import ProtocolManager

    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    session = store.resolve_primary_session(owner_id="user:default", agent_id="lily", establish=True)
    pm = ProtocolManager(hashi_root=tmp_path, instance_info={"instance_id": "HASHI1"},
                         peer_registry=None, workbench_port=18800)
    pm.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    pm._frontend_session_store = lambda: store
    calls = []

    async def enqueue(_agent, prompt, **kwargs):
        calls.append(kwargs["message_id"])
        accepted = store.accept_run(
            session_id=session["session_id"], owner_id="user:default", agent_id="lily",
            request_id="req-hchat-restart", text=prompt, source="hchat",
            idempotency_key=kwargs["idempotency_key"],
            expected_context_generation=session["context_generation"],
        )
        return {"request_id": accepted.request_id, "session_id": accepted.session_id,
                "run_id": accepted.run_id}

    pm._enqueue_local_prompt = enqueue
    payload = {"message_type": "agent_message", "message_id": "restart-message",
        "idempotency_key": "hchat:restart-message", "conversation_id": "conversation-1",
        "from_instance": "HASHI2", "from_agent": "rain", "to_instance": "HASHI1",
        "to_agent": "lily", "originated_at": _make_utc_iso(), "body": {"text": "original"},
        "ttl": 5, "route_trace": ["HASHI2"]}
    code, first = await pm.handle_protocol_message(payload)
    assert code == 202 and first["accepted"] is True

    resumed = ProtocolManager(hashi_root=tmp_path, instance_info={"instance_id": "HASHI1"},
                              peer_registry=None, workbench_port=18800)
    resumed.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    resumed._frontend_session_store = lambda: store
    resumed._enqueue_local_prompt = AsyncMock(side_effect=AssertionError("must not enqueue again"))
    resumed._process_superloop_receipts = AsyncMock()
    resumed._poll_session_result = AsyncMock(return_value={"state": "running"})
    await resumed._process_inflight_once()
    code, retry = await resumed.handle_protocol_message(payload)
    assert code == 202 and retry["accepted"] is True
    assert retry["request_id"] == first["request_id"]
    assert calls == ["restart-message"]
    assert len(store.recent_session_runs(session["session_id"], owner_id="user:default")) == 1
    resumed._poll_session_result.assert_awaited_once_with("lily", first["session_id"], first["run_id"])
    resumed._enqueue_local_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_with_incomplete_receipt_never_reenqueues_ambiguous_message(tmp_path: Path):
    from remote.protocol_manager import ProtocolManager, _protocol_ingress_identity

    payload = {"message_type": "agent_message", "message_id": "restart-unknown",
        "idempotency_key": "hchat:restart-unknown", "conversation_id": "conversation-1",
        "from_instance": "HASHI2", "from_agent": "rain", "to_instance": "HASHI1",
        "to_agent": "lily", "originated_at": _make_utc_iso(), "body": {"text": "original"},
        "ttl": 5, "route_trace": ["HASHI2"]}
    pm = ProtocolManager(hashi_root=tmp_path, instance_info={"instance_id": "HASHI1"},
                         peer_registry=None, workbench_port=18800)
    pm.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    pm._frontend_session_store = lambda: SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    pm._inflight[payload["message_id"]] = {
        "state": "abandoned_after_restart",
        "ingress_identity": _protocol_ingress_identity(payload, local_instance="HASHI1", kind="message"),
    }
    pm._enqueue_local_prompt = AsyncMock()
    code, result = await pm.handle_protocol_message(payload)
    assert code == 409 and result["body"]["code"] == "delivery_outcome_unknown"
    pm._enqueue_local_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_inflight_without_original_content_never_fabricates_a_retry_receipt(tmp_path: Path):
    from remote.protocol_manager import ProtocolManager

    pm = ProtocolManager(hashi_root=tmp_path, instance_info={"instance_id": "HASHI1"},
                         peer_registry=None, workbench_port=18800)
    pm._inflight = {"old-message": {"state": "delivered_to_local_queue", "request_id": "old-run"}}
    pm._enqueue_local_prompt = AsyncMock()
    payload = {"message_type": "agent_message", "message_id": "old-message",
        "idempotency_key": "hchat:old-message", "conversation_id": "conversation-1",
        "from_instance": "HASHI2", "from_agent": "rain", "to_instance": "HASHI1",
        "to_agent": "lily", "originated_at": _make_utc_iso(), "body": {"text": "unknown"},
        "ttl": 5, "route_trace": ["HASHI2"]}
    code, result = await pm.handle_protocol_message(payload)
    assert code == 409 and result["body"]["code"] == "delivery_outcome_unknown"
    pm._enqueue_local_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_attachment_retry_uses_content_identity_not_staging_path(tmp_path: Path):
    from copy import deepcopy
    from remote.protocol_manager import ProtocolManager

    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    pm = ProtocolManager(hashi_root=tmp_path, instance_info={"instance_id": "HASHI1"},
                         peer_registry=None, workbench_port=18800)
    pm.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    pm._frontend_session_store = lambda: store
    pm._enqueue_local_prompt = AsyncMock(return_value={"request_id": "req-1", "session_id": "ses-1", "run_id": "run-1"})
    attachment = {"attachment_id": "asset-1", "filename": "note.txt", "size_bytes": 1,
                  "sha256": "a" * 64, "stored_path": "first-location", "mime_type": "text/plain"}
    payload = {"message_type": "agent_message", "message_id": "attachment-retry",
        "idempotency_key": "hchat:attachment-retry", "conversation_id": "conversation-1",
        "from_instance": "HASHI2", "from_agent": "rain", "to_instance": "HASHI1", "to_agent": "lily",
        "originated_at": _make_utc_iso(), "body": {"text": "look", "attachments": [attachment]},
        "ttl": 5, "route_trace": ["HASHI2"], "_local_attachment_manifest_verified": True}
    assert (await pm.handle_protocol_message(payload))[0] == 202
    retry = deepcopy(payload)
    retry["body"]["attachments"][0]["stored_path"] = "new-location"
    assert (await pm.handle_protocol_message(retry))[0] == 202
    retry["body"]["attachments"][0]["sha256"] = "b" * 64
    assert (await pm.handle_protocol_message(retry))[0] == 409
    assert pm._enqueue_local_prompt.await_count == 1


@pytest.mark.asyncio
async def test_protocol_reply_retry_does_not_claim_a_different_reply_was_delivered(tmp_path: Path):
    from copy import deepcopy
    from remote.protocol_manager import ProtocolManager

    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")
    pm = ProtocolManager(hashi_root=tmp_path, instance_info={"instance_id": "HASHI1"},
                         peer_registry=None, workbench_port=18800)
    pm.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    pm._frontend_session_store = lambda: store
    pm._enqueue_local_prompt = AsyncMock(return_value={"request_id": "req-1", "session_id": "ses-1", "run_id": "run-1"})
    payload = {"message_type": "agent_reply", "message_id": "reply-binding",
        "idempotency_key": "hchat:reply-binding", "conversation_id": "conversation-1",
        "in_reply_to": "message-original", "from_instance": "HASHI2", "from_agent": "rain",
        "to_instance": "HASHI1", "to_agent": "lily", "originated_at": _make_utc_iso(),
        "body": {"text": "answer"}, "ttl": 5, "route_trace": ["HASHI2"]}
    first_code, first = await pm.handle_protocol_message(payload)
    assert first_code == 202 and first["accepted"] is True
    assert (await pm.handle_protocol_message(deepcopy(payload)))[0] == 200
    for field in ("text", "in_reply_to", "conversation_id"):
        changed = deepcopy(payload)
        if field == "text":
            changed["body"]["text"] = "different"
        else:
            changed[field] = "different"
        code, result = await pm.handle_protocol_message(changed)
        assert code == 409 and result["ok"] is False
    assert pm._enqueue_local_prompt.await_count == 1


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
        age_policy={"mode": "confirm"},
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
        "idempotency_key": f"hchat:{message_id}",
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
        "idempotency_key": "hchat:msg-reply-stale-001",
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


@pytest.mark.asyncio
async def test_protocol_delivery_status_updates_sender_and_never_invokes_agent(
    tmp_path: Path,
):
    from orchestrator.hchat_delivery_status import (
        delivery_status_message_id,
        encode_delivery_status,
    )
    from remote.protocol_manager import ProtocolManager

    original_message_id = "msg-status-original"
    status_message_id = delivery_status_message_id(
        original_message_id, "discarded"
    )
    pm = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI1", "platform": "windows"},
        peer_registry=None,
        workbench_port=18800,
    )
    pm._outbound_path = tmp_path / "protocol_outbound_hashi1.json"
    pm._save_outbound_correlations(
        {
            original_message_id: {
                "message_id": original_message_id,
                "conversation_id": "conv-status-1",
                "from_instance": "HASHI1",
                "from_agent": "lily",
                "to_instance": "HASHI2",
                "to_agent": "rain",
                "state": "awaiting_confirmation",
            }
        }
    )
    pm.get_local_agents_snapshot = lambda: [
        {"agent_name": "lily", "is_active": True}
    ]
    pm._enqueue_local_prompt = AsyncMock(
        return_value={"state": "displayed", "session_id": "ses-status"}
    )
    originated_at = _make_utc_iso()
    payload = {
        "message_type": "delivery_status",
        "message_id": status_message_id,
        "idempotency_key": f"hchat:{status_message_id}",
        "in_reply_to": original_message_id,
        "conversation_id": "conv-status-1",
        "from_instance": "HASHI2",
        "from_agent": "rain",
        "to_instance": "HASHI1",
        "to_agent": "lily",
        "originated_at": originated_at,
        "body": {
            "text": encode_delivery_status(
                original_message_id=original_message_id,
                state="discarded",
            ),
            "originated_at": originated_at,
        },
        "ttl": 5,
        "route_trace": ["HASHI2"],
    }

    code, response = await pm.handle_protocol_message(payload)

    assert code == 200
    assert response["state"] == "displayed"
    assert response["delivery_status"] == "discarded"
    outbound = pm._outbound_correlation_for(original_message_id)
    assert outbound["delivery_status"] == "discarded"
    pm._enqueue_local_prompt.assert_awaited_once()
    assert pm._enqueue_local_prompt.await_args.kwargs["exchange_kind"] == "status"


@pytest.mark.asyncio
async def test_hchat_reply_and_optional_features_use_live_support_instead_of_stale_flags():
    from remote.protocol_manager import ProtocolManager

    manager = object.__new__(ProtocolManager)
    manager._instance_info = {"instance_id": "HASHI1"}
    manager._shared_token = "test-secret"
    manager._max_allowed_ttl = 5
    peer = SimpleNamespace(properties={"live_status": "online"}, capabilities=[])
    manager._peer_registry = SimpleNamespace(get_peer=lambda _instance: peer)
    manager._resolve_peer_route = lambda _instance: {"host": "peer.invalid", "port": 8767}
    manager._candidate_urls = lambda host, port, path: [f"http://{host}:{port}{path}"]
    manager._record_route_probe = lambda *_args, **_kwargs: None
    calls = []
    live = {"capabilities": ["hchat_delivery_status_v1", "message_attachments_v1"]}

    def post(url, payload, timeout):
        calls.append((url, payload))
        if url.endswith("/protocol/handshake"):
            return {"status": "handshake_accept", "instance_id": "HASHI2", **live}
        return {"ok": True}

    manager._post_json = post
    item = {"message_id": "original", "conversation_id": "conversation", "from_instance": "HASHI2",
            "from_agent": "rain", "to_agent": "lily", "ttl": 5}
    # A completed text reply does not depend on optional freshness/status flags.
    assert await manager._send_agent_reply(item.copy(), "complete answer") is True
    assert calls[-1][1]["message_type"] == "agent_reply"

    # A negative cached status flag is refreshed from the authenticated peer.
    assert await manager._send_delivery_status(item.copy(), "discarded") is True
    assert calls[-2][0].endswith("/protocol/handshake")
    assert calls[-1][1]["message_type"] == "delivery_status"

    manager._send_agent_reply_attachments = AsyncMock(return_value=True)
    with_attachment = {**item, "reply_attachments": [{"attachment_id": "att-1"}]}
    assert await manager._send_agent_reply(with_attachment, "complete answer") is True
    manager._send_agent_reply_attachments.assert_awaited_once()

    # A live peer lacking attachment support must not silently drop the file.
    live["capabilities"] = []
    manager._send_agent_reply_attachments.reset_mock()
    unsupported = {**item, "reply_attachments": [{"attachment_id": "att-1"}]}
    assert await manager._send_agent_reply(unsupported, "complete answer") is False
    assert unsupported["reply_error_code"] == "attachment_capability_missing"
    manager._send_agent_reply_attachments.assert_not_awaited()
    status = item.copy()
    assert await manager._send_delivery_status(status, "discarded") is False
    assert status["delivery_status_error_code"] == "hchat_delivery_status_capability_missing"

    # A different signed instance is not evidence for the intended peer.
    manager._post_json = lambda *_args, **_kwargs: {
        "status": "handshake_accept", "instance_id": "HASHI9",
        "capabilities": ["message_attachments_v1"],
    }
    unknown = {**item, "reply_attachments": [{"attachment_id": "att-1"}]}
    assert await manager._send_agent_reply(unknown, "complete answer") is False
    assert unknown["reply_error_code"] == "attachment_capability_unverified"
    assert unknown.get("reply_failure_known") is not True
    manager._send_agent_reply_attachments.assert_not_awaited()
