"""Office deadlines honor selected duration and still reap cancelled workers."""

import asyncio
import sys

import pytest

from tools.office import runtime as office_runtime


def configure_runtime(tmp_path, monkeypatch, worker_code, timeout):
    worker = tmp_path / "worker.py"
    worker.write_text(worker_code, encoding="utf-8")
    runtime = office_runtime.OfficeRuntime({"timeout_seconds": timeout})
    monkeypatch.setattr(runtime, "manifest", lambda: {
        "python": sys.executable, "worker": str(worker), "pins": {}, "converter": None,
        "transport_capabilities": ["resource-budgets-v1"],
    })
    return runtime


def test_long_deadline_is_not_clamped_and_is_sent_to_worker(tmp_path, monkeypatch):
    worker = (
        "import hashlib,json,sys\n"
        "r=json.load(sys.stdin)\n"
        "v={'status':'ok','deadline':r['deadline']}\n"
        "data=json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()\n"
        "print(json.dumps({'protocol':1,'request_id':r['request_id'],'result':v,"
        "'sha256':hashlib.sha256(data).hexdigest()}))\n"
    )
    runtime = configure_runtime(tmp_path, monkeypatch, worker, 321.5)
    clock = [1000.0]
    monkeypatch.setattr(office_runtime.time, "time", lambda: clock[0])
    wait_for = asyncio.wait_for
    waits = []

    async def advance_past_old_limit(awaitable, timeout):
        waits.append(timeout)
        clock[0] += 121
        return await wait_for(awaitable, 10)

    monkeypatch.setattr(office_runtime.asyncio, "wait_for", advance_past_old_limit)
    response = asyncio.run(runtime._invoke_staged("inspect", {}, tmp_path / "job"))
    assert waits == [321.5]
    assert clock[0] == 1121.0
    assert response["deadline"] == 1321.5


@pytest.mark.parametrize("cancel", [False, True])
def test_short_deadline_or_cancellation_reaps_worker(tmp_path, monkeypatch, cancel):
    runtime = configure_runtime(tmp_path, monkeypatch, "import time; time.sleep(20)\n", 0.2)
    original = asyncio.create_subprocess_exec
    started = []

    async def start(*args, **kwargs):
        process = await original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(office_runtime.asyncio, "create_subprocess_exec", start)

    async def run():
        task = asyncio.create_task(runtime._invoke_staged("inspect", {}, tmp_path / "job"))
        if cancel:
            while not started and not task.done():
                await asyncio.sleep(0.01)
            if not started:
                await task  # Surface a startup failure instead of waiting forever.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(asyncio.TimeoutError):
                await task

    asyncio.run(run())
    assert started and all(process.returncode is not None for process in started)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), "invalid"])
def test_invalid_deadline_is_reported(value):
    with pytest.raises(office_runtime.RuntimeUnavailable, match="positive finite"):
        office_runtime.OfficeRuntime({"timeout_seconds": value})
# HASHI-Fingerprint: hfp_c6d3e409984249e088024e2b0dd4e80d
