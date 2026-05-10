from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_usecomputer
from orchestrator.bridge_memory import SysPromptManager


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(tmp_path: Path):
    replies = []
    queued = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        sys_prompt_manager=SysPromptManager(tmp_path),
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def enqueue_request(chat_id, prompt, source, summary):
        queued.append((chat_id, prompt, source, summary))

    runtime._reply_text = _reply_text
    runtime.enqueue_request = enqueue_request
    return runtime, replies, queued


@pytest.mark.asyncio
async def test_cmd_usecomputer_reports_usage(tmp_path):
    runtime, replies, queued = _runtime(tmp_path)

    await runtime_usecomputer.cmd_usecomputer(runtime, _update(), _context())

    assert "/usecomputer on - enable managed GUI-aware mode" in replies[-1][0]
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_usecomputer_reports_status(tmp_path):
    runtime, replies, queued = _runtime(tmp_path)

    await runtime_usecomputer.cmd_usecomputer(runtime, _update(), _context("status"))

    assert "/usecomputer is OFF" in replies[-1][0]


@pytest.mark.asyncio
async def test_cmd_usecomputer_enables_mode(tmp_path):
    runtime, replies, queued = _runtime(tmp_path)

    await runtime_usecomputer.cmd_usecomputer(runtime, _update(), _context("on"))

    assert "/usecomputer is ON via /sys 10." in replies[-1][0]


@pytest.mark.asyncio
async def test_cmd_usecomputer_enqueues_task(tmp_path):
    runtime, replies, queued = _runtime(tmp_path)

    await runtime_usecomputer.cmd_usecomputer(runtime, _update(), _context("Please", "use", "NVivo"))

    assert replies[-1][0] == "Running in /usecomputer mode..."
    assert queued == [(456, "The user wants this handled in /usecomputer mode.\nTreat GUI/desktop control as available when needed, but do not force it if a better non-GUI method exists.\n\nTask:\nPlease use NVivo", "usecomputer", "Computer-use task")]


@pytest.mark.asyncio
async def test_cmd_usercomputer_alias_delegates(tmp_path):
    runtime, replies, queued = _runtime(tmp_path)

    await runtime_usecomputer.cmd_usercomputer(runtime, _update(), _context("examples"))

    assert "/usecomputer on" in replies[-1][0]
