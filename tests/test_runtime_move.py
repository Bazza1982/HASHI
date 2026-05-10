from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_move


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _callback_update(data):
    answers = []
    edits = []

    async def answer(text=None, show_alert=False):
        answers.append((text, show_alert))

    async def edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    return SimpleNamespace(
        callback_query=SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            data=data,
            answer=answer,
            edit_message_text=edit_message_text,
        )
    ), answers, edits


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(instances=None):
    replies = []
    move_calls = []
    target_picker_calls = []
    agent_picker_calls = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _load_instances=lambda: instances or {},
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def _do_move(update, agent_id, target, known_instances, keep_source=False, sync=False, dry_run=False):
        move_calls.append((agent_id, target, known_instances, keep_source, sync, dry_run))

    async def _move_show_target_picker(update, agent_id, known_instances):
        target_picker_calls.append((agent_id, known_instances))

    async def _move_show_agent_picker(update, known_instances):
        agent_picker_calls.append(known_instances)

    runtime._reply_text = _reply_text
    runtime._do_move = _do_move
    runtime._move_show_target_picker = _move_show_target_picker
    runtime._move_show_agent_picker = _move_show_agent_picker
    return runtime, replies, move_calls, target_picker_calls, agent_picker_calls


@pytest.mark.asyncio
async def test_cmd_move_reports_missing_instances():
    runtime, replies, *_ = _runtime(instances={})

    await runtime_move.cmd_move(runtime, _update(), _context())

    assert replies[-1][0] == "⚠️ No instances.json found. Create one at the project root."


@pytest.mark.asyncio
async def test_cmd_move_lists_instances():
    instances = {"hashi1": {"display_name": "HASHI1", "root": "/srv/h1"}}
    runtime, replies, *_ = _runtime(instances=instances)

    await runtime_move.cmd_move(runtime, _update(), _context("list"))

    assert "<b>Known HASHI Instances:</b>" in replies[-1][0]
    assert "<code>hashi1</code>" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_cmd_move_executes_direct_move():
    instances = {"hashi1": {"display_name": "HASHI1"}}
    runtime, replies, move_calls, *_ = _runtime(instances=instances)

    await runtime_move.cmd_move(runtime, _update(), _context("sunny", "hashi1", "--keep-source", "--sync"))

    assert move_calls == [("sunny", "hashi1", instances, True, True, False)]


@pytest.mark.asyncio
async def test_cmd_move_shows_target_picker_for_agent():
    instances = {"hashi1": {"display_name": "HASHI1"}}
    runtime, replies, move_calls, target_picker_calls, agent_picker_calls = _runtime(instances=instances)

    await runtime_move.cmd_move(runtime, _update(), _context("sunny"))

    assert target_picker_calls == [("sunny", instances)]
    assert move_calls == []
    assert agent_picker_calls == []


@pytest.mark.asyncio
async def test_cmd_move_shows_agent_picker_without_args():
    instances = {"hashi1": {"display_name": "HASHI1"}}
    runtime, replies, move_calls, target_picker_calls, agent_picker_calls = _runtime(instances=instances)

    await runtime_move.cmd_move(runtime, _update(), _context())

    assert agent_picker_calls == [instances]
    assert move_calls == []
    assert target_picker_calls == []


@pytest.mark.asyncio
async def test_callback_move_cancels():
    runtime, _replies, move_calls, _target_picker_calls, _agent_picker_calls = _runtime(instances={"hashi1": {"display_name": "HASHI1"}})
    update, answers, edits = _callback_update("move:cancel")

    await runtime_move.callback_move(runtime, update, SimpleNamespace())

    assert answers[-1] == (None, False)
    assert edits[-1][0] == "Move cancelled."
    assert move_calls == []


@pytest.mark.asyncio
async def test_callback_move_executes_selected_mode():
    instances = {"hashi1": {"display_name": "HASHI1"}}
    runtime, _replies, move_calls, _target_picker_calls, _agent_picker_calls = _runtime(instances=instances)
    update, answers, edits = _callback_update("move:exec:sunny:hashi1:keep")

    await runtime_move.callback_move(runtime, update, SimpleNamespace())

    assert answers[-1] == (None, False)
    assert edits == []
    assert move_calls == [("sunny", "hashi1", instances, True, False, False)]
