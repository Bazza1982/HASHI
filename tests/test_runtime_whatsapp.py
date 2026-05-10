from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_whatsapp


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(orchestrator=None):
    replies = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        orchestrator=orchestrator,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_wa_on_reports_unavailable_without_orchestrator():
    runtime, replies = _runtime()

    await runtime_whatsapp.cmd_wa_on(runtime, _update(), _context())

    assert replies[-1][0] == "WhatsApp lifecycle control is unavailable."


@pytest.mark.asyncio
async def test_cmd_wa_on_starts_transport():
    async def start_whatsapp_transport(persist_enabled):
        return True, "started"

    runtime, replies = _runtime(SimpleNamespace(start_whatsapp_transport=start_whatsapp_transport))

    await runtime_whatsapp.cmd_wa_on(runtime, _update(), _context())

    assert replies[-1][0] == "started"


@pytest.mark.asyncio
async def test_cmd_wa_off_stops_transport():
    async def stop_whatsapp_transport(persist_enabled):
        return True, "stopped"

    runtime, replies = _runtime(SimpleNamespace(stop_whatsapp_transport=stop_whatsapp_transport))

    await runtime_whatsapp.cmd_wa_off(runtime, _update(), _context())

    assert replies[-1][0] == "stopped"


@pytest.mark.asyncio
async def test_cmd_wa_send_requires_message():
    async def send_whatsapp_text(phone_number, text):
        return True, "sent"

    runtime, replies = _runtime(SimpleNamespace(send_whatsapp_text=send_whatsapp_text))

    await runtime_whatsapp.cmd_wa_send(runtime, _update(), _context("+123"))

    assert replies[-1][0] == "Usage: /wa_send <+number> <message>"


@pytest.mark.asyncio
async def test_cmd_wa_send_sends_message():
    calls = []

    async def send_whatsapp_text(phone_number, text):
        calls.append((phone_number, text))
        return True, "sent"

    runtime, replies = _runtime(SimpleNamespace(send_whatsapp_text=send_whatsapp_text))

    await runtime_whatsapp.cmd_wa_send(runtime, _update(), _context("+123", "hello", "world"))

    assert calls == [("+123", "hello world")]
    assert replies[-1][0] == "sent"
