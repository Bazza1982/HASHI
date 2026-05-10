from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_reboot


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


class _RuntimeRef:
    def __init__(self, name: str):
        self.name = name


class _Orchestrator:
    def __init__(self, names=None, running=None):
        self._names = list(names or [])
        self.runtimes = [_RuntimeRef(name) for name in (running or [])]
        self.restarts = []

    def configured_agent_names(self):
        return list(self._names)

    def request_restart(self, **kwargs):
        self.restarts.append(kwargs)


def _runtime(orchestrator=None):
    replies = []
    runtime = SimpleNamespace(
        name="lin_yueru",
        orchestrator=orchestrator,
        _is_authorized_user=lambda user_id: True,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_reboot_without_args_shows_picker():
    runtime, replies = _runtime(_Orchestrator(["lin_yueru", "barry"], running=["barry"]))

    await runtime_reboot.cmd_reboot(runtime, _update(), _context())

    assert "<b>Reboot</b> — select target:" in replies[-1][0]
    assert "1. ○ lin_yueru" in replies[-1][0]
    assert "2. ● barry" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"
    assert replies[-1][1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_cmd_reboot_with_number_requests_specific_restart():
    orchestrator = _Orchestrator(["lin_yueru", "barry"])
    runtime, replies = _runtime(orchestrator)

    await runtime_reboot.cmd_reboot(runtime, _update(), _context("2"))

    assert replies[-1][0] == "Restarting agent #2 (<b>barry</b>)..."
    assert orchestrator.restarts == [{"mode": "number", "agent_name": "lin_yueru", "agent_number": 2}]


@pytest.mark.asyncio
async def test_cmd_reboot_rejects_invalid_number():
    orchestrator = _Orchestrator(["lin_yueru", "barry"])
    runtime, replies = _runtime(orchestrator)

    await runtime_reboot.cmd_reboot(runtime, _update(), _context("3"))

    assert replies[-1][0] == "Invalid agent number. Use 1–2. /reboot help to list."
    assert orchestrator.restarts == []


@pytest.mark.asyncio
async def test_callback_reboot_toggle_requests_restart():
    orchestrator = _Orchestrator(["lin_yueru", "barry"])
    runtime, _replies = _runtime(orchestrator)
    answers = []
    edits = []

    async def _answer(text=None, **kwargs):
        answers.append((text, kwargs))

    async def _edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    query = SimpleNamespace(
        answer=_answer,
        edit_message_text=_edit_message_text,
    )

    await runtime_reboot.callback_reboot_toggle(runtime, query, "2")

    assert edits[-1][0] == "Restarting agent #2 (<b>barry</b>)..."
    assert answers[-1][0] is None
    assert orchestrator.restarts == [{"mode": "number", "agent_name": "lin_yueru", "agent_number": 2}]
