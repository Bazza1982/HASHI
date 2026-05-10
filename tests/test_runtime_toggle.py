from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_toggle


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
            message=SimpleNamespace(chat_id=456),
            answer=answer,
            edit_message_text=edit_message_text,
        )
    ), answers, edits


def _runtime():
    sent = []
    queued = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        workspace_dir=Path("/tmp/toggle-workspace"),
        _verbose=False,
        _think=False,
        last_response=None,
        last_prompt=None,
        name="lily",
        skill_manager=None,
    )

    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)

    async def send_long_message(chat_id, text, request_id=None, purpose=None):
        sent.append((chat_id, text, request_id, purpose))

    async def enqueue_request(chat_id, prompt, source, summary):
        queued.append((chat_id, prompt, source, summary))

    runtime.send_long_message = send_long_message
    runtime.enqueue_request = enqueue_request
    runtime._load_last_text_from_transcript = lambda role: None
    runtime._active_keyboard = lambda: "ACTIVE-KBD"
    return runtime, sent, queued


@pytest.mark.asyncio
async def test_callback_toggle_toggles_verbose():
    runtime, _sent, _queued = _runtime()
    update, answers, edits = _callback_update("tgl:verbose:on")

    await runtime_toggle.callback_toggle(runtime, update, SimpleNamespace())

    assert runtime._verbose is True
    assert edits[-1][0] == "Verbose mode: ON 🔍"
    assert answers[-1] == ("Verbose ON 🔍", False)


@pytest.mark.asyncio
async def test_callback_toggle_retries_last_prompt():
    runtime, _sent, queued = _runtime()
    runtime.last_prompt = SimpleNamespace(chat_id=789, prompt="hello")
    update, answers, edits = _callback_update("tgl:retry:prompt")

    await runtime_toggle.callback_toggle(runtime, update, SimpleNamespace())

    assert answers[-1] == ("Retrying prompt...", False)
    assert edits == []
    assert queued == [(789, "hello", "retry", "Retry request")]


@pytest.mark.asyncio
async def test_callback_toggle_updates_active_heartbeat():
    class _SkillManager:
        ACTIVE_HEARTBEAT_DEFAULT_MINUTES = 10

        def set_active_heartbeat(self, name, enabled, minutes=None):
            return True, f"saved:{name}:{enabled}:{minutes}"

        def describe_active_heartbeat(self, name):
            return f"status:{name}"

    runtime, _sent, _queued = _runtime()
    runtime.skill_manager = _SkillManager()
    update, answers, edits = _callback_update("tgl:active:30")

    await runtime_toggle.callback_toggle(runtime, update, SimpleNamespace())

    assert edits[-1][0] == "status:lily\n\nsaved:lily:True:30"
    assert edits[-1][1]["reply_markup"] == "ACTIVE-KBD"
    assert answers[-1] == (None, False)
