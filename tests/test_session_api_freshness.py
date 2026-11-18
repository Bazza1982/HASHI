"""Tests for Session API message freshness gate and ingress confirmation endpoints.

Functional owner: PAO / Functions layer
Engineering layer: Functions
Verification: Red-green proof for Session API freshness gate and confirmation endpoints.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest

from orchestrator import workbench_api as workbench_api_module
from orchestrator.message_freshness import classify_message_freshness
from orchestrator.workbench_api import WorkbenchApiServer
from orchestrator.session_store import SessionConflict
from orchestrator.hchat_delivery_status import (
    decode_delivery_status,
    delivery_status_message_id,
    encode_delivery_status,
)
from orchestrator.message_context import (
    NETWORK_INGRESS_SCOPE_CLAIM_KEY,
    REMOTE_WORKBENCH_INGRESS_SCOPE,
    seal_connector_evidence,
)


class _MockRequest:
    def __init__(
        self,
        payload=None,
        *,
        query=None,
        match_info=None,
        headers=None,
        remote="127.0.0.1",
    ):
        self._payload = payload or {}
        self.query = query or {}
        self.match_info = match_info or {}
        self.headers = headers or {}
        self.remote = remote
        self.content_type = "application/json"

    async def json(self):
        return self._payload


class _MockRuntime:
    name = "lily"
    backend_ready = True

    def __init__(self, server=None):
        self.server = server
        self.enqueue_request_calls = 0
        self.replay_lookup_calls = 0
        self.last_enqueued_text = None
        self.last_idempotency_key = None
        self.last_request_metadata = None
        self.last_request_content = None

    def _primary_chat_id(self):
        return 123

    async def enqueue_request(
        self,
        _chat_id,
        prompt,
        source,
        _summary,
        *,
        idempotency_key,
        request_metadata,
        **kwargs,
    ):
        replay_only = bool(kwargs.get("replay_only", False))
        if replay_only:
            self.replay_lookup_calls += 1
        else:
            self.enqueue_request_calls += 1
        self.last_enqueued_text = prompt
        self.last_idempotency_key = idempotency_key
        self.last_request_metadata = dict(request_metadata)
        request_id = f"req-{self.enqueue_request_calls}"
        accepted = self.server.session_store.accept_run(
            session_id=request_metadata["session_id"],
            owner_id=request_metadata["owner_id"],
            agent_id=self.name,
            request_id=request_id,
            text=str(request_metadata.get("session_message_text", prompt)),
            source=source,
            idempotency_key=idempotency_key,
            content=request_metadata.get("session_message_content"),
            display_text=request_metadata.get("session_message_display_text"),
            execution_mode=request_metadata.get("execution_mode"),
            parent_run_id=request_metadata.get("parent_run_id"),
            response_preferences=request_metadata.get("response_preferences"),
            replay_only=replay_only,
        )
        return accepted.request_id

    async def enqueue_api_text(
        self,
        text,
        *,
        source,
        request_metadata,
        request_content=None,
        idempotency_key,
        **_kwargs,
    ):
        self.enqueue_request_calls += 1
        self.last_enqueued_text = text
        self.last_idempotency_key = idempotency_key
        self.last_request_metadata = dict(request_metadata)
        self.last_request_content = request_content
        request_id = f"req-{self.enqueue_request_calls}"
        accepted = self.server.session_store.accept_run(
            session_id=request_metadata["session_id"],
            owner_id=request_metadata["owner_id"],
            agent_id=self.name,
            request_id=request_id,
            text=text,
            source=source,
            idempotency_key=idempotency_key,
            content=request_metadata.get("session_message_content"),
        )
        return accepted.request_id


@pytest.fixture
def api_server(tmp_path: Path):
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps({"global": {}, "agents": [{"name": "lily"}]}),
        encoding="utf-8",
    )
    runtime = _MockRuntime()
    server = WorkbenchApiServer(
        config_path=config_path,
        global_config=SimpleNamespace(
            bridge_home=tmp_path,
            project_root=tmp_path,
            instance_id="HASHI3",
            authorized_id=7,
            workbench_port=18800,
            api_gateway_port=18801,
            deployment_profile="personal",
            session_message_age_policy={"mode": "confirm"},
        ),
        runtimes=[runtime],
    )
    runtime.server = server
    return server, runtime


@pytest.fixture
def session(api_server):
    server, _ = api_server
    return server.session_store.resolve_primary_session(
        owner_id="user:7",
        agent_id="lily",
        establish=True,
    )


@pytest.mark.asyncio
async def test_capabilities_advertises_message_freshness(api_server):
    server, _ = api_server
    req = _MockRequest()
    resp = await server.handle_v1_capabilities(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    assert "message_freshness" in data
    freshness_cap = data["message_freshness"]
    assert freshness_cap["capability"] == "message_freshness_v1"
    assert "originated_at" in freshness_cap["required_fields"]
    assert (
        freshness_cap["missing_required_field_error_code"]
        == "frontend_message_protocol_incompatible"
    )
    assert freshness_cap["pending_receipt_supported"] is True
    assert "confirm" in freshness_cap["decisions_supported"]
    assert "discard" in freshness_cap["decisions_supported"]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_mode", ["observe", "expire"])
async def test_authenticated_hchat_request_age_policy_is_applied_by_workbench(api_server, session, tmp_path, policy_mode):
    server, runtime = api_server
    server.global_config.hchat_request_age_policy = (
        {"mode": "expire", "max_age_seconds": 60} if policy_mode == "expire" else {"mode": "observe"}
    )
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "synthetic-hchat-token"}), encoding="utf-8")
    originated = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    context = {"from_agent": "rain", "from_instance": "HASHI2", "to_agent": "lily",
        "to_instance": "HASHI3", "message_id": "delayed-hchat", "originated_at": originated,
        "message_type": "agent_message", "conversation_id": "conversation-1",
        "network_authentication": "shared_network_hmac"}
    evidence = seal_connector_evidence(tmp_path, claims={
        "_message_source_reserved": "hchat", "_hchat_context": context,
    }, prompt="hello")
    response = await server.handle_chat(_MockRequest({
        "agent": "lily", "text": "hello", "source": "protocol:message",
        "message_id": "delayed-hchat", "idempotency_key": "hchat:delayed-hchat",
        "originated_at": originated, "message_type": "agent_message",
        "request_metadata": {"_connector_evidence": evidence},
    }))
    data = json.loads(response.text)
    if policy_mode == "observe":
        assert response.status == 200 and data["ok"] is True
        assert runtime.enqueue_request_calls == 1
        assert runtime.last_request_metadata["_frontend_message_age"]["reason"] == "stale"
    else:
        assert response.status == 409 and data["error_code"] == "hchat_message_expired"
        assert runtime.enqueue_request_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("originated_at", [None, "not-a-time"])
async def test_workbench_hchat_request_without_usable_time_is_not_blocked(api_server, session, originated_at):
    from unittest.mock import AsyncMock

    server, runtime = api_server
    runtime.enqueue_api_text = AsyncMock(return_value="req-hchat")
    response = await server.handle_hchat_exchange(_MockRequest({
        "to_agent": "lily", "to_instance": "HASHI3", "from_agent": "rain", "from_instance": "HASHI2",
        "text": "hello", "message_id": "missing-time", "idempotency_key": "hchat:missing-time",
        "conversation_id": "conversation-1", "message_type": "agent_message", "originated_at": originated_at,
    }))
    result = json.loads(response.text)
    assert response.status == 200 and result["state"] == "queued"
    metadata = runtime.enqueue_api_text.await_args.kwargs["request_metadata"]
    assert metadata["_frontend_message_age"]["reason"] in {"timestamp_missing", "timestamp_invalid"}


@pytest.mark.asyncio
async def test_hchat_delivery_status_is_displayed_without_agent_run(
    api_server, session, tmp_path: Path, monkeypatch
):
    from remote import protocol_outbound

    server, runtime = api_server
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {"hashi_remote_shared_token": "synthetic-status-shared-token"}
        ),
        encoding="utf-8",
    )
    original_message_id = "status-original-message"
    status_message_id = delivery_status_message_id(
        original_message_id, "confirmation_expired"
    )
    conversation_id = "status-conversation"
    outbound_path = tmp_path / "protocol-outbound.json"
    monkeypatch.setattr(
        protocol_outbound,
        "outbound_state_path",
        lambda _instance: outbound_path,
    )
    assert protocol_outbound.register_outbound_correlation(
        {
            "message_id": original_message_id,
            "conversation_id": conversation_id,
            "from_instance": "HASHI3",
            "from_agent": "lily",
            "to_instance": "HASHI2",
            "to_agent": "rain",
        },
        local_instance="HASHI3",
        state_path=outbound_path,
    )["ok"]
    text = encode_delivery_status(
        original_message_id=original_message_id,
        state="confirmation_expired",
        reason="confirmation_timeout_15m",
    )
    context = {
        "from_agent": "rain",
        "from_instance": "HASHI2",
        "to_agent": "lily",
        "to_instance": "HASHI3",
        "message_id": status_message_id,
        "originated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "message_type": "delivery_status",
        "conversation_id": conversation_id,
        "in_reply_to": original_message_id,
        "network_authentication": "shared_network_hmac",
    }
    evidence = seal_connector_evidence(
        tmp_path,
        claims={
            "_message_source_reserved": "hchat",
            "_hchat_context": context,
        },
        prompt=text,
    )

    response = await server.handle_chat(
        _MockRequest(
            {
                "agent": "lily",
                "text": text,
                "source": "protocol:status",
                "message_id": status_message_id,
                "idempotency_key": f"hchat:{status_message_id}",
                "originated_at": context["originated_at"],
                "message_type": "delivery_status",
                "request_metadata": {"_connector_evidence": evidence},
            }
        )
    )

    data = json.loads(response.text)
    assert response.status == 200, data
    assert data["state"] == "displayed"
    assert data["delivery_status"] == "confirmation_expired"
    assert runtime.enqueue_request_calls == 0
    visible = server.session_store.recent_visible_messages(
        session["session_id"], owner_id="user:7"
    )
    assert visible[-1]["source"] == "hchat-status"
    assert "15 minutes" in visible[-1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["user:7", "user:8"])
async def test_delayed_hchat_reply_stays_in_original_bound_session(api_server, session, tmp_path, monkeypatch, owner):
    import asyncio

    from orchestrator.hchat_origin_binding import bind_outbound_origin
    from remote import protocol_outbound
    from remote.protocol_manager import ProtocolManager

    server, runtime = api_server
    store = server.session_store
    if owner != "user:7":
        session = store.create_session(owner_id=owner, agent_id="lily")
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "synthetic-hchat-token"}), encoding="utf-8",
    )
    run = store.accept_run(
        session_id=session["session_id"], owner_id=owner, agent_id="lily",
        request_id="req-outbound", text="send to rain", source="test", idempotency_key="outbound-run",
    )
    fence = store.mark_request_running(run.request_id, worker_id="test-worker")
    bind_outbound_origin(
        store, message_id="outbound-message", owner_id=owner, agent_id="lily",
        session_id=session["session_id"], run_id=run.run_id, request_id=run.request_id,
        context_generation=1, fencing_token=fence, target="rain@HASHI2",
    )
    outbound_path = tmp_path / "outbound.json"
    monkeypatch.setattr(protocol_outbound, "outbound_state_path", lambda _instance: outbound_path)
    assert protocol_outbound.register_outbound_correlation({
        "message_id": "outbound-message", "conversation_id": "conv-1", "from_instance": "HASHI3",
        "from_agent": "lily", "to_instance": "HASHI2", "to_agent": "rain",
    }, local_instance="HASHI3", state_path=outbound_path)["ok"]
    other = store.create_session(owner_id=owner, agent_id="lily")
    store.bind_primary_session(owner_id=owner, agent_id="lily", session_id=other["session_id"])
    originated = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    context = {"from_agent": "rain", "from_instance": "HASHI2", "to_agent": "lily",
               "to_instance": "HASHI3", "message_id": "reply-1", "originated_at": originated,
               "message_type": "agent_reply", "conversation_id": "conv-1",
               "in_reply_to": "outbound-message", "network_authentication": "shared_network_hmac",
               "authenticated_peer": "HASHI2"}

    def request_for(verified_context, text="delayed answer"):
        evidence = seal_connector_evidence(tmp_path, claims={
            "_message_source_reserved": "hchat", "_hchat_context": verified_context,
        }, prompt=text)
        return _MockRequest({"agent": "lily", "text": text, "source": "protocol:reply",
            "message_id": "reply-1", "idempotency_key": "hchat:reply-1", "originated_at": originated,
            "message_type": "agent_reply", "request_metadata": {"_connector_evidence": evidence}})

    # Exercise ProtocolManager's actual envelope/evidence path into Workbench's
    # real handler; only the local HTTP hop is replaced with an in-process call.
    manager = ProtocolManager(
        hashi_root=tmp_path, instance_info={"instance_id": "HASHI3"},
        peer_registry=None, workbench_port=18800,
    )
    manager._outbound_path = outbound_path
    manager._frontend_session_store = lambda: store
    manager.get_local_agents_snapshot = lambda: [{"agent_name": "lily", "is_active": True}]
    manager._local_workbench_routes = lambda: [("127.0.0.1", 18800)]
    manager._probe_local_workbench = lambda _host, _port: True
    loop = asyncio.get_running_loop()

    def local_http(_url, payload, timeout=10):
        future = asyncio.run_coroutine_threadsafe(server.handle_chat(_MockRequest(payload)), loop)
        response = future.result(timeout)
        body = json.loads(response.text)
        if response.status >= 400:
            body["__http_status"] = response.status
        return body

    manager._post_json = local_http
    protocol_payload = {"message_type": "agent_reply", "message_id": "reply-1",
        "idempotency_key": "hchat:reply-1", "in_reply_to": "outbound-message", "conversation_id": "conv-1",
        "from_instance": "HASHI2", "from_agent": "rain", "to_instance": "HASHI3", "to_agent": "lily",
        "originated_at": originated, "body": {"text": "delayed answer"}, "ttl": 5, "route_trace": ["HASHI2"],
        "_network_authenticated_instance": "HASHI2", "_network_authentication": "shared_network_hmac"}
    status, receipt = await manager.handle_protocol_message(protocol_payload)
    assert status == 202 and receipt["session_id"] == session["session_id"]
    assert receipt["state"] == "reply_delivered_locally"
    status, again = await manager.handle_protocol_message(protocol_payload)
    assert status == 200 and again["state"] == "reply_delivered_locally"
    from unittest.mock import AsyncMock

    resumed = ProtocolManager(
        hashi_root=tmp_path, instance_info={"instance_id": "HASHI3"},
        peer_registry=None, workbench_port=18800,
    )
    resumed._outbound_path = outbound_path
    resumed._frontend_session_store = lambda: store
    resumed.get_local_agents_snapshot = manager.get_local_agents_snapshot
    resumed._enqueue_local_prompt = AsyncMock(side_effect=AssertionError("must not republish after restart"))
    status, again = await resumed.handle_protocol_message(protocol_payload)
    assert status == 200 and again["state"] == "reply_delivered_locally"
    resumed._enqueue_local_prompt.assert_not_awaited()
    changed_payload = {**protocol_payload, "body": {"text": "different answer"}}
    status, conflict = await resumed.handle_protocol_message(changed_payload)
    assert status == 409 and conflict["body"]["code"] == "idempotency_conflict"

    first = await server.handle_chat(request_for(context))
    repeated = await server.handle_chat(request_for(context))
    assert first.status == repeated.status == 200
    assert json.loads(first.text)["session_id"] == session["session_id"]
    visible = store.recent_visible_messages(session["session_id"], owner_id=owner)
    assert sum(item["source"] == "hchat-reply" for item in visible) == 1
    assert visible[-1]["message_context"]["message_age"]["reason"] == "stale"
    assert not store.recent_visible_messages(other["session_id"], owner_id=owner)
    assert runtime.enqueue_request_calls == 0
    wrong = await server.handle_chat(request_for({**context, "from_agent": "other"}))
    assert wrong.status == 409
    assert json.loads(wrong.text)["error_code"] == "reply_correlation_mismatch"
    changed = await server.handle_chat(request_for(context, "different answer"))
    assert changed.status == 409
    assert json.loads(changed.text)["error_code"] == "hchat_reply_conflict"
    unverified = await server.handle_chat(request_for({**context, "network_authentication": "not_verified"}))
    assert unverified.status == 401
    forged_id = request_for(context)
    forged_id._payload.update(message_id="reply-2", idempotency_key="hchat:reply-2")
    assert (await server.handle_chat(forged_id)).status == 409


@pytest.mark.asyncio
async def test_hchat_confirmation_terminal_status_is_returned_once_to_sender(
    api_server, session, monkeypatch
):
    from tools import hchat_send

    server, _runtime = api_server
    now = datetime.now(timezone.utc)
    confirmation = server.ingress_confirmation.register_pending(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="lily",
        connector_id="hchat_workbench",
        transport="workbench_api",
        source_message_id="legacy-status-original",
        idempotency_key="hchat:legacy-status-original",
        payload_digest="a" * 64,
        message_kind="hchat_request",
        context_generation=session["context_generation"],
        originated_at=(now - timedelta(seconds=90)),
        received_at=now,
        threshold_seconds=60,
        reason="stale",
        delay_seconds=90,
        expires_at=(now + timedelta(minutes=15)),
        payload={
            "runtime_payload": {"text": "do not execute"},
            "status_notification": {
                "to_agent": "rain",
                "target_instance": "HASHI2",
                "from_agent": "lily",
                "source_instance": "HASHI3",
                "conversation_id": "conversation-status",
            },
        },
        display_metadata={},
    )
    discarded = server.ingress_confirmation.decide(
        confirmation_id=confirmation["confirmation_id"],
        decision="discard",
        owner_id="user:7",
    )
    calls = []

    def fake_send_hchat(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(hchat_send, "send_hchat", fake_send_hchat)

    assert await server._notify_hchat_confirmation_status(discarded) is True
    refreshed = server.ingress_confirmation.get(
        confirmation["confirmation_id"], owner_id="user:7"
    )
    assert await server._notify_hchat_confirmation_status(refreshed) is True

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "rain"
    assert args[1] == "lily"
    assert args[3] == "HASHI2"
    assert decode_delivery_status(args[2])["state"] == "discarded"
    assert kwargs["message_type"] == "delivery_status"
    assert kwargs["in_reply_to"] == "legacy-status-original"
    assert kwargs["idempotency_key"].startswith("hchat:hstatus-")


@pytest.mark.asyncio
async def test_hchat_sender_receives_waiting_then_terminal_status(
    api_server, session, monkeypatch
):
    from tools import hchat_send

    server, _runtime = api_server
    now = datetime.now(timezone.utc)
    confirmation = server.ingress_confirmation.register_pending(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="lily",
        connector_id="hchat_workbench",
        transport="workbench_api",
        source_message_id="status-lifecycle-original",
        idempotency_key="hchat:status-lifecycle-original",
        payload_digest="b" * 64,
        message_kind="hchat_request",
        context_generation=session["context_generation"],
        originated_at=(now - timedelta(seconds=90)),
        received_at=now,
        threshold_seconds=60,
        reason="stale",
        delay_seconds=90,
        expires_at=(now + timedelta(minutes=15)),
        payload={
            "runtime_payload": {"text": "execute only after confirmation"},
            "status_notification": {
                "to_agent": "rain",
                "target_instance": "HASHI2",
                "from_agent": "lily",
                "source_instance": "HASHI3",
                "conversation_id": "conversation-lifecycle",
            },
        },
        display_metadata={},
    )
    calls = []
    monkeypatch.setattr(
        hchat_send,
        "send_hchat",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )

    assert await server._notify_hchat_confirmation_status(confirmation) is True
    discarded = server.ingress_confirmation.decide(
        confirmation_id=confirmation["confirmation_id"],
        decision="discard",
        owner_id="user:7",
    )
    assert await server._notify_hchat_confirmation_status(discarded) is True
    refreshed = server.ingress_confirmation.get(
        confirmation["confirmation_id"], owner_id="user:7"
    )
    assert await server._notify_hchat_confirmation_status(refreshed) is True

    assert [decode_delivery_status(args[2])["state"] for args, _ in calls] == [
        "awaiting_confirmation",
        "discarded",
    ]
    assert refreshed["display_metadata"]["sender_delivery_status_complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp_kind", ["old", "missing", "invalid", "future"])
async def test_default_session_messages_do_not_require_age_consent(
    api_server, session, timestamp_kind
):
    server, runtime = api_server
    # An empty policy is the instance default; the existing fixture explicitly
    # selects legacy confirmation for tests of the optional policy below.
    server.global_config.session_message_age_policy = {}
    now = datetime.now(timezone.utc)
    values = {
        "old": (now - timedelta(minutes=20)).isoformat(),
        "missing": None,
        "invalid": "2026-11-17T11:00:00",
        "future": (now + timedelta(minutes=2)).isoformat(),
    }
    payload = {
        "idempotency_key": f"default-age-{timestamp_kind}",
        "message": {"content": [{"type": "text", "text": "ordinary message"}]},
    }
    if values[timestamp_kind] is not None:
        payload["originated_at"] = values[timestamp_kind]
    response = await server.handle_v1_session_runs_create(_MockRequest(
        payload, match_info={"session_id": session["session_id"]},
    ))
    data = json.loads(response.text)
    assert response.status == 202, data
    assert data.get("run_id") and not data.get("confirmation_required")
    assert runtime.enqueue_request_calls == 1
    assert not server.ingress_confirmation.list(
        session_id=session["session_id"], owner_id="user:7"
    )
    age = data["message_age"]
    if timestamp_kind in {"missing", "invalid"}:
        assert age["originated_at"] is None
        assert age["age_seconds"] is None
    else:
        assert age["originated_at"] is not None
        assert isinstance(age["age_seconds"], (int, float))
    assert runtime.last_request_metadata["_frontend_message_age"] == age


@pytest.mark.asyncio
async def test_default_age_capability_does_not_require_sender_timestamp(api_server):
    server, _ = api_server
    server.global_config.session_message_age_policy = {}
    response = await server.handle_v1_capabilities(_MockRequest())
    capability = json.loads(response.text)["message_freshness"]
    assert capability["required_fields"] == []
    assert capability["session_api_policy"]["mode"] == "observe"
    assert json.loads(response.text)["session_message_age"] == {
        "version": "1.0", "mode": "observe", "max_age_seconds": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("originated_at", [None, "2026-11-17T11:00:00"])
async def test_selected_expiry_refuses_unknown_time_without_fabrication(
    api_server, session, originated_at
):
    server, runtime = api_server
    server.global_config.session_message_age_policy = {"mode": "expire", "max_age_seconds": 60}
    response = await server.handle_v1_session_runs_create(_MockRequest(
        {"idempotency_key": f"expiry-{originated_at}", "originated_at": originated_at,
         "message": {"content": [{"type": "text", "text": "expiring message"}]}},
        match_info={"session_id": session["session_id"]},
    ))
    data = json.loads(response.text)
    assert response.status == 409, data
    assert data["code"] == "message_age_expired"
    assert data["message_age"]["age_seconds"] is None
    assert runtime.enqueue_request_calls == 0


@pytest.mark.asyncio
async def test_selected_expiry_enforces_its_actual_age_limit(api_server, session):
    server, runtime = api_server
    server.global_config.session_message_age_policy = {"mode": "expire", "max_age_seconds": 60}
    now = datetime.now(timezone.utc)
    for age, expected_status in [(120, 409), (10, 202)]:
        response = await server.handle_v1_session_runs_create(_MockRequest(
            {"idempotency_key": f"expiry-age-{age}",
             "originated_at": (now - timedelta(seconds=age)).isoformat(),
             "message": {"content": [{"type": "text", "text": "expiring action"}]}},
            match_info={"session_id": session["session_id"]},
        ))
        assert response.status == expected_status, json.loads(response.text)
    assert runtime.enqueue_request_calls == 1


@pytest.mark.asyncio
async def test_expired_retry_returns_only_a_matching_previously_accepted_run(api_server, session):
    server, runtime = api_server
    server.global_config.session_message_age_policy = {"mode": "expire", "max_age_seconds": 120}
    payload = {
        "idempotency_key": "expiring-idempotent-request",
        "originated_at": (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(),
        "message": {"content": [{"type": "text", "text": "do this once"}]},
    }

    async def send(body):
        return await server.handle_v1_session_runs_create(_MockRequest(
            body, match_info={"session_id": session["session_id"]},
        ))

    first = await send(payload)
    assert first.status == 202, first.text
    original = json.loads(first.text)
    server.global_config.session_message_age_policy = {"mode": "expire", "max_age_seconds": 30}
    retry = await send(payload)
    assert retry.status == 202, retry.text
    assert json.loads(retry.text)["run_id"] == original["run_id"]
    assert json.loads(retry.text)["request_id"] == original["request_id"]

    changed = dict(payload, message={"content": [{"type": "text", "text": "do something else"}]})
    conflict = await send(changed)
    assert conflict.status == 409, conflict.text
    assert json.loads(conflict.text)["code"] == "idempotency_conflict"
    missing = await send(dict(payload, idempotency_key="never-accepted"))
    assert missing.status == 409 and json.loads(missing.text)["code"] == "message_age_expired"
    assert runtime.enqueue_request_calls == 1
    assert runtime.replay_lookup_calls == 2
    assert server.session_store.find_run_by_idempotency(
        session_id=session["session_id"], owner_id="user:7", idempotency_key="never-accepted"
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_error,expected_code", [
    ("IdempotencyConflict", "idempotency_conflict"),
    ("ExistingRunNotFound", "message_age_expired"),
])
async def test_expired_retry_maps_typed_worker_receipt_errors(api_server, session, worker_error, expected_code):
    from orchestrator.function_worker_protocol import FunctionWorkerRemoteError

    server, runtime = api_server
    server.global_config.session_message_age_policy = {"mode": "expire", "max_age_seconds": 120}
    payload = {
        "idempotency_key": "worker-replay-error",
        "originated_at": (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(),
        "message": {"content": [{"type": "text", "text": "once"}]},
    }
    def request():
        return _MockRequest(payload, match_info={"session_id": session["session_id"]})
    assert (await server.handle_v1_session_runs_create(request())).status == 202
    server.global_config.session_message_age_policy = {"mode": "expire", "max_age_seconds": 30}

    async def reject(*args, **kwargs):
        assert kwargs["replay_only"] is True
        raise FunctionWorkerRemoteError("runtime.enqueue_request", {"type": worker_error})

    runtime.enqueue_request = reject
    result = await server.handle_v1_session_runs_create(request())
    assert result.status == 409 and json.loads(result.text)["code"] == expected_code


@pytest.mark.asyncio
async def test_session_runs_missing_originated_at_rejects_incompatible_frontend(api_server, session):
    server, runtime = api_server
    req = _MockRequest(
        {
            "idempotency_key": "idemp-no-originated",
            "message": {"content": [{"type": "text", "text": "hello without timestamp"}]},
        },
        match_info={"session_id": session["session_id"]},
    )
    resp = await server.handle_v1_session_runs_create(req)
    assert resp.status == 409
    data = json.loads(resp.text)
    assert data.get("accepted") is False
    assert data.get("code") == "frontend_message_protocol_incompatible"
    assert data.get("freshness_reason") == "timestamp_missing"
    assert resp.headers["Cache-Control"] == "no-store"
    assert "run_id" not in data
    assert runtime.enqueue_request_calls == 0
    assert server.ingress_confirmation.list(
        session_id=session["session_id"], owner_id="user:7"
    ) == []


@pytest.mark.asyncio
async def test_session_runs_stale_originated_at_requires_confirmation(api_server, session):
    server, runtime = api_server
    # 40 seconds ago (local threshold is 30s)
    originated = (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat().replace("+00:00", "Z")
    req = _MockRequest(
        {
            "idempotency_key": "idemp-stale-msg",
            "originated_at": originated,
            "message": {"content": [{"type": "text", "text": "stale message"}]},
        },
        match_info={"session_id": session["session_id"]},
        remote="127.0.0.1",
    )
    resp = await server.handle_v1_session_runs_create(req)
    assert resp.status == 202
    data = json.loads(resp.text)
    assert data.get("confirmation_required") is True
    assert data.get("accepted") is False
    assert data["confirmation"]["reason"] == "stale"
    assert runtime.enqueue_request_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {
            "display_text": 42,
            "content": [{"type": "text", "text": "invalid display text"}],
        },
        {
            "content": [
                {
                    "type": "attachment",
                    "attachment_id": "att-does-not-exist",
                }
            ]
        },
        {"content": [{"type": "unsupported", "text": "invalid part"}]},
    ],
)
async def test_stale_invalid_payload_is_rejected_before_confirmation(
    api_server, session, message
):
    server, runtime = api_server
    originated = (
        datetime.now(timezone.utc) - timedelta(seconds=40)
    ).isoformat().replace("+00:00", "Z")
    req = _MockRequest(
        {
            "idempotency_key": "idemp-stale-invalid",
            "originated_at": originated,
            "message": message,
        },
        match_info={"session_id": session["session_id"]},
        remote="127.0.0.1",
    )

    resp = await server.handle_v1_session_runs_create(req)

    assert resp.status >= 400
    assert runtime.enqueue_request_calls == 0
    assert server.ingress_confirmation.list(
        session_id=session["session_id"], owner_id="user:7"
    ) == []


def _stage_committed_attachment(
    server,
    session,
    *,
    payload: bytes,
    filename: str,
    media_type: str,
    semantic_role: str | None = None,
):
    staged = server.session_store.stage_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        filename=filename,
        media_type=media_type,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        semantic_role=semantic_role,
    )
    server.session_store.upload_attachment_bytes(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
        payload=payload,
    )
    server.session_store.commit_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
    )
    return staged["attachment_id"]


@pytest.mark.asyncio
async def test_stale_committed_attachment_waits_then_executes_once(
    api_server, session
):
    server, runtime = api_server
    attachment_id = _stage_committed_attachment(
        server,
        session,
        payload=b"one",
        filename="evidence.txt",
        media_type="text/plain",
    )
    originated = (
        datetime.now(timezone.utc) - timedelta(seconds=40)
    ).isoformat().replace("+00:00", "Z")
    create = await server.handle_v1_session_runs_create(
        _MockRequest(
            {
                "idempotency_key": "idemp-stale-attachment",
                "originated_at": originated,
                "message": {
                    "content": [
                        {
                            "type": "attachment",
                            "attachment_id": attachment_id,
                        }
                    ]
                },
            },
            match_info={"session_id": session["session_id"]},
        )
    )
    created = json.loads(create.text)
    assert create.status == 202
    assert created["confirmation_required"] is True
    assert created["confirmation"]["display_metadata"]["attachment_names"] == [
        "evidence.txt"
    ]
    assert runtime.enqueue_request_calls == 0

    confirmation_id = created["confirmation"]["confirmation_id"]
    decide = _MockRequest(
        {"decision": "confirm"},
        match_info={
            "session_id": session["session_id"],
            "confirmation_id": confirmation_id,
        },
    )
    first = await server.handle_v1_session_ingress_confirmation_decide(decide)
    second = await server.handle_v1_session_ingress_confirmation_decide(decide)

    assert first.status == 200, first.text
    assert second.status == 200, second.text
    assert json.loads(first.text)["state"] == "confirmed"
    assert json.loads(second.text)["run_id"] == json.loads(first.text)["run_id"]
    assert runtime.enqueue_request_calls == 1
    assert runtime.last_request_metadata["session_message_content"] == [
        {"type": "attachment", "attachment_id": attachment_id}
    ]


@pytest.mark.asyncio
async def test_confirmed_stale_protocol_attachment_uses_primary_session_once(
    api_server, session, tmp_path: Path
):
    from remote.protocol_manager import ProtocolManager

    server, runtime = api_server
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "synthetic-protocol-secret"}),
        encoding="utf-8",
    )
    payload_bytes = b"verified delayed protocol attachment"
    source_dir = (
        tmp_path
        / "state"
        / "remote_attachments"
        / "hashi3"
        / "messages"
        / "protocol-attachment-confirm"
    )
    source_dir.mkdir(parents=True)
    source_path = source_dir / "evidence.txt"
    source_path.write_bytes(payload_bytes)
    attachment = {
        "attachment_id": "remote-att-confirm-1",
        "filename": "evidence.txt",
        "mime_type": "text/plain",
        "size_bytes": len(payload_bytes),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "stored_path": str(source_path),
    }
    message_id = "protocol-attachment-confirm"
    protocol = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI3", "platform": "windows"},
        peer_registry=None,
        workbench_port=18800,
    )
    protocol._inflight = {}
    protocol.get_local_agents_snapshot = lambda: [
        {"agent_name": "lily", "is_active": True}
    ]
    protocol._frontend_session_store = lambda: server.session_store
    protocol._hchat_request_age_policy = lambda: {"mode": "confirm"}
    protocol._enqueue_local_prompt = pytest.fail
    code, receipt = await protocol.handle_protocol_message(
        {
            "message_type": "agent_message",
            "message_id": message_id,
            "idempotency_key": f"hchat:{message_id}",
            "originated_at": (
                datetime.now(timezone.utc) - timedelta(seconds=90)
            ).isoformat().replace("+00:00", "Z"),
            "conversation_id": "conv-protocol-attachment-confirm",
            "from_instance": "HASHI2",
            "from_agent": "rain",
            "to_instance": "HASHI3",
            "to_agent": "lily",
            "body": {"text": "inspect attached evidence", "attachments": [attachment]},
            "ttl": 8,
            "route_trace": ["HASHI2"],
            "_local_attachment_manifest_verified": True,
            "_network_authenticated_instance": "HASHI2",
            "_network_authentication": "shared_network_hmac",
        }
    )
    assert code == 202
    assert receipt["confirmation_required"] is True
    assert runtime.enqueue_request_calls == 0

    decide_request = _MockRequest(
        {"decision": "confirm"},
        match_info={
            "session_id": session["session_id"],
            "confirmation_id": receipt["confirmation_id"],
        },
    )
    first = await server.handle_v1_session_ingress_confirmation_decide(
        decide_request
    )
    second = await server.handle_v1_session_ingress_confirmation_decide(
        decide_request
    )

    first_data = json.loads(first.text)
    second_data = json.loads(second.text)
    assert first.status == 200, first_data
    assert second.status == 200, second_data
    assert first_data["state"] == "confirmed"
    assert second_data["run_id"] == first_data["run_id"]
    assert runtime.enqueue_request_calls == 1
    assert runtime.last_request_metadata["session_id"] == session["session_id"]
    assert runtime.last_idempotency_key == f"hchat:{message_id}"
    attachment_parts = [
        item
        for item in runtime.last_request_content["parts"]
        if item.get("type") == "media"
    ]
    assert len(attachment_parts) == 1
    stored, stored_bytes = server.session_store.attachment_bytes(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=attachment_parts[0]["attachment_id"],
    )
    assert stored["filename"] == "evidence.txt"
    assert stored_bytes == payload_bytes


@pytest.mark.asyncio
async def test_pending_confirmation_reserves_attachment_until_terminal(
    api_server, session
):
    server, runtime = api_server
    attachment_id = _stage_committed_attachment(
        server,
        session,
        payload=b"keep while pending",
        filename="pending.txt",
        media_type="text/plain",
    )
    originated = (
        datetime.now(timezone.utc) - timedelta(seconds=40)
    ).isoformat().replace("+00:00", "Z")
    create = await server.handle_v1_session_runs_create(
        _MockRequest(
            {
                "idempotency_key": "idemp-pending-attachment-reservation",
                "originated_at": originated,
                "message": {
                    "content": [
                        {
                            "type": "attachment",
                            "attachment_id": attachment_id,
                        }
                    ]
                },
            },
            match_info={"session_id": session["session_id"]},
        )
    )
    created = json.loads(create.text)
    assert created["confirmation_required"] is True
    with pytest.raises(SessionConflict, match="message-bound"):
        server.session_store.discard_unbound_attachments(
            session_id=session["session_id"],
            owner_id="user:7",
            attachment_ids=[attachment_id],
        )
    assert runtime.enqueue_request_calls == 0

    discard = await server.handle_v1_session_ingress_confirmation_decide(
        _MockRequest(
            {"decision": "discard"},
            match_info={
                "session_id": session["session_id"],
                "confirmation_id": created["confirmation"]["confirmation_id"],
            },
        )
    )
    assert json.loads(discard.text)["state"] == "discarded"
    assert server.session_store.discard_unbound_attachments(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_ids=[attachment_id],
    ) == [attachment_id]
    assert runtime.enqueue_request_calls == 0


@pytest.mark.asyncio
async def test_stale_voice_attachment_does_not_start_transcription_before_confirmation(
    api_server, session, monkeypatch
):
    server, runtime = api_server
    monkeypatch.setattr(server, "_native_audio_chat_v1_ready", lambda: True)
    transcription_calls = []

    def _unexpected_transcription(**kwargs):
        transcription_calls.append(kwargs)
        raise AssertionError("stale voice was transcribed before confirmation")

    monkeypatch.setattr(
        server, "_begin_session_voice_transcription", _unexpected_transcription
    )
    attachment_id = _stage_committed_attachment(
        server,
        session,
        payload=b"OggS-delayed-voice",
        filename="delayed.ogg",
        media_type="audio/ogg",
        semantic_role="voice_message",
    )
    originated = (
        datetime.now(timezone.utc) - timedelta(seconds=40)
    ).isoformat().replace("+00:00", "Z")

    response = await server.handle_v1_session_runs_create(
        _MockRequest(
            {
                "idempotency_key": "idemp-stale-voice",
                "originated_at": originated,
                "message": {
                    "content": [
                        {
                            "type": "audio",
                            "attachment_id": attachment_id,
                            "semantic_role": "voice_message",
                        }
                    ]
                },
            },
            match_info={"session_id": session["session_id"]},
        )
    )

    assert response.status == 202
    assert json.loads(response.text)["confirmation_required"] is True
    assert transcription_calls == []
    assert runtime.enqueue_request_calls == 0


@pytest.mark.asyncio
async def test_session_runs_fresh_originated_at_executes_immediately(api_server, session):
    server, runtime = api_server
    # 5 seconds ago (fresh for both local 30s and remote 60s)
    originated = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    req = _MockRequest(
        {
            "idempotency_key": "idemp-fresh-msg",
            "originated_at": originated,
            "message": {"content": [{"type": "text", "text": "fresh message"}]},
        },
        match_info={"session_id": session["session_id"]},
        remote="127.0.0.1",
    )
    resp = await server.handle_v1_session_runs_create(req)
    assert resp.status == 202
    data = json.loads(resp.text)
    assert data.get("ok") is True
    assert data.get("confirmation_required") is not True
    assert "run_id" in data
    assert runtime.enqueue_request_calls == 1
    assert runtime.last_enqueued_text == "fresh message"


@pytest.mark.asyncio
async def test_virtual_120s_network_delay_is_quarantined_replayed_and_executed_once(
    api_server, session, monkeypatch
):
    server, runtime = api_server
    originated = datetime.now(timezone.utc)
    virtual_arrival = originated + timedelta(seconds=120)

    def classify_at_virtual_arrival(*args, **kwargs):
        kwargs["received_at"] = virtual_arrival
        return classify_message_freshness(*args, **kwargs)

    monkeypatch.setattr(
        workbench_api_module,
        "classify_freshness",
        classify_at_virtual_arrival,
    )
    payload = {
        "idempotency_key": "virtual-network-delay-key",
        "originated_at": originated.isoformat().replace("+00:00", "Z"),
        "message": {
            "client_message_id": "virtual-network-delay-source",
            "content": [
                {"type": "text", "text": "execute after virtual delay review"}
            ],
        },
    }
    request = _MockRequest(
        payload,
        match_info={"session_id": session["session_id"]},
        remote="127.0.0.1",
    )

    first = await server.handle_v1_session_runs_create(request)
    replay = await server.handle_v1_session_runs_create(request)
    first_data = json.loads(first.text)
    replay_data = json.loads(replay.text)
    assert first.status == 202
    assert first_data["state"] == "awaiting_confirmation"
    assert first_data["confirmation"]["reason"] == "stale"
    assert first_data["confirmation"]["delay_seconds"] == 120.0
    assert first_data["confirmation"]["threshold_seconds"] == 30
    assert replay_data["confirmation"]["confirmation_id"] == (
        first_data["confirmation"]["confirmation_id"]
    )
    assert runtime.enqueue_request_calls == 0

    confirmation_id = first_data["confirmation"]["confirmation_id"]
    decision = _MockRequest(
        {"decision": "confirm"},
        match_info={
            "session_id": session["session_id"],
            "confirmation_id": confirmation_id,
        },
    )
    confirmed = await server.handle_v1_session_ingress_confirmation_decide(decision)
    duplicate = await server.handle_v1_session_ingress_confirmation_decide(decision)
    confirmed_data = json.loads(confirmed.text)
    duplicate_data = json.loads(duplicate.text)
    assert confirmed_data["state"] == "confirmed"
    assert duplicate_data["run_id"] == confirmed_data["run_id"]
    assert runtime.enqueue_request_calls == 1


@pytest.mark.asyncio
async def test_session_runs_remote_workbench_threshold_60s(api_server, session):
    server, runtime = api_server
    # 45 seconds ago on remote connection (remote threshold is 60s -> fresh!)
    originated = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat().replace("+00:00", "Z")
    req = _MockRequest(
        {
            "idempotency_key": "idemp-remote-45s",
            "originated_at": originated,
            "message": {"content": [{"type": "text", "text": "remote 45s message"}]},
        },
        match_info={"session_id": session["session_id"]},
        remote="192.168.1.100",  # non-loopback
    )
    resp = await server.handle_v1_session_runs_create(req)
    assert resp.status == 202
    data = json.loads(resp.text)
    assert "run_id" in data
    assert runtime.enqueue_request_calls == 1

    # 70 seconds ago on remote connection (exceeds 60s -> stale!)
    originated_70s = (datetime.now(timezone.utc) - timedelta(seconds=70)).isoformat().replace("+00:00", "Z")
    req_stale = _MockRequest(
        {
            "idempotency_key": "idemp-remote-70s",
            "originated_at": originated_70s,
            "message": {"content": [{"type": "text", "text": "remote 70s message"}]},
        },
        match_info={"session_id": session["session_id"]},
        remote="192.168.1.100",
    )
    resp_stale = await server.handle_v1_session_runs_create(req_stale)
    assert resp_stale.status == 202
    data_stale = json.loads(resp_stale.text)
    assert data_stale.get("confirmation_required") is True
    assert runtime.enqueue_request_calls == 1  # not incremented


@pytest.mark.asyncio
async def test_remote_gateway_evidence_retains_60s_window_on_loopback_only_when_signed(
    api_server, session
):
    server, runtime = api_server
    root = server.config_path.parent
    (root / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "remote-workbench-secret"}),
        encoding="utf-8",
    )
    prompt = "remote through gateway"
    originated_45s = (
        datetime.now(timezone.utc) - timedelta(seconds=45)
    ).isoformat().replace("+00:00", "Z")

    # A caller-owned marker cannot turn a local 30-second request into remote.
    forged = _MockRequest(
        {
            "idempotency_key": "remote-gateway-forged",
            "originated_at": originated_45s,
            "message": {"content": [{"type": "text", "text": prompt}]},
            "request_metadata": {
                "_connector_evidence": {
                    "claims": {
                        NETWORK_INGRESS_SCOPE_CLAIM_KEY:
                            REMOTE_WORKBENCH_INGRESS_SCOPE,
                    }
                }
            },
        },
        match_info={"session_id": session["session_id"]},
        remote="127.0.0.1",
    )
    forged_response = await server.handle_v1_session_runs_create(forged)
    forged_data = json.loads(forged_response.text)
    assert forged_data["confirmation_required"] is True
    assert forged_data["confirmation"]["threshold_seconds"] == 30
    assert runtime.enqueue_request_calls == 0

    evidence = seal_connector_evidence(
        root,
        claims={
            NETWORK_INGRESS_SCOPE_CLAIM_KEY: REMOTE_WORKBENCH_INGRESS_SCOPE,
            "_origin_instance_evidence": {
                "id": "WORKBENCH",
                "assurance": "shared_network_hmac",
            },
        },
        prompt=prompt,
    )
    trusted = _MockRequest(
        {
            "idempotency_key": "remote-gateway-trusted",
            "originated_at": originated_45s,
            "message": {"content": [{"type": "text", "text": prompt}]},
            "request_metadata": {"_connector_evidence": evidence},
        },
        match_info={"session_id": session["session_id"]},
        remote="127.0.0.1",
    )
    trusted_response = await server.handle_v1_session_runs_create(trusted)
    trusted_data = json.loads(trusted_response.text)
    assert trusted_data.get("confirmation_required") is not True
    assert "run_id" in trusted_data
    assert runtime.enqueue_request_calls == 1

    stale_prompt = "remote delayed too long"
    stale_evidence = seal_connector_evidence(
        root,
        claims={
            NETWORK_INGRESS_SCOPE_CLAIM_KEY: REMOTE_WORKBENCH_INGRESS_SCOPE,
            "_origin_instance_evidence": {
                "id": "WORKBENCH",
                "assurance": "shared_network_hmac",
            },
        },
        prompt=stale_prompt,
    )
    stale = _MockRequest(
        {
            "idempotency_key": "remote-gateway-stale",
            "originated_at": (
                datetime.now(timezone.utc) - timedelta(seconds=65)
            ).isoformat().replace("+00:00", "Z"),
            "message": {"content": [{"type": "text", "text": stale_prompt}]},
            "request_metadata": {"_connector_evidence": stale_evidence},
        },
        match_info={"session_id": session["session_id"]},
        remote="127.0.0.1",
    )
    stale_response = await server.handle_v1_session_runs_create(stale)
    stale_data = json.loads(stale_response.text)
    assert stale_data["confirmation_required"] is True
    assert stale_data["confirmation"]["threshold_seconds"] == 60
    assert runtime.enqueue_request_calls == 1


@pytest.mark.asyncio
async def test_client_declared_tui_surface_never_bypasses_freshness(api_server, session):
    server, runtime = api_server
    root = server.config_path.parent
    (root / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "tui-connector-secret"}),
        encoding="utf-8",
    )
    # Loopback and TUI-shaped body values are not connector identity.
    req_local_tui = _MockRequest(
        {
            "idempotency_key": "tui-forged-local-1",
            "surface": "tui",
            "client_id": "tui-forged-client-1",
            "originated_at": (
                datetime.now(timezone.utc) - timedelta(seconds=40)
            ).isoformat().replace("+00:00", "Z"),
            "message": {"content": [{"type": "text", "text": "local tui prompt"}]},
        },
        match_info={"session_id": session["session_id"]},
        remote="127.0.0.1",
    )
    resp = await server.handle_v1_session_runs_create(req_local_tui)
    assert resp.status == 202
    data = json.loads(resp.text)
    assert data.get("confirmation_required") is True
    assert "run_id" not in data
    assert runtime.enqueue_request_calls == 0

    # The existing Remote-to-Backend hop seals the real TUI identity with the
    # local shared secret.  No TUI payload or connector behavior is changed.
    trusted_prompt = "real local tui"
    evidence = seal_connector_evidence(
        root,
        claims={
            "_message_source_reserved": "tui",
            "_origin_instance_evidence": {
                "id": "HASHI3",
                "assurance": "shared_network_hmac",
            },
        },
        prompt=trusted_prompt,
    )
    req_real_local_tui = _MockRequest(
        {
            "idempotency_key": "tui-real-local-1",
            "surface": "tui",
            "client_id": "tui-client-1",
            "message": {"content": [{"type": "text", "text": trusted_prompt}]},
            "request_metadata": {"_connector_evidence": evidence},
        },
        match_info={"session_id": session["session_id"]},
        remote="127.0.0.1",
    )
    resp_real = await server.handle_v1_session_runs_create(req_real_local_tui)
    real_data = json.loads(resp_real.text)
    assert resp_real.status == 202
    assert real_data.get("confirmation_required") is not True
    assert "run_id" in real_data
    assert runtime.enqueue_request_calls == 1

    # Non-loopback remote claiming surface="tui" -> NOT exempt!
    req_remote_fake_tui = _MockRequest(
        {
            "idempotency_key": "idemp-remote-tui",
            "surface": "tui",
            "originated_at": (
                datetime.now(timezone.utc) - timedelta(seconds=65)
            ).isoformat().replace("+00:00", "Z"),
            "message": {"content": [{"type": "text", "text": "remote claiming tui"}]},
        },
        match_info={"session_id": session["session_id"]},
        remote="192.168.1.100",
    )
    resp_remote = await server.handle_v1_session_runs_create(req_remote_fake_tui)
    assert resp_remote.status == 202
    data_remote = json.loads(resp_remote.text)
    assert data_remote.get("confirmation_required") is True
    assert runtime.enqueue_request_calls == 1


@pytest.mark.asyncio
async def test_ingress_confirmations_list_and_get_endpoints(api_server, session):
    server, _ = api_server
    # Trigger a pending confirmation
    req = _MockRequest(
        {
            "idempotency_key": "idemp-pending-query",
            "originated_at": (
                datetime.now(timezone.utc) - timedelta(seconds=40)
            ).isoformat().replace("+00:00", "Z"),
            "message": {"content": [{"type": "text", "text": "pending query test"}]},
        },
        match_info={"session_id": session["session_id"]},
    )
    create_resp = await server.handle_v1_session_runs_create(req)
    conf_id = json.loads(create_resp.text)["confirmation"]["id"]

    # List confirmations
    list_req = _MockRequest(match_info={"session_id": session["session_id"]})
    list_resp = await server.handle_v1_session_ingress_confirmations_list(list_req)
    assert list_resp.status == 200
    assert list_resp.headers.get("Cache-Control") == "no-store"
    list_data = json.loads(list_resp.text)
    assert list_data.get("ok") is True
    assert len(list_data["confirmations"]) >= 1
    assert any(c["confirmation_id"] == conf_id for c in list_data["confirmations"])

    # Get single confirmation
    get_req = _MockRequest(
        match_info={"session_id": session["session_id"], "confirmation_id": conf_id}
    )
    get_resp = await server.handle_v1_session_ingress_confirmation_get(get_req)
    assert get_resp.status == 200
    assert get_resp.headers.get("Cache-Control") == "no-store"
    get_data = json.loads(get_resp.text)
    assert get_data["confirmation"]["confirmation_id"] == conf_id

    # A confirmation ID cannot be decided through another Session owned by
    # the same user.
    other_session = server.session_store.create_session(
        owner_id="user:7", agent_id="lily", title="other session"
    )
    cross_session_req = _MockRequest(
        {"decision": "discard"},
        match_info={
            "session_id": other_session["session_id"],
            "confirmation_id": conf_id,
        },
    )
    cross_session_resp = (
        await server.handle_v1_session_ingress_confirmation_decide(
            cross_session_req
        )
    )
    assert cross_session_resp.status == 404
    unchanged = server.ingress_confirmation.get(
        conf_id,
        owner_id="user:7",
        session_id=session["session_id"],
    )
    assert unchanged["state"] == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_ingress_confirmation_decision_confirm_and_discard(api_server, session):
    server, runtime = api_server
    # 1. Test Discard
    req_discard = _MockRequest(
        {
            "idempotency_key": "idemp-discard-test",
            "originated_at": (
                datetime.now(timezone.utc) - timedelta(seconds=40)
            ).isoformat().replace("+00:00", "Z"),
            "message": {"content": [{"type": "text", "text": "to be discarded"}]},
        },
        match_info={"session_id": session["session_id"]},
    )
    resp_discard = await server.handle_v1_session_runs_create(req_discard)
    conf_discard_id = json.loads(resp_discard.text)["confirmation"]["id"]

    decide_req = _MockRequest(
        {"decision": "discard"},
        match_info={"session_id": session["session_id"], "confirmation_id": conf_discard_id},
    )
    decide_resp = await server.handle_v1_session_ingress_confirmation_decide(decide_req)
    assert decide_resp.status == 200
    assert decide_resp.headers.get("Cache-Control") == "no-store"
    d_data = json.loads(decide_resp.text)
    assert d_data["state"] == "discarded"
    assert d_data["run_id"] is None
    assert runtime.enqueue_request_calls == 0

    # 2. Test Confirm
    req_confirm = _MockRequest(
        {
            "idempotency_key": "idemp-confirm-test",
            "originated_at": (
                datetime.now(timezone.utc) - timedelta(seconds=40)
            ).isoformat().replace("+00:00", "Z"),
            "message": {"content": [{"type": "text", "text": "to be confirmed"}]},
        },
        match_info={"session_id": session["session_id"]},
    )
    resp_confirm = await server.handle_v1_session_runs_create(req_confirm)
    conf_confirm_id = json.loads(resp_confirm.text)["confirmation"]["id"]

    decide_conf_req = _MockRequest(
        {"decision": "confirm", "text": "attempted override text"},
        match_info={"session_id": session["session_id"], "confirmation_id": conf_confirm_id},
    )
    decide_conf_resp = await server.handle_v1_session_ingress_confirmation_decide(decide_conf_req)
    assert decide_conf_resp.status == 400
    assert runtime.enqueue_request_calls == 0

    decide_conf_req = _MockRequest(
        {"decision": "confirm"},
        match_info={"session_id": session["session_id"], "confirmation_id": conf_confirm_id},
    )
    decide_conf_resp = await server.handle_v1_session_ingress_confirmation_decide(decide_conf_req)
    assert decide_conf_resp.status == 200
    assert decide_conf_resp.headers.get("Cache-Control") == "no-store"
    c_data = json.loads(decide_conf_resp.text)
    assert c_data["state"] == "confirmed"
    assert c_data["run_id"] is not None
    assert runtime.enqueue_request_calls == 1
    # Verify original text executed, NOT override
    assert runtime.last_enqueued_text == "to be confirmed"

    # Repeated confirm returns same result
    repeat_resp = await server.handle_v1_session_ingress_confirmation_decide(decide_conf_req)
    assert repeat_resp.status == 200
    r_data = json.loads(repeat_resp.text)
    assert r_data["run_id"] == c_data["run_id"]
    assert runtime.enqueue_request_calls == 1  # not re-executed

@pytest.mark.asyncio
async def test_confirmation_registration_keeps_status_responsive_under_database_writer(api_server, session, monkeypatch):
    import asyncio
    import sqlite3
    import threading
    import time

    server, runtime = api_server
    started = threading.Event()
    original = server.ingress_confirmation.register_pending

    def observed_register(**kwargs):
        started.set()
        return original(**kwargs)

    monkeypatch.setattr(server.ingress_confirmation, "register_pending", observed_register)
    payload = {
        "idempotency_key": "stale-message-contention",
        "originated_at": (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat(),
        "message": {"content": [{"type": "text", "text": "send once"}]},
    }

    def request():
        return _MockRequest(payload, match_info={"session_id": session["session_id"]})

    connection = sqlite3.connect(server.session_store.db_path, isolation_level=None)
    task = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        task = asyncio.create_task(server.handle_v1_session_runs_create(request()))
        assert await asyncio.to_thread(started.wait, 2)
        assert not task.done()
        before = time.monotonic()
        status = await server.handle_v1_capabilities(_MockRequest())
        assert status.status == 200
        assert time.monotonic() - before < 1
        connection.rollback()
        result = await asyncio.wait_for(task, 3)
        assert result.status == 202, result.text
        first = json.loads(result.text)
        assert first["confirmation_required"] is True
        retry = await server.handle_v1_session_runs_create(request())
        assert retry.status == 202
        assert json.loads(retry.text)["confirmation"]["id"] == first["confirmation"]["id"]
        assert runtime.enqueue_request_calls == 0
    finally:
        connection.rollback()
        connection.close()
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
