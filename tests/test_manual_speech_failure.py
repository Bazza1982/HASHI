"""Real manual-direction rejection survives the task owner and restart."""
import asyncio
from types import SimpleNamespace
import pytest
from orchestrator.speech_performance import prepare
from orchestrator.speech_tasks import SpeechTasks

@pytest.mark.asyncio
async def test_unsupported_manual_direction_keeps_typed_durable_failure(tmp_path):
    manager = SimpleNamespace(
        _require_voice_catalog=lambda: None,
        voice_catalog=SimpleNamespace(service=lambda _: {"adapter": "windows_sapi", "location": "local"}),
    )
    state = {"provider": "windows", "manual_direction": {"enabled": True, "instructions": "Read calmly."}}
    executions = []
    async def execute(_deadline):
        executions.append(True)
        return await prepare(manager, "Acceptance text", None, state=state)
    tasks = SpeechTasks(tmp_path)
    try:
        tasks.start("unsupported-manual", "Acceptance text", "prepare", {}, execute)
        for _ in range(100):
            row = tasks.status("unsupported-manual")
            if row["state"] == "failed":
                break
            await asyncio.sleep(.005)
        assert row["state"] == "failed"
        assert row["error_code"] == "speech_manual_direction_unsupported"
        assert len(executions) == 1
    finally:
        await tasks.close()
    reopened = SpeechTasks(tmp_path)
    try:
        row = reopened.status("unsupported-manual")
        assert row["state"] == "failed"
        assert row["error_code"] == "speech_manual_direction_unsupported"
        reopened.start("unsupported-manual", "Acceptance text", "prepare", {}, execute)
        assert len(executions) == 1
    finally:
        await reopened.close()
