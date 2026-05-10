from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_group


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


def _runtime(directory):
    replies = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        name="lily",
        agent_directory=directory,
        _group_detail_view=lambda directory, name: (f"DETAIL:{name}", f"MARKUP:{name}"),
        _group_list_view=lambda directory: ("LIST", "LISTMARKUP"),
        orchestrator=None,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_group_shows_unavailable_without_directory():
    runtime, replies = _runtime(directory=None)
    runtime.agent_directory = None

    await runtime_group.cmd_group(runtime, _update(), _context())

    assert replies[-1][0] == "❌ Agent directory unavailable."


@pytest.mark.asyncio
async def test_cmd_group_shows_overview_without_args():
    directory = SimpleNamespace()
    runtime, replies = _runtime(directory)

    await runtime_group.cmd_group(runtime, _update(), _context())

    assert replies[-1][0] == "LIST"
    assert replies[-1][1]["parse_mode"] == "HTML"
    assert replies[-1][1]["reply_markup"] == "LISTMARKUP"


@pytest.mark.asyncio
async def test_cmd_group_reports_unknown_named_group():
    class _Directory:
        def group_exists(self, name):
            assert name == "staff"
            return False

    runtime, replies = _runtime(_Directory())

    await runtime_group.cmd_group(runtime, _update(), _context("staff"))

    assert replies[-1][0] == "❌ Group 'staff' not found."


@pytest.mark.asyncio
async def test_cmd_group_creates_new_group():
    class _Directory:
        def create_group(self, name, desc):
            assert name == "staff"
            assert desc == "core team"
            return True, "created"

    runtime, replies = _runtime(_Directory())

    await runtime_group.cmd_group(runtime, _update(), _context("new", "staff", "core", "team"))

    assert replies[-1][0] == "✅ created\n\nDETAIL:staff"
    assert replies[-1][1]["reply_markup"] == "MARKUP:staff"


@pytest.mark.asyncio
async def test_callback_group_shows_list_on_back():
    runtime, _replies = _runtime(SimpleNamespace())
    update, answers, edits = _callback_update("group:back")

    await runtime_group.callback_group(runtime, update, SimpleNamespace())

    assert answers[-1] == (None, False)
    assert edits[-1][0] == "LIST"
    assert edits[-1][1]["reply_markup"] == "LISTMARKUP"


@pytest.mark.asyncio
async def test_callback_group_confirms_delete():
    class _Directory:
        def delete_group(self, name):
            assert name == "staff"
            return True, "deleted"

    runtime, _replies = _runtime(_Directory())
    update, answers, edits = _callback_update("group:delete_confirm:staff")

    await runtime_group.callback_group(runtime, update, SimpleNamespace())

    assert answers[-1] == (None, False)
    assert edits[-1][0] == "🗑 deleted\n\nLIST"
    assert edits[-1][1]["reply_markup"] == "LISTMARKUP"
