from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from orchestrator.bridge_memory import BridgeMemoryStore
from orchestrator.memory_plus_mode import append_memory_plus_manual_note, read_memory_plus_notepad
from orchestrator.pcm import load_pcm_document
from orchestrator.pcm_forget_artifacts import quarantine_unproven_continuity
from orchestrator.session_store import SessionConflict, SessionStore


def test_unproven_continuity_is_quarantined_with_audit_and_can_start_clean(tmp_path):
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

    count = quarantine_unproven_continuity(
        store, operation_id=result["operation_id"], owner_id="user:7", agent_id="lily",
        agent_home=home, session_ids=[session["session_id"]],
    )

    document = load_pcm_document(home / "agent.md", workspace_dir=home)
    assert document.persona == "Kind persona" and document.system == "Permanent rules"
    assert not document.memory and not document.hcc
    assert "old bad" not in read_memory_plus_notepad(home).content
    assert not (memory_dir / "memory_plus_wiki" / "2026-10-28.md").exists()
    assert not (memory_dir / "left_brain_continuity.jsonl").exists()
    assert not (memory_dir / "left_brain_artifacts" / "last.json").exists()
    assert compaction.read_state()["active_capsule"] is None
    with store._connection() as connection:
        audit = connection.execute(
            "SELECT payload_json FROM pcm_forget_audit WHERE source_kind='artifact'"
        ).fetchall()
    assert count >= 4
    assert any("old bad memory" in row[0] for row in audit)
    assert any("old bad notepad" in row[0] for row in audit)
    assert any("old bad archive" in row[0] for row in audit)
    assert any("old bad dual brain" in row[0] for row in audit)
    assert any("old bad capsule" in row[0] for row in audit)
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is not None
    # Repeating after interruption must never resurrect removed artifacts.
    quarantine_unproven_continuity(
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
    old_generation = store.session_workspace(first["session_id"], 1)
    unrelated = store.session_workspace(other["session_id"], 1)
    targets = [BridgeMemoryStore(home), BridgeMemoryStore(old_generation)]
    other_memory = BridgeMemoryStore(unrelated)
    for memory in targets + [other_memory]:
        monkeypatch.setattr(memory, "_now", lambda: "2026-10-28T09:30:00+00:00")
        memory.record_memory("episodic", "test", "bad remembered idea")
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


def test_changed_artifact_is_not_overwritten_during_retry(tmp_path):
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
    with pytest.raises(SessionConflict, match="changed"):
        quarantine_unproven_continuity(
            store, operation_id=result["operation_id"], owner_id="user:7",
            agent_id="lily", agent_home=home, session_ids=[],
        )
    assert "changed since snapshot" in (home / "agent.md").read_text(encoding="utf-8")
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is not None
