from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import pcm_forget_coordinator, runtime_session
from orchestrator.bridge_memory import BridgeMemoryStore
from orchestrator.session_store import SessionConflict, SessionStore
from tests.test_session_store import _complete


@pytest.mark.asyncio
async def test_confirmed_operation_resumes_after_interruption_and_creates_new_session(tmp_path, monkeypatch):
    home = tmp_path / "agent"
    home.mkdir()
    (home / "agent.md").write_text(
        "[persona]\nPersona\n[persona_end]\n[sys]\nRules\n[sys_end]\n"
        "[memory]\nwrong memory\n[memory_end]\n",
        encoding="utf-8",
    )
    memory = BridgeMemoryStore(home)
    monkeypatch.setattr(memory, "_now", lambda: "2026-10-28T10:00:00+00:00")
    memory.record_memory("episodic", "test", "wrong memory")
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: "2026-10-28T10:00:00+00:00")
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="old", key="old", text="wrong idea", answer="wrong answer")
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    preview = store.preview_pcm_forget(
        owner_id="user:7", agent_id="lily", session_id=session["session_id"],
        duration=timedelta(hours=3), now=now,
    )
    store.confirm_pcm_forget(
        owner_id="user:7", agent_id="lily", session_id=session["session_id"],
        token=preview["token"], now=now,
    )
    runtime = SimpleNamespace(name="lily", workspace_dir=home, session_store=store)
    update = object()
    reset = AsyncMock(return_value="session")
    bind = AsyncMock()
    monkeypatch.setattr(runtime_session, "_reset_cli_backend", reset)
    monkeypatch.setattr(runtime_session, "_bind_session", bind)
    monkeypatch.setattr(runtime_session, "_discard_session_transients", lambda runtime: None)
    monkeypatch.setattr(runtime_session, "_prepare_clean_context", lambda *a, **k: None)
    original = pcm_forget_coordinator.quarantine_unproven_continuity

    def interrupted(*args, **kwargs):
        raise OSError("simulated interruption")

    monkeypatch.setattr(pcm_forget_coordinator, "quarantine_unproven_continuity", interrupted)
    with pytest.raises(OSError):
        await pcm_forget_coordinator.finish_pending(runtime, update, owner_id="user:7")
    pending = store.pending_pcm_forget(owner_id="user:7", agent_id="lily")
    assert "local_memory" in pending["steps"]
    assert "continuity" not in pending["steps"]
    with pytest.raises(SessionConflict, match="forget"):
        store.accept_run(session_id=session["session_id"], owner_id="user:7", agent_id="lily",
                         request_id="blocked", text="new", source="test", idempotency_key="blocked")

    monkeypatch.setattr(pcm_forget_coordinator, "quarantine_unproven_continuity", original)
    result = await pcm_forget_coordinator.finish_pending(runtime, update, owner_id="user:7")
    assert result["new_session_id"] != session["session_id"]
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is None
    assert memory.retrieve_memories("wrong", limit=3) == []
    assert "wrong memory" not in (home / "agent.md").read_text(encoding="utf-8")
    reset.assert_awaited_once()
    bind.assert_awaited_once_with(runtime, update, result["new_session_id"])


@pytest.mark.asyncio
async def test_completion_retry_reestablishes_backend_session(tmp_path, monkeypatch):
    home = tmp_path / "agent"
    home.mkdir()
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    result = store.forget_pcm_window(
        owner_id="user:7", agent_id="lily",
        start=datetime(2026, 10, 28, 9, tzinfo=timezone.utc),
        end=datetime(2026, 10, 28, 11, tzinfo=timezone.utc),
    )
    runtime = SimpleNamespace(name="lily", workspace_dir=home, session_store=store)
    reset = AsyncMock(return_value="session")
    bind = AsyncMock()
    monkeypatch.setattr(runtime_session, "_reset_cli_backend", reset)
    monkeypatch.setattr(runtime_session, "_bind_session", bind)
    monkeypatch.setattr(runtime_session, "_discard_session_transients", lambda runtime: None)
    monkeypatch.setattr(runtime_session, "_prepare_clean_context", lambda *a, **k: None)
    original = store.complete_pcm_forget
    monkeypatch.setattr(store, "complete_pcm_forget", lambda **kwargs: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        await pcm_forget_coordinator.finish_pending(runtime, object(), owner_id="user:7")
    pending = store.pending_pcm_forget(owner_id="user:7", agent_id="lily")
    new_id = pending["result"]["new_session_id"]
    assert "backend" in pending["steps"]

    monkeypatch.setattr(store, "complete_pcm_forget", original)
    completed = await pcm_forget_coordinator.finish_pending(runtime, object(), owner_id="user:7")
    assert completed["new_session_id"] == new_id
    assert reset.await_count == 2
    assert bind.await_count == 2
