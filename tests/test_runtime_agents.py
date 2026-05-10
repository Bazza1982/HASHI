from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_agents


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


class _RuntimeRef:
    def __init__(self, name: str):
        self.name = name


class _Orchestrator:
    def __init__(self):
        self.started = []
        self.stopped = []
        self.deleted = []
        self.active_changes = []
        self._agents = [
            {"name": "lin_yueru", "display_name": "Lin Yueru", "is_active": True},
            {"name": "barry", "display_name": "Barry", "is_active": True},
        ]
        self.runtimes = [_RuntimeRef("barry")]
        self._startup_tasks = {}
        self.add_calls = []

    def get_all_agents_raw(self):
        return list(self._agents)

    def _runtime_map(self):
        return {runtime.name: runtime for runtime in self.runtimes}

    async def start_agent(self, name):
        self.started.append(name)
        return True, f"started {name}"

    async def stop_agent(self, name):
        self.stopped.append(name)
        return True, f"stopped {name}"

    def set_agent_active(self, name, value):
        self.active_changes.append((name, value))

    def delete_agent_from_config(self, name):
        self.deleted.append(name)

    def add_agent_to_config(self, new_id, display_name, token):
        self.add_calls.append((new_id, display_name, token))
        return True, f"added {new_id}"


def _runtime(orchestrator=None):
    replies = []
    runtime = SimpleNamespace(
        name="lin_yueru",
        orchestrator=orchestrator,
        _is_authorized_user=lambda user_id: True,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_agents_shows_agents_view():
    runtime, replies = _runtime(_Orchestrator())

    await runtime_agents.cmd_agents(runtime, _update(), _context())

    assert "<b>📋 HASHI Agents</b>" in replies[-1][0]
    assert "barry" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_cmd_agents_add_forwards_to_orchestrator():
    orchestrator = _Orchestrator()
    runtime, replies = _runtime(orchestrator)

    await runtime_agents.cmd_agents(runtime, _update(), _context("add", "sunny", "Sunny", "123:abc"))

    assert orchestrator.add_calls == [("sunny", "Sunny", "123:abc")]
    assert replies[-1][0] == "added sunny"


@pytest.mark.asyncio
async def test_callback_agents_refresh_updates_message():
    orchestrator = _Orchestrator()
    runtime, _replies = _runtime(orchestrator)
    answers = []
    edits = []

    async def _answer(text=None, **kwargs):
        answers.append((text, kwargs))

    async def _edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        data="agents:refresh",
        answer=_answer,
        edit_message_text=_edit_message_text,
    )

    await runtime_agents.callback_agents(runtime, SimpleNamespace(callback_query=query), _context())

    assert answers[-1][0] is None
    assert "<b>📋 HASHI Agents</b>" in edits[-1][0]


@pytest.mark.asyncio
async def test_callback_agents_delete_shows_confirmation():
    orchestrator = _Orchestrator()
    runtime, _replies = _runtime(orchestrator)
    answers = []
    edits = []

    async def _answer(text=None, **kwargs):
        answers.append((text, kwargs))

    async def _edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        data="agents:delete:barry",
        answer=_answer,
        edit_message_text=_edit_message_text,
    )

    await runtime_agents.callback_agents(runtime, SimpleNamespace(callback_query=query), _context())

    assert answers[-1][0] is None
    assert "Delete 'barry'?" in edits[-1][0]
