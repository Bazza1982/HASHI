import asyncio
import json
from types import SimpleNamespace

import pytest
from orchestrator import lifecycle_timing as timing


@pytest.mark.asyncio
async def test_concurrent_lifecycle_intervals_preserve_identity_and_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(timing, "_home", tmp_path)
    monkeypatch.setattr(timing, "_monitor_enabled", False)
    clock = [1_000_000]
    monkeypatch.setattr(timing.time, "perf_counter_ns", lambda: clock[0])

    class Host:
        def __init__(self, agent):
            self.bootstrap = {"bridge_home": str(tmp_path), "manifest": {"generation_id": "generation-1"}}
            self.agent_name = agent

        @timing.timed("worker.prepare")
        async def prepare(self, fail=False, secret="do-not-record"):
            await asyncio.sleep(0)
            clock[0] += 20_000_000
            if fail:
                raise ValueError(secret)
            return self.agent_name

    results = await asyncio.gather(Host("a").prepare(), Host("b").prepare(True), return_exceptions=True)
    assert results[0] == "a" and isinstance(results[1], ValueError)
    text = next((tmp_path / "logs/lifecycle").glob("*.jsonl")).read_text("utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    ends = [r for r in rows if r["event"] == "end"]
    assert {r["agent"]: r["outcome"] for r in ends} == {"a": "ok", "b": "error"}
    assert len({r["span_id"] for r in ends}) == 2
    assert {r["agent"]: r["duration_ms"] for r in ends} == {"a": 20, "b": 40}
    assert all(r["generation_id"] == "generation-1" for r in ends)
    assert "do-not-record" not in text


@pytest.mark.asyncio
async def test_cancelled_lifecycle_records_end_without_suppressing_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(timing, "_home", tmp_path)
    monkeypatch.setattr(timing, "_monitor_enabled", False)
    entered = asyncio.Event()

    @timing.timed("restart.wait")
    async def wait():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(wait())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = [json.loads(line) for line in next((tmp_path / "logs/lifecycle").glob("*.jsonl")).read_text("utf-8").splitlines()]
    assert rows[-1]["event"] == "end" and rows[-1]["outcome"] == "error"


def test_diagnostic_write_failure_does_not_change_runtime_result(tmp_path, monkeypatch):
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory")
    monkeypatch.setattr(timing, "_home", tmp_path)
    monkeypatch.setattr(timing, "_monitor_enabled", False)

    @timing.timed("qualification")
    def qualify(payload):
        return {"qualified": True}
    assert qualify({}) == {"qualified": True}
# HASHI-Fingerprint: hfp_0a15e05327534d6e98d0c5814e599d9c
