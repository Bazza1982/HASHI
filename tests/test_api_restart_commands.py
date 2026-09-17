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
            "available_models": [
                "gpt-5.5",
                "claude-sonnet-4-6",
                "gemini-2.5-flash",
                "grok-4.3",
                "grok-imagine-video",
            ],
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
        self.name = "hashiko"
        self.global_config = SimpleNamespace(instance_id="HASHI_TEST")
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
    assert "<b>Address</b> · <code>http://127.0.0.1:18801</code>" in text
    assert "Images · <code>http://127.0.0.1:18801/v1/images/generations</code>" in text
    assert "Videos · <code>http://127.0.0.1:18801/v1/videos/generations</code>" in text
    assert "<b>Default model</b> · <code>gpt-5.5</code>" in text
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
async def test_api_model_menu_includes_grok_models():
    runtime = _FakeRuntime()
    query = _FakeCallbackQuery("apigw:menu:model")
    update = SimpleNamespace(callback_query=query)

    await api_restart.api_callback(runtime, update, SimpleNamespace())

    markup = query.edits[-1][1]["reply_markup"]
    labels = [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "grok-4.3" in labels
    assert "grok-imagine-video" in labels


@pytest.mark.asyncio
async def test_api_callback_updates_default_model_to_grok():
    runtime = _FakeRuntime()
    query = _FakeCallbackQuery("apigw:model:grok-4.3")
    update = SimpleNamespace(callback_query=query)

    await api_restart.api_callback(runtime, update, SimpleNamespace())

    assert runtime.service_manager.models == ["grok-4.3"]
    assert "grok-4.3" in query.edits[-1][0]


@pytest.mark.asyncio
async def test_restart_command_dispatches_background_request(monkeypatch):
    runtime = _FakeRuntime()
    observed = {}
    monkeypatch.setattr(api_restart.remote_rescue, "rescue_status", lambda *args, **kwargs: (0, {"state": "running", "workbench_url": "http://127.0.0.1:18819/api/health"}))
    monkeypatch.setattr(api_restart.remote_rescue, "_candidate_base_urls", lambda instance: ["http://127.0.0.1:43766"])
    monkeypatch.setattr(
        api_restart,
        "_build_watchtower_restart_payload",
        lambda *args, **kwargs: {
            "reason": "telegram /restart hard restart",
            "target_instance": "HASHI_TEST",
            "requester_agent": "hashiko",
        },
    )

    async def fake_dispatch(runtime_arg, chat_id, request_payload):
        observed["chat_id"] = chat_id
        observed["payload"] = request_payload

    monkeypatch.setattr(api_restart, "_dispatch_watchtower_restart", fake_dispatch)

    await api_restart.restart_command(runtime, _command_update(), SimpleNamespace(args=[]))
    await asyncio.sleep(0)

    text, _kwargs = runtime.messages[-1]
    assert "WatchTower hard restart requested" in text
    assert observed["chat_id"] == 777
    assert observed["payload"]["target_instance"] == "HASHI_TEST"
    assert "human_restart_proof" not in observed["payload"]


@pytest.mark.asyncio
async def test_restart_command_fails_closed_when_watchtower_unavailable(monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(api_restart.remote_rescue, "rescue_status", lambda *args, **kwargs: (4, {"error": "forbidden"}))
    monkeypatch.setattr(api_restart.remote_rescue, "_candidate_base_urls", lambda instance: ["http://127.0.0.1:43766"])

    await api_restart.restart_command(runtime, _command_update(), SimpleNamespace(args=[]))

    text, kwargs = runtime.messages[-1]
    assert "forbidden" in text
    assert "reply_markup" not in kwargs


@pytest.mark.asyncio
async def test_restart_confirm_dispatches_background_request(monkeypatch):
    runtime = _FakeRuntime()
    query = _FakeCallbackQuery("hardrestart:confirm")
    update = SimpleNamespace(callback_query=query)
    observed = {}

    monkeypatch.setattr(api_restart.remote_rescue, "rescue_status", lambda *args, **kwargs: (0, {"state": "running"}))
    monkeypatch.setattr(
        api_restart,
        "_build_watchtower_restart_payload",
        lambda *args, **kwargs: {
            "reason": "telegram /restart hard restart",
            "target_instance": "HASHI_TEST",
            "requester_agent": "hashiko",
        },
    )

    async def fake_dispatch(runtime_arg, chat_id, request_payload):
        observed["chat_id"] = chat_id
        observed["payload"] = request_payload

    monkeypatch.setattr(api_restart, "_dispatch_watchtower_restart", fake_dispatch)

    await api_restart.restart_callback(runtime, update, SimpleNamespace())
    await asyncio.sleep(0)

    assert observed["chat_id"] == 777
    assert "WatchTower hard restart requested" in query.edits[-1][0]


@pytest.mark.asyncio
async def test_restart_payload_needs_no_second_human_proof(monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.delenv("HASHI_HUMAN_RESTART_SECRET", raising=False)

    payload = api_restart._build_watchtower_restart_payload(
        runtime,
        request_source="telegram",
        reason="telegram /restart hard restart",
    )

    assert payload["target_instance"] == "HASHI_TEST"
    assert payload["requester_agent"] == "hashiko"
    assert payload["request_source"] == "telegram"
    assert "human_restart_proof" not in payload


@pytest.mark.asyncio
async def test_watchtower_dispatch_reports_only_verified_terminal_success(monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(
        api_restart.remote_rescue,
        "rescue_restart",
        lambda *args, **kwargs: (
            0,
            {
                "ok": True,
                "state": "completed",
                "restart_id": "rst_123",
                "target_instance": "HASHI_TEST",
                "evidence": {
                    "old_pid": 100,
                    "old_pid_exited": True,
                    "new_pid": 200,
                    "new_pid_alive": True,
                    "new_pid_differs": True,
                    "backend_health_ok": True,
                    "actual_instance": "HASHI_TEST",
                    "instance_matches": True,
                    "runtime_version": {"core_api": "4", "function_api": "4"},
                    "runtime_version_verified": True,
                    "generation_id": "sha256:abc",
                    "generation_verified": True,
                },
            },
        ),
    )

    await api_restart._dispatch_watchtower_restart(
        runtime,
        777,
        {
            "reason": "test",
            "target_instance": "HASHI_TEST",
            "requester_agent": "hashiko",
        },
    )

    assert "HASHI_TEST" in runtime.sent[-1][1]
    assert "100" in runtime.sent[-1][1]
    assert "200" in runtime.sent[-1][1]
    assert "rst_123" in runtime.sent[-1][1]


@pytest.mark.asyncio
async def test_watchtower_dispatch_rejects_unverified_success_shape(monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(
        api_restart.remote_rescue,
        "rescue_restart",
        lambda *args, **kwargs: (
            0,
            {"ok": True, "restart_launched": True, "pid": 200},
        ),
    )

    await api_restart._dispatch_watchtower_restart(
        runtime,
        777,
        {
            "reason": "test",
            "target_instance": "HASHI_TEST",
            "requester_agent": "hashiko",
        },
    )

    assert "failed" in runtime.sent[-1][1].lower()
    assert "terminal" in runtime.sent[-1][1].lower()


@pytest.mark.asyncio
async def test_restart_confirm_rejects_duplicate_inflight():
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
    assert buttons[0][0].text == "↻ Refresh"


@pytest.mark.asyncio
async def test_unauthorized_callbacks_are_answered():
    runtime = _FakeRuntime()
    query = _FakeCallbackQuery("hardrestart:refresh")
    query.from_user = SimpleNamespace(id=999)

    await api_restart.restart_callback(runtime, SimpleNamespace(callback_query=query), SimpleNamespace())

    assert query.answers[-1] == ("Not authorized.", True)
