from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import runtime_pipeline, telegram_stream_policy
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from adapters.stream_events import (
    DELIVERY_USER_COMMENTARY,
    KIND_COMMENTARY,
    KIND_PROGRESS,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    StreamEvent,
)


def _runtime(tmp_path, *, extra=None):
    workspace = tmp_path / "workspaces" / "zelda"
    workspace.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        workspace_dir=workspace,
        config=SimpleNamespace(extra=extra or {}),
    )


def test_stream_policy_migrates_to_live_typing_only_persisted_default(tmp_path):
    runtime = _runtime(tmp_path, extra={"answer_stream_preview": True})

    policy = telegram_stream_policy.get_policy(runtime)

    assert policy.enabled is True
    assert policy.source == "persisted override"
    assert policy.placeholder is False
    assert policy.typing is True
    assert policy.progress is False
    assert policy.preview is False
    assert policy.promote is False
    assert policy.preview_enabled is False
    assert policy.final_only is False


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
    assert payload["version"] == 3
    assert payload["telegram_stream"] == {
        "enabled": True,
        "placeholder": False,
        "typing": True,
        "progress": False,
        "preview": False,
        "promote": False,
    }
    assert policy.enabled is True
    assert policy.preview_enabled is False


def test_stream_policy_repairs_invalid_preference_version(tmp_path):
    runtime = _runtime(tmp_path)
    path = telegram_stream_policy.preferences_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "invalid", "keep": 1}), encoding="utf-8")

    telegram_stream_policy.set_policy_value(runtime, "enabled", True)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    assert payload["keep"] == 1


def test_stream_policy_reset_preserves_unrelated_preferences_and_returns_live_default(tmp_path):
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
    assert payload["telegram_stream"] == {
        "enabled": True,
        "placeholder": False,
        "typing": True,
        "progress": False,
        "preview": False,
        "promote": False,
    }
    assert "answer_stream_preview" not in payload
    assert policy.enabled is True
    assert policy.source == "persisted override"


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


def test_display_policy_migrates_effective_legacy_typing_and_persists_override(tmp_path):
    runtime = _runtime(tmp_path)
    telegram_stream_policy.set_policy_value(runtime, "enabled", True)
    telegram_stream_policy.set_policy_value(runtime, "placeholder", True)
    telegram_stream_policy.set_policy_value(runtime, "typing", False)

    migrated = telegram_stream_policy.get_display_policy(runtime)
    assert migrated.typing_enabled is True
    assert migrated.source.startswith("legacy stream preference")

    telegram_stream_policy.set_typing_enabled(runtime, False)
    persisted = telegram_stream_policy.get_display_policy(runtime)
    assert persisted.typing_enabled is False
    assert persisted.source == "persisted override"


def test_display_preferences_survive_new_runtime_objects(tmp_path):
    first_runtime = _runtime(tmp_path)
    telegram_stream_policy.set_display_preference(first_runtime, "verbose", False)
    telegram_stream_policy.set_display_preference(first_runtime, "think", False)
    telegram_stream_policy.set_display_preference(first_runtime, "commentary", False)
    telegram_stream_policy.set_typing_enabled(first_runtime, False)

    restarted_runtime = _runtime(tmp_path)

    assert telegram_stream_policy.get_display_preference(restarted_runtime, "verbose") is False
    assert telegram_stream_policy.get_display_preference(restarted_runtime, "think") is False
    assert telegram_stream_policy.get_display_preference(restarted_runtime, "commentary") is False
    assert telegram_stream_policy.get_display_policy(restarted_runtime).typing_enabled is False


@pytest.mark.parametrize(
    ("marker", "name", "expected"),
    [
        (".verbose", "verbose", True),
        (".verbose_off", "verbose", False),
        (".think", "think", True),
        (".think_off", "think", False),
        (".commentary_off", "commentary", False),
    ],
)
def test_display_preferences_migrate_legacy_markers(tmp_path, marker, name, expected):
    runtime = _runtime(tmp_path)
    (runtime.workspace_dir / marker).touch()

    assert telegram_stream_policy.get_display_preference(runtime, name) is expected

    payload = json.loads(
        telegram_stream_policy.preferences_path(runtime).read_text(encoding="utf-8")
    )
    assert payload["telegram_display"][name] is expected


def test_persisted_display_preference_wins_over_stale_marker(tmp_path):
    runtime = _runtime(tmp_path)
    (runtime.workspace_dir / ".think_off").touch()
    telegram_stream_policy.set_display_preference(runtime, "think", True)

    assert telegram_stream_policy.get_display_preference(_runtime(tmp_path), "think") is True


