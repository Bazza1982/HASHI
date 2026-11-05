"""Script heartbeat integration with durable results and cleanup, without an OS sandbox claim."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.result_store import EncryptedResultStore
from orchestrator.permissions.script_runner import RestrictedScriptRuns
from tests.test_permission_runner_records import environment


@pytest.mark.asyncio
@pytest.mark.parametrize("sink_fails", [False, True])
async def test_running_script_heartbeat_preserves_result_and_cleanup(tmp_path, monkeypatch, sink_fails):
    binding, records, _, profiles = environment(tmp_path)
    operation = "running-script"
    declaration = records.reserve(binding, operation_id=operation)
    scratch = Path(declaration["scratch"])
    scratch.mkdir()
    (scratch / "outputs").mkdir()
    (scratch / "stdout.txt").write_bytes(b"native result\n")
    (scratch / "stderr.txt").write_text("", encoding="utf-8")
    args = {"script": "print('native result')", "inputs": [], "outputs": []}
    descriptor = describe_tool("run_script", args, cwd=tmp_path, read_roots=(tmp_path,))
    results = EncryptedResultStore(tmp_path / "results.sqlite", key=bytes(range(32)))
    actions = []
    class Process:
        polls = 0
        def start(self):
            actions.append("start")
        def poll(self):
            self.polls += 1
            return self.polls > 1
        def exit_code(self):
            return 0
        def stop(self, **kwargs):
            actions.append("stop")
    scripts = RestrictedScriptRuns.__new__(RestrictedScriptRuns)
    scripts.runner, scripts.results = SimpleNamespace(records=records), results
    scripts._closing = False
    scripts._within_scratch_limit = lambda record: True
    profile = SimpleNamespace(_closed=False)
    scripts._owned = {scripts._identity(binding, operation): {
        "binding": binding, "peer": 42, "lock": asyncio.Lock(), "started": False,
        "process": Process(), "parents": [], "profile": profile,
        "arguments": args, "descriptor": descriptor, "scratch": scratch,
    }}
    events = []
    async def emit(event):
        events.append(event)
        if sink_fails:
            raise OSError("private channel lost")
    monkeypatch.setattr("orchestrator.permissions.script_runner.current_peer",
                        lambda: SimpleNamespace(process_id=42))
    monkeypatch.setattr("orchestrator.permissions.script_runner.emit_private_event", emit)
    if sink_fails:
        with pytest.raises(OSError, match="private channel lost"):
            await scripts.run(binding=binding, operation_id=operation)
        assert results.get(binding.context, operation, descriptor) is None
    else:
        result = await scripts.run(binding=binding, operation_id=operation)
        assert result["outcome"] == "succeeded"
        assert '"stdout": "native result\\n"' in result["payload"]
        assert results.get(binding.context, operation, descriptor).result_digest == result["result_digest"]
    assert len(events) == 1
    assert events[0].kind == "tool_activity"
    assert events[0].metadata["liveness"] is True
    assert events[0].metadata["progress"] is False
    assert actions.count("start") == 1 and "stop" in actions
    assert not scripts._owned and not records.pending() and not scratch.exists()
    assert profile._closed and profiles == [declaration["profile"]]
# HASHI-Fingerprint: hfp_43b03495387340babfff1e6fbabfc7e6
