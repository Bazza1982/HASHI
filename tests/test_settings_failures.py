"""Settings keep failed writes distinct from changes already published."""
from __future__ import annotations

import errno
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator import config_json, model_fallback_policy as policy_owner, ui_language
from orchestrator import runtime_her_fallback as fallback_menu
from orchestrator.commands import askme
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from tests.test_run_question_connectors import running
from tests.test_runtime_her_fallback import _runtime as fallback_runtime, _query, _update


def language_runtime(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "fixture"
    runtime.global_config = SimpleNamespace(
        bridge_home=tmp_path, project_root=tmp_path, authorized_id=42, ui_language="en",
    )
    runtime._is_authorized_user = lambda actor: actor == 42
    runtime._reply_text = AsyncMock()
    runtime._schedule_language_menu_sync = Mock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(id=42),
    )
    ui_language.set_preferred_locale(runtime, "en", update)
    return runtime, update


def fail_replace(monkeypatch):
    def rejected(*args, **kwargs):
        raise OSError(errno.ENOSPC, "private-storage-path")
    monkeypatch.setattr(config_json.os, "replace", rejected)


def fail_sync(monkeypatch):
    calls = []
    def uncertain(*args, **kwargs):
        calls.append(True)
        raise OSError(errno.EIO, "private-directory")
    monkeypatch.setattr(config_json, "_sync_directory", uncertain)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["command", "button"])
@pytest.mark.parametrize("failure,reason", [
    ("disk", "disk_full"), ("conflict", "state_changed"), ("durability", "durability_unconfirmed"),
])
async def test_language_settings_keep_actual_storage_cause_and_commit_facts(
    tmp_path, monkeypatch, entry, failure, reason,
):
    runtime, update = language_runtime(tmp_path)
    sync_calls = []
    if failure == "disk":
        fail_replace(monkeypatch)
    elif failure == "conflict":
        def conflict(*args, **kwargs):
            raise config_json.ConfigConflictError("private-stale-read")
        monkeypatch.setattr(ui_language, "write_config_json", conflict)
    else:
        sync_calls = fail_sync(monkeypatch)
    if entry == "command":
        await runtime.cmd_language(update, SimpleNamespace(args=["zh-CN"]))
        message = runtime._reply_text.await_args.args[1]
    else:
        query = SimpleNamespace(
            data="language:set:zh-CN", from_user=SimpleNamespace(id=42),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        update.callback_query = query
        await runtime.callback_language(update, None)
        message = query.edit_message_text.await_args.args[0] if query.edit_message_text.await_args else ""
        assert query.answer.await_args.kwargs["show_alert"] is True
    assert "language_settings_persist_" + reason in message and "operr_" in message
    assert "private-" not in message
    expected = "zh-CN" if failure == "durability" else "en"
    assert ui_language.preferred_locale(runtime, update) == expected
    if failure == "durability":
        assert sync_calls == [True]
        assert "unconfirmed" in message.lower()
        assert "previous setting was kept" not in message.lower()
    runtime._schedule_language_menu_sync.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b'{"version":1,"users":[]}', b'{"users":', b'{"version":2,"users":{}}'])
async def test_invalid_saved_language_document_is_not_an_invalid_requested_language(tmp_path, raw):
    runtime, update = language_runtime(tmp_path)
    path = ui_language.preferences_path(runtime)
    path.write_bytes(raw)
    await runtime.cmd_language(update, SimpleNamespace(args=["zh-CN"]))
    message = runtime._reply_text.await_args.args[1]
    assert "language_settings_persist_configuration_invalid" in message
    assert path.read_bytes() == raw


@pytest.mark.asyncio
async def test_invalid_saved_fallback_document_is_reported_without_overwriting_it(tmp_path):
    runtime = fallback_runtime(tmp_path)
    path = policy_owner.policy_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b'{"users":'
    path.write_bytes(raw)
    query = _query("fallback:mode:auto")
    await fallback_menu.callback_fallback(runtime, _update(query), None)
    message = query.edit_message_text.await_args.args[0]
    assert "fallback_settings_read_configuration_invalid" in message
    assert path.read_bytes() == raw


