from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_controls


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(tmp_path: Path):
    replies = []
    runtime = SimpleNamespace()
    runtime._verbose = False
    runtime._think = False
    runtime.workspace_dir = tmp_path
    runtime._is_authorized_user = lambda user_id: True

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_verbose_toggles_and_persists(tmp_path: Path):
    runtime, replies = _runtime(tmp_path)

    await runtime_controls.cmd_verbose(runtime, _update(), _context())

    assert runtime._verbose is True
    assert not (tmp_path / ".verbose_off").exists()
    assert "Verbose mode: ON 🔍" in replies[-1][0]

    await runtime_controls.cmd_verbose(runtime, _update(), _context("off"))

    assert runtime._verbose is False
    assert (tmp_path / ".verbose_off").exists()
    assert "Verbose mode: OFF" in replies[-1][0]


@pytest.mark.asyncio
async def test_cmd_think_toggles_and_persists(tmp_path: Path):
    runtime, replies = _runtime(tmp_path)

    await runtime_controls.cmd_think(runtime, _update(), _context("on"))

    assert runtime._think is True
    assert not (tmp_path / ".think_off").exists()
    assert "Thinking display: ON 💭" in replies[-1][0]

    await runtime_controls.cmd_think(runtime, _update(), _context())

    assert runtime._think is False
    assert (tmp_path / ".think_off").exists()
    assert "Thinking display: OFF" in replies[-1][0]
