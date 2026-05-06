import json
from types import SimpleNamespace

import pytest

from orchestrator import runtime_remote


class _Query:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append({"text": text, **kwargs})


def _runtime(tmp_path):
    replies = []
    return SimpleNamespace(
        global_config=SimpleNamespace(project_root=tmp_path),
        _reply_text=lambda update, text, **kwargs: _reply(replies, text, kwargs),
        replies=replies,
    )


async def _reply(replies, text, kwargs):
    replies.append({"text": text, **kwargs})


def test_load_instances_reads_first_available_file(tmp_path):
    missing = tmp_path / "missing.json"
    path = tmp_path / "instances.json"
    path.write_text(json.dumps({"instances": {"hashi2": {"display_name": "HASHI2"}}}), encoding="utf-8")

    assert runtime_remote.load_instances([missing, path]) == {"hashi2": {"display_name": "HASHI2"}}


@pytest.mark.asyncio
async def test_move_show_agent_picker_lists_agents(tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"agents": [{"name": "zelda"}, {"name": "akane"}]}),
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path)

    await runtime_remote.move_show_agent_picker(runtime, SimpleNamespace(), {})

    assert runtime.replies[-1]["text"] == "<b>Move Agent</b> — select agent to move:"
    buttons = [button for row in runtime.replies[-1]["reply_markup"].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == ["move:agent:zelda", "move:agent:akane"]


@pytest.mark.asyncio
async def test_move_show_target_picker_lists_instances(tmp_path):
    runtime = _runtime(tmp_path)

    await runtime_remote.move_show_target_picker(
        runtime,
        SimpleNamespace(),
        "zelda",
        {"hashi2": {"display_name": "HASHI2"}},
    )

    assert "select target instance" in runtime.replies[-1]["text"]
    button = runtime.replies[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "move:target:zelda:hashi2"


@pytest.mark.asyncio
async def test_move_show_options_edits_callback_message(tmp_path):
    runtime = _runtime(tmp_path)
    update = SimpleNamespace(callback_query=_Query())

    await runtime_remote.move_show_options(runtime, update, "zelda", "hashi2")

    assert "Choose move mode" in update.callback_query.edits[-1]["text"]
    callbacks = [
        button.callback_data
        for row in update.callback_query.edits[-1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "move:exec:zelda:hashi2:plain" in callbacks
    assert "move:cancel" in callbacks
