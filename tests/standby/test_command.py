from types import SimpleNamespace

import pytest

from orchestrator import runtime_session, ui_language
from orchestrator.commands.standby import standby_command
from orchestrator.command_registry import load_runtime_commands
from orchestrator.session_store import SessionStore


async def test_native_command_discovery_readonly_no_capture_and_localization(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="SB-COMMAND")
    session = store.ensure_default_session(owner_id="user:7", agent_id="alice")
    replies = []
    async def reply(update, text, **options):
        replies.append((text, options))
    runtime = SimpleNamespace(_is_authorized_user=lambda actor: actor == 7, _reply_text=reply)
    monkeypatch.setattr(runtime_session, "ensure_store", lambda runtime: store)
    monkeypatch.setattr(runtime_session, "current_session_for_update", lambda runtime, update: session)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
    assert any(command.name == "standby" for command in load_runtime_commands())
    for locale in ("en", "zh-CN"):
        replies.clear()
        with ui_language.language_scope(runtime, locale=locale):
            await standby_command(runtime, update, SimpleNamespace(args=[]))
        assert len(replies) == 1 and replies[0][1]["parse_mode"] == "HTML"
        assert ("均已停止" if locale == "zh-CN" else "are stopped") in replies[0][0]
        assert ("尚未完成资格验证" if locale == "zh-CN" else "not qualified") in replies[0][0]
        assert ui_language.tr("standby.no_capture", locale=locale) in replies[0][0]
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM standby_controls").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


async def test_unauthenticated_command_does_not_open_native_store(monkeypatch):
    async def reply(update, text, **options):
        assert "authenticated" in text
    runtime = SimpleNamespace(_is_authorized_user=lambda actor: False, _reply_text=reply)
    monkeypatch.setattr(runtime_session, "ensure_store", lambda runtime: pytest.fail("unauthorized store read"))
    with ui_language.language_scope(runtime, locale="en"):
        await standby_command(runtime, SimpleNamespace(effective_user=SimpleNamespace(id=8)), SimpleNamespace(args=[]))
