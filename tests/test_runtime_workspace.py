from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_workspace


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def _update():
    message = _Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        message=message,
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(tmp_path: Path):
    replies = []
    runtime = SimpleNamespace()
    runtime.name = "lin_yueru"
    runtime.workspace_dir = tmp_path / "workspace"
    runtime.workspace_dir.mkdir()
    runtime.media_dir = tmp_path / "media"
    runtime.media_dir.mkdir()
    runtime._is_authorized_user = lambda user_id: True
    runtime._backend_busy = lambda: False
    runtime._get_skill_state = lambda: {"memory_sync": False}
    runtime._set_skill_state = lambda key, value: replies.append((key, value))
    runtime._observer_workspace_keep_names = lambda: {"observer-extra.json"}
    runtime._pending_auto_recall_context = "pending"
    runtime._pending_session_primer = "primer"
    runtime._clear_transfer_state = lambda: replies.append("cleared-transfer")
    runtime._get_active_skill_sections = lambda: []
    runtime._get_agent_class = lambda: "general"
    runtime.reload_post_turn_observers = lambda: replies.append("reloaded-observers")
    runtime.logger = SimpleNamespace(warning=lambda msg: replies.append(msg))
    runtime.config = SimpleNamespace(system_md=None)
    runtime.global_config = SimpleNamespace(project_root=tmp_path)
    runtime.sys_prompt_manager = object()

    async def _shutdown():
        replies.append("shutdown")

    async def _new_session():
        replies.append("new-session")

    runtime.backend_manager = SimpleNamespace(
        current_backend=SimpleNamespace(
            capabilities=SimpleNamespace(supports_sessions=True),
            shutdown=_shutdown,
            handle_new_session=_new_session,
        ),
        get_state_snapshot=lambda: {"active_backend": "codex-cli", "wrapper": {"enabled": True}},
        _write_state_dict=lambda data: replies.append(("write_state", data)),
    )
    runtime.memory_store = SimpleNamespace(
        get_stats=lambda: {"turns": 5, "memories": 6},
        clear_all=lambda: {"deleted_turns": 2, "deleted_memories": 3},
    )
    runtime.context_assembler = SimpleNamespace(memory_injection_enabled=True)

    async def _reply_text(update, text, **kwargs):
        replies.append(text)

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_memory_status_pause_and_sync(tmp_path: Path):
    runtime, replies = _runtime(tmp_path)
    await runtime_workspace.cmd_memory(runtime, _update(), _context())
    assert "Memory injection: ON ✅" in replies[-1]

    await runtime_workspace.cmd_memory(runtime, _update(), _context("pause"))
    assert runtime.context_assembler.memory_injection_enabled is False

    await runtime_workspace.cmd_memory(runtime, _update(), _context("sync", "on"))
    assert ("memory_sync", True) in replies


def test_preserve_backend_state_keeps_expected_runtime_keys(tmp_path: Path):
    runtime, _ = _runtime(tmp_path)
    preserved = runtime_workspace._preserve_backend_state(runtime)
    assert preserved == {"active_backend": "codex-cli", "wrapper": {"enabled": True}}


def test_reset_pending_context_clears_runtime_flags(tmp_path: Path):
    runtime, replies = _runtime(tmp_path)
    runtime_workspace._reset_pending_context(runtime)
    assert runtime._pending_auto_recall_context is None
    assert runtime._pending_session_primer is None
    assert "cleared-transfer" in replies
