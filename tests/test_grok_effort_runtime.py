from types import SimpleNamespace
from unittest.mock import Mock

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


def _runtime() -> FlexibleAgentRuntime:
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(
        active_backend="grok-cli",
        allowed_backends=[{"engine": "grok-cli", "model": "grok-4.5"}],
    )
    runtime.backend_manager = SimpleNamespace(
        current_backend=SimpleNamespace(
            effort="medium",
            config=SimpleNamespace(model="grok-4.5"),
        ),
        persist_state=Mock(),
    )
    runtime.get_current_model = lambda: "grok-4.5"
    return runtime


def test_grok_effort_runtime_switches_and_persists_selection():
    runtime = _runtime()

    assert runtime._get_available_efforts() == ["low", "medium", "high"]
    assert runtime._get_current_effort() == "medium"

    runtime._set_active_effort("high")

    assert runtime.backend_manager.current_backend.effort == "high"
    assert runtime.config.allowed_backends[0]["effort"] == "high"
    runtime.backend_manager.persist_state.assert_called_once_with()


def test_grok_effort_telegram_keyboard_marks_medium_as_current():
    runtime = _runtime()

    keyboard = runtime._effort_keyboard()

    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [button.callback_data for button in buttons] == [
        "effort:low",
        "effort:medium",
        "effort:high",
    ]
    assert [button.text for button in buttons] == ["low", ">> medium", "high"]
