from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_sys


class _Message:
    def __init__(self):
        self.calls = []

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, kwargs))


def _update():
    message = _Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        message=message,
    ), message


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(manager):
    return SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        sys_prompt_manager=manager,
    )


class _Manager:
    SLOTS = {str(i) for i in range(1, 11)}

    def __init__(self):
        self.saved = []
        self.replaced = []

    def display_all(self):
        return "ALL"

    def _slot(self, slot):
        return {"text": "RAW" if slot == "1" else ""}

    def display_slot(self, slot):
        return f"SLOT:{slot}"

    def activate(self, slot):
        return f"ON:{slot}"

    def deactivate(self, slot):
        return f"OFF:{slot}"

    def delete(self, slot):
        return f"DEL:{slot}"

    def save(self, slot, text):
        self.saved.append((slot, text))
        return f"SAVE:{slot}:{text}"

    def replace(self, slot, text):
        self.replaced.append((slot, text))
        return f"REPLACE:{slot}:{text}"


@pytest.mark.asyncio
async def test_cmd_sys_displays_all_slots():
    manager = _Manager()
    runtime = _runtime(manager)
    update, message = _update()

    await runtime_sys.cmd_sys(runtime, update, _context())

    assert message.calls[-1] == ("ALL", {"parse_mode": "Markdown"})


@pytest.mark.asyncio
async def test_cmd_sys_outputs_raw_slot_text():
    manager = _Manager()
    runtime = _runtime(manager)
    update, message = _update()

    await runtime_sys.cmd_sys(runtime, update, _context("output", "1"))

    assert message.calls[-1] == ("RAW", {"parse_mode": None})


@pytest.mark.asyncio
async def test_cmd_sys_rejects_invalid_slot():
    manager = _Manager()
    runtime = _runtime(manager)
    update, message = _update()

    await runtime_sys.cmd_sys(runtime, update, _context("11"))

    assert message.calls[-1] == ("Invalid slot '11'. Use 1-10.", {})


@pytest.mark.asyncio
async def test_cmd_sys_shows_slot():
    manager = _Manager()
    runtime = _runtime(manager)
    update, message = _update()

    await runtime_sys.cmd_sys(runtime, update, _context("2"))

    assert message.calls[-1] == ("SLOT:2", {})


@pytest.mark.asyncio
async def test_cmd_sys_saves_text():
    manager = _Manager()
    runtime = _runtime(manager)
    update, message = _update()

    await runtime_sys.cmd_sys(runtime, update, _context("3", "save", "hello", "world"))

    assert manager.saved == [("3", "hello world")]
    assert message.calls[-1] == ("SAVE:3:hello world", {})


@pytest.mark.asyncio
async def test_cmd_sys_shows_usage_for_unknown_subcommand():
    manager = _Manager()
    runtime = _runtime(manager)
    update, message = _update()

    await runtime_sys.cmd_sys(runtime, update, _context("4", "weird"))

    assert "/sys output <n> - return raw content of slot" in message.calls[-1][0]
