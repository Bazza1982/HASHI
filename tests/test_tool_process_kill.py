from __future__ import annotations

import os

import pytest
import psutil

from tools.builtins import execute_process_kill


@pytest.mark.asyncio
async def test_process_kill_refuses_own_runtime_process(monkeypatch):
    signals = []

    class SafeProcess:
        def name(self):
            return "hashi-test"

        def terminate(self):
            signals.append("terminate")

        def kill(self):
            signals.append("kill")

    monkeypatch.setattr(psutil, "Process", lambda _pid: SafeProcess())
    result = await execute_process_kill({"pid": os.getpid()})
    assert result.startswith("Error: refusing to terminate HASHI process")
    assert signals == []
