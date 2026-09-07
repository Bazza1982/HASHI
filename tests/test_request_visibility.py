from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator import runtime_session
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["protocol:message", "protocol:reply", "api", "handoff"])
async def test_legacy_hidden_request_is_admitted_as_visible(tmp_path, monkeypatch, source):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "visibility"
    runtime.global_config = SimpleNamespace(project_root=None)
    runtime.next_request_id = lambda: "req-visible"
    runtime.session_store = SimpleNamespace(session_workspace=lambda *args: tmp_path)
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    monkeypatch.setattr(runtime_session, "accept_request", lambda *args, **kwargs: (
        {"session_id": "session-visible", "context_generation": 1},
        SimpleNamespace(replayed=False, run_id="run-visible", message_id="message-visible"),
        "user:123", "workbench", "default",
    ))
    monkeypatch.setattr(runtime_session, "session_workzone_state", lambda *args, **kwargs: {})

    result = await runtime.enqueue_request(
        123, "Visible work", source, "Visible work",
        deliver_to_telegram=False, silent=True,
    )

    item = runtime.queue.get_nowait()
    assert result == item.request_id == "req-visible"
    assert item.deliver_to_telegram is True
    assert item.silent is False
    assert item.chat_id == 123
    assert item.session_id == "session-visible"