@pytest.mark.asyncio
async def test_typing_command_does_not_change_verbose_or_think_preferences(tmp_path):
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

    await FlexibleAgentRuntime.cmd_typing(
        runtime,
        update,
        SimpleNamespace(args=["off"]),
    )

    policy = telegram_stream_policy.get_display_policy(runtime)
    assert policy.typing_enabled is False
    assert runtime._verbose is False
    assert runtime._think is False
    assert (runtime.workspace_dir / ".verbose_off").exists()
    assert (runtime.workspace_dir / ".think_off").exists()
    assert "<b>Current</b> · <b>OFF</b>" in replies[-1][0]
    assert "Temporary bubble" in replies[-1][0]
    assert "Telegram header" in replies[-1][0]
    keyboard = replies[-1][1]["reply_markup"].inline_keyboard
    button_labels = [button.text for row in keyboard for button in row]
    assert "On" in button_labels
    assert "✓ Off" in button_labels

    await FlexibleAgentRuntime.cmd_typing(
        runtime,
        update,
        SimpleNamespace(args=["on"]),
    )
    assert telegram_stream_policy.get_display_policy(runtime).typing_enabled is True
    assert runtime._verbose is False
    assert runtime._think is False


@pytest.mark.asyncio
async def test_her_commentary_command_persists_without_changing_think_or_verbose(tmp_path):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path / "workspaces" / "sunny"
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime.config = SimpleNamespace(active_backend="her-v2", extra={})
    runtime.backend_manager = SimpleNamespace(current_backend=SimpleNamespace(effort="high"))
    runtime._commentary = True
    runtime._verbose = False
    runtime._think = False
    runtime._is_authorized_user = lambda _user_id: True
    replies = []

    async def _reply_text(_update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_commentary(
        runtime,
        update,
        SimpleNamespace(args=["off"]),
    )

    assert runtime._commentary is False
    assert runtime._verbose is False
    assert runtime._think is False
    assert telegram_stream_policy.get_display_preference(runtime, "commentary") is False
    assert (runtime.workspace_dir / ".commentary_off").exists()
    assert "<b>Current</b> · <b>OFF</b>" in replies[-1][0]
    assert "does not change /think or /verbose" in replies[-1][0]


@pytest.mark.asyncio
async def test_commentary_command_on_non_her_backend_reports_only_and_does_not_mutate(tmp_path):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path / "workspaces" / "sunny"
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime.config = SimpleNamespace(active_backend="codex-cli", extra={})
    runtime._commentary = False
    runtime._verbose = True
    runtime._think = True
    runtime._is_authorized_user = lambda _user_id: True
    telegram_stream_policy.set_display_preference(runtime, "commentary", False)
    replies = []

    async def _reply_text(_update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_commentary(
        runtime,
        update,
        SimpleNamespace(args=["on"]),
    )

    assert runtime._commentary is False
    assert telegram_stream_policy.get_display_preference(runtime, "commentary") is False
    assert "HER ONLY" in replies[-1][0]
    assert "codex-cli" in replies[-1][0]
    assert "Nothing was changed" in replies[-1][0]


@pytest.mark.asyncio
async def test_preview_alias_only_reports_retirement(tmp_path):
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
    assert policy.preview is False
    assert "Live answer preview retired" in replies[-1]
    assert "/verbose" in replies[-1]


@pytest.mark.asyncio
async def test_typing_inline_callback_updates_preference_and_renders_menu(tmp_path):
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
        data="tgl:typing:off",
        from_user=SimpleNamespace(id=1),
        edit_message_text=edit_message_text,
        answer=answer,
    )

    await FlexibleAgentRuntime.callback_toggle(
        runtime,
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    assert telegram_stream_policy.get_display_policy(runtime).typing_enabled is False
    assert "<b>Current</b> · <b>OFF</b>" in edits[-1][0]
    assert edits[-1][1]["reply_markup"] is not None
    assert answers[-1][0] == "Typing OFF"


@pytest.mark.asyncio
async def test_verbose_stream_display_rolls_over_after_each_message_edit_budget(tmp_path):
    edits = []
    sends = []
    log_messages = []

    class Bot:
        async def edit_message_text(self, **kwargs):
            edits.append(kwargs)

        async def send_message(self, **kwargs):
            sends.append(kwargs)
            return SimpleNamespace(message_id=77 + len(sends))

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
    display_state = runtime_pipeline.VerboseDisplayState(
        current_message=SimpleNamespace(message_id=77),
        message_ids=[77],
    )
    task = asyncio.create_task(
        FlexibleAgentRuntime._streaming_display_loop(
            runtime,
            123,
            display_state.current_message,
            "req-1",
            stop_event,
            event_queue,
            display_state=display_state,
        )
    )

    for index in range(3):
        summary = "step **zero** with `details`" if index == 0 else f"step {index}"
        await event_queue.put(StreamEvent(kind=KIND_PROGRESS, summary=summary))
        for _ in range(20):
            if len(edits) + len(sends) >= index + 1:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)

    stop_event.set()
    await task

    assert len(sends) == 1
    assert sends[0]["disable_notification"] is True
    assert display_state.message_ids == [77, 78]
    assert display_state.current_message.message_id == 78
    assert display_state.rollover_count == 1
    assert len(edits) == 3
    assert [edit["message_id"] for edit in edits] == [77, 77, 78]
    assert any("<b>zero</b>" in edit["text"] for edit in edits)
    assert any("<code>details</code>" in edit["text"] for edit in edits)
    assert all(edit["parse_mode"] == "HTML" for edit in edits)
    assert any("Rolled over streaming display" in message for message in log_messages)


@pytest.mark.asyncio
async def test_her_compaction_start_and_failure_are_both_visible_with_verbose(tmp_path):
    edits = []

    class Bot:
        async def edit_message_text(self, **kwargs):
            edits.append(kwargs)

    runtime = SimpleNamespace(
        name="sunny",
        workspace_dir=tmp_path / "workspaces" / "sunny",
        config=SimpleNamespace(
            active_backend="her-v2",
            extra={
                "telegram_stream_enabled": True,
                "answer_stream_edit_interval_s": 0.01,
                "answer_stream_max_edits": 5,
            },
        ),
        telegram_connected=True,
        app=SimpleNamespace(bot=Bot()),
        telegram_logger=SimpleNamespace(info=lambda _message: None, warning=lambda _message: None),
    )
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    event_queue = asyncio.Queue(maxsize=200)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        FlexibleAgentRuntime._streaming_display_loop(
            runtime,
            123,
            SimpleNamespace(message_id=77),
            "req-compaction",
            stop_event,
            event_queue,
        )
    )

    await event_queue.put(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary=(
                "🧠 semantic_compaction started · ~351K tokens "
                "· budget 3595s (user override) · post_tool"
            ),
        )
    )
    for _ in range(30):
        if edits:
            break
        await asyncio.sleep(0.01)
    await event_queue.put(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary=(
                "⚠️ semantic_compaction failed · original context unchanged "
                "· continuing · provider call timed out"
            ),
        )
    )
    for _ in range(30):
        if len(edits) >= 2:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await task

    assert any("semantic_compaction started" in edit["text"] for edit in edits)
    assert any("semantic_compaction failed" in edit["text"] for edit in edits)
    assert any("original context unchanged" in edit["text"] for edit in edits)


