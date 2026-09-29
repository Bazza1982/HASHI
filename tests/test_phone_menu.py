from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.pcm import render_pcm_document
from orchestrator.phone_manager import PhoneManager


def _runtime(tmp_path):
    workspace = tmp_path / "moon"
    workspace.mkdir()
    (workspace / "agent.md").write_text(
        render_pcm_document(
            persona="You are Moon and speak respectfully.",
            system="Private runtime rules.",
        ),
        encoding="utf-8",
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "moon"
    runtime.config = SimpleNamespace(extra={"display_name": "Moon"})
    runtime.phone_manager = PhoneManager(workspace)
    runtime._is_authorized_user = lambda _user_id: True
    runtime._reply_text = AsyncMock()
    return runtime


@pytest.mark.asyncio
async def test_phone_command_persists_voice_and_custom_style(tmp_path):
    runtime = _runtime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7))

    await runtime.cmd_phone(update, SimpleNamespace(args=["voice", "willow"]))
    await runtime.cmd_phone(
        update,
        SimpleNamespace(args=["instructions", "Speak", "a", "little", "slower."]),
    )

    state = runtime.phone_manager.get_state()
    assert state["voice"] == "willow"
    assert state["style_instructions"] == "Speak a little slower."
    rendered = runtime._reply_text.await_args.args[1]
    assert "Willow" in rendered
    assert "Speak a little slower." in rendered


@pytest.mark.asyncio
async def test_phone_callback_changes_style_and_returns_to_summary(tmp_path):
    runtime = _runtime(tmp_path)

    class Query:
        data = "phone:style:calm"
        from_user = SimpleNamespace(id=7)

        def __init__(self):
            self.edits = []
            self.answers = []

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

    query = Query()
    await runtime.callback_phone(SimpleNamespace(callback_query=query), SimpleNamespace())

    assert runtime.phone_manager.get_state()["style"] == "calm"
    assert len(query.edits) == 1
    assert query.edits[0][1]["parse_mode"] == "HTML"
    assert query.answers
