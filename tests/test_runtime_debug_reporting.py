from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.admin_local_testing import try_execute_slash_command_text
from orchestrator import (
    runtime_debug_reporting,
    runtime_media,
    runtime_session,
    terminal_console,
)
from orchestrator.config_json import new_config_json, read_config_json, write_config_json
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, message: object, *args: object) -> None:
        rendered = str(message)
        if args:
            try:
                rendered = rendered % args
            except TypeError:
                pass
        self.messages.append(rendered)

    debug = _record
    info = _record
    warning = _record
    error = _record


def _runtime(tmp_path: Path) -> SimpleNamespace:
    session_dir = tmp_path / "logs" / "source" / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        name="source",
        global_config=SimpleNamespace(
            bridge_home=tmp_path,
            project_root=tmp_path,
            instance_id="HASHI1",
        ),
        config=SimpleNamespace(active_backend="codex-cli"),
        session_dir=session_dir,
        workspace_dir=tmp_path / "workspaces" / "source",
        logger=_Logger(),
    )


def test_instance_settings_round_trip_and_preserve_unknown_fields(tmp_path: Path):
    runtime = _runtime(tmp_path)
    path = runtime_debug_reporting.settings_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_config_json(
        path,
        new_config_json(path, {"version": 1, "extension": {"keep": True}}),
    )

    saved = runtime_debug_reporting.enable(
        runtime,
        target="zhaojun@HASHI1",
        journal=r"C:\Users\thene\Desktop\HASHI_Nightly_Batch_Inbox.md",
    )

    assert saved.enabled is True
    assert saved.target == "zhaojun@HASHI1"
    assert saved.journal == r"C:\Users\thene\Desktop\HASHI_Nightly_Batch_Inbox.md"
    assert read_config_json(path)["extension"] == {"keep": True}

    another_agent = _runtime(tmp_path)
    another_agent.name = "another"
    assert runtime_debug_reporting.load_settings(another_agent) == saved

    disabled = runtime_debug_reporting.disable(another_agent)
    assert disabled.enabled is False
    assert disabled.target == saved.target
    assert disabled.journal == saved.journal
    assert read_config_json(path)["extension"] == {"keep": True}


def test_enable_rejects_corrupt_state_without_overwriting_it(tmp_path: Path):
    runtime = _runtime(tmp_path)
    path = runtime_debug_reporting.settings_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{broken")
    before = path.read_bytes()

    with pytest.raises(ValueError):
        runtime_debug_reporting.enable(
            runtime,
            target="zhaojun@HASHI1",
            journal="journal.md",
        )

    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["", "all", "@team", "bad target", "agent@@HASHI1"])
def test_enable_requires_one_agent_target(tmp_path: Path, target: str):
    with pytest.raises(ValueError):
        runtime_debug_reporting.enable(
            _runtime(tmp_path),
            target=target,
            journal="journal.md",
        )


def test_report_contains_error_provenance_and_receiver_instructions(tmp_path: Path):
    runtime = _runtime(tmp_path)
    settings = runtime_debug_reporting.DebugReportingSettings(
        enabled=True,
        target="zhaojun@HASHI1",
        journal=r"C:\Users\thene\Desktop\HASHI_Nightly_Batch_Inbox.md",
    )

    message = runtime_debug_reporting.build_report_message(
        runtime,
        "req-source-0001",
        {
            "success": False,
            "error": "Bad Request",
            "source": "text",
            "summary": "Browser verification",
            "error_code": "PROVIDER_BAD_REQUEST",
            "http_status": 400,
            "side_effects_possible": True,
        },
        settings,
    )

    assert "HASHI AUTOMATED DEBUG REPORT" in message
    assert "HASHI1" in message
    assert "source" in message
    assert "req-source-0001" in message
    assert "PROVIDER_BAD_REQUEST" in message
    assert "Bad Request" in message
    assert str(runtime.session_dir / "errors.log") in message
    assert settings.journal in message
    assert "do not create a duplicate" in message
    assert "Do not fix" in message


@pytest.mark.asyncio
async def test_forward_failure_uses_existing_hchat_sender_exactly_once(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime_debug_reporting.enable(
        runtime,
        target="zhaojun@HASHI1",
        journal="journal.md",
    )
    calls: list[tuple[str, str, str]] = []
    runtime._debug_report_sender = (
        lambda target, from_agent, message: calls.append(
            (target, from_agent, message)
        )
        or True
    )

    forwarded = await runtime_debug_reporting.forward_failure_once(
        runtime,
        "req-source-0002",
        {"success": False, "error": "backend failed", "source": "text"},
    )

    assert forwarded is True
    assert len(calls) == 1
    assert calls[0][0:2] == ("zhaojun@HASHI1", "source")
    assert "backend failed" in calls[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "error": ""},
        {"success": False, "error": "stopped", "interrupted": True},
        {"success": False, "error": "peer task failed", "source": "bridge:hchat"},
        {"success": False, "error": "peer reply failed", "source": "hchat-reply:agent"},
    ],
)
async def test_forward_skips_non_error_and_recursive_hchat_paths(
    tmp_path: Path, payload: dict
):
    runtime = _runtime(tmp_path)
    runtime_debug_reporting.enable(
        runtime,
        target="zhaojun@HASHI1",
        journal="journal.md",
    )
    calls: list[object] = []
    runtime._debug_report_sender = lambda *_args: calls.append(_args) or True

    assert not await runtime_debug_reporting.forward_failure_once(
        runtime, "req-source-0003", payload
    )
    assert calls == []


