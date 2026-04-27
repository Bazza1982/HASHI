from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from adapters.codex_cli import CodexCLIAdapter
from tests.mocks.mock_adapters import SimpleGlobalConfig, SimpleTestConfig


class _FakeStdout:
    def __init__(self, proc: "_HangingProc", lines: list[str]):
        self._proc = proc
        self._lines = [line.encode("utf-8") + b"\n" for line in lines]

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        await self._proc.wait()
        return b""


class _FakeStderr:
    def __init__(self, proc: "_HangingProc"):
        self._proc = proc

    async def read(self, _size: int) -> bytes:
        await self._proc.wait()
        return b""


class _HangingProc:
    def __init__(self, lines: list[str], pid: int = 12345):
        self.pid = pid
        self.returncode = None
        self.stdin = None
        self._exit_event = asyncio.Event()
        self.stdout = _FakeStdout(self, lines)
        self.stderr = _FakeStderr(self)

    async def wait(self) -> int:
        await self._exit_event.wait()
        return int(self.returncode or 0)

    def finish(self, code: int) -> None:
        self.returncode = code
        self._exit_event.set()


def _build_adapter(tmp_path: Path) -> CodexCLIAdapter:
    cfg = SimpleTestConfig(name="hashiko", workspace_dir=str(tmp_path))
    cfg.model = "gpt-5.4"
    global_cfg = SimpleGlobalConfig()
    return CodexCLIAdapter(cfg, global_cfg)


def test_codex_accepts_completed_turn_even_if_process_needs_forced_exit(tmp_path, monkeypatch: pytest.MonkeyPatch):
    adapter = _build_adapter(tmp_path)
    adapter.POST_TURN_COMPLETION_GRACE_SEC = 0.01

    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thread_123"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final answer"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ]
    proc = _HangingProc(lines)
    killed_reasons: list[str] = []

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    async def _fake_force_kill_process_tree(proc_obj, logger=None, reason: str = ""):
        killed_reasons.append(reason)
        proc_obj.finish(-9)
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(adapter, "force_kill_process_tree", _fake_force_kill_process_tree)

    response = asyncio.run(adapter.generate_response("hello", "req-0001"))

    assert response.is_success is True
    assert response.text == "final answer"
    assert response.usage is not None
    assert response.usage.input_tokens == 10
    assert adapter._session_id == "thread_123"
    assert killed_reasons == ["turn-completed-grace-expired:req-0001"]


def test_codex_idle_timeout_is_enforced_when_process_stalls(tmp_path, monkeypatch: pytest.MonkeyPatch):
    adapter = _build_adapter(tmp_path)
    adapter.config.extra["idle_timeout_sec"] = 1
    adapter.config.extra["hard_timeout_sec"] = 30
    proc = _HangingProc([])
    killed_reasons: list[str] = []

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    async def _fake_force_kill_process_tree(proc_obj, logger=None, reason: str = ""):
        killed_reasons.append(reason)
        proc_obj.finish(-9)
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(adapter, "force_kill_process_tree", _fake_force_kill_process_tree)

    response = asyncio.run(adapter.generate_response("hello", "req-0002"))

    assert response.is_success is False
    assert "idle for 1s" in (response.error or "")
    assert killed_reasons == ["idle-timeout:req-0002"]
