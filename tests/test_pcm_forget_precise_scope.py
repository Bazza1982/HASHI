from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import pcm_forget_coordinator, runtime_session
from orchestrator.bridge_memory import BridgeMemoryStore
from orchestrator.memory_plus_mode import append_memory_plus_manual_note, read_memory_plus_notepad
from orchestrator.session_store import SessionStore
from tests.test_session_store import _complete
from tests.test_live_voice_actions import phone


def stamp(hour, minute=0):
    return datetime(2026, 10, 28, hour, minute, tzinfo=timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_forget_leaves_untraceable_summaries_in_place(tmp_path, monkeypatch):
    home = tmp_path / "agent"
    home.mkdir()
    agent_file = home / "agent.md"
    agent_file.write_text(
        "[persona]\nPersona\n[persona_end]\n[sys]\nRules\n[sys_end]\n"
        "[memory]\nuntraceable long-term memory\n[memory_end]\n"
        "[hcc]\nuntraceable cache\n[hcc_end]\n",
        encoding="utf-8",
    )
    append_memory_plus_manual_note(home, "untraceable mixed summary")
    before = agent_file.read_bytes()
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    store.create_session(owner_id="user:7", agent_id="lily")
    store.forget_pcm_window(owner_id="user:7", agent_id="lily",
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))
    runtime = SimpleNamespace(name="lily", workspace_dir=home, session_store=store)
    monkeypatch.setattr(runtime_session, "_reset_cli_backend", AsyncMock(return_value="session"))
    monkeypatch.setattr(runtime_session, "_bind_session", AsyncMock())
    monkeypatch.setattr(runtime_session, "_discard_session_transients", lambda runtime: None)
    monkeypatch.setattr(runtime_session, "_prepare_clean_context", lambda *a, **k: None)
    await pcm_forget_coordinator.finish_pending(runtime, object(), owner_id="user:7")
    assert agent_file.read_bytes() == before
    assert "untraceable mixed summary" in read_memory_plus_notepad(home).content


def test_forget_uses_each_message_time_and_preserves_unknown_time(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: stamp(9))
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="cross", key="cross", text="older request", answer="in-window reply")
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="unknown", key="unknown", text="unknown request", answer="unknown reply")
    with store._lock, store._connection() as connection:
        connection.execute("UPDATE messages SET created_at=? WHERE text='older request'", (stamp(8, 55),))
        connection.execute("UPDATE messages SET created_at=? WHERE text='in-window reply'", (stamp(9, 5),))
        connection.execute("UPDATE messages SET created_at='' WHERE text LIKE 'unknown %'")
    store.forget_pcm_window(owner_id="user:7", agent_id="lily",
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))
    visible = store.messages(session["session_id"], owner_id="user:7")
    assert {item["text"] for item in visible if item["text"]} == {
        "older request", "unknown request", "unknown reply"
    }


def test_forget_leaves_unattributed_late_summary_searchable(tmp_path, monkeypatch):
    store = BridgeMemoryStore(tmp_path)
    monkeypatch.setattr(store, "_now", lambda: stamp(12))
    store.record_memory("episodic", "test", "untraceable later summary")
    store.forget_window(start=datetime.fromisoformat(stamp(9)),
                        end=datetime.fromisoformat(stamp(11)))
    assert [item["content"] for item in store.retrieve_memories("untraceable", limit=3)] == [
        "untraceable later summary"
    ]


def test_central_search_filters_proven_raw_record_but_keeps_summaries(tmp_path, monkeypatch):
    import sqlite3
    from orchestrator.central_memory import MemorySearchService

    home = tmp_path / "agent"
    home.mkdir()
    memory = BridgeMemoryStore(home)
    monkeypatch.setattr(memory, "_now", lambda: stamp(10))
    memory.record_turn("user", "test", "tracked raw idea")
    memory.forget_window(start=datetime.fromisoformat(stamp(9)),
                         end=datetime.fromisoformat(stamp(11)))
    database = tmp_path / "central.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE consolidated (
            id INTEGER PRIMARY KEY, instance TEXT, agent_id TEXT, content TEXT,
            source_ts TEXT, ts_source TEXT, memory_type TEXT, importance REAL, embedding BLOB)""")
        db.executemany("INSERT INTO consolidated VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (1, "HASHI3", "lily", "tracked raw idea", stamp(10),
             "remote:batch:transcript:transcript.jsonl", "episodic", 1, None),
            (2, "HASHI3", "lily", "untraceable mixed summary", stamp(10),
             "remote:batch:continuity:memory.jsonl", "summary", 1, None),
            (3, "HASHI3", "lily", "unrelated older note", stamp(8),
             "remote:batch:transcript:transcript.jsonl", "episodic", 1, None),
            (4, "HASHI3", "lily", "tracked raw idea", stamp(8),
             "remote:batch:transcript:transcript.jsonl", "episodic", 1, None),
        ])
    service = MemorySearchService(workspace_dir=home, agent_id="lily",
        global_config=SimpleNamespace(instance_id="HASHI3",
            central_memory={"database_path": str(database), "model_dir": str(tmp_path / "missing")}))
    results = service.search({"query": "idea", "source": "central", "limit": 20})["results"]
    assert {row["content"] for row in results} == {
        "untraceable mixed summary", "unrelated older note", "tracked raw idea"
    }
    assert not any(row["content"] == "tracked raw idea" and row["timestamp"] == stamp(10)
                   for row in results)


@pytest.mark.asyncio
async def test_cross_boundary_phone_call_remains_available(phone):
    from orchestrator.frontend_live_voice.protocol import Fragment

    await phone.manager.append_fragment_once(
        phone.binding, Fragment("cross-phone", "user", "mixed phone conversation", 0, 100)
    )
    with phone.store._lock, phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended', started_at=?, ended_at=? WHERE call_id=?",
                           (stamp(8, 50), stamp(9, 10), phone.call_id))
    result = phone.store.forget_pcm_window(
        owner_id=phone.owner_id, agent_id=phone.agent_id,
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))
    assert result["phone_calls"] == 0
    assert [item["text"] for item in phone.store.live_transcript_segments(
        phone.session_id, owner_id=phone.owner_id)] == ["mixed phone conversation"]
