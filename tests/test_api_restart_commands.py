from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.commands import api_restart


class _FakeServiceManager:
    def __init__(self):
        self.snapshot = {
            "enabled": False,
            "running": False,
            "default_model": "gpt-5.5",
            "available_models": ["gpt-5.5", "claude-sonnet-4-6", "gemini-2.5-flash"],
            "base_url": "http://127.0.0.1:18801",
            "port": 18801,
        }
        self.started = 0
        self.stopped = 0
        self.models = []

    def api_gateway_state_snapshot(self):
        return dict(self.snapshot)

    async def start_api_gateway_runtime(self):
        self.started += 1
        self.snapshot["enabled"] = True
        self.snapshot["running"] = True
        return True, "API Gateway started."

    async def stop_api_gateway_runtime(self, timeout: float = 5.0):
        self.stopped += 1
        self.snapshot["enabled"] = False
        self.snapshot["running"] = False
        return True, "API Gateway stopped."

    def set_api_gateway_default_model(self, model: str):
        self.models.append(model)
        self.snapshot["default_model"] = model
        return True, f"API Gateway default model set to {model}."


class _FakeRuntime:
    def __init__(self):
        self.messages = []
        self.sent = []
        self.service_manager = _FakeServiceManager()
        self.orchestrator = SimpleNamespace(service_manager=self.service_manager)

    def _is_authorized_user(self, user_id):
        return user_id == 1

    async def _reply_text(self, update, text, **kwargs):
        self.messages.append((text, kwargs))

    async def _send_text(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class _FakeMessage:
    def __init__(self, chat_id=777):
        self.chat_id = chat_id


class _FakeCallbackQuery:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = _FakeMessage()
        self.edits = []
        self.answers = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _command_update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=777))


@pytest.mark.asyncio
async def test_api_command_status_includes_address_and_default_model():
    runtime = _FakeRuntime()

    await api_restart.api_command(runtime, _command_update(), SimpleNamespace(args=[]))

    text, kwargs = runtime.messages[-1]
    assert "Address: <code>http://127.0.0.1:18801</code>" in text
    assert "Default model: <code>gpt-5.5</code>" in text
    assert kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_api_callback_updates_default_model():
    runtime = _FakeRuntime()
    query = _FakeCallbackQuery("apigw:model:claude-sonnet-4-6")
    update = SimpleNamespace(callback_query=query)

    await api_restart.api_callback(runtime, update, SimpleNamespace())

    assert runtime.service_manager.models == ["claude-sonnet-4-6"]
    assert "claude-sonnet-4-6" in query.edits[-1][0]


@pytest.mark.asyncio
async def test_restart_command_reports_watchtower_status(monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(api_restart.remote_rescue, "rescue_status", lambda *args, **kwargs: (0, {"state": "running", "workbench_url": "http://127.0.0.1:18819/api/health"}))
    monkeypatch.setattr(api_restart.remote_rescue, "_candidate_base_urls", lambda instance: ["http://127.0.0.1:43766"])

    await api_restart.restart_command(runtime, _command_update(), SimpleNamespace(args=[]))

    text, _kwargs = runtime.messages[-1]
    assert "Controller: <code>WATCHTOWER</code>" in text
    assert "Controlled state: <code>running</code>" in text
    buttons = _kwargs["reply_markup"].inline_keyboard
    assert buttons[0][0].text == "Hard Restart"


@pytest.mark.asyncio
async def test_restart_command_fails_closed_when_watchtower_unavailable(monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(api_restart.remote_rescue, "rescue_status", lambda *args, **kwargs: (4, {"error": "forbidden"}))
    monkeypatch.setattr(api_restart.remote_rescue, "_candidate_base_urls", lambda instance: ["http://127.0.0.1:43766"])

    await api_restart.restart_command(runtime, _command_update(), SimpleNamespace(args=[]))

    text, kwargs = runtime.messages[-1]
    assert "forbidden" in text
    buttons = kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 1
    assert buttons[0][0].text == "Refresh"


@pytest.mark.asyncio
async def test_restart_confirm_dispatches_background_request(monkeypatch):
    runtime = _FakeRuntime()
    query = _FakeCallbackQuery("hardrestart:confirm")
    update = SimpleNamespace(callback_query=query)
    observed = {}

    monkeypatch.setattr(api_restart.remote_rescue, "rescue_status", lambda *args, **kwargs: (0, {"state": "running"}))

    async def fake_dispatch(runtime_arg, chat_id):
        observed["chat_id"] = chat_id

    monkeypatch.setattr(api_restart, "_dispatch_watchtower_restart", fake_dispatch)

    await api_restart.restart_callback(runtime, update, SimpleNamespace())
    await asyncio.sleep(0)

    assert observed["chat_id"] == 777
    assert "WatchTower hard restart requested" in query.edits[-1][0]


@pytest.mark.asyncio
async def test_restart_confirm_rejects_duplicate_inflight(monkeypatch):
    runtime = _FakeRuntime()
    runtime._watchtower_restart_inflight = True
    query = _FakeCallbackQuery("hardrestart:confirm")
    update = SimpleNamespace(callback_query=query)

    await api_restart.restart_callback(runtime, update, SimpleNamespace())

    assert query.answers[-1] == ("Restart is already in progress.", True)
    assert query.edits == []


@pytest.mark.asyncio
async def test_restart_arm_fails_closed_when_watchtower_status_fails(monkeypatch):
    runtime = _FakeRuntime()
    query = _FakeCallbackQuery("hardrestart:arm")
    update = SimpleNamespace(callback_query=query)
    monkeypatch.setattr(api_restart.remote_rescue, "rescue_status", lambda *args, **kwargs: (4, {"error": "forbidden"}))
    monkeypatch.setattr(api_restart.remote_rescue, "_candidate_base_urls", lambda instance: ["http://127.0.0.1:43766"])

    await api_restart.restart_callback(runtime, update, SimpleNamespace())

    assert "forbidden" in query.edits[-1][0]
    assert query.answers[-1] == ("WatchTower unavailable.", True)
    buttons = query.edits[-1][1]["reply_markup"].inline_keyboard
    assert len(buttons) == 1
    assert buttons[0][0].text == "Refresh"


@pytest.mark.asyncio
async def test_unauthorized_callbacks_are_answered():
    runtime = _FakeRuntime()
    query = _FakeCallbackQuery("hardrestart:refresh")
    query.from_user = SimpleNamespace(id=999)

    await api_restart.restart_callback(runtime, SimpleNamespace(callback_query=query), SimpleNamespace())

    assert query.answers[-1] == ("Not authorized.", True)