@pytest.mark.asyncio
async def test_invalid_language_is_not_reported_as_a_failed_storage_write(tmp_path):
    runtime, update = language_runtime(tmp_path)
    await runtime.cmd_language(update, SimpleNamespace(args=["not-a-locale"]))
    message = runtime._reply_text.await_args.args[1]
    assert "language_settings_persist" not in message
    assert ui_language.preferred_locale(runtime, update) == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,reason", [
    ("disk", "disk_full"), ("conflict", "state_changed"), ("durability", "durability_unconfirmed"),
])
async def test_fallback_settings_show_cause_and_reloaded_committed_value(tmp_path, monkeypatch, failure, reason):
    runtime = fallback_runtime(tmp_path)
    policy_owner.write_policy(runtime, fallback_menu._actor(runtime), policy_owner.default_policy(), document=None)
    sync_calls = []
    if failure == "disk":
        fail_replace(monkeypatch)
    elif failure == "conflict":
        def conflict(*args, **kwargs):
            raise config_json.ConfigConflictError("private-stale-read")
        monkeypatch.setattr(policy_owner, "write_policy", conflict)
    else:
        sync_calls = fail_sync(monkeypatch)
    query = _query("fallback:mode:auto")
    await fallback_menu.callback_fallback(runtime, _update(query), None)
    message = query.edit_message_text.await_args.args[0]
    assert "fallback_settings_persist_" + reason in message and "operr_" in message
    assert "private-" not in message
    policy, _ = policy_owner.read_policy(runtime, fallback_menu._actor(runtime))
    assert policy.mode == (policy_owner.MODE_AUTO if failure == "durability" else policy_owner.MODE_OFF)
    assert ("AUTO" if failure == "durability" else "OFF") in message
    if failure == "durability":
        assert sync_calls == [True] and "unconfirmed" in message.lower()


@pytest.mark.asyncio
async def test_fallback_read_failure_does_not_escape_or_show_a_default_as_saved(tmp_path, monkeypatch):
    runtime = fallback_runtime(tmp_path)
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "private-read-path")
    monkeypatch.setattr(policy_owner, "read_policy", denied)
    query = _query("fallback:mode:auto")
    await fallback_menu.callback_fallback(runtime, _update(query), None)
    message = query.edit_message_text.await_args.args[0]
    assert "fallback_settings_read_access_denied" in message and "operr_" in message
    assert "private-" not in message and "AUTO" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["command", "button"])
@pytest.mark.parametrize("committed", [False, True])
async def test_question_settings_keep_read_write_cause_and_uncertain_commit(tmp_path, monkeypatch, entry, committed):
    service, _, _, _ = running(tmp_path)
    runtime = SimpleNamespace(
        name="zelda", session_store=service.store,
        _is_authorized_user=lambda actor: actor == 7, _reply_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=7))
    setter = type(service).set_question_policy
    calls = []
    def set_policy(self, **kwargs):
        calls.append(True)
        if committed:
            setter(self, **kwargs)
            raise OSError(errno.EIO, "private-after-commit")
        raise sqlite3.DatabaseError("private-query-content")
    monkeypatch.setattr(type(service), "set_question_policy", set_policy)
    if entry == "command":
        await askme.askme_command(runtime, update, SimpleNamespace(args=["off"]))
        message = runtime._reply_text.await_args.args[1]
    else:
        query = SimpleNamespace(
            data="askme:agent:off", from_user=SimpleNamespace(id=7),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        update.callback_query = query
        await askme.askme_callback(runtime, update, None)
        message = query.edit_message_text.await_args.args[0] if query.edit_message_text.await_args else ""
    expected = "io_error" if committed else "storage_failed"
    assert "question_settings_persist_" + expected in message and "operr_" in message
    assert "unconfirmed" in message.lower() and "private-" not in message
    assert service.question_policy("zelda")["agent_enabled"] is (not committed)
    assert calls == [True]


@pytest.mark.asyncio
async def test_question_settings_read_failure_is_not_a_saved_or_empty_policy(tmp_path, monkeypatch):
    service, _, _, _ = running(tmp_path)
    runtime = SimpleNamespace(
        name="zelda", session_store=service.store,
        _is_authorized_user=lambda actor: actor == 7, _reply_text=AsyncMock(),
    )
    def unreadable(*args, **kwargs):
        raise sqlite3.DatabaseError("private-query-content")
    monkeypatch.setattr(type(service), "question_policy", unreadable)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
    await askme.askme_command(runtime, update, SimpleNamespace(args=["status"]))
    message = runtime._reply_text.await_args.args[1]
    assert "question_settings_read_storage_failed" in message and "operr_" in message
    assert "private-" not in message
