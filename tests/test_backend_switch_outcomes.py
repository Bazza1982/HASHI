"""Backend selection exposes the owning transaction's cause and commit facts."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import config_json
from tests.test_backend_selection_transaction import make_selection


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,phase,reason", [
    ("configuration", "configuration", "invalid_input"),
    ("initialize_false", "initialize", "backend_unavailable"),
    ("initialize_timeout", "initialize", "timed_out"),
    ("new_session_false", "session", "backend_refused"),
    ("new_session_error", "session", "internal_error"),
    ("fence", "fence", "state_changed"),
    ("persist", "persist", "disk_full"),
])
async def test_backend_failure_preserves_the_owning_stage_and_cause(
    tmp_path, monkeypatch, failure, phase, reason,
):
    from orchestrator.session_store import SessionConflict

    manager, original, candidate, before = make_selection(tmp_path, monkeypatch)
    if failure == "configuration":
        def invalid(*args, **kwargs):
            raise ValueError("private-model-config token=private-secret")
        monkeypatch.setattr(manager, "_build_adapter_config", invalid)
    elif failure == "initialize_false":
        candidate.initialize.return_value = False
    elif failure == "initialize_timeout":
        candidate.initialize.side_effect = TimeoutError("private-service-url")
    elif failure == "new_session_false":
        candidate.handle_new_session.return_value = False
    elif failure == "new_session_error":
        candidate.handle_new_session.side_effect = RuntimeError("private-engine-session")
    elif failure == "fence":
        manager.runtime = SimpleNamespace(permission_runs=SimpleNamespace(
            before_backend_switch=AsyncMock(side_effect=SessionConflict("private-running-task")),
        ))
    else:
        replace = os.replace

        def no_space(source, destination, *args, **kwargs):
            if Path(destination).resolve() == manager.state_store.path.resolve():
                raise OSError(errno.ENOSPC, "private-state-path")
            return replace(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "replace", no_space)

    result = await manager.switch_backend_result("claude-cli")
    assert result.switched is False
    assert result.issue.phase == phase
    assert result.issue.error_code == "backend_switch_" + phase + "_" + reason
    assert "operr_" in result.issue.reference
    assert "private-" not in result.issue.render()
    assert manager.current_backend is original
    assert manager.config.active_backend == "codex-cli"
    assert manager.state_store.path.read_bytes() == before
    original.shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_switch_command_does_not_lose_an_initialization_timeout(tmp_path, monkeypatch):
    from orchestrator import runtime_session
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    manager, original, candidate, _ = make_selection(tmp_path, monkeypatch)
    candidate.initialize.side_effect = TimeoutError("private-service-url")
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = manager.config
    runtime.backend_manager = manager
    runtime.workspace_dir = tmp_path
    runtime._backend_busy = lambda: False
    runtime._evaluate_enterprise_policy = lambda *a, **kw: SimpleNamespace(allowed=True)
    monkeypatch.setattr(runtime_session, "current_session", lambda *a, **kw: {"session_id": "fixture"})
    monkeypatch.setattr(runtime_session, "apply_session_workzones", lambda *a, **kw: None)
    switched, message = await runtime._switch_backend_mode(42, "claude-cli")
    assert switched is False and manager.current_backend is original
    assert "backend_switch_initialize_timed_out" in message
    assert "operr_" in message and "private-" not in message


def command_runtime(manager, monkeypatch):
    from unittest.mock import Mock
    from orchestrator import runtime_session
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = manager.config
    runtime.backend_manager = manager
    runtime.workspace_dir = manager.config.workspace_dir
    runtime._backend_busy = lambda: False
    runtime._evaluate_enterprise_policy = lambda *a, **kw: SimpleNamespace(allowed=True)
    runtime._sync_workzone_to_backend_config = Mock()
    runtime._clear_handoff_state = Mock()
    runtime._arm_session_primer = Mock()
    runtime.get_current_model = lambda: manager.current_backend.config.model
    runtime.get_current_provider = lambda: None
    runtime._get_current_effort = lambda: None
    monkeypatch.setattr(runtime_session, "current_session", lambda *a, **kw: {"session_id": "fixture"})
    monkeypatch.setattr(runtime_session, "apply_session_workzones", lambda *a, **kw: None)
    return runtime


@pytest.mark.asyncio
async def test_command_displays_published_but_uncertain_durability(tmp_path, monkeypatch):
    manager, _, candidate, _ = make_selection(tmp_path, monkeypatch)
    runtime = command_runtime(manager, monkeypatch)

    def fail_sync(directory):
        raise OSError(errno.EIO, "private-sync")

    monkeypatch.setattr(config_json, "_sync_directory", fail_sync)
    switched, message = await runtime._switch_backend_mode(42, "claude-cli")
    assert switched is True and manager.current_backend is candidate
    assert "backend_switch_persist_durability_unconfirmed" in message
    assert "original selection was kept" not in message
    assert "private-" not in message


@pytest.mark.asyncio
async def test_activation_error_after_switch_does_not_claim_the_selection_failed(tmp_path, monkeypatch):
    manager, _, candidate, _ = make_selection(tmp_path, monkeypatch)
    runtime = command_runtime(manager, monkeypatch)
    runtime._sync_workzone_to_backend_config.side_effect = OSError(errno.EIO, "private-activation")
    switched, message = await runtime._switch_backend_mode(42, "claude-cli")
    assert switched is True and manager.current_backend is candidate
    assert "backend_switch_activate_io_error" in message and "operr_" in message
    assert "original selection was kept" not in message and "private-" not in message


@pytest.mark.asyncio
async def test_configuration_read_error_before_switch_is_reported_safely(tmp_path, monkeypatch):
    from orchestrator import runtime_session

    manager, original, candidate, before = make_selection(tmp_path, monkeypatch)
    runtime = command_runtime(manager, monkeypatch)

    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "private-zone")

    monkeypatch.setattr(runtime_session, "apply_session_workzones", denied)
    switched, message = await runtime._switch_backend_mode(42, "claude-cli")
    assert switched is False and manager.current_backend is original
    assert "backend_switch_configuration_access_denied" in message and "operr_" in message
    assert manager.state_store.path.read_bytes() == before
    candidate.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_allowed_backend_returns_a_safe_configuration_rejection(tmp_path, monkeypatch):
    manager, original, candidate, _ = make_selection(tmp_path, monkeypatch)
    result = await manager.switch_backend_result("disabled-backend")
    assert result.switched is False
    assert result.issue.error_code == "backend_switch_configuration_backend_not_allowed"
    assert manager.current_backend is original
    candidate.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_published_selection_is_not_rolled_back_on_directory_durability_error(tmp_path, monkeypatch):
    manager, original, candidate, _ = make_selection(tmp_path, monkeypatch)
    sync = config_json._sync_directory
    attempts = []

    def fail_state_directory(directory):
        if Path(directory).resolve() == manager.state_store.path.parent.resolve():
            attempts.append(True)
            raise OSError(errno.EIO, "private-directory-sync")
        return sync(directory)

    monkeypatch.setattr(config_json, "_sync_directory", fail_state_directory)
    result = await manager.switch_backend_result("claude-cli")
    assert attempts == [True]
    assert result.switched is True and result.issue.unconfirmed is True
    assert result.issue.error_code == "backend_switch_persist_durability_unconfirmed"
    assert manager.current_backend is candidate
    assert manager.config.active_backend == "claude-cli"
    assert json.loads(manager.state_store.path.read_text(encoding="utf-8"))["active_backend"] == "claude-cli"
    original.shutdown.assert_awaited_once()
    candidate.shutdown.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["shutdown", "proxy"])
async def test_old_backend_cleanup_failure_is_a_warning_after_commit(tmp_path, monkeypatch, failure):
    from unittest.mock import Mock

    manager, original, candidate, _ = make_selection(tmp_path, monkeypatch)
    if failure == "shutdown":
        original.shutdown.side_effect = RuntimeError("private-shutdown")
    else:
        original._browser_gateway_proxy = SimpleNamespace(close=Mock(side_effect=RuntimeError("private-proxy")))
    result = await manager.switch_backend_result("claude-cli")
    assert result.switched is True and manager.current_backend is candidate
    assert result.issue.error_code == "backend_switch_retire_internal_error"
    assert "private-" not in result.issue.render()
    assert manager.state_store.read_strict()["active_backend"] == "claude-cli"
    original.shutdown.assert_awaited_once()
    candidate.shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_candidate_cleanup_failure_does_not_replace_the_initialization_cause(tmp_path, monkeypatch):
    manager, original, candidate, _ = make_selection(tmp_path, monkeypatch)
    candidate.initialize.side_effect = TimeoutError("private-initialization")
    candidate.shutdown.side_effect = RuntimeError("private-cleanup")
    result = await manager.switch_backend_result("claude-cli")
    assert result.switched is False and manager.current_backend is original
    assert result.issue.error_code == "backend_switch_initialize_timed_out"
    candidate.shutdown.assert_awaited_once()
    original.shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_boolean_switch_also_preserves_published_selection(tmp_path, monkeypatch):
    manager, original, candidate, _ = make_selection(tmp_path, monkeypatch)

    def fail_sync(directory):
        raise OSError(errno.EIO, "private-sync")

    monkeypatch.setattr(config_json, "_sync_directory", fail_sync)
    assert await manager.switch_backend("claude-cli") is True
    assert manager.current_backend is candidate
    assert manager.config.active_backend == "claude-cli"
    assert manager.state_store.read_strict()["active_backend"] == "claude-cli"
    original.shutdown.assert_awaited_once()
    candidate.shutdown.assert_not_awaited()
