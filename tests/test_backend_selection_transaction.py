"""Backend selection commits capability-derived mode without losing the old session."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator import runtime_session
from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.memory_plus_mode import set_memory_plus_enabled


def make_selection(tmp_path, monkeypatch, *, mode="fixed", memory=True,
                   supports_sessions=True):
    cfg = FlexibleAgentConfig(
        name="selection-test", workspace_dir=tmp_path, system_md=tmp_path / "AGENT.md",
        telegram_token_key="test", active_backend="codex-cli",
        allowed_backends=[
            {"engine": "codex-cli", "model": "gpt-5.4"},
            {"engine": "claude-cli", "model": "claude-haiku-4-5"},
            {"engine": "her-v2", "model": "role-configured",
             "her_v2": {"profiles": {"configured": {}}}},
        ],
        project_root=tmp_path,
    )
    global_cfg = GlobalConfig(
        authorized_id=1, base_logs_dir=tmp_path / "logs",
        base_media_dir=tmp_path / "media", project_root=tmp_path,
    )
    manager = FlexibleBackendManager(cfg, global_cfg, secrets={})
    manager.agent_mode = mode
    manager._active_model_override = "gpt-5.4"
    original = SimpleNamespace(
        config=SimpleNamespace(engine="codex-cli", model="gpt-5.4"),
        _session_id="original-native-session", shutdown=AsyncMock(),
    )
    manager.current_backend = original
    manager._save_state()
    set_memory_plus_enabled(tmp_path, memory)
    before = (tmp_path / "state.json").read_bytes()
    candidate = SimpleNamespace(
        capabilities=SimpleNamespace(supports_sessions=supports_sessions),
        initialize=AsyncMock(return_value=True), shutdown=AsyncMock(),
        set_session_mode=Mock(), handle_new_session=AsyncMock(return_value=True),
    )

    def create(config, global_config, api_key):
        assert manager.current_backend is original
        assert manager.config.active_backend == "codex-cli"
        assert manager.agent_mode == mode
        assert (tmp_path / "state.json").read_bytes() == before
        candidate.config = config
        return candidate

    monkeypatch.setattr("adapters.registry.get_backend_class", lambda _engine: create)
    monkeypatch.setattr(manager, "_resolve_tools_config", lambda _cfg: None)
    return manager, original, candidate, before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fixed", "flex"])
@pytest.mark.parametrize("memory", [True, False])
@pytest.mark.parametrize("target,sessions", [
    ("codex-cli", True), ("claude-cli", True), ("her-v2", True),
    ("claude-cli", False),
])
async def test_selection_commits_backend_and_mode_together(
    tmp_path, monkeypatch, mode, memory, target, sessions,
):
    manager, original, candidate, _ = make_selection(
        tmp_path, monkeypatch, mode=mode, memory=memory,
        supports_sessions=sessions,
    )
    writes = []
    replace_state = manager.state_store.replace

    def record_write(state):
        writes.append(dict(state))
        return replace_state(state)

    monkeypatch.setattr(manager.state_store, "replace", record_write)
    assert await manager.switch_backend(target) is True
    expected_mode = "fixed" if sessions else "flex"
    assert manager.current_backend is candidate
    assert manager.agent_mode == expected_mode
    assert manager.config.active_backend == target
    candidate.set_session_mode.assert_called_once_with(sessions)
    assert candidate.handle_new_session.await_count == int(sessions)
    original.shutdown.assert_awaited_once()
    candidate.shutdown.assert_not_awaited()
    assert len(writes) == 1
    assert writes[0]["active_backend"] == target
    assert writes[0]["agent_mode"] == expected_mode
    assert writes[0]["memory_plus"]["enabled"] is memory
    reloaded = FlexibleBackendManager(manager.config, manager.global_config, secrets={})
    assert reloaded.config.active_backend == target
    assert reloaded.agent_mode == expected_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fixed", "flex"])
@pytest.mark.parametrize("failure", ["initialize_false", "initialize_error", "session_mode",
                                    "new_session_false", "new_session_error", "persist", "cancel"])
async def test_failed_selection_keeps_live_session_mode_and_saved_state(
    tmp_path, monkeypatch, mode, failure,
):
    manager, original, candidate, before = make_selection(tmp_path, monkeypatch, mode=mode)
    if failure == "initialize_false":
        candidate.initialize.return_value = False
    elif failure == "initialize_error":
        candidate.initialize.side_effect = RuntimeError("cannot initialize")
    elif failure == "session_mode":
        candidate.set_session_mode.side_effect = RuntimeError("cannot set mode")
    elif failure == "new_session_false":
        candidate.handle_new_session.return_value = False
    elif failure == "new_session_error":
        candidate.handle_new_session.side_effect = RuntimeError("cannot reset session")
    elif failure == "cancel":
        candidate.initialize.side_effect = asyncio.CancelledError()
    else:
        monkeypatch.setattr(manager.state_store, "replace", Mock(side_effect=OSError("disk full")))
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await manager.switch_backend("claude-cli")
    else:
        assert await manager.switch_backend("claude-cli") is False
    assert manager.current_backend is original
    assert original._session_id == "original-native-session"
    original.shutdown.assert_not_awaited()
    candidate.shutdown.assert_awaited_once()
    assert manager.agent_mode == mode
    assert manager.config.active_backend == "codex-cli"
    assert manager._active_model_override == "gpt-5.4"
    assert (tmp_path / "state.json").read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_failed_backend_button_preserves_selection_and_allows_retry(tmp_path, monkeypatch, busy):
    manager, original, candidate, before = make_selection(tmp_path, monkeypatch)
    candidate.initialize.return_value = False
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = manager.config
    runtime.backend_manager = manager
    runtime.workspace_dir = tmp_path
    runtime._is_authorized_user = lambda user: user == 1
    runtime._backend_busy = lambda: busy
    runtime._evaluate_enterprise_policy = lambda *a, **kw: SimpleNamespace(allowed=True)
    runtime.get_current_model = lambda: manager.current_backend.config.model
    monkeypatch.setattr(runtime_session, "current_session", lambda *a, **kw: {"session_id": "test"})
    monkeypatch.setattr(runtime_session, "apply_session_workzones", lambda *a, **kw: None)
    query = SimpleNamespace(
        data="bmodel:claude-cli:p:claude-haiku-4-5", from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(chat_id=42), answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    await runtime.callback_model(SimpleNamespace(callback_query=query), SimpleNamespace())
    assert manager.current_backend is original
    assert (tmp_path / "state.json").read_bytes() == before
    if busy:
        candidate.initialize.assert_not_awaited()
        query.edit_message_text.assert_not_awaited()
        assert query.answer.await_args.kwargs["show_alert"] is True
    else:
        candidate.initialize.assert_awaited_once()
        query.edit_message_text.assert_awaited_once()
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        assert query.data in [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_retirement_error_does_not_undo_committed_selection(tmp_path, monkeypatch):
    manager, original, candidate, _ = make_selection(tmp_path, monkeypatch)
    original.shutdown.side_effect = RuntimeError("old cleanup failed")
    assert await manager.switch_backend("claude-cli") is True
    assert manager.current_backend is candidate
    assert manager.state_store.read()["active_backend"] == "claude-cli"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fixed", "flex"])
@pytest.mark.parametrize("memory", [True, False])
@pytest.mark.parametrize("entry", ["typed", "button"])
@pytest.mark.parametrize("with_context", [False, True])
@pytest.mark.parametrize("target,sessions", [("claude-cli", True), ("claude-cli", False), ("her-v2", True)])
async def test_commands_reach_real_selection_transaction(
    tmp_path, monkeypatch, mode, memory, entry, target, sessions, with_context,
):
    manager, _, candidate, _ = make_selection(
        tmp_path, monkeypatch, mode=mode, memory=memory,
        supports_sessions=sessions,
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = manager.config
    runtime.backend_manager = manager
    runtime.workspace_dir = tmp_path
    runtime._is_authorized_user = lambda user: user == 1
    runtime._backend_busy = lambda: False
    runtime._evaluate_enterprise_policy = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    runtime._sync_workzone_to_backend_config = Mock()
    runtime._clear_handoff_state = Mock()
    runtime._arm_session_primer = Mock()
    runtime._reply_text = AsyncMock()
    runtime.enqueue_request = AsyncMock()
    handoff = SimpleNamespace(
        refresh_recent_context=Mock(), build_handoff=Mock(),
        build_session_restore_prompt=Mock(return_value=("RESTORE ONCE", 3, 30)),
    )
    monkeypatch.setattr(runtime_session, "session_handoff_builder", lambda *a, **kw: handoff)
    runtime.get_current_model = lambda: manager.current_backend.config.model
    runtime.get_current_provider = lambda: None
    runtime._get_current_effort = lambda: None
    runtime._configuration_followup = lambda source: ("Selection saved", None)
    monkeypatch.setattr(runtime_session, "current_session", lambda *a, **kw: {"session_id": "test-session"})
    monkeypatch.setattr(runtime_session, "apply_session_workzones", lambda *a, **kw: None)
    if entry == "typed":
        args = [target] if target == "her-v2" else [target, "claude-haiku-4-5"]
        if with_context:
            args.append("+")
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=42))
        await runtime.cmd_backend(update, SimpleNamespace(args=args))
        runtime._reply_text.assert_awaited_once()
    else:
        data = "backend:her-v2:plain" if target == "her-v2" else "bmodel:claude-cli:p:claude-haiku-4-5"
        if with_context:
            data = data.replace(":plain", ":context").replace(":p:", ":c:")
        query = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=1), message=SimpleNamespace(chat_id=42),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        await runtime.callback_model(SimpleNamespace(callback_query=query), SimpleNamespace())
        query.edit_message_text.assert_awaited_once()
    assert manager.current_backend is candidate
    assert manager.agent_mode == ("fixed" if sessions else "flex")
    state = manager.state_store.read()
    assert state["active_backend"] == target
    assert state["agent_mode"] == manager.agent_mode
    assert state["memory_plus"]["enabled"] is memory
    assert candidate.handle_new_session.await_count == int(sessions)
    assert runtime.enqueue_request.await_count == int(with_context and sessions)
    if with_context and sessions:
        runtime.enqueue_request.assert_awaited_once_with(
            42, "RESTORE ONCE", "handoff", "Backend continuation [3 exchanges]",
            silent=True, deliver_to_telegram=False, skip_memory_injection=True,
        )
    assert runtime._clear_handoff_state.call_count == int(not with_context)
