from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import telegram_stream_policy
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from adapters.stream_events import KIND_PROGRESS, StreamEvent


def _runtime(tmp_path, *, extra=None):
    workspace = tmp_path / "workspaces" / "zelda"
    workspace.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        workspace_dir=workspace,
        config=SimpleNamespace(extra=extra or {}),
    )


def test_stream_policy_defaults_off_even_with_legacy_preview_enabled(tmp_path):
    runtime = _runtime(tmp_path, extra={"answer_stream_preview": True})

    policy = telegram_stream_policy.get_policy(runtime)

    assert policy.enabled is False
    assert policy.source == "functional default"
    assert policy.preview is True
    assert policy.preview_enabled is False
    assert policy.final_only is True


def test_stream_policy_persists_components_without_clobbering_other_preferences(tmp_path):
    runtime = _runtime(tmp_path)
    path = telegram_stream_policy.preferences_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "unrelated": {"keep": True}}),
        encoding="utf-8",
    )

    telegram_stream_policy.set_policy_value(runtime, "enabled", True)
    telegram_stream_policy.set_policy_value(runtime, "preview", False)

    payload = json.loads(path.read_text(encoding="utf-8"))
    policy = telegram_stream_policy.get_policy(runtime)
    assert payload["unrelated"] == {"keep": True}
    assert payload["version"] == 2
    assert payload["telegram_stream"] == {"enabled": True, "preview": False}
    assert policy.enabled is True
    assert policy.preview_enabled is False


def test_stream_policy_repairs_invalid_preference_version(tmp_path):
    runtime = _runtime(tmp_path)
    path = telegram_stream_policy.preferences_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "invalid", "keep": 1}), encoding="utf-8")

    telegram_stream_policy.set_policy_value(runtime, "enabled", True)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["keep"] == 1


def test_stream_policy_reset_preserves_unrelated_preferences_and_returns_default_off(tmp_path):
    runtime = _runtime(tmp_path)
    path = telegram_stream_policy.preferences_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "unrelated": "keep",
                "answer_stream_preview": True,
                "telegram_stream": {"enabled": True, "typing": False},
            }
        ),
        encoding="utf-8",
    )

    telegram_stream_policy.reset_policy(runtime)

    payload = json.loads(path.read_text(encoding="utf-8"))
    policy = telegram_stream_policy.get_policy(runtime)
    assert payload["unrelated"] == "keep"
    assert "telegram_stream" not in payload
    assert "answer_stream_preview" not in payload
    assert policy.enabled is False
    assert policy.source == "functional default"


def test_stream_subswitches_require_master_and_placeholder_dependencies(tmp_path):
    runtime = _runtime(tmp_path)
    telegram_stream_policy.set_policy_value(runtime, "enabled", True)
    telegram_stream_policy.set_policy_value(runtime, "placeholder", False)

    policy = telegram_stream_policy.get_policy(runtime)

    assert policy.enabled is True
    assert policy.typing_enabled is True
    assert policy.placeholder_enabled is False
    assert policy.progress_enabled is False
    assert policy.preview_enabled is False
    assert policy.promote_enabled is False


@pytest.mark.asyncio
async def test_stream_command_does_not_change_verbose_or_think_preferences(tmp_path):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path / "workspaces" / "zelda"
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime.config = SimpleNamespace(extra={})
    runtime._verbose = False
    runtime._think = False
    (runtime.workspace_dir / ".verbose_off").touch()
    (runtime.workspace_dir / ".think_off").touch()
    runtime._is_authorized_user = lambda _user_id: True
    replies = []

    async def _reply_text(_update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_stream(
        runtime,
        update,
        SimpleNamespace(args=["on"]),
    )

    policy = telegram_stream_policy.get_policy(runtime)
    assert policy.enabled is True
    assert runtime._verbose is False
    assert runtime._think is False
    assert (runtime.workspace_dir / ".verbose_off").exists()
    assert (runtime.workspace_dir / ".think_off").exists()
    assert "Telegram streaming: ON" in replies[-1][0]

    await FlexibleAgentRuntime.cmd_stream(
        runtime,
        update,
        SimpleNamespace(args=["off"]),
    )
    assert telegram_stream_policy.get_policy(runtime).enabled is False
    assert runtime._verbose is False
    assert runtime._think is False


@pytest.mark.asyncio
async def test_preview_alias_does_not_enable_stream_master(tmp_path):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path / "workspaces" / "zelda"
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime.config = SimpleNamespace(extra={})
    runtime._is_authorized_user = lambda _user_id: True
    replies = []

    async def _reply_text(_update, text, **_kwargs):
        replies.append(text)

    runtime._reply_text = _reply_text
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_preview(
        runtime,
        update,
        SimpleNamespace(args=["on"]),
    )

    policy = telegram_stream_policy.get_policy(runtime)
    assert policy.preview is True
    assert policy.preview_enabled is False
    assert policy.enabled is False
    assert "inactive until /stream on" in replies[-1]


@pytest.mark.asyncio
async def test_stream_inline_callback_updates_master_and_renders_menu(tmp_path):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path / "workspaces" / "zelda"
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime.config = SimpleNamespace(extra={})
    runtime._verbose = False
    runtime._think = False
    runtime._is_authorized_user = lambda _user_id: True
    edits = []
    answers = []

    async def edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    async def answer(text=None, **kwargs):
        answers.append((text, kwargs))

    query = SimpleNamespace(
        data="tgl:stream:enabled:on",
        from_user=SimpleNamespace(id=1),
        edit_message_text=edit_message_text,
        answer=answer,
    )

    await FlexibleAgentRuntime.callback_toggle(
        runtime,
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    assert telegram_stream_policy.get_policy(runtime).enabled is True
    assert "Telegram streaming: ON" in edits[-1][0]
    assert edits[-1][1]["reply_markup"] is not None
    assert answers[-1][0] == "enabled ON"


@pytest.mark.asyncio
async def test_verbose_stream_display_obeys_shared_edit_budget(tmp_path):
    edits = []
    log_messages = []

    class Bot:
        async def edit_message_text(self, **kwargs):
            edits.append(kwargs)

    runtime = SimpleNamespace(
        name="zelda",
        workspace_dir=tmp_path / "workspaces" / "zelda",
        config=SimpleNamespace(
            active_backend="codex-cli",
            extra={
                "telegram_stream_enabled": True,
                "answer_stream_edit_interval_s": 0.01,
                "answer_stream_max_edits": 2,
            },
        ),
        telegram_connected=True,
        app=SimpleNamespace(bot=Bot()),
        telegram_logger=SimpleNamespace(
            info=lambda message: log_messages.append(message),
            warning=lambda message: log_messages.append(message),
        ),
    )
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    event_queue = asyncio.Queue()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        FlexibleAgentRuntime._streaming_display_loop(
            runtime,
            123,
            SimpleNamespace(message_id=77),
            "req-1",
            stop_event,
            event_queue,
        )
    )

    for index in range(3):
        await event_queue.put(StreamEvent(kind=KIND_PROGRESS, summary=f"step {index}"))
        for _ in range(20):
            if len(edits) >= min(index + 1, 2):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)

    stop_event.set()
    await task

    assert len(edits) == 2
    assert any("Streaming display budget exhausted" in message for message in log_messages)
