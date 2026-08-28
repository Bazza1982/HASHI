from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse, TokenUsage
from adapters.stream_events import (
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamEvent,
)
from orchestrator import terminal_console
from orchestrator.api_gateway import _print_api_in, _print_api_out
from orchestrator.bootstrap_logging import (
    ConsoleOutputFilter,
    refresh_console_output_filters,
)
from orchestrator.conversation_router import _print_bridge_message
from orchestrator.runtime_common import (
    _print_final_response,
    _print_thinking,
    _print_user_message,
)


@pytest.fixture(autouse=True)
def _reset_terminal_console():
    terminal_console.reset_for_tests()
    yield
    terminal_console.reset_for_tests()


def _set_level(tmp_path, level: str) -> None:
    terminal_console.configure(tmp_path)
    terminal_console.set_level(level)


def test_terminal_defaults_to_quiet_and_persists_per_instance(tmp_path) -> None:
    assert terminal_console.configure(tmp_path) == "quiet"
    assert terminal_console.setting_path() == tmp_path / "state" / "instance" / "terminal.json"

    assert terminal_console.set_level("debug") == "debug"
    terminal_console.reset_for_tests()

    assert terminal_console.configure(tmp_path) == "debug"


@pytest.mark.parametrize("level", ["quiet", "activity", "debug"])
def test_non_raw_levels_never_print_chat_or_reasoning(
    tmp_path, capsys, level: str
) -> None:
    _set_level(tmp_path, level)

    _print_user_message("sunny", "USER-SECRET")
    _print_thinking("sunny", "THINKING-SECRET")
    _print_final_response("sunny", "ASSISTANT-SECRET")

    output = capsys.readouterr().out
    assert "USER-SECRET" not in output
    assert "THINKING-SECRET" not in output
    assert "ASSISTANT-SECRET" not in output


def test_raw_restores_historical_plaintext_content(tmp_path, capsys) -> None:
    _set_level(tmp_path, "raw")

    _print_user_message("sunny", "USER-CONTENT")
    _print_thinking("sunny", "THINKING-CONTENT")
    _print_final_response("sunny", "ASSISTANT-CONTENT")

    output = capsys.readouterr().out
    assert "USER-CONTENT" in output
    assert "THINKING-CONTENT" in output
    assert "ASSISTANT-CONTENT" in output
    assert "final response:" in output


def test_raw_emits_provider_reasoning_even_without_telegram_think_buffer(
    tmp_path, capsys
) -> None:
    _set_level(tmp_path, "raw")
    terminal_console.start_request("sunny", "req-reasoning", backend="her-v2")
    terminal_console.record_stream_event(
        "sunny",
        "req-reasoning",
        StreamEvent(
            kind=KIND_THINKING,
            summary="",
            raw_delta="VISIBLE-REASONING",
            origin="provider",
        ),
    )
    terminal_console.finish_request("sunny", "req-reasoning", success=True)

    output = capsys.readouterr().out
    assert "VISIBLE-REASONING" in output
    assert "started" not in output


def test_activity_bridge_and_api_lines_exclude_payloads(tmp_path, capsys) -> None:
    _set_level(tmp_path, "activity")

    _print_bridge_message("sunny", "zelda", "BRIDGE-PAYLOAD")
    _print_api_in("safe-model", "API-PAYLOAD")
    _print_api_out("safe-model", 1.25, 42, stream=True)

    output = capsys.readouterr().out
    assert "sunny -> zelda" in output
    assert "safe-model" in output
    assert "1.25s" in output
    assert "BRIDGE-PAYLOAD" not in output
    assert "API-PAYLOAD" not in output


