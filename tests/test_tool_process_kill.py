from __future__ import annotations

import os

import pytest
import psutil

from tools.builtins import execute_process_kill, execute_process_list


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


@pytest.mark.asyncio
async def test_process_kill_refuses_runtime_ancestors_before_signalling(monkeypatch):
    from types import SimpleNamespace
    signals = []
    class Process:
        def parents(self):
            return [SimpleNamespace(pid=456789)]
        def name(self):
            return 'runtime-grandparent'
        def terminate(self):
            signals.append('terminate')
        def kill(self):
            signals.append('kill')
    monkeypatch.setattr(psutil, 'Process', lambda _pid: Process())
    result = await execute_process_kill({'pid':456789})
    assert result.startswith('Error: refusing to terminate HASHI process')
    assert signals == []


@pytest.mark.asyncio
async def test_process_list_survives_unavailable_runtime_ancestry(monkeypatch):
    from types import SimpleNamespace

    def inaccessible(_pid):
        raise psutil.AccessDenied(pid=_pid)

    rows = [
        SimpleNamespace(info={"pid": os.getpid(), "name": "fixture-runtime",
                              "cpu_percent": 0, "memory_percent": 0, "cmdline": []}),
        SimpleNamespace(info={"pid": 456789, "name": "fixture-independent",
                              "cpu_percent": 0, "memory_percent": 0, "cmdline": []}),
    ]
    monkeypatch.setattr(psutil, "Process", inaccessible)
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter(rows))
    result = await execute_process_list({})
    assert "fixture-independent" in result
    assert "fixture-runtime [runtime protected]" in result
    assert "runtime ancestry could not be verified" in result



@pytest.mark.asyncio
async def test_process_kill_still_refuses_unverified_ancestry(monkeypatch):
    signals = []

    def inaccessible(pid):
        raise psutil.AccessDenied(pid=pid)

    monkeypatch.setattr(psutil, "Process", inaccessible)
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))
    result = await execute_process_kill({"pid": 456789, "signal": 15})
    assert "runtime process ancestry could not be verified" in result
    assert signals == []


@pytest.mark.asyncio
async def test_empty_process_list_still_reports_unverified_ancestry(monkeypatch):
    def inaccessible(pid):
        raise psutil.AccessDenied(pid=pid)

    monkeypatch.setattr(psutil, "Process", inaccessible)
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter(()))
    result = await execute_process_list({"filter": "absent"})
    assert "runtime ancestry could not be verified" in result
    assert "No matching processes found." in result
