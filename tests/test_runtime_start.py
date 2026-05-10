from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_start


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


class _Orchestrator:
    def __init__(self, names=None):
        self.names = list(names or [])
        self.started = []

    def get_startable_agent_names(self, *, exclude_name):
        return [name for name in self.names if name != exclude_name]

    async def start_agent(self, name):
        self.started.append(name)
        return True, f"started {name}"


def _runtime(orchestrator=None):
    replies = []
    runtime = SimpleNamespace(
        name="lin_yueru",
        orchestrator=orchestrator,
        _is_authorized_user=lambda user_id: True,
        _startable_agent_keyboard=lambda: "kbd",
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_start_shows_picker_when_agents_available():
    runtime, replies = _runtime(_Orchestrator(["lin_yueru", "barry"]))

    await runtime_start.cmd_start(runtime, _update(), _context())

    assert replies[-1][0] == "Start another agent:"
    assert replies[-1][1]["reply_markup"] == "kbd"


@pytest.mark.asyncio
async def test_cmd_start_all_starts_all_available_agents():
    orchestrator = _Orchestrator(["lin_yueru", "barry", "sunny"])
    runtime, replies = _runtime(orchestrator)

    await runtime_start.cmd_start(runtime, _update(), _context("all"))

    assert orchestrator.started == ["barry", "sunny"]
    assert replies[-1][0] == "started barry\nstarted sunny"


@pytest.mark.asyncio
async def test_callback_start_agent_handles_all():
    orchestrator = _Orchestrator(["lin_yueru", "barry"])
    runtime, _replies = _runtime(orchestrator)
    answers = []
    edits = []

    async def _answer(text=None, **kwargs):
        answers.append((text, kwargs))

    async def _edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        data="startagent:__all__",
        answer=_answer,
        edit_message_text=_edit_message_text,
    )
    update = SimpleNamespace(callback_query=query)

    await runtime_start.callback_start_agent(runtime, update, _context())

    assert orchestrator.started == ["barry"]
    assert answers[-1][0] == "Starting all agents..."
    assert edits[-1][0] == "started barry"


@pytest.mark.asyncio
async def test_callback_start_agent_handles_single_target():
    orchestrator = _Orchestrator(["lin_yueru", "barry"])
    runtime, _replies = _runtime(orchestrator)
    answers = []
    edits = []

    async def _answer(text=None, **kwargs):
        answers.append((text, kwargs))

    async def _edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        data="startagent:barry",
        answer=_answer,
        edit_message_text=_edit_message_text,
    )
    update = SimpleNamespace(callback_query=query)

    await runtime_start.callback_start_agent(runtime, update, _context())

    assert orchestrator.started == ["barry"]
    assert answers[-1][0] == "Starting barry..."
    assert edits[-1][0] == "started barry"
    assert edits[-1][1]["reply_markup"] == "kbd"
