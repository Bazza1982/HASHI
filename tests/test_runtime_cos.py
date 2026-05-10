from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_cos


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(name="sunny", cos_enabled=False, tmp_path=None):
    replies = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        name=name,
        _cos_enabled=cos_enabled,
        workspace_dir=tmp_path or Path("/tmp"),
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_cos_blocks_for_lily(tmp_path):
    runtime, replies = _runtime(name="lily", tmp_path=tmp_path)

    await runtime_cos.cmd_cos(runtime, _update(), _context())

    assert replies[-1][0] == "Lily cannot use /cos — that would go in circles 🌸"


@pytest.mark.asyncio
async def test_cmd_cos_reports_status(tmp_path):
    runtime, replies = _runtime(cos_enabled=True, tmp_path=tmp_path)

    await runtime_cos.cmd_cos(runtime, _update(), _context())

    assert replies[-1][0] == "Chief of Staff routing: ON ✅\nUse /cos on or /cos off to toggle."


@pytest.mark.asyncio
async def test_cmd_cos_enables_routing(tmp_path):
    runtime, replies = _runtime(tmp_path=tmp_path)

    await runtime_cos.cmd_cos(runtime, _update(), _context("on"))

    assert runtime._cos_enabled is True
    assert (tmp_path / ".cos_on").exists()
    assert "enabled" in replies[-1][0]


@pytest.mark.asyncio
async def test_cmd_cos_disables_routing(tmp_path):
    marker = tmp_path / ".cos_on"
    marker.touch()
    runtime, replies = _runtime(cos_enabled=True, tmp_path=tmp_path)

    await runtime_cos.cmd_cos(runtime, _update(), _context("off"))

    assert runtime._cos_enabled is False
    assert not marker.exists()
    assert "disabled" in replies[-1][0]


@pytest.mark.asyncio
async def test_cmd_cos_reports_usage_for_unknown_arg(tmp_path):
    runtime, replies = _runtime(tmp_path=tmp_path)

    await runtime_cos.cmd_cos(runtime, _update(), _context("weird"))

    assert replies[-1][0] == "Usage: /cos [on|off]"
