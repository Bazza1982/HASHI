from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.flexible_backend_manager import FlexibleBackendManager


def _make_manager(workspace: Path) -> FlexibleBackendManager:
    workspace.mkdir(parents=True, exist_ok=True)
    cfg = FlexibleAgentConfig(
        name="test-flex",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="test-flex",
        allowed_backends=[
            {"engine": "codex-cli", "model": "gpt-5.4"},
            {"engine": "claude-cli", "model": "claude-haiku-4-5"},
        ],
        active_backend="codex-cli",
        project_root=workspace,
    )
    global_cfg = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
    )
    return FlexibleBackendManager(cfg, global_cfg, secrets={})


def _make_runtime(manager: FlexibleBackendManager) -> tuple[FlexibleAgentRuntime, list[str]]:
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.backend_manager = manager
    runtime.config = manager.config
    runtime._is_authorized_user = lambda user_id: user_id == 1
    messages: list[str] = []

    async def _reply_text(update, text, **kwargs):
        messages.append(text)

    runtime._reply_text = _reply_text
    return runtime, messages


def _update(args: list[str] | None = None):
    return (
        SimpleNamespace(effective_user=SimpleNamespace(id=1)),
        SimpleNamespace(args=args or []),
    )


def _read_state(workspace: Path) -> dict:
    return json.loads((workspace / "state.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_wrapper_only_commands_reject_flex_mode(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    runtime, messages = _make_runtime(manager)
    update, context = _update(["backend=codex-cli"])

    await FlexibleAgentRuntime.cmd_core(runtime, update, context)

    assert messages
    assert "only applies in **wrapper** mode" in messages[-1]
    assert not (tmp_path / "agent" / "state.json").exists()


@pytest.mark.asyncio
async def test_cmd_mode_wrapper_persists_mode(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    runtime, messages = _make_runtime(manager)
    update, context = _update(["wrapper"])

    await FlexibleAgentRuntime.cmd_mode(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["agent_mode"] == "wrapper"
    assert "Switched to **wrapper** mode" in messages[-1]


@pytest.mark.asyncio
async def test_backend_and_model_commands_guide_wrapper_mode(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    manager.current_backend = SimpleNamespace(config=SimpleNamespace(model="gpt-5.5"))
    runtime, messages = _make_runtime(manager)

    update, context = _update([])
    await FlexibleAgentRuntime.cmd_backend(runtime, update, context)
    assert "/core" in messages[-1]
    assert "/wrap" in messages[-1]

    await FlexibleAgentRuntime.cmd_model(runtime, update, context)
    assert "/core" in messages[-1]
    assert "/wrap" in messages[-1]


@pytest.mark.asyncio
async def test_cmd_core_updates_wrapper_core_state(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    runtime, messages = _make_runtime(manager)
    update, context = _update(["backend=codex-cli", "model=gpt-5.5"])

    await FlexibleAgentRuntime.cmd_core(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["agent_mode"] == "wrapper"
    assert state["core"] == {"backend": "codex-cli", "model": "gpt-5.5"}
    assert "Wrapper core updated" in messages[-1]


@pytest.mark.asyncio
async def test_cmd_wrap_updates_wrapper_translator_state(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    runtime, messages = _make_runtime(manager)
    update, context = _update(["backend=claude-cli", "model=claude-haiku-4-5", "context=5"])

    await FlexibleAgentRuntime.cmd_wrap(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["wrapper"] == {
        "backend": "claude-cli",
        "model": "claude-haiku-4-5",
        "context_window": 5,
        "fallback": "passthrough",
    }
    assert "Wrapper translator updated" in messages[-1]


@pytest.mark.asyncio
async def test_cmd_wrapper_set_list_and_clear_slots(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    runtime, messages = _make_runtime(manager)

    update, context = _update(["set", "1", "Be", "warm"])
    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["wrapper_slots"] == {"1": "Be warm"}
    assert "updated" in messages[-1]

    update, context = _update(["list"])
    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)
    assert "Be warm" in messages[-1]

    update, context = _update(["clear", "1"])
    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["wrapper_slots"] == {}
