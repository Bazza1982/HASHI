from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from orchestrator.bridge_memory import BridgeMemoryStore
from orchestrator.memory_plus_mode import append_memory_plus_manual_note, read_memory_plus_notepad
from orchestrator.pcm import load_pcm_document
from orchestrator.pcm_forget_artifacts import validate_untracked_continuity_scope
from orchestrator.session_store import SessionConflict, SessionStore


def test_unproven_continuity_is_preserved_without_modifying_audit(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    (home / "agent.md").write_text(
        "[persona]\nKind persona\n[persona_end]\n[sys]\nPermanent rules\n[sys_end]\n"
        "[memory]\nold bad memory\n[memory_end]\n[hcc]\nold bad cache\n[hcc_end]\n",
        encoding="utf-8",
    )
    append_memory_plus_manual_note(home, "old bad notepad")
    from orchestrator.context_compaction import CompactionStore
    compaction = CompactionStore(home)
    compaction.compare_and_swap(
        expected_generation=0, expected_capsule_hash="",
        active_capsule={"ref": "old bad capsule", "sha256": "bad"},
    )
    memory_dir = home / "memory"
    (memory_dir / "memory_plus_wiki").mkdir(exist_ok=True)
    (memory_dir / "memory_plus_wiki" / "2026-10-28.md").write_text("old bad archive", encoding="utf-8")
    (memory_dir / "left_brain_continuity.jsonl").write_text('{"summary":"old bad dual brain"}', encoding="utf-8")
    (memory_dir / "left_brain_artifacts").mkdir()
    (memory_dir / "left_brain_artifacts" / "last.json").write_text('{"summary":"old bad artifact"}', encoding="utf-8")

    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    start = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
    end = datetime(2026, 10, 28, 11, tzinfo=timezone.utc)
    result = store.forget_pcm_window(owner_id="user:7", agent_id="lily", start=start, end=end)

    count = validate_untracked_continuity_scope(
        store, operation_id=result["operation_id"], owner_id="user:7", agent_id="lily",
        agent_home=home, session_ids=[session["session_id"]],
    )

    document = load_pcm_document(home / "agent.md", workspace_dir=home)
    assert document.persona == "Kind persona" and document.system == "Permanent rules"
    assert document.memory == "old bad memory" and document.hcc == "old bad cache"
    assert "old bad notepad" in read_memory_plus_notepad(home).content
    assert (memory_dir / "memory_plus_wiki" / "2026-10-28.md").exists()
    assert (memory_dir / "left_brain_continuity.jsonl").exists()
    assert (memory_dir / "left_brain_artifacts" / "last.json").exists()
    assert compaction.read_state()["active_capsule"] is not None
    with store._connection() as connection:
        audit = connection.execute(
            "SELECT payload_json FROM pcm_forget_audit WHERE source_kind='artifact'"
        ).fetchall()
    assert count == 0 and audit == []
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is not None
    # Repeating after interruption leaves those artifacts untouched.
    validate_untracked_continuity_scope(
        store, operation_id=result["operation_id"], owner_id="user:7", agent_id="lily",
        agent_home=home, session_ids=[session["session_id"]],
    )
    append_memory_plus_manual_note(home, "new safe note")
    assert "new safe note" in read_memory_plus_notepad(home).content


def test_all_session_generations_and_agent_memory_are_fenced(tmp_path, monkeypatch):
    from orchestrator.pcm_forget_artifacts import forget_local_memories

    home = tmp_path / "agent"
    home.mkdir()
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    first = store.create_session(owner_id="user:7", agent_id="lily")
    other = store.create_session(owner_id="user:7", agent_id="another")
    from tests.test_session_store import _complete
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: "2026-10-28T09:30:00+00:00")
    run = _complete(store, session_id=first["session_id"], owner_id="user:7", agent_id="lily",
                    request_id="tracked", key="tracked", text="bad request", answer="bad answer")
    reference = f"session:{first['session_id']}:run:{run.run_id}"
    old_generation = store.session_workspace(first["session_id"], 1)
    unrelated = store.session_workspace(other["session_id"], 1)
    targets = [BridgeMemoryStore(home), BridgeMemoryStore(old_generation)]
    other_memory = BridgeMemoryStore(unrelated)
    for memory in targets + [other_memory]:
        monkeypatch.setattr(memory, "_now", lambda: "2026-10-28T09:30:00+00:00")
        memory.record_memory("episodic", "test", "bad remembered idea", origin_ref=reference)
    result = store.forget_pcm_window(
        owner_id="user:7", agent_id="lily",
        start=datetime(2026, 10, 28, 9, tzinfo=timezone.utc),
        end=datetime(2026, 10, 28, 11, tzinfo=timezone.utc),
    )
    report = forget_local_memories(
        store, operation_id=result["operation_id"], owner_id="user:7",
        agent_id="lily", agent_home=home,
    )
    assert report["stores"] == 2
    assert all(memory.retrieve_memories("bad", limit=3) == [] for memory in targets)
    assert len(other_memory.retrieve_memories("bad", limit=3)) == 1
    assert "local_memory" in store.pending_pcm_forget(owner_id="user:7", agent_id="lily")["steps"]


def test_untracked_artifact_is_not_overwritten_during_retry(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    (home / "agent.md").write_text(
        "[persona]\nPersona\n[persona_end]\n[sys]\nRules\n[sys_end]\n"
        "[memory]\nchanged since snapshot\n[memory_end]\n",
        encoding="utf-8",
    )
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    result = store.forget_pcm_window(
        owner_id="user:7", agent_id="lily",
        start=datetime(2026, 10, 28, 9, tzinfo=timezone.utc),
        end=datetime(2026, 10, 28, 11, tzinfo=timezone.utc),
    )
    store.audit_pcm_forget_artifact(
        owner_id="user:7", agent_id="lily", operation_id=result["operation_id"],
        identity="agent:agent.md#memory", content="original snapshot\n",
    )
    count = validate_untracked_continuity_scope(
        store, operation_id=result["operation_id"], owner_id="user:7",
        agent_id="lily", agent_home=home, session_ids=[],
    )
    assert count == 0
    assert "changed since snapshot" in (home / "agent.md").read_text(encoding="utf-8")
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is not None
