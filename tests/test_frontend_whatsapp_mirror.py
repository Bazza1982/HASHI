"""The central WhatsApp switch controls mirrors without touching primary replies."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.frontend_delivery import (
    configured_whatsapp_mirror_channel,
    freeze_run_delivery_route,
    route_destination,
)
from orchestrator.frontend_whatsapp_mirror import deliver_whatsapp_mirror
from orchestrator.session_store import SessionStore


def test_whatsapp_mirror_requires_one_explicit_recipient():
    config = lambda numbers: SimpleNamespace(whatsapp={"allowed_numbers": numbers})
    assert configured_whatsapp_mirror_channel(config(["+61400111222"])) == "61400111222@s.whatsapp.net"
    assert configured_whatsapp_mirror_channel(config([])) is None
    assert configured_whatsapp_mirror_channel(config(["61400111222", "61400333444"])) is None
    assert configured_whatsapp_mirror_channel(config(["not-a-number"])) is None


@pytest.mark.asyncio
async def test_whatsapp_mirror_dispatches_once_without_claiming_primary(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    route = freeze_run_delivery_route(
        message_source_id="tui",
        session_surface="workbench",
        session_channel_key="default",
        chat_id=7,
        telegram_requested=False,
        whatsapp_requested=True,
        whatsapp_channel_key="61400111222@s.whatsapp.net",
    )
    assert route_destination(route, "whatsapp")["channel_key"] == "61400111222@s.whatsapp.net"
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id=owner, agent_id="lily",
        request_id="req-wa-mirror", text="question", source="tui",
        idempotency_key="wa-mirror-key", delivery_route=route,
    )
    store.mark_request_running(accepted.request_id, worker_id="test-worker")
    store.finish_request(accepted.request_id, success=True, assistant_text="answer")
    sent = []

    async def send(phone, text):
        sent.append((phone, text))
        return True, "accepted"

    runtime = SimpleNamespace(
        name="lily", session_store=store,
        global_config=SimpleNamespace(authorized_id=7),
        orchestrator=SimpleNamespace(send_whatsapp_text=send),
    )
    first = await deliver_whatsapp_mirror(runtime, accepted.request_id)
    second = await deliver_whatsapp_mirror(runtime, accepted.request_id)
    assert first["status"] == "completed"
    assert sent == [("61400111222", "[lily]: answer")]
    assert second["state"] == "completed"
    primary = store.claim_run_delivery_outbox(
        request_id=accepted.request_id, owner_id=owner,
        surface="tui", channel_key="default", worker_id="tui-worker",
    )
    assert primary["state"] == "claimed"


def test_whatsapp_primary_remains_when_mirror_off():
    route = freeze_run_delivery_route(
        message_source_id="whatsapp", session_surface="whatsapp",
        session_channel_key="61400111222@s.whatsapp.net", chat_id=7,
        telegram_requested=False, whatsapp_requested=False,
    )
    assert route_destination(route, "whatsapp")["channel_key"] == "61400111222@s.whatsapp.net"
    assert route["mirrors"] == []
