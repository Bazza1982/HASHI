from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp import web

from orchestrator.exchange_config import ExchangeConfig
from remote.exchange_protocol import (
    AUTHORIZED_ROUTES_CAPABILITY,
    send_frame,
    utc_timestamp,
)
from remote.exchange_transport import ExchangeTransport, ExchangeTransportError


def _target():
    return {
        "authority_id": "authority_1",
        "actor_id": "actor_alice",
        "registered_instance_id": "instance_alice",
        "agent_id": "planner",
        "address": "planner@home.alice",
    }


def _ready_transport(tmp_path, *, now: float):
    transport = ExchangeTransport(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI2"},
        workbench_port=18802,
        clock=lambda: now,
    )
    transport._config = ExchangeConfig(
        enabled=True,
        authority_id="authority_1",
        url="wss://exchange.example.test/v1/connect",
        registered_instance_id="instance_barry",
        instance_alias="server",
        credential_ref="exchange_token",
        published_agents=("reviewer",),
    )
    transport._published_agents = {"reviewer"}
    transport._ready_event.set()
    transport._ws = SimpleNamespace(closed=False)
    return transport


def test_explicit_retry_reuses_the_exact_persisted_frame(tmp_path):
    now = 1_800_000_000.0
    transport = _ready_transport(tmp_path, now=now + 10)
    original = send_frame(
        message_id="message_1",
        conversation_id="conversation_1",
        from_agent="reviewer",
        to=_target(),
        created_at=utc_timestamp(now),
        expires_at=utc_timestamp(now + 300),
        message_type="agent_message",
        in_reply_to=None,
        text="Please review.",
    )
    transport.outbox.put(original)
    sent = []

    async def fake_send(_ws, frame):
        sent.append(frame)
        if frame.get("type") == "send":
            transport._receipt_waiters["message_1"].set_result(
                {
                    "v": 1,
                    "type": "receipt",
                    "message_id": "message_1",
                    "status": "accepted",
                }
            )

    transport._send_frame = fake_send
    result = asyncio.run(
        transport.send_message(
            from_agent="reviewer",
            to_address="planner@home.alice",
            text="Please review.",
            message_id="message_1",
            conversation_id="conversation_1",
        )
    )

    assert result["ok"] is True
    assert result["replayed"] is True
    assert sent == [original]


def test_explicit_retry_never_changes_content_under_the_same_id(tmp_path):
    now = 1_800_000_000.0
    transport = _ready_transport(tmp_path, now=now + 10)
    transport.outbox.put(
        send_frame(
            message_id="message_1",
            conversation_id="conversation_1",
            from_agent="reviewer",
            to=_target(),
            created_at=utc_timestamp(now),
            expires_at=utc_timestamp(now + 300),
            message_type="agent_message",
            in_reply_to=None,
            text="Please review.",
        )
    )

    with pytest.raises(ExchangeTransportError, match="IDEMPOTENCY_CONFLICT"):
        asyncio.run(
            transport.send_message(
                from_agent="reviewer",
                to_address="planner@home.alice",
                text="Changed request.",
                message_id="message_1",
                conversation_id="conversation_1",
            )
        )


@pytest.mark.asyncio
async def test_authorized_route_snapshot_is_projected_and_becomes_stale(tmp_path):
    now = 1_800_000_000.0
    transport = _ready_transport(tmp_path, now=now)
    transport._negotiated_capabilities = {AUTHORIZED_ROUTES_CAPABILITY}
    target = _target()

    async def fake_send(_ws, frame):
        transport._request_waiters[frame["request_id"]].set_result({
            "v": 1,
            "type": "authorized_routes",
            "request_id": frame["request_id"],
            "grant_revision": 11,
            "refreshed_at": "2027-01-15T08:00:00Z",
            "routes": [{
                "to": target,
                "message_kinds": ["agent_message", "agent_reply"],
                "available": True,
            }],
        })

    transport._send_frame = fake_send
    await transport._refresh_authorized_routes(transport._ws)

    status = transport.status()
    assert status["authorized_routes_supported"] is True
    assert status["routes_grant_revision"] == 11
    assert status["routes_stale"] is False
    assert status["authorized_routes"] == [{
        "to": target,
        "message_kinds": ["agent_message", "agent_reply"],
        "available": True,
    }]

    transport._ready_event.clear()
    transport._routes_stale = True
    assert transport.status()["routes_stale"] is True


