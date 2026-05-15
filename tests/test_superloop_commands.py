from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.runtime_superloop import handle_superloop_command


class _FakeRuntime:
    def __init__(self, root: Path):
        self.name = "zelda"
        self.global_config = SimpleNamespace(project_root=root)
        self.messages: list[str] = []

    async def _reply_text(self, _update, text: str, **_kwargs):
        self.messages.append(text)


class _FakeUpdate:
    def __init__(self, text: str):
        self.message = SimpleNamespace(text=text)


@pytest.mark.asyncio
async def test_superloop_record_start_try_finish_status(tmp_path: Path) -> None:
    runtime = _FakeRuntime(tmp_path)

    await handle_superloop_command(runtime, _FakeUpdate("/superloop record start test loop goal"), "record start test loop goal")
    assert any("recording started" in text.lower() for text in runtime.messages)
    rec_line = next(text for text in runtime.messages if "recording_id:" in text)
    recording_id = rec_line.split("`")[1]

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop record try {recording_id} first step"), f"record try {recording_id} first step")
    assert any("Recorded trial step" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop record finish {recording_id}"), f"record finish {recording_id}")
    compiled_text = next(text for text in runtime.messages if "Superloop compiled" in text)
    loop_id = compiled_text.split("`")[3]
    assert loop_id.startswith("sl-")

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop record status {recording_id}"), f"record status {recording_id}")
    assert any("recording status" in text.lower() for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop status {loop_id}"), f"status {loop_id}")
    assert any("Superloop status" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop resume {loop_id}"), f"resume {loop_id}")
    assert any("Resumed" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop next {loop_id}"), f"next {loop_id}")
    assert any("Next action evaluated" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop task add {loop_id} review notes"), f"task add {loop_id} review notes")
    assert any("task added" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop issue add {loop_id} reviewer missing"), f"issue add {loop_id} reviewer missing")
    assert any("issue opened" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop wait add {loop_id} await_hchat_reply"), f"wait add {loop_id} await_hchat_reply")
    assert any("wait added" in text for text in runtime.messages)
