from types import SimpleNamespace

import pytest

from orchestrator.frontend_connector_registry import endpoint_id_for
from orchestrator.session_store import SessionStore
from transports.whatsapp import WhatsAppTransport


class _Audit:
    def __init__(self):
        self.blocked = []
        self.failed = []

    def block(self, reason):
        self.blocked.append(reason)

    def fail(self, error):
        self.failed.append(error)

    def finish(self):
        return None


def _transport(tmp_path, *, manager):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    runtime = SimpleNamespace(
        name="source",
        global_config=SimpleNamespace(authorized_id=73, instance_id="HASHI3"),
        session_store=store,
    )
    orchestrator = SimpleNamespace(
        runtimes=[runtime],
        agent_management=manager,
        start_agent=lambda *_args, **_kwargs: pytest.fail("bypassed PAO start"),
        stop_agent=lambda *_args, **_kwargs: pytest.fail("bypassed PAO stop"),
    )
    transport = WhatsAppTransport.__new__(WhatsAppTransport)
    transport.orchestrator = orchestrator
    transport._router = SimpleNamespace(get_targets=lambda _chat: ["source"])
    transport._get_runtime = lambda name: runtime if name == "source" else None
    transport._whatsapp_command_session = lambda *_args, **_kwargs: _Audit()
    return transport, runtime, store


@pytest.mark.asyncio
async def test_whatsapp_ingress_passes_stable_message_and_actor_identity(tmp_path):
    transport, _runtime, _store = _transport(tmp_path, manager=SimpleNamespace())
    transport._refresh_runtime_config = lambda: None
    transport._allowed_numbers = set()
    transport._allowed_chat_ids = set()
    transport._jid_cache = {}
    transport._jid_str = lambda jid: str(jid)
    transport._phone_candidates = lambda _jid: {"+61400000000"}
    transport._extract_text = lambda _msg: "/start sleeping-agent"
    transport._is_voice = lambda _msg: False
    transport._detect_media_kind = lambda _msg: None

    async def allow_ingress(**_kwargs):
        return True

    captured = {}

    async def lifecycle(chat_key, text, **identity):
        captured.update(chat_key=chat_key, text=text, **identity)

    transport._check_whatsapp_ingress_allowed = allow_ingress
    transport._handle_lifecycle_command = lifecycle
    chat_key = "61400000000@s.whatsapp.net"
    message = SimpleNamespace(
        Info=SimpleNamespace(
            ID="wamid-real-001",
            MessageSource=SimpleNamespace(
                IsFromMe=False,
                Chat=chat_key,
                Sender=chat_key,
            ),
        )
    )

    await transport._on_message(message)

    assert captured == {
        "chat_key": chat_key,
        "text": "/start sleeping-agent",
        "transport_id": "wamid-real-001",
        "actor_id": "+61400000000",
    }


@pytest.mark.asyncio
async def test_whatsapp_start_uses_typed_pao_and_canonical_outbox(tmp_path):
    order = []
    captured = {}

    class Manager:
        async def dispatch(self, action, *, session_store):
            order.append("dispatch")
            captured.update(action=action, store=session_store)
            return {
                "ok": True,
                "state": "starting",
                "status": 202,
                "replayed": False,
                "message": "Agent startup accepted.",
            }

    transport, runtime, store = _transport(tmp_path, manager=Manager())
    sent = []

    async def send_text(chat_key, text):
        order.append("send")
        sent.append((chat_key, text))
        return True

    transport._send_text = send_text
    chat_key = "61400000000@s.whatsapp.net"
    await transport._handle_lifecycle_command(
        chat_key,
        "/start sleeping-agent",
        transport_id="wamid-001",
        actor_id="+61400000000",
    )

    assert order == ["dispatch", "send"]
    action = captured["action"]
    assert captured["store"] is store
    assert action.kind == "control"
    assert action.operation == "lifecycle.set_active"
    assert action.owner_id == "user:73"
    assert action.connector_id == "whatsapp"
    assert action.agent_id == "sleeping-agent"
    assert action.payload == {
        "is_active": True,
        "reason": "start",
        "source": "whatsapp.lifecycle",
    }
    assert action.admission.actor_id == "+61400000000"
    assert action.admission.source_agent_id == "source"
    assert action.admission.endpoint_id == endpoint_id_for(
        "whatsapp",
        ingress_transport="whatsapp.message",
        channel_key=chat_key,
    )
    assert sent == [(chat_key, "🚀 sleeping-agent: Agent startup accepted.")]

    session = store.resolve_session(
        owner_id="user:73",
        agent_id="source",
        surface="whatsapp",
        channel_key=chat_key,
    )
    messages = store.messages(session["session_id"], owner_id="user:73")
    assert [message["source"] for message in messages] == ["whatsapp.lifecycle"]
    delivery_event = next(
        event
        for event in store.events(session["session_id"], owner_id="user:73")
        if event["kind"] == "frontend.message.recorded"
    )
    receipts = store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id="user:73",
        event_id=delivery_event["event_id"],
    )
    assert len(receipts) == 1
    assert receipts[0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_whatsapp_lifecycle_transport_replay_does_not_resend_result(tmp_path):
    class Manager:
        async def dispatch(self, action, *, session_store):
            del action, session_store
            return {
                "ok": True,
                "state": "succeeded",
                "status": 200,
                "replayed": True,
                "message": "Agent is inactive.",
            }

    transport, _runtime, _store = _transport(tmp_path, manager=Manager())
    sent = []

    async def send_text(chat_key, text):
        sent.append((chat_key, text))
        return True

    transport._send_text = send_text
    kwargs = {
        "transport_id": "wamid-replay",
        "actor_id": "+61400000000",
    }
    chat_key = "61400000000@s.whatsapp.net"
    await transport._handle_lifecycle_command(
        chat_key, "/stop worker", **kwargs
    )
    await transport._handle_lifecycle_command(
        chat_key, "/stop worker", **kwargs
    )

    assert sent == [(chat_key, "✓ /stop worker: Agent is inactive.")]


@pytest.mark.asyncio
async def test_whatsapp_lifecycle_fails_closed_without_one_source_runtime(tmp_path):
    class Manager:
        async def dispatch(self, *_args, **_kwargs):
            raise AssertionError("lifecycle mutation must not be attempted")

    transport, runtime, _store = _transport(tmp_path, manager=Manager())
    other = SimpleNamespace(name="other")
    transport.orchestrator.runtimes = [runtime, other]
    transport._router = SimpleNamespace(get_targets=lambda _chat: [])
    sent = []

    async def send_text(chat_key, text):
        sent.append((chat_key, text))
        return True

    transport._send_text = send_text
    await transport._handle_lifecycle_command(
        "61400000000@s.whatsapp.net",
        "/start worker",
        transport_id="wamid-ambiguous",
        actor_id="+61400000000",
    )

    assert len(sent) == 1
    assert "exactly one running source Agent" in sent[0][1]