@pytest.mark.asyncio
async def test_verbose_and_think_receive_disjoint_event_classes():
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.logger = SimpleNamespace(debug=lambda _message: None)
    runtime._thinking_chars_this_req = 0
    runtime._openrouter_think_chunk = ""
    runtime._last_openrouter_think_snippet = None
    verbose_queue = asyncio.Queue()
    think_buffer = []
    callback = FlexibleAgentRuntime._make_stream_callback(
        runtime,
        event_queue=verbose_queue,
        think_buffer=think_buffer,
    )

    await callback(StreamEvent(kind=KIND_TEXT_DELTA, summary="draft answer"))
    await callback(StreamEvent(kind=KIND_PROGRESS, summary="checking files"))
    await callback(StreamEvent(kind=KIND_THINKING, summary="r" * 160))
    commentary = "model update\n\n" + " ".join(["complete"] * 80)
    await callback(StreamEvent(kind=KIND_COMMENTARY, summary=commentary))

    verbose_event = verbose_queue.get_nowait()
    assert verbose_event.kind == KIND_PROGRESS
    assert verbose_queue.empty()
    assert think_buffer == ["r" * 160, commentary]


@pytest.mark.asyncio
async def test_her_commentary_never_enters_think_buffer():
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(active_backend="her-v2")
    runtime.logger = SimpleNamespace(debug=lambda _message: None)
    runtime._thinking_chars_this_req = 0
    runtime._openrouter_think_chunk = ""
    runtime._last_openrouter_think_snippet = None
    think_buffer = []
    callback = FlexibleAgentRuntime._make_stream_callback(
        runtime,
        think_buffer=think_buffer,
    )

    await callback(
        StreamEvent(
            kind=KIND_COMMENTARY,
            summary="Sunny has a persona progress update. ☀️",
            event_id="req-1:commentary:replan:1",
            delivery_class=DELIVERY_USER_COMMENTARY,
        )
    )

    assert think_buffer == []


@pytest.mark.asyncio
async def test_thinking_deltas_preserve_exact_provider_spacing():
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(active_backend="her-v2")
    runtime.logger = SimpleNamespace(debug=lambda _message: None)
    runtime._thinking_chars_this_req = 0
    runtime._openrouter_think_chunk = ""
    runtime._last_openrouter_think_snippet = None
    think_buffer = []
    callback = FlexibleAgentRuntime._make_stream_callback(
        runtime,
        think_buffer=think_buffer,
    )
    fragments = [
        "A",
        " EST",
        " and was flag",
        "ged as miss",
        "ed by ~",
        " 2",
        ".",
        "5 hours, so sun",
        "ny.",
    ]

    for fragment in fragments:
        await callback(
            StreamEvent(
                kind=KIND_THINKING,
                summary=fragment[:400],
                raw_delta=fragment,
            )
        )

    assert think_buffer == []
    assert runtime._openrouter_think_chunk == (
        "A EST and was flagged as missed by ~ 2.5 hours, so sunny."
    )