def test_server_without_route_capability_clears_old_snapshot(tmp_path):
    transport = _ready_transport(tmp_path, now=1_800_000_000.0)
    transport._authorized_routes = [{
        "to": _target(),
        "message_kinds": ["agent_message"],
        "available": True,
    }]
    transport._routes_supported = True
    transport._routes_stale = True

    transport._clear_authorized_routes(supported=False)

    status = transport.status()
    assert status["authorized_routes_supported"] is False
    assert status["authorized_routes"] == []
    assert status["routes_stale"] is False


def test_delivery_ack_happens_after_accept_and_before_pao_schedule(
    tmp_path,
):
    now = time.time()
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {"hashi_remote_shared_token": "synthetic-local-shared-token"}
        ),
        encoding="utf-8",
    )
    transport = ExchangeTransport(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI2"},
        workbench_port=18802,
    )
    transport._welcome = {
        "epoch": "epoch_1",
        "actor_id": "actor_barry",
        "instance_address": "server.barry",
    }
    delivery = {
        "v": 1,
        "type": "delivery",
        "delivery_id": "delivery_1",
        "recipient_epoch": "epoch_1",
        "sender": _target(),
        "recipient": {
            "authority_id": "authority_1",
            "actor_id": "actor_barry",
            "registered_instance_id": "instance_barry",
            "agent_id": "reviewer",
            "address": "reviewer@server.barry",
        },
        "grant_revision": 1,
        "authorization_expires_at": utc_timestamp(now + 60),
        "message_id": "message_1",
        "conversation_id": "conversation_1",
        "created_at": utc_timestamp(now),
        "expires_at": utc_timestamp(now + 60),
        "message_type": "agent_message",
        "in_reply_to": None,
        "content": {
            "format": "hchat",
            "text": "Please review.",
            "authorization_message_id": None,
            "authorization_resources": [],
            "private_authorization_proofs": [],
        },
    }
    events = []

    async def fake_post(path, **_kwargs):
        events.append(path)
        return 200, {"ok": True, "state": "accepted"}

    async def fake_send(_ws, frame):
        events.append(frame["type"])

    transport._post_ingress = fake_post
    transport._send_frame = fake_send

    asyncio.run(
        transport._accept_ack_schedule(
            SimpleNamespace(closed=False),
            delivery,
        )
    )

    assert events == [
        "/api/bridge/exchange/accept",
        "ack",
        "/api/bridge/exchange/schedule",
    ]


def test_retry_respects_the_negotiated_server_frame_limit(tmp_path):
    now = 1_800_000_000.0
    transport = _ready_transport(tmp_path, now=now + 10)
    transport._welcome = {
        "limits": {"max_message_bytes": 256},
    }
    record = transport.outbox.put(
        send_frame(
            message_id="message_large",
            conversation_id="conversation_1",
            from_agent="reviewer",
            to=_target(),
            created_at=utc_timestamp(now),
            expires_at=utc_timestamp(now + 300),
            message_type="agent_message",
            in_reply_to=None,
            text="x" * 300,
        )
    )
    sent = []

    async def fake_send(_ws, frame):
        sent.append(frame)

    transport._send_frame = fake_send
    result = asyncio.run(
        transport._transmit_record(
            transport._ws,
            record,
            replayed=True,
        )
    )

    assert result["ok"] is False
    assert result["code"] == "PAYLOAD_TOO_LARGE"
    assert sent == []
    assert transport.outbox.get("message_large").state == "rejected"


@pytest.mark.asyncio
async def test_websocket_redirect_is_rejected_before_followup_request(
    tmp_path,
):
    hits = []

    async def redirect(_request):
        hits.append("configured")
        raise web.HTTPFound("/unexpected")

    async def unexpected(_request):
        hits.append("unexpected")
        return web.Response(text="credential must not be redirected")

    app = web.Application()
    app.router.add_get("/v1/connect", redirect)
    app.router.add_get("/unexpected", unexpected)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    (tmp_path / "agents.json").write_text(
        json.dumps(
            {
                "exchange": {
                    "enabled": True,
                    "authority_id": "authority_1",
                    "url": f"ws://127.0.0.1:{port}/v1/connect",
                    "registered_instance_id": "instance_example",
                    "instance_alias": "server",
                    "credential_ref": "exchange_token",
                    "published_agents": ["reviewer"],
                },
                "agents": [
                    {"name": "reviewer", "is_active": True}
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "secrets.json").write_text(
        json.dumps({"exchange_token": "synthetic-exchange-token"}),
        encoding="utf-8",
    )
    transport = ExchangeTransport(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI2"},
        workbench_port=18802,
    )
    try:
        with pytest.raises(ExchangeTransportError, match="REDIRECT_REJECTED"):
            await transport._connect_once()
    finally:
        await runner.cleanup()

    assert hits == ["configured"]
