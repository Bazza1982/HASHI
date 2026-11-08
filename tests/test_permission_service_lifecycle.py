"""Entrypoint lifecycle must work on the SCM thread and retain failed cleanup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from orchestrator.permissions import installation, runner_installation


@pytest.fixture(params=[installation, runner_installation], ids=["safety", "runner"])
def entry(request, monkeypatch):
    calls = []
    options = {"start_error": False, "close_error": False, "stop_during_start": None}

    async def start(*_args):
        calls.append("start")
        if options["stop_during_start"] is not None:
            options["stop_during_start"].set()
        if options["start_error"]:
            raise OSError("endpoint did not start")

    async def close():
        calls.append("close")
        if options["close_error"]:
            raise OSError("cleanup unverified")

    def recover():
        calls.append("recover")

    def release():
        calls.append("release")

    module = request.param
    if module is installation:
        monkeypatch.setattr(module, "build_service", lambda _path: (
            SimpleNamespace(start=start, close=close), (),
        ))
    else:
        monkeypatch.setattr(module, "build_runner", lambda _path: (
            SimpleNamespace(records=SimpleNamespace(recover_all=recover), close=close),
            SimpleNamespace(start=start, close=close), SimpleNamespace(close=release),
        ))
    # The external lifecycle owner must never touch main-thread process signals.
    monkeypatch.setattr(module.signal, "signal", lambda *_args: pytest.fail("signal handler touched"))
    return module, calls, options


def on_service_thread(coroutine):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result(timeout=5)


def test_external_stop_reports_ready_only_after_endpoints_start(entry):
    module, calls, _ = entry

    async def run():
        stopped = asyncio.Event()

        def ready():
            assert calls[-1] == "start"
            calls.append("ready")
            stopped.set()

        await module.serve("private-installation.json", stopped=stopped, on_ready=ready)

    on_service_thread(run())
    assert calls.index("start") < calls.index("ready") < calls.index("close")
    if module is runner_installation:
        assert calls == ["recover", "start", "ready", "close", "close", "release"]


def test_start_failure_never_reports_ready_and_still_closes(entry):
    module, calls, options = entry
    options["start_error"] = True

    async def run():
        await module.serve("private-installation.json", stopped=asyncio.Event(),
                           on_ready=lambda: calls.append("ready"))

    with pytest.raises(OSError, match="did not start"):
        on_service_thread(run())
    assert "ready" not in calls and "close" in calls


def test_failed_shutdown_is_not_reported_as_clean_or_released(entry):
    module, calls, options = entry
    options["close_error"] = True

    async def run():
        stopped = asyncio.Event()
        await module.serve("private-installation.json", stopped=stopped, on_ready=stopped.set)

    with pytest.raises(OSError, match="unverified"):
        on_service_thread(run())
    assert "release" not in calls


def test_stop_before_start_does_not_publish_readiness(entry):
    module, calls, _ = entry

    async def run():
        stopped = asyncio.Event()
        stopped.set()
        await module.serve("private-installation.json", stopped=stopped,
                           on_ready=lambda: calls.append("ready"))

    on_service_thread(run())
    assert "start" not in calls and "ready" not in calls


def test_stop_during_start_never_reports_running(entry):
    module, calls, options = entry

    async def run():
        stopped = asyncio.Event()
        options["stop_during_start"] = stopped
        await module.serve("private-installation.json", stopped=stopped,
                           on_ready=lambda: calls.append("ready"))

    on_service_thread(run())
    assert "start" in calls and "close" in calls and "ready" not in calls


def test_foreground_signals_are_restored_even_when_shutdown_fails(entry, monkeypatch):
    module, calls, options = entry
    options["close_error"] = True
    originals = {kind: object() for kind in (module.signal.SIGINT, module.signal.SIGTERM)}
    handlers = []
    monkeypatch.setattr(module.signal, "getsignal", originals.__getitem__)
    monkeypatch.setattr(module.signal, "signal", lambda kind, handler: handlers.append((kind, handler)))

    def ready():
        handlers[-1][1](None, None)

    with pytest.raises(OSError, match="unverified"):
        asyncio.run(module.serve("private-installation.json", on_ready=ready))
    assert handlers[-2:] == list(originals.items())
    assert "close" in calls and "release" not in calls
