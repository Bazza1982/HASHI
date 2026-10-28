from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sqlite3

import pytest

from orchestrator.bridge_memory import BridgeMemoryStore
from orchestrator.central_memory import MemorySearchService


def test_central_search_keeps_provably_older_records_after_forget(tmp_path):
    database = tmp_path / "central.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE consolidated (
            id INTEGER PRIMARY KEY, instance TEXT, agent_id TEXT, content TEXT,
            source_ts TEXT, consolidated_at TEXT, source TEXT,
            memory_type TEXT, importance REAL, embedding BLOB)""")
        db.executemany("INSERT INTO consolidated VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (1, "HASHI3", "lily", "old safe", "2026-10-28T08:00:00+00:00",
             "2026-10-28T08:15:00+00:00", "test", "episodic", 1, None),
            (2, "HASHI3", "lily", "forgotten unsafe", "2026-10-28T10:00:00+00:00",
             "2026-10-28T10:15:00+00:00", "test", "episodic", 1, None),
            (3, "HASHI3", "lily", "old timestamp but later summary",
             "2026-10-28T08:00:00+00:00", "2026-10-28T12:00:00+00:00",
             "test", "summary", 1, None),
            (4, "HASHI3", "lily", "unknown provenance", "2026-10-28T08:00:00+00:00",
             None, "test", "episodic", 1, None),
        ])
    home = tmp_path / "agent"
    home.mkdir()
    BridgeMemoryStore(home).forget_window(
        start=datetime(2026, 10, 28, 9, tzinfo=timezone.utc),
        end=datetime(2026, 10, 28, 11, tzinfo=timezone.utc))
    service = MemorySearchService(workspace_dir=home, agent_id="lily",
        global_config=SimpleNamespace(instance_id="HASHI3",
            central_memory={"database_path": str(database), "model_dir": str(tmp_path / "missing")}))
    result = service.search({"query": "safe", "source": "central", "limit": 20})
    assert [row["content"] for row in result["results"]] == ["old safe"]
    assert result["central_status"] == "filtered_after_forget"


def test_central_search_allows_new_records_with_proven_local_source(tmp_path):
    database = tmp_path / "central.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE consolidated (
            id INTEGER PRIMARY KEY, instance TEXT, agent_id TEXT, content TEXT,
            source_ts TEXT, consolidated_at TEXT, source TEXT,
            memory_type TEXT, importance REAL, embedding BLOB)""")
        db.executemany("INSERT INTO consolidated VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (1, "HASHI3", "lily", "fresh request", "2026-10-28T12:00:00+00:00",
             "2026-10-28T13:00:00+00:00", "test", "episodic", 1, None),
            (2, "HASHI3", "lily", "unverified wrong summary", "2026-10-28T12:00:00+00:00",
             "2026-10-28T13:00:00+00:00", "test", "summary", 1, None),
        ])
    home = tmp_path / "agent"
    home.mkdir()
    memory = BridgeMemoryStore(home)
    memory.forget_window(start=datetime(2026, 10, 28, 9, tzinfo=timezone.utc),
                         end=datetime(2026, 10, 28, 11, tzinfo=timezone.utc))
    memory.record_completed_exchange("fresh request", "fresh response", "test",
        user_ts="2026-10-28T12:00:00+00:00", assistant_ts="2026-10-28T12:01:00+00:00",
        origin_ref="new-session:new-run")
    service = MemorySearchService(workspace_dir=home, agent_id="lily",
        global_config=SimpleNamespace(instance_id="HASHI3",
            central_memory={"database_path": str(database), "model_dir": str(tmp_path / "missing")}))
    result = service.search({"query": "fresh", "source": "central", "limit": 20})
    assert [row["content"] for row in result["results"]] == ["fresh request"]


def test_unchanged_older_agent_memory_is_preserved(tmp_path):
    from orchestrator.pcm_forget_artifacts import quarantine_unproven_continuity
    from orchestrator.session_store import SessionStore

    home = tmp_path / "agent"
    home.mkdir()
    agent_file = home / "agent.md"
    agent_file.write_text("[persona]\nPersona\n[persona_end]\n[sys]\nRules\n[sys_end]\n"
                          "[memory]\nold useful memory\n[memory_end]\n",
                          encoding="utf-8")
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    store.create_session(owner_id="user:7", agent_id="lily")
    lower = datetime.now(timezone.utc) + timedelta(seconds=5)
    result = store.forget_pcm_window(owner_id="user:7", agent_id="lily",
                                     start=lower, end=lower + timedelta(hours=1))
    quarantine_unproven_continuity(store, operation_id=result["operation_id"],
        owner_id="user:7", agent_id="lily", agent_home=home,
        session_ids=result["all_session_ids"])
    assert "old useful memory" in agent_file.read_text(encoding="utf-8")