def test_activity_request_summary_has_phase_counts_and_tokens_without_content(
    tmp_path, capsys
) -> None:
    _set_level(tmp_path, "activity")
    terminal_console.start_request(
        "sunny", "req-0001", source="telegram", backend="her-v2"
    )
    terminal_console.record_stream_event(
        "sunny",
        "req-0001",
        StreamEvent(
            kind=KIND_TOOL_START,
            summary="RUN-SECRET",
            detail="DETAIL-SECRET",
            tool_name="Read",
            file_path="/private/PATH-SECRET",
            phase="execution",
            origin="provider",
        ),
    )
    terminal_console.record_stream_event(
        "sunny",
        "req-0001",
        StreamEvent(
            kind=KIND_TOOL_END,
            summary="RESULT-SECRET",
            tool_name="Read",
            metadata={"status": "success"},
        ),
    )
    terminal_console.observe_response(
        "sunny",
        "req-0001",
        BackendResponse(
            text="ASSISTANT-SECRET",
            duration_ms=10,
            usage=TokenUsage(input_tokens=120, output_tokens=30, thinking_tokens=5),
            tool_call_count=1,
        ),
    )
    terminal_console.finish_request("sunny", "req-0001", success=True)

    output = capsys.readouterr().out
    assert "Execution" in output
    assert "tools=1" in output
    assert "tokens(i/o/t)=120/30/5" in output
    for secret in (
        "RUN-SECRET",
        "DETAIL-SECRET",
        "PATH-SECRET",
        "RESULT-SECRET",
        "ASSISTANT-SECRET",
    ):
        assert secret not in output


def test_activity_estimates_are_request_local_and_marked(tmp_path, capsys) -> None:
    _set_level(tmp_path, "activity")
    terminal_console.start_request("sunny", "req-estimate", backend="codex-cli")
    terminal_console.observe_estimated_usage(
        "sunny",
        "req-estimate",
        input_tokens=80,
        output_tokens=0,
        thinking_tokens=0,
    )
    terminal_console.record_stream_event(
        "sunny",
        "req-estimate",
        StreamEvent(
            kind=KIND_THINKING,
            summary="TWENTY-SECRET-CHARS",
            origin="provider",
        ),
    )
    terminal_console.observe_estimated_usage(
        "sunny",
        "req-estimate",
        input_tokens=None,
        output_tokens=12,
        thinking_tokens=None,
    )
    terminal_console.finish_request("sunny", "req-estimate", success=True)

    output = capsys.readouterr().out
    assert "tokens(i/o/t)≈80/12/" in output
    assert "TWENTY-SECRET-CHARS" not in output


def test_debug_reports_event_and_failure_clues_without_event_content(
    tmp_path, capsys
) -> None:
    _set_level(tmp_path, "debug")
    terminal_console.start_request("sunny", "req-0002", backend="openrouter-api")
    terminal_console.record_stream_event(
        "sunny",
        "req-0002",
        StreamEvent(
            kind=KIND_TOOL_START,
            summary="REASONING-SECRET",
            detail="STACK-CONTENT-SECRET",
            tool_name="Read",
            origin="provider",
        ),
    )
    terminal_console.observe_response(
        "sunny",
        "req-0002",
        BackendResponse(
            text="",
            duration_ms=20,
            error="PROVIDER-BODY-SECRET",
            is_success=False,
            error_code="provider_bad_request",
            http_status=400,
        ),
    )
    terminal_console.finish_request(
        "sunny", "req-0002", success=False, error="PROVIDER-BODY-SECRET"
    )

    output = capsys.readouterr().out
    assert "event=tool_start" in output
    assert "origin=provider" in output
    assert "tool=Read" in output
    assert "PROVIDER_BAD_REQUEST" in output
    assert "http=400" in output
    assert "REASONING-SECRET" not in output
    assert "STACK-CONTENT-SECRET" not in output
    assert "PROVIDER-BODY-SECRET" not in output


def test_quiet_failure_is_sanitised(tmp_path, capsys) -> None:
    _set_level(tmp_path, "quiet")
    terminal_console.start_request("sunny", "req-0003", backend="her-v2")
    terminal_console.finish_request(
        "sunny",
        "req-0003",
        success=False,
        error="[PROVIDER_BAD_REQUEST] PRIVATE-PROVIDER-BODY",
    )

    output = capsys.readouterr().out
    assert "PROVIDER_BAD_REQUEST" in output
    assert "PRIVATE-PROVIDER-BODY" not in output

    terminal_console.start_request("sunny", "req-0004", backend="her-v2")
    terminal_console.finish_request(
        "sunny",
        "req-0004",
        success=False,
        error="[PRIVATE_SECRET] echoed provider body",
    )
    output = capsys.readouterr().out
    assert "REQUEST_FAILED" in output
    assert "PRIVATE_SECRET" not in output


