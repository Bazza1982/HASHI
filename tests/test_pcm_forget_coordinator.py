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
    original = pcm_forget_coordinator.validate_untracked_continuity_scope

    def interrupted(*args, **kwargs):
        raise OSError("simulated interruption")

    monkeypatch.setattr(pcm_forget_coordinator, "validate_untracked_continuity_scope", interrupted)
    with pytest.raises(OSError):
        await pcm_forget_coordinator.finish_pending(runtime, update, owner_id="user:7")
    pending = store.pending_pcm_forget(owner_id="user:7", agent_id="lily")
    assert "local_memory" in pending["steps"]
    assert "continuity" not in pending["steps"]
    with pytest.raises(SessionConflict, match="forget"):
        store.accept_run(session_id=session["session_id"], owner_id="user:7", agent_id="lily",
                         request_id="blocked", text="new", source="test", idempotency_key="blocked")

    monkeypatch.setattr(pcm_forget_coordinator, "validate_untracked_continuity_scope", original)
    result = await pcm_forget_coordinator.finish_pending(runtime, update, owner_id="user:7")
    assert result["new_session_id"] != session["session_id"]
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is None
    assert [row["content"] for row in memory.retrieve_memories("wrong", limit=3)] == ["wrong memory"]
    assert "wrong memory" in (home / "agent.md").read_text(encoding="utf-8")
    assert "wrong idea" not in str(store.messages(session["session_id"], owner_id="user:7"))
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


@pytest.mark.asyncio
async def test_forgotten_session_carries_verified_old_archives_to_new_session(tmp_path, monkeypatch):
    import time
    from orchestrator.memory_plus_mode import (
        MemoryPlusConfig, _append_history_index, _rollover_memory_plus_state,
        append_memory_plus_manual_note, load_memory_plus_state, search_memory_plus_history,
    )

    home = tmp_path / "agent"
    home.mkdir()
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    old = store.create_session(owner_id="user:7", agent_id="lily")
    workspace = store.session_workspace(old["session_id"], 1)
    append_memory_plus_manual_note(workspace, "older safe memory about orchids")
    old_state = load_memory_plus_state(workspace)
    old_state["date"] = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
    _rollover_memory_plus_state(workspace, old_state, today="2099-01-01", cfg=MemoryPlusConfig())
    old_archive = workspace / "memory" / "memory_plus_wiki" / f"{old_state['date']}_memory_plus_state.json"
    other = store.create_session(owner_id="user:7", agent_id="other")
    other_workspace = store.session_workspace(other["session_id"], 1)
    append_memory_plus_manual_note(other_workspace, "other agent private memory about lilies")
    other_state = load_memory_plus_state(other_workspace)
    other_state["date"] = old_state["date"]
    _rollover_memory_plus_state(other_workspace, other_state, today="2099-01-01", cfg=MemoryPlusConfig())
    cutoff = datetime.now(timezone.utc) + timedelta(milliseconds=100)
    time.sleep(0.2)
    append_memory_plus_manual_note(workspace, "recent wrong memory about violets")
    _append_history_index(workspace, load_memory_plus_state(workspace), old_archive)
    now = datetime.now(timezone.utc)
    preview = store.preview_pcm_forget(owner_id="user:7", agent_id="lily",
        session_id=old["session_id"], duration=now - cutoff, now=now)
    store.confirm_pcm_forget(owner_id="user:7", agent_id="lily",
        session_id=old["session_id"], token=preview["token"], now=now)
    runtime = SimpleNamespace(name="lily", workspace_dir=home, session_store=store)
    monkeypatch.setattr(runtime_session, "_reset_cli_backend", AsyncMock(return_value="session"))
    monkeypatch.setattr(runtime_session, "_bind_session", AsyncMock())
    monkeypatch.setattr(runtime_session, "_discard_session_transients", lambda runtime: None)
    monkeypatch.setattr(runtime_session, "_prepare_clean_context", lambda *a, **k: None)

    complete = store.complete_pcm_forget
    monkeypatch.setattr(store, "complete_pcm_forget",
        lambda **kwargs: (_ for _ in ()).throw(OSError("simulated crash before completion")))
    with pytest.raises(OSError):
        await pcm_forget_coordinator.finish_pending(runtime, object(), owner_id="user:7")
    pending = store.pending_pcm_forget(owner_id="user:7", agent_id="lily")
    pending_id = pending["result"]["new_session_id"]
    monkeypatch.setattr(store, "complete_pcm_forget", complete)
    result = await pcm_forget_coordinator.finish_pending(runtime, object(), owner_id="user:7")
    assert result["new_session_id"] == pending_id
    fresh = store.session_workspace(result["new_session_id"], 1)
    assert any("older safe memory about orchids" in row["excerpt"]
               for row in search_memory_plus_history(fresh, "orchids"))
    assert not search_memory_plus_history(fresh, "violets")
    assert not search_memory_plus_history(fresh, "lilies")

    # Modifying a verified copy cannot introduce a forgotten item.
    import json
    import os
    copied = next((fresh / "memory" / "memory_plus_wiki").rglob("*_memory_plus_state.json"))
    original = copied.read_bytes()
    modified = json.loads(original)
    modified["today"]["facts"] = ["violets returned through a changed archive"]
    copied.write_text(json.dumps(modified), encoding="utf-8")
    os.utime(copied, (cutoff.timestamp() - 100, cutoff.timestamp() - 100))
    assert not search_memory_plus_history(fresh, "violets")
    copied.write_bytes(original)

    # A later, narrower forget must not lose or misdate the copied old archive.
    second = store.preview_pcm_forget(owner_id="user:7", agent_id="lily",
        session_id=result["new_session_id"], duration=timedelta(milliseconds=20))
    store.confirm_pcm_forget(owner_id="user:7", agent_id="lily",
        session_id=result["new_session_id"], token=second["token"])
    result2 = await pcm_forget_coordinator.finish_pending(runtime, object(), owner_id="user:7")
    fresh2 = store.session_workspace(result2["new_session_id"], 1)
    assert any("older safe memory about orchids" in row["excerpt"]
               for row in search_memory_plus_history(fresh2, "orchids"))
    assert not search_memory_plus_history(fresh2, "violets")

    # An archive copied earlier must not survive a later wider forget that
    # includes the archive's original, now-unverifiable creation time.
    third = store.preview_pcm_forget(owner_id="user:7", agent_id="lily",
        session_id=result2["new_session_id"], duration=timedelta(hours=1))
    store.confirm_pcm_forget(owner_id="user:7", agent_id="lily",
        session_id=result2["new_session_id"], token=third["token"])
    result3 = await pcm_forget_coordinator.finish_pending(runtime, object(), owner_id="user:7")
    fresh3 = store.session_workspace(result3["new_session_id"], 1)
    assert not search_memory_plus_history(fresh3, "orchids")
    assert not search_memory_plus_history(fresh3, "violets")