def test_older_verified_compaction_is_preserved(tmp_path):
    from orchestrator.context_compaction import (
        ARCHIVE_FORMAT, CAPSULE_FORMAT, CAPSULE_RECORD_FORMAT, CompactionStore,
    )
    from orchestrator.pcm_forget_artifacts import quarantine_unproven_continuity
    from orchestrator.session_store import SessionStore

    home = tmp_path / "agent"
    home.mkdir()
    compaction = CompactionStore(home)
    archive_ref, archive_hash = compaction.write_immutable(
        compaction.archive_dir, "old.json", {"format": ARCHIVE_FORMAT})
    capsule_ref, capsule_hash = compaction.write_immutable(
        compaction.capsule_dir, "old.json",
        {"format": CAPSULE_RECORD_FORMAT, "capsule": {"format": CAPSULE_FORMAT}})
    compaction.compare_and_swap(expected_generation=0, expected_capsule_hash="",
        active_capsule={"ref": capsule_ref, "sha256": capsule_hash,
                        "archive_ref": archive_ref, "archive_sha256": archive_hash})
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    lower = datetime.now(timezone.utc) + timedelta(seconds=5)
    result = store.forget_pcm_window(owner_id="user:7", agent_id="lily",
                                     start=lower, end=lower + timedelta(hours=1))
    quarantine_unproven_continuity(store, operation_id=result["operation_id"],
        owner_id="user:7", agent_id="lily", agent_home=home,
        session_ids=[session["session_id"]])
    assert compaction.read_state()["active_capsule"] is not None
    assert compaction.load_active()[0]["capsule"]["format"] == CAPSULE_FORMAT


def test_handoff_preserves_provably_old_notes_without_recent_notes(tmp_path):
    import time
    from orchestrator.handoff_builder import HandoffBuilder

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "project.md").write_text("old project", encoding="utf-8")
    cutoff = datetime.now(timezone.utc) + timedelta(milliseconds=50)
    time.sleep(0.1)
    (memory_dir / "tasks.md").write_text("recent wrong task", encoding="utf-8")
    BridgeMemoryStore(tmp_path).forget_window(start=cutoff, end=cutoff + timedelta(hours=1))
    builder = HandoffBuilder(tmp_path)
    package = builder.build_transfer_package(transfer_id="transfer", source_agent="a",
        source_instance="i", target_agent="a", target_instance="j", created_at=cutoff.isoformat())
    assert package["memory_files"] == {"project.md": "old project"}
    assert "recent wrong task" not in str(package)
    builder.build_handoff()
    assert "old project" in (tmp_path / "handoff.md").read_text(encoding="utf-8")
    assert "recent wrong task" not in (tmp_path / "handoff.md").read_text(encoding="utf-8")


def test_backdated_modified_file_is_not_mistaken_for_old(tmp_path):
    import os
    import time
    from orchestrator.pcm_forget_artifacts import _unchanged_before

    path = tmp_path / "note.md"
    path.write_text("older", encoding="utf-8")
    cutoff = datetime.now(timezone.utc).timestamp() + 0.05
    time.sleep(0.1)
    path.write_text("recent wrong idea", encoding="utf-8")
    os.utime(path, (cutoff - 1000, cutoff - 1000))
    assert not _unchanged_before(path, cutoff)


