from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_session
from orchestrator.admin_local_testing import execute_local_command
from orchestrator.bridge_memory import BridgeMemoryStore
from orchestrator.session_store import SessionStore
from tests.test_session_store import _complete


class Runtime:
    name = "lily"

    def __init__(self, workspace, store):
        self.workspace_dir = workspace
        self.global_config = SimpleNamespace(authorized_id=7, instance_id="HASHI3", bridge_home=workspace)
        self.session_store = store

    def _is_authorized_user(self, actor):
        return actor == 7

    def _is_command_allowed(self, command):
        return True

    async def _reply_text(self, update, text, **kwargs):
        await update.message.reply_text(text, **kwargs)


@pytest.mark.asyncio
async def test_forget_requires_verified_api_identity_and_confirm_then_opens_new_session(tmp_path, monkeypatch):
    home = tmp_path / "agent"
    home.mkdir()
    (home / "agent.md").write_text(
        "[persona]\nPersona\n[persona_end]\n[sys]\nRules\n[sys_end]\n"
        "[memory]\nwrong idea\n[memory_end]\n",
        encoding="utf-8",
    )
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="old", key="old", text="wrong idea", answer="wrong answer")
    legacy = store.create_session(owner_id="exchange:legacy", agent_id="lily")
    _complete(store, session_id=legacy["session_id"], owner_id="exchange:legacy", agent_id="lily",
              request_id="legacy", key="legacy", text="wrong legacy idea", answer="wrong legacy answer")
    memory = BridgeMemoryStore(home)
    memory.record_memory("episodic", "test", "wrong idea")
    runtime = Runtime(home, store)
    from orchestrator.canonical_audit import CanonicalAuditStore
    monkeypatch.setenv("HASHI_CANONICAL_AUDIT_KEY", "test-only-key-for-forget")
    runtime.canonical_audit = CanonicalAuditStore(
        tmp_path / "audit-home", instance_id="HASHI3", agent_id="lily",
    )
    metadata = {"_hashi_owner_id": "user:7", "session_surface": "workbench",
                "session_id": session["session_id"]}
    denied = await execute_local_command(runtime, "/forget 2h", session_metadata=metadata)
    assert "authenticated owner" in str(denied["messages"])
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is None

    metadata["_hashi_forget_authenticated"] = True
    preview = await execute_local_command(runtime, "/forget 2h", session_metadata=metadata)
    text = str(preview["messages"])
    token = re.search(r"/forget confirm ([A-Za-z0-9_-]+)", text).group(1)
    assert "wrong idea" in str(store.messages(session["session_id"], owner_id="user:7"))
    monkeypatch.setattr(runtime_session, "_reset_cli_backend", AsyncMock(return_value="session"))
    monkeypatch.setattr(runtime_session, "_bind_session", AsyncMock())
    monkeypatch.setattr(runtime_session, "_discard_session_transients", lambda runtime: None)
    monkeypatch.setattr(runtime_session, "_prepare_clean_context", lambda *a, **k: None)

    completed = await execute_local_command(runtime, f"/forget confirm {token}", session_metadata=metadata)
    assert completed["ok"]
    assert "new session is ready" in str(completed["messages"])
    assert completed["result"]["new_session_id"] != session["session_id"]
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is None
    assert "wrong idea" not in str(store.agent_history_page(owner_id="user:7", agent_id="lily"))
    assert "wrong legacy idea" not in str(store.messages(legacy["session_id"], owner_id="exchange:legacy"))
    assert memory.retrieve_memories("wrong idea", limit=3) == []
    assert "wrong idea" not in (home / "agent.md").read_text(encoding="utf-8")
    with store._connection() as connection:
        copies = [row[0] for row in connection.execute("SELECT payload_json FROM pcm_forget_audit")]
    with memory._connect() as connection:
        copies += [row[0] for row in connection.execute("SELECT payload_json FROM pcm_forget_quarantine")]
    assert copies and all("wrong idea" not in copy for copy in copies)
    assert b"wrong idea" not in runtime.canonical_audit.events_path.read_bytes()
    import json
    events = runtime.canonical_audit.read_events(
        {"actor": "test", "purpose": "verify retention", "allow_raw_audit": True}
    )
    assert "wrong idea" in json.dumps(events, ensure_ascii=False)


@pytest.mark.asyncio
async def test_audit_failure_does_not_remove_any_history(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="old", key="old", text="must stay", answer="must stay too")
    runtime = Runtime(home, store)
    runtime.canonical_audit = SimpleNamespace(
        instance_id="HASHI3", agent_id="lily",
        record=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("audit unavailable")),
    )
    metadata = {"_hashi_owner_id": "user:7", "_hashi_forget_authenticated": True,
                "session_surface": "workbench", "session_id": session["session_id"]}
    preview = await execute_local_command(runtime, "/forget 2h", session_metadata=metadata)
    token = re.search(r"/forget confirm ([A-Za-z0-9_-]+)", str(preview["messages"])).group(1)
    failed = await execute_local_command(runtime, f"/forget confirm {token}", session_metadata=metadata)
    assert not failed["ok"]
    assert "must stay" in str(store.messages(session["session_id"], owner_id="user:7"))
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is None


@pytest.mark.asyncio
async def test_forget_waits_for_background_memory_writers(tmp_path):
    import asyncio

    home = tmp_path / "agent"
    home.mkdir()
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    runtime = Runtime(home, store)
    task = asyncio.create_task(asyncio.Event().wait())
    runtime._background_tasks = {task}
    try:
        result = await execute_local_command(
            runtime, "/forget 2h", session_metadata={
                "_hashi_owner_id": "user:7", "_hashi_forget_authenticated": True,
                "session_surface": "workbench", "session_id": session["session_id"],
            },
        )
        assert "active or queued" in str(result["messages"])
        assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("locale", ["en", "zh-CN"])
def test_forget_preview_has_readable_line_breaks(locale):
    from orchestrator import ui_language

    text = ui_language.tr(
        "forget.preview", locale=locale, agent="agent", start="start", end="end", token="token",
    )
    assert "\n" in text
    assert "\\n" not in text
    assert "HCC" in text
    assert ("更早内容" if locale == "zh-CN" else "older information") in text
    assert ("不能自动恢复" if locale == "zh-CN" else "cannot yet be restored automatically") in text


@pytest.mark.parametrize("duration", ["0h", "1w", "-1h", "9999d", "3", "1.5d"])
def test_invalid_durations_are_rejected(duration):
    from orchestrator.commands.forget import _duration

    with pytest.raises(ValueError):
        _duration(duration)
