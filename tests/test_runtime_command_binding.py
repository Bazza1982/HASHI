from __future__ import annotations

import sys
import types
from types import SimpleNamespace

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator import runtime_command_binding


def test_command_binding_names_are_unique_except_declared_aliases():
    names = [binding.name for binding in runtime_command_binding.COMMAND_BINDINGS]
    assert len(names) == len(set(names))
    assert "workzone" in names
    assert "worzone" in names


def test_command_binding_method_names_exist_on_flexible_runtime():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    missing = [
        binding.method_name
        for binding in runtime_command_binding.COMMAND_BINDINGS
        if not hasattr(FlexibleAgentRuntime, binding.method_name)
    ]
    missing.extend(
        binding.method_name
        for binding in runtime_command_binding.CALLBACK_BINDINGS
        if not hasattr(FlexibleAgentRuntime, binding.method_name)
    )
    assert missing == []


def test_bind_flexible_runtime_handlers_preserves_static_binding_count(monkeypatch):
    added_handlers = []
    added_errors = []
    runtime = SimpleNamespace()
    runtime.app = SimpleNamespace(
        add_handler=lambda handler: added_handlers.append(handler),
        add_error_handler=lambda handler: added_errors.append(handler),
    )
    runtime.handle_telegram_error = object()
    runtime._wrap_cmd = lambda name, callback: ("wrapped", name, callback)

    for binding in runtime_command_binding.COMMAND_BINDINGS:
        setattr(runtime, binding.method_name, object())
    for binding in runtime_command_binding.CALLBACK_BINDINGS:
        setattr(runtime, binding.method_name, object())
    for method_name in (
        "handle_message",
        "handle_photo",
        "handle_voice",
        "handle_audio",
        "handle_document",
        "handle_video",
        "handle_sticker",
    ):
        setattr(runtime, method_name, object())

    monkeypatch.setattr(runtime_command_binding, "bind_runtime_commands", lambda runtime, wrap: None)

    runtime_command_binding.bind_flexible_runtime_handlers(runtime)

    expected = (
        len(runtime_command_binding.COMMAND_BINDINGS)
        + len(runtime_command_binding.CALLBACK_BINDINGS)
        + 7
    )
    assert added_errors == [runtime.handle_telegram_error]
    assert len(added_handlers) == expected
