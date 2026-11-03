"""Failure explanations follow the actual PAO command boundary and durable state."""
from __future__ import annotations

import asyncio
import errno
import sqlite3
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_session
from tests.test_fresh_context import _session_command_runtime, fake_context, fake_update


def selected_runtime(tmp_path, operation):
    runtime, default, replies, resets = _session_command_runtime(
        tmp_path, engine="codex-cli", supports_sessions=True,
    )
    target = runtime.session_store.create_session(
        owner_id="user:123", agent_id="arale", title="Target",
    )
    if operation == "archive":
        runtime.session_store.bind_primary_session(
            owner_id="user:123", agent_id="arale", session_id=target["session_id"],
        )
    context = fake_context(target["session_id"]) if operation == "use" else fake_context()
    return runtime, default, target, replies, resets, context


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["new", "fresh", "use", "archive"])
async def test_adapter_failure_has_stage_safe_reason_and_diagnostic(tmp_path, operation):
    runtime, _, _, replies, _, context = selected_runtime(tmp_path, operation)
    runtime.backend_manager.current_backend.handle_new_session = AsyncMock(
        side_effect=RuntimeError("private-source token=private-secret"),
    )
    await getattr(runtime, "cmd_" + operation)(fake_update(), context)
    assert "session_" + operation + "_reset_backend_internal_error" in replies[-1]
    assert "operr_" in replies[-1]
    assert "private-" not in replies[-1]


@pytest.mark.asyncio
async def test_new_session_storage_failure_is_reported_without_escaping(tmp_path, monkeypatch):
    runtime, _, _, replies, _, context = selected_runtime(tmp_path, "new")

    def denied(**kwargs):
        raise PermissionError(errno.EACCES, "private-path")

    monkeypatch.setattr(runtime.session_store, "create_session", denied)
    await runtime.cmd_new(fake_update(), context)
    assert "session_new_create_access_denied" in replies[-1]
    assert "operr_" in replies[-1] and "private-path" not in replies[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,supports_sessions", [("codex-cli", True), ("her-v2", False)])
async def test_fresh_storage_failure_has_a_storage_cause(tmp_path, monkeypatch, engine, supports_sessions):
    runtime, _, _, replies, _, context = selected_runtime(tmp_path, "fresh")
    runtime.config.active_backend = engine
    runtime.backend_manager.current_backend.capabilities.supports_sessions = supports_sessions

    def unreadable(*args, **kwargs):
        raise sqlite3.DatabaseError("private-sql-and-session-content")

    monkeypatch.setattr(runtime.session_store, "start_fresh_generation", unreadable)
    await runtime.cmd_fresh(fake_update(), context)
    assert "session_fresh_save_storage_failed" in replies[-1]
    assert "unconfirmed" in replies[-1].lower()
    assert "private-" not in replies[-1]


@pytest.mark.asyncio
async def test_new_binding_ack_loss_never_archives_the_now_active_session(tmp_path, monkeypatch):
    runtime, default, target, replies, _, context = selected_runtime(tmp_path, "new")

    def route_record_failed(*args, **kwargs):
        raise OSError(errno.EIO, "private-route")

    monkeypatch.setattr(runtime_session, "record_telegram_route", route_record_failed)
    await runtime.cmd_new(fake_update(), context)
    rows = runtime.session_store.list_sessions(
        owner_id="user:123", agent_id="arale", include_archived=True,
    )
    created = [row for row in rows if row["session_id"] not in {default["session_id"], target["session_id"]}]
    assert len(created) == 1
    assert created[0]["status"] == "active"
    current = runtime.session_store.resolve_primary_session(owner_id="user:123", agent_id="arale")
    assert current["session_id"] == created[0]["session_id"]
    assert "session_new_bind_io_error" in replies[-1]
    assert "unconfirmed" in replies[-1].lower()
    assert "previous channel binding remains active" not in replies[-1].lower()


@pytest.mark.asyncio
async def test_fresh_generation_ack_loss_never_claims_old_generation_is_active(tmp_path, monkeypatch):
    runtime, default, _, replies, _, context = selected_runtime(tmp_path, "fresh")
    start = runtime.session_store.start_fresh_generation
    calls = []

    def committed_then_error(*args, **kwargs):
        calls.append(True)
        start(*args, **kwargs)
        raise OSError(errno.EIO, "private-postcommit")

    monkeypatch.setattr(runtime.session_store, "start_fresh_generation", committed_then_error)
    await runtime.cmd_fresh(fake_update(), context)
    assert calls == [True]
    assert runtime.session_store.get_session(default["session_id"])["context_generation"] == 2
    assert "session_fresh_save_io_error" in replies[-1]
    assert "unconfirmed" in replies[-1].lower()
    assert "previous session generation remains active" not in replies[-1].lower()


@pytest.mark.asyncio
async def test_archive_followup_error_reports_partial_state_without_replay(tmp_path, monkeypatch):
    runtime, _, target, replies, _, context = selected_runtime(tmp_path, "archive")
    bind = runtime_session._bind_session

    async def cannot_bind(*args, **kwargs):
        raise OSError(errno.EIO, "private-fallback-binding")

    monkeypatch.setattr(runtime_session, "_bind_session", cannot_bind)
    await runtime.cmd_archive(fake_update(), context)
    assert runtime.session_store.get_session(target["session_id"])["status"] == "archived"
    assert "session_archive_bind_io_error" in replies[-1]
    assert "unconfirmed" in replies[-1].lower()
    monkeypatch.setattr(runtime_session, "_bind_session", bind)


@pytest.mark.asyncio
async def test_use_selection_read_error_is_not_an_unhandled_exception(tmp_path, monkeypatch):
    runtime, _, _, replies, _, context = selected_runtime(tmp_path, "use")

    def unreadable(**kwargs):
        raise PermissionError(errno.EACCES, "private-store")

    monkeypatch.setattr(runtime.session_store, "list_sessions", unreadable)
    await runtime.cmd_use(fake_update(), context)
    assert "session_use_read_access_denied" in replies[-1]
    assert "operr_" in replies[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["new", "fresh", "use", "archive"])
async def test_explicit_backend_rejection_has_a_specific_safe_cause(tmp_path, operation):
    runtime, _, _, replies, _, context = selected_runtime(tmp_path, operation)
    runtime.backend_manager.current_backend.handle_new_session = AsyncMock(return_value=False)
    await getattr(runtime, "cmd_" + operation)(fake_update(), context)
    assert "session_" + operation + "_reset_backend_backend_refused" in replies[-1]
    assert "The backend refused to start a new session." in replies[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["new", "fresh", "use", "archive"])
async def test_cancelled_session_command_does_not_become_a_failure_notice(tmp_path, operation):
    runtime, _, _, replies, _, context = selected_runtime(tmp_path, operation)
    runtime.backend_manager.current_backend.handle_new_session = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await getattr(runtime, "cmd_" + operation)(fake_update(), context)
    assert replies == []
