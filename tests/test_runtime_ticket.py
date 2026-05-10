from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_ticket


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime():
    replies = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        global_config=SimpleNamespace(project_root=Path("/tmp/project")),
        name="lily",
        workspace_dir=Path("/tmp/workspace"),
        orchestrator=None,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


def _install_ticket_manager(monkeypatch, module):
    monkeypatch.setitem(sys.modules, "orchestrator.ticket_manager", module)


@pytest.mark.asyncio
async def test_cmd_ticket_lists_open_and_in_progress_tickets(monkeypatch):
    runtime, replies = _runtime()
    ticket_manager = types.ModuleType("orchestrator.ticket_manager")
    ticket_manager._resolve_tickets_dir = lambda project_root: Path("/tmp/tickets")
    ticket_manager.list_tickets = lambda tickets_dir, state: (
        [{"ticket_id": "T1", "source_agent": "a", "summary": "open issue"}]
        if state == "open"
        else [{"ticket_id": "T2", "source_agent": "b", "summary": "progress issue"}]
    )
    _install_ticket_manager(monkeypatch, ticket_manager)

    await runtime_ticket.cmd_ticket(runtime, _update(), _context())

    text = replies[-1][0]
    assert "Open tickets:" in text
    assert "[T1] a — open issue" in text
    assert "In progress:" in text
    assert "[T2] b — progress issue" in text


@pytest.mark.asyncio
async def test_cmd_ticket_creates_ticket_and_notifies_local_arale(monkeypatch):
    runtime, replies = _runtime()
    delivered = []

    async def enqueue_api_text(text, source, deliver_to_telegram):
        delivered.append((text, source, deliver_to_telegram))

    runtime.orchestrator = SimpleNamespace(runtimes=[SimpleNamespace(name="arale", enqueue_api_text=enqueue_api_text)])

    ticket_manager = types.ModuleType("orchestrator.ticket_manager")
    ticket_manager.detect_instance = lambda project_root: "hashi2"
    ticket_manager.create_ticket = lambda **kwargs: {"ticket_id": "TKT-1"}
    ticket_manager.format_ticket_notification = lambda ticket: "NOTICE"
    _install_ticket_manager(monkeypatch, ticket_manager)

    await runtime_ticket.cmd_ticket(runtime, _update(), _context("need", "help"))

    assert replies[-1][0] == "🎫 Ticket TKT-1 created.\nArale has been notified and will investigate."
    assert delivered and delivered[-1][1] == "ticket:TKT-1"
    assert "[TICKET RECEIVED]\nNOTICE" in delivered[-1][0]


@pytest.mark.asyncio
async def test_cmd_ticket_falls_back_to_hchat_when_local_arale_missing(monkeypatch):
    runtime, replies = _runtime()
    hchat_calls = []

    ticket_manager = types.ModuleType("orchestrator.ticket_manager")
    ticket_manager.detect_instance = lambda project_root: "hashi2"
    ticket_manager.create_ticket = lambda **kwargs: {"ticket_id": "TKT-2"}
    ticket_manager.format_ticket_notification = lambda ticket: "NOTICE"
    _install_ticket_manager(monkeypatch, ticket_manager)

    hchat_module = types.ModuleType("tools.hchat_send")
    hchat_module.send_hchat = lambda target, source, text: hchat_calls.append((target, source, text)) or True
    monkeypatch.setitem(sys.modules, "tools.hchat_send", hchat_module)

    await runtime_ticket.cmd_ticket(runtime, _update(), _context("need", "help"))

    assert replies[-1][0] == "🎫 Ticket TKT-2 created.\nArale has been notified and will investigate."
    assert hchat_calls and hchat_calls[-1][0:2] == ("arale", "lily")