@pytest.mark.asyncio
async def test_failed_forward_is_logged_and_never_raised(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime_debug_reporting.enable(
        runtime,
        target="zhaojun@HASHI1",
        journal="journal.md",
    )
    calls = 0

    def fail(*_args):
        nonlocal calls
        calls += 1
        raise RuntimeError("peer offline")

    runtime._debug_report_sender = fail

    assert not await runtime_debug_reporting.forward_failure_once(
        runtime,
        "req-source-0004",
        {"success": False, "error": "backend failed", "source": "text"},
    )
    assert calls == 1
    assert any("peer offline" in line for line in runtime.logger.messages)


@pytest.mark.asyncio
async def test_terminal_listener_schedules_failure_without_blocking_listeners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "source"
    runtime.logger = _Logger()
    runtime._request_listeners = {}
    runtime._pending_request_results = {}
    runtime.request_activity = SimpleNamespace(complete=lambda *_args, **_kwargs: None)

    monkeypatch.setattr(
        runtime_session,
        "finish_request_from_listener",
        lambda *_args, **_kwargs: None,
    )

    async def finish_voice(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime_media, "finish_native_voice_transcript_path", finish_voice)
    monkeypatch.setattr(terminal_console, "finish_request", lambda *_args, **_kwargs: None)
    scheduled: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime_debug_reporting,
        "schedule_failure_report",
        lambda _runtime, request_id, payload: scheduled.append(
            (request_id, dict(payload))
        ),
    )

    payload = {"success": False, "error": "backend failed", "source": "text"}
    await FlexibleAgentRuntime._notify_request_listeners(runtime, "req-source-0005", payload)

    assert scheduled == [("req-source-0005", payload)]


@pytest.mark.asyncio
async def test_debug_command_configures_instance_reporting_and_keeps_manual_form(
    tmp_path: Path,
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "source"
    runtime.workspace_dir = tmp_path / "workspaces" / "source"
    runtime.global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        instance_id="HASHI1",
    )
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.skill_manager = None
    runtime._is_authorized_user = lambda user_id: user_id == 7
    replies: list[tuple[str, dict]] = []

    async def reply(_update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = reply
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
    context = SimpleNamespace(
        args=[
            "on",
            "zhaojun@HASHI1",
            r"C:\Users\thene\Desktop\HASHI",
            "Nightly",
            "Batch.md",
        ]
    )

    await FlexibleAgentRuntime.cmd_debug(runtime, update, context)

    settings = runtime_debug_reporting.load_settings(runtime)
    assert settings.enabled is True
    assert settings.target == "zhaojun@HASHI1"
    assert settings.journal == r"C:\Users\thene\Desktop\HASHI Nightly Batch.md"
    assert "zhaojun@HASHI1" in replies[-1][0]
    assert "HASHI Nightly Batch.md" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"

    context.args = ["off"]
    await FlexibleAgentRuntime.cmd_debug(runtime, update, context)
    assert runtime_debug_reporting.load_settings(runtime).enabled is False


@pytest.mark.asyncio
async def test_debug_on_requires_target_and_journal(tmp_path: Path):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "source"
    runtime.workspace_dir = tmp_path / "workspaces" / "source"
    runtime.global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        instance_id="HASHI1",
    )
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.skill_manager = None
    runtime._is_authorized_user = lambda _user_id: True
    replies: list[str] = []

    async def reply(_update, text, **_kwargs):
        replies.append(text)

    runtime._reply_text = reply
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7))

    await FlexibleAgentRuntime.cmd_debug(
        runtime,
        update,
        SimpleNamespace(args=["on", "zhaojun@HASHI1"]),
    )

    assert not runtime_debug_reporting.settings_path(runtime).exists()
    assert "/debug on" in replies[-1]


@pytest.mark.parametrize(
    ("journal", "journal_arg"),
    [
        (
            r"C:\Users\thene\Desktop\HASHI_Nightly_Batch_Inbox.md",
            r"C:\Users\thene\Desktop\HASHI_Nightly_Batch_Inbox.md",
        ),
        (
            r"C:\Users\thene\Desktop\HASHI Nightly Batch Inbox.md",
            r'"C:\Users\thene\Desktop\HASHI Nightly Batch Inbox.md"',
        ),
    ],
)
@pytest.mark.asyncio
async def test_local_command_preserves_windows_journal_path(
    tmp_path: Path,
    journal: str,
    journal_arg: str,
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "source"
    runtime.workspace_dir = tmp_path / "workspaces" / "source"
    runtime.global_config = SimpleNamespace(
        authorized_id=7,
        bridge_home=tmp_path,
        project_root=tmp_path,
        instance_id="HASHI1",
        ui_language="en",
    )
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.skill_manager = None
    runtime.logger = _Logger()
    runtime._is_authorized_user = lambda user_id: user_id == 7
    runtime._is_command_allowed = lambda _command: True
    replies: list[str] = []

    async def reply(_update, text, **_kwargs):
        replies.append(text)

    runtime._reply_text = reply
    result = await try_execute_slash_command_text(
        runtime,
        f"/debug on zhaojun@HASHI1 {journal_arg}",
    )

    assert result is not None
    assert result["ok"] is True
    assert result["args"][-1] == journal
    assert runtime_debug_reporting.load_settings(runtime).journal == journal