@pytest.mark.asyncio
async def test_wiki_command_is_available_through_filtered_capability(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from orchestrator.commands.wiki import wiki_command

    home = tmp_path / "agent"
    home.mkdir()
    BridgeMemoryStore(home).forget_window(
        start=datetime(2026, 10, 28, 9, tzinfo=timezone.utc),
        end=datetime(2026, 10, 28, 11, tzinfo=timezone.utc))
    runtime = SimpleNamespace(workspace_dir=home, _is_authorized_user=lambda actor: actor == 7,
        _reply_text=AsyncMock(), enqueue_request=AsyncMock(return_value="request"),
        _is_hashi_tool_connected=lambda capability: True,
        global_config=SimpleNamespace(wiki_provider={"id": "test", "capability": "wiki_search"}))
    route = SimpleNamespace(chat_id=1, metadata={}, idempotency_key="wiki-key")
    monkeypatch.setattr("orchestrator.command_request_context.command_request_context",
                        lambda *args, **kwargs: route)
    context = SimpleNamespace(args=["safe", "topic"])
    await wiki_command(runtime, SimpleNamespace(effective_user=SimpleNamespace(id=7)), context)
    assert context.derived_request_id == "request"
    runtime.enqueue_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_wiki_search_keeps_unchanged_older_pages_and_blocks_recent_pages(tmp_path):
    import json
    import time
    from tools.registry import ToolRegistry

    home = tmp_path / "agent"
    home.mkdir()
    vault = tmp_path / "vault"
    zone = vault / "topics"
    zone.mkdir(parents=True)
    (zone / "old.md").write_text("topic old safe reference", encoding="utf-8")
    cutoff = datetime.now(timezone.utc) + timedelta(milliseconds=50)
    time.sleep(0.1)
    (zone / "recent.md").write_text("topic recent wrong idea", encoding="utf-8")
    BridgeMemoryStore(home).forget_window(start=cutoff, end=cutoff + timedelta(hours=1))
    registry = ToolRegistry(
        allowed_tools=["wiki_search"], access_root=tmp_path, workspace_dir=home, secrets={},
        audit_context={"global_config": SimpleNamespace(wiki_provider={
            "id": "test", "capability": "wiki_search", "root": str(vault), "zones": ["topics"]})},
    )
    search = await registry.execute("wiki_search", {"operation": "search", "query": "topic"})
    assert "old safe reference" in search.output
    assert "recent wrong idea" not in search.output
    old = await registry.execute("wiki_search", {"operation": "read", "path": "topics/old.md"})
    recent = await registry.execute("wiki_search", {"operation": "read", "path": "topics/recent.md"})
    assert "old safe reference" in old.output
    assert "recent wrong idea" not in recent.output


def test_export_retains_old_and_new_rounds_without_republishing_forgotten_log(tmp_path):
    import json
    from orchestrator.pcm_forget_memory import checkpoint_forget_exports
    from scripts.remote_memory_consolidation import iter_source_records

    home = tmp_path / "workspaces" / "lily"
    home.mkdir(parents=True)
    path = home / "transcript.jsonl"

    def pair(label, user_time, reply_time):
        return "".join(json.dumps({"role": role, "ts": when, "text": label + " " + role}) + "\n"
                       for role, when in (("user", user_time), ("assistant", reply_time)))

    old = "2026-10-28T08:00:00+00:00"
    near = "2026-10-28T08:55:00+00:00"
    bad = "2026-10-28T10:00:00+00:00"
    new = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    path.write_text(pair("old", old, old) + pair("cross", near, bad) + pair("bad", bad, bad),
                    encoding="utf-8")
    BridgeMemoryStore(home).forget_window(
        start=datetime(2026, 10, 28, 9, tzinfo=timezone.utc),
        end=datetime(2026, 10, 28, 11, tzinfo=timezone.utc))
    # Before the cleanup is complete there is no export checkpoint.
    assert list(iter_source_records(tmp_path, "HASHI3", ["lily"])) == []
    checkpoint_forget_exports(home)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(pair("new", new, new))
    records = list(iter_source_records(tmp_path, "HASHI3", ["lily"]))
    assert {row["text"] for row in records} == {
        "old user", "old assistant", "new user", "new assistant"}
    assert "bad user" in path.read_text(encoding="utf-8")

    first_new = datetime.fromisoformat(new)
    second_bad = first_new + timedelta(seconds=2)
    latest = first_new + timedelta(seconds=5)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(pair("bad-second", second_bad.isoformat(), second_bad.isoformat()))
    BridgeMemoryStore(home).forget_window(
        start=first_new + timedelta(seconds=1), end=first_new + timedelta(seconds=3))
    checkpoint_forget_exports(home)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(pair("newest", latest.isoformat(), latest.isoformat()))
    again = list(iter_source_records(tmp_path, "HASHI3", ["lily"]))
    assert {row["text"] for row in again} == {
        "old user", "old assistant", "new user", "new assistant",
        "newest user", "newest assistant"}

    # Replacing the retained prefix invalidates the checkpoint.
    path.write_text(pair("bad", bad, bad), encoding="utf-8")
    assert list(iter_source_records(tmp_path, "HASHI3", ["lily"])) == []