def _record(name: str, level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, (), None)


def test_console_filter_keeps_risky_logs_outside_raw(tmp_path) -> None:
    filter_ = ConsoleOutputFilter()
    _set_level(tmp_path, "quiet")

    assert not filter_.filter(
        _record("FlexRuntime.sunny", logging.ERROR, "provider body PRIVATE")
    )
    assert filter_.filter(
        _record("BridgeU.Orchestrator", logging.ERROR, "Service failed to start")
    )
    assert filter_.filter(
        _record("FlexRuntime.sunny", logging.INFO, "Hot restart complete.")
    )

    terminal_console.set_level("raw")
    assert filter_.filter(
        _record("FlexRuntime.sunny", logging.ERROR, "provider body PRIVATE")
    )


def test_hot_reload_refresh_replaces_only_console_filter(monkeypatch) -> None:
    class ConsoleOutputFilter:  # noqa: N801 - simulates the pre-reload class
        pass

    class KeepFilter(logging.Filter):
        pass

    handler = logging.StreamHandler()
    stale = ConsoleOutputFilter()
    keep = KeepFilter()
    handler.addFilter(stale)
    handler.addFilter(keep)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [handler])

    assert refresh_console_output_filters() == 1
    assert keep in handler.filters
    assert stale not in handler.filters
    assert any(
        isinstance(filter_, globals()["ConsoleOutputFilter"])
        for filter_ in handler.filters
    )


@pytest.mark.asyncio
async def test_terminal_command_changes_global_setting_and_renders_scope(tmp_path) -> None:
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    terminal_console.configure(tmp_path)
    replies = []

    async def reply_text(_update, text, **kwargs):
        replies.append((text, kwargs))

    runtime = SimpleNamespace(
        global_config=SimpleNamespace(bridge_home=tmp_path, instance_id="HASHI1"),
        _is_authorized_user=lambda _user_id: True,
        _reply_text=reply_text,
    )
    runtime._terminal_menu_text = lambda: FlexibleAgentRuntime._terminal_menu_text(runtime)
    runtime._terminal_keyboard = lambda: FlexibleAgentRuntime._terminal_keyboard(runtime)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))

    await FlexibleAgentRuntime.cmd_terminal(
        runtime, update, SimpleNamespace(args=["activity"])
    )

    assert terminal_console.get_level() == "activity"
    text, kwargs = replies[-1]
    assert "<b>ACTIVITY</b>" in text
    assert "<code>HASHI1</code> instance" in text
    assert "Workbench, TUI chat, Telegram" in text
    assert kwargs["parse_mode"] == "HTML"
    labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert "✓ Activity" in labels


@pytest.mark.asyncio
async def test_terminal_callback_changes_same_instance_setting(tmp_path) -> None:
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    terminal_console.configure(tmp_path)
    edits = []
    answers = []

    class Query:
        data = "tgl:terminal:debug"
        from_user = SimpleNamespace(id=42)

        async def edit_message_text(self, text, **kwargs):
            edits.append((text, kwargs))

        async def answer(self, text=None, **kwargs):
            answers.append((text, kwargs))

    runtime = SimpleNamespace(
        global_config=SimpleNamespace(bridge_home=tmp_path, instance_id="HASHI1"),
        _is_authorized_user=lambda _user_id: True,
    )
    runtime._terminal_menu_text = lambda: FlexibleAgentRuntime._terminal_menu_text(runtime)
    runtime._terminal_keyboard = lambda: FlexibleAgentRuntime._terminal_keyboard(runtime)

    await FlexibleAgentRuntime.callback_toggle(
        runtime,
        SimpleNamespace(callback_query=Query()),
        SimpleNamespace(),
    )

    assert terminal_console.get_level() == "debug"
    assert "<b>DEBUG</b>" in edits[-1][0]
    assert answers[-1] == ("Terminal: debug", {})
