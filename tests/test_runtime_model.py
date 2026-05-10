from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_model


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
            message=SimpleNamespace(chat_id=456),
            answer=answer,
            edit_message_text=edit_message_text,
        )
    ), answers, edits


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(*, active_backend="gemini-cli", current_model="gemini-2.5-flash", available=None):
    replies = []
    set_calls = []
    effort_calls = []
    switch_calls = []
    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend=active_backend),
        backend_manager=SimpleNamespace(current_backend=SimpleNamespace(config=SimpleNamespace(model=current_model))),
        _is_authorized_user=lambda user_id: True,
        _get_available_models=lambda: list(available or []),
        _model_keyboard=lambda model: ("kbd", model),
        _build_backend_menu_text=lambda: "BACKEND MENU",
        _backend_keyboard=lambda: "BACKEND KBD",
        _build_backend_model_prompt=lambda engine, with_context: f"PROMPT:{engine}:{with_context}",
        _backend_model_keyboard=lambda engine, with_context, model=None: ("BACKEND MODEL", engine, with_context, model),
        _get_available_efforts=lambda: ["medium", "high"],
        _effort_keyboard=lambda effort: ("EFFORT", effort),
        error_logger=SimpleNamespace(error=lambda *args, **kwargs: None),
    )

    def _set_backend_model(engine, requested):
        set_calls.append((engine, requested))

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    def _set_active_effort(value):
        effort_calls.append(value)

    async def _switch_backend_mode(chat_id, target_engine, target_model=None, with_context=False):
        switch_calls.append((chat_id, target_engine, target_model, with_context))
        return True, f"switched:{target_engine}:{target_model}:{with_context}"

    runtime._set_backend_model = _set_backend_model
    runtime._reply_text = _reply_text
    runtime._set_active_effort = _set_active_effort
    runtime._switch_backend_mode = _switch_backend_mode
    return runtime, replies, set_calls, effort_calls, switch_calls


@pytest.mark.asyncio
async def test_cmd_model_switches_to_requested_model():
    runtime, replies, set_calls, _effort_calls, _switch_calls = _runtime(available=["gemini-2.5-flash", "gemini-2.5-pro"])

    await runtime_model.cmd_model(runtime, _update(), _context("gemini-2.5-pro"))

    assert set_calls == [("gemini-cli", "gemini-2.5-pro")]
    assert replies[-1][0] == "Model switched to: gemini-2.5-pro"


@pytest.mark.asyncio
async def test_cmd_model_rejects_unknown_model():
    runtime, replies, set_calls, _effort_calls, _switch_calls = _runtime(available=["gemini-2.5-flash"])

    await runtime_model.cmd_model(runtime, _update(), _context("bad-model"))

    assert set_calls == []
    assert replies[-1][0] == "Unknown model: bad-model\nUse /model to see available options."


@pytest.mark.asyncio
async def test_cmd_model_shows_keyboard_when_models_available():
    runtime, replies, _set_calls, _effort_calls, _switch_calls = _runtime(available=["gemini-2.5-flash"])

    await runtime_model.cmd_model(runtime, _update(), _context())

    assert replies[-1][0] == "Current model: gemini-2.5-flash\nSelect:"
    assert replies[-1][1]["reply_markup"] == ("kbd", "gemini-2.5-flash")


@pytest.mark.asyncio
async def test_callback_model_opens_backend_menu():
    runtime, _replies, _set_calls, _effort_calls, _switch_calls = _runtime(available=["gemini-2.5-flash"])
    update, answers, edits = _callback_update("backend_menu")

    await runtime_model.callback_model(runtime, update, SimpleNamespace())

    assert edits[-1][0] == "BACKEND MENU"
    assert edits[-1][1]["reply_markup"] == "BACKEND KBD"
    assert answers[-1] == (None, False)


@pytest.mark.asyncio
async def test_callback_model_switches_effort():
    runtime, _replies, _set_calls, effort_calls, _switch_calls = _runtime(available=["gemini-2.5-flash"])
    update, answers, edits = _callback_update("effort:high")

    await runtime_model.callback_model(runtime, update, SimpleNamespace())

    assert effort_calls == ["high"]
    assert edits[-1][0] == "Effort switched to: high"
    assert edits[-1][1]["reply_markup"] == ("EFFORT", "high")
    assert answers[-1] == (None, False)
