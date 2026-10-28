from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from orchestrator.bridge_memory import BridgeMemoryStore


def stamp(hour: int, minute: int = 0) -> str:
    return datetime(2026, 10, 28, hour, minute, tzinfo=timezone.utc).isoformat()


def test_forget_removes_selected_memory_but_keeps_audit_and_survives_reconciliation(tmp_path, monkeypatch):
    store = BridgeMemoryStore(tmp_path)
    transcript = tmp_path / "transcript.jsonl"
    transcript_entries = []

    def add_exchange(label, user_ts, assistant_ts, *, origin_ref):
        monkeypatch.setattr(store, "_now", lambda: user_ts)
        user_id = store.record_turn("user", "test", f"{label} request")
        monkeypatch.setattr(store, "_now", lambda: assistant_ts)
        assistant_id = store.record_turn("assistant", "test", f"{label} reply")
        store.record_completed_exchange(
            f"{label} request", f"{label} reply", "test",
            user_turn_id=user_id, assistant_turn_id=assistant_id,
            user_ts=user_ts, assistant_ts=assistant_ts,
            origin="session", origin_ref=origin_ref,
        )
        for role, ts, text in (
            ("user", user_ts, f"{label} request"),
            ("assistant", assistant_ts, f"{label} reply"),
        ):
            transcript_entries.append({"role": role, "source": "test", "ts": ts, "text": text})

    add_exchange("old", stamp(8), stamp(8, 10), origin_ref="old-run")
    # A pair that straddles the beginning must be removed as a complete exchange.
    add_exchange("forgotten", stamp(8, 55), stamp(9, 5), origin_ref="forgotten-run")
    add_exchange("after", stamp(11, 5), stamp(11, 10), origin_ref="after-run")
    transcript.write_text(
        "".join(json.dumps(entry) + "\n" for entry in transcript_entries),
        encoding="utf-8",
    )

    monkeypatch.setattr(store, "_now", lambda: stamp(8))
    store.record_memory("episodic", "test", "old memory", origin_ref="old-run")
    # A late promotion still depends on the forgotten exchange.
    monkeypatch.setattr(store, "_now", lambda: stamp(12))
    store.record_memory("episodic", "test", "forgotten memory", origin_ref="forgotten-run")
    monkeypatch.setattr(store, "_now", lambda: stamp(11, 30))
    store.record_memory("episodic", "test", "later memory", origin_ref="after-run")

    report = store.forget_window(
        start=datetime.fromisoformat(stamp(9)),
        end=datetime.fromisoformat(stamp(11)),
    )

    assert report["exchanges"] == 1
    assert report["memories"] == 1
    assert {item["user_text"] for item in store.get_completed_exchanges(limit=10)} == {
        "old request", "after request",
    }
    assert {item["content"] for item in store.retrieve_memories("memory", limit=10)} == {
        "old memory", "later memory",
    }
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE text LIKE 'forgotten %'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE content='forgotten memory'"
        ).fetchone()[0] == 0
        retained = connection.execute(
            "SELECT payload_json FROM pcm_forget_quarantine WHERE item_type='memory'"
        ).fetchall()
        assert any("forgotten memory" in row[0] for row in retained)
    assert json.loads(transcript.read_text(encoding="utf-8").splitlines()[2])["text"] == "forgotten request"

    reopened = BridgeMemoryStore(tmp_path)
    texts = {item["user_text"] for item in reopened.get_completed_exchanges(limit=10)}
    assert texts == {"old request", "after request"}
    assert not any("forgotten" in item["content"] for item in reopened.retrieve_memories("forgotten", limit=10))


def test_forget_rejects_invalid_or_naive_ranges_without_changing_memory(tmp_path):
    store = BridgeMemoryStore(tmp_path)
    store.record_memory("episodic", "test", "keep me")
    with pytest.raises(ValueError):
        store.forget_window(start=datetime(2026, 1, 1), end=datetime(2026, 1, 2))
    with pytest.raises(ValueError):
        store.forget_window(start=datetime.fromisoformat(stamp(11)), end=datetime.fromisoformat(stamp(9)))
    assert [item["content"] for item in store.retrieve_memories("keep", limit=3)] == ["keep me"]


def test_forget_prevents_late_reimport_of_the_same_exchange_and_promoted_memory(tmp_path, monkeypatch):
    store = BridgeMemoryStore(tmp_path)
    store.record_completed_exchange(
        "old bad request", "old bad answer", "test",
        user_ts=stamp(9, 10), assistant_ts=stamp(9, 20),
        origin="session", origin_ref="session:a:run:bad",
    )
    monkeypatch.setattr(store, "_now", lambda: stamp(9, 30))
    store.record_memory(
        "episodic", "session:a", "old bad promoted",
        origin="session_promotion", origin_ref="session:a:run:bad",
        session_id="a", run_id="bad",
    )
    store.forget_window(start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))
    monkeypatch.setattr(store, "_now", lambda: stamp(12))

    assert store.record_completed_exchange(
        "old bad request", "old bad answer", "test",
        user_ts=stamp(9, 10), assistant_ts=stamp(9, 20),
        origin="session", origin_ref="session:a:run:bad",
    ) is None
    assert store.record_memory(
        "episodic", "session:a", "old bad promoted",
        origin="session_promotion", origin_ref="session:a:run:bad",
        session_id="a", run_id="bad",
    ) is None
    assert store.get_completed_exchanges(limit=10) == []
    assert store.retrieve_memories("bad", limit=10) == []


def test_handoff_never_reloads_forgotten_audit_transcript(tmp_path):
    from orchestrator.handoff_builder import HandoffBuilder

    transcript = tmp_path / "transcript.jsonl"
    entries = []
    for label, hour in (("old", 8), ("forgotten", 9), ("later", 12)):
        entries.extend([
            {"role": "user", "text": label + " request", "source": "test", "ts": stamp(hour)},
            {"role": "assistant", "text": label + " answer", "source": "test", "ts": stamp(hour)},
        ])
    transcript.write_text("".join(json.dumps(row) + "\n" for row in entries), encoding="utf-8")
    store = BridgeMemoryStore(tmp_path)
    store.forget_window(start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))

    builder = HandoffBuilder(tmp_path)
    block, _, _ = builder.build_recent_context_block()
    assert "old request" in block and "later request" in block
    assert "forgotten" not in block
    assert "forgotten request" in transcript.read_text(encoding="utf-8")
    # Pre-existing handoff files and untimestamped working notes are a
    # separate route back into later Agent transfers.
    (tmp_path / "handoff.md").write_text("forgotten summary", encoding="utf-8")
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "tasks.md").write_text("forgotten task", encoding="utf-8")
    (tmp_path / "recent_context.jsonl").write_text(
        json.dumps({"role": "user", "text": "forgotten cached message"}) + "\n", encoding="utf-8"
    )
    package = builder.build_transfer_package(
        transfer_id="t", source_agent="a", source_instance="i",
        target_agent="a", target_instance="j", created_at=stamp(12),
    )
    assert "forgotten" not in json.dumps(package)
    builder.build_handoff()
    assert "forgotten" not in (tmp_path / "handoff.md").read_text(encoding="utf-8")


def test_memory_export_does_not_republish_retained_audit_log(tmp_path):
    from scripts.remote_memory_consolidation import iter_source_records

    first = tmp_path / "workspaces" / "lily"
    other = tmp_path / "workspaces" / "other"
    first.mkdir(parents=True)
    other.mkdir(parents=True)
    record = json.dumps({"role": "user", "ts": stamp(9, 5), "text": "old wrong idea"}) + "\n"
    (first / "transcript.jsonl").write_text(record, encoding="utf-8")
    (other / "transcript.jsonl").write_text(record, encoding="utf-8")
    BridgeMemoryStore(first).forget_window(
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    records = list(iter_source_records(tmp_path, "HASHI3", ["lily", "other"]))
    assert {row["source_agent"] for row in records} == {"other"}
    assert "old wrong idea" in (first / "transcript.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_unverifiable_wiki_is_quarantined_for_the_forgotten_agent(tmp_path):
    from types import SimpleNamespace
    from tools.registry import ToolRegistry

    home = tmp_path / "agent"
    home.mkdir()
    vault = tmp_path / "vault"
    topic = vault / "topics"
    topic.mkdir(parents=True)
    (topic / "idea.md").write_text("old wrong idea", encoding="utf-8")
    registry = ToolRegistry(
        allowed_tools=["wiki_search"], access_root=tmp_path, workspace_dir=home, secrets={},
        audit_context={"global_config": SimpleNamespace(
            wiki_provider={"id": "test", "capability": "wiki_search", "root": str(vault), "zones": ["topics"]}
        )},
    )
    before = await registry.execute("wiki_search", {"operation": "search", "query": "wrong idea"})
    assert "old wrong idea" in before.output
    BridgeMemoryStore(home).forget_window(
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    after = await registry.execute("wiki_search", {"operation": "search", "query": "wrong idea"})
    assert "old wrong idea" not in after.output
    assert json.loads(after.output)["results"] == []


@pytest.mark.asyncio
async def test_wiki_command_does_not_bypass_quarantine_with_other_capability(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from orchestrator.commands.wiki import wiki_command

    home = tmp_path / "agent"
    home.mkdir()
    BridgeMemoryStore(home).forget_window(
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    reply = AsyncMock()
    enqueue = AsyncMock()
    runtime = SimpleNamespace(
        workspace_dir=home, _is_authorized_user=lambda actor: actor == 7,
        _reply_text=reply, enqueue_request=enqueue,
        global_config=SimpleNamespace(wiki_provider={"id": "test", "capability": "web_search"}),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
    await wiki_command(runtime, update, SimpleNamespace(args=["old", "idea"]))
    assert "quarantined" in reply.await_args.args[1]
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_dual_brain_preflight_cannot_fetch_unverified_wiki_after_forget(tmp_path):
    from types import SimpleNamespace
    from orchestrator.dual_brain_mode import DualBrainObserver
    from orchestrator.post_turn_observer import TurnContextRequest

    home = tmp_path / "agent"
    home.mkdir()
    (home / "state.json").write_text(json.dumps({
        "agent_mode": "dual-brain",
        "dual_brain": {
            "left_brain": {"backend": "codex-cli", "model": "test"},
            "right_brain": {"backend": "codex-cli", "model": "test"},
        },
    }), encoding="utf-8")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "bad.md").write_text("old wrong idea", encoding="utf-8")
    BridgeMemoryStore(home).forget_window(
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    prompts = []

    async def invoke(**kwargs):
        prompts.append(kwargs["prompt"])
        return SimpleNamespace(is_success=True, error="", text=json.dumps({
            "useful": False, "wiki_needed": True, "wiki_query": "old wrong idea",
            "same_day_context": [], "open_items": [], "notes_for_executor": [],
            "sources": [], "confidence": 1.0,
        }))

    observer = DualBrainObserver(
        workspace_dir=home, backend_invoker=invoke,
        backend_context_getter=lambda: {"engine": "codex-cli", "model": "test"},
        options={"wiki_roots": [str(wiki)]},
    )
    await observer.build_context_sections(TurnContextRequest(
        request_id="new", source="telegram", user_text="hello", model_name="test",
    ))
    assert len(prompts) == 1
    assert "old wrong idea" not in prompts[0]


def test_central_memory_without_source_lineage_is_quarantined_after_forget(tmp_path):
    from types import SimpleNamespace
    from orchestrator.central_memory import MemorySearchService
    from tests.test_pcm_audit_memory_wiki import _central_db

    database = tmp_path / "central.sqlite"
    _central_db(database)
    workspace = tmp_path / "agent"
    workspace.mkdir()
    config = SimpleNamespace(
        instance_id="HASHI2",
        central_memory={"database_path": str(database), "model_dir": str(tmp_path / "missing")},
    )
    service = MemorySearchService(workspace_dir=workspace, global_config=config, agent_id="rika")
    assert service.search({"query": "decision", "source": "central"})["results"]
    store = BridgeMemoryStore(workspace)
    store.forget_window(start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))

    result = service.search({"query": "decision", "source": "central"})
    assert result["results"] == []
    assert result["central_status"] == "filtered_after_forget"


def test_emotional_memory_and_relationship_cache_are_not_left_in_pcm(tmp_path):
    from orchestrator.anatta.bridge_adapter import BridgeMemoryAdapter
    from orchestrator.anatta.config import AnattaConfig
    from orchestrator.anatta.memory import AnattaMemoryStore

    memory = BridgeMemoryStore(tmp_path)
    AnattaMemoryStore(BridgeMemoryAdapter(memory), AnattaConfig(tmp_path))
    with memory._connect() as connection:
        for label, when in (("old", stamp(8)), ("forgotten", stamp(10))):
            connection.execute(
                """INSERT INTO emotional_annotations(
                    bridge_row_type, bridge_row_id, event_ts, created_at, source, actor_role,
                    event_type, summary, intensity, dominant_drives_json, drive_contributions_json,
                    tags_json, metadata_json
                ) VALUES ('turn', 1, ?, ?, 'test', 'user', 'event', ?, 1, '[]', '[]', '[]', '{}')""",
                (when, when, label + " emotional summary"),
            )
            connection.execute(
                """INSERT INTO relationship_events(
                    relationship_key, event_ts, event_type, summary, intensity,
                    drive_contributions_json, metadata_json
                ) VALUES ('user', ?, 'event', ?, 1, '[]', '{}')""",
                (when, label + " relationship idea"),
            )
        connection.execute(
            """INSERT INTO derived_relationship_cache(relationship_key, derived_summary_json, computed_at, expires_at)
               VALUES ('user', '{"summary":"forgotten cache"}', ?, ?)""", (stamp(10), stamp(12)),
        )
    memory.forget_window(start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))
    with memory._connect() as connection:
        emotions = [row[0] for row in connection.execute("SELECT summary FROM emotional_annotations")]
        relationships = [row[0] for row in connection.execute("SELECT summary FROM relationship_events")]
        cache = connection.execute("SELECT * FROM derived_relationship_cache").fetchall()
        audit = [row[0] for row in connection.execute(
            "SELECT payload_json FROM pcm_forget_quarantine WHERE item_type LIKE 'anatta_%'"
        )]
    assert emotions == ["old emotional summary"]
    assert relationships == ["old relationship idea"]
    assert cache == []
    assert any("forgotten emotional summary" in row for row in audit)


def test_selected_derived_memory_is_retained_only_in_private_audit(tmp_path, monkeypatch):
    store = BridgeMemoryStore(tmp_path)
    monkeypatch.setattr(store, "_now", lambda: stamp(10))
    store.record_memory("episodic", "test", "targeted memory")
    store.forget_window(start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))
    assert store.retrieve_memories("targeted memory", limit=3) == []
    with store._connect() as connection:
        audit = connection.execute(
            "SELECT payload_json FROM pcm_forget_quarantine WHERE item_type='memory'"
        ).fetchall()
    assert any("targeted memory" in row[0] for row in audit)


def test_unattributed_late_memory_is_quarantined_not_silently_reused(tmp_path, monkeypatch):
    store = BridgeMemoryStore(tmp_path)
    monkeypatch.setattr(store, "_now", lambda: stamp(12))
    store.record_memory("episodic", "test", "unproven late summary")
    report = store.forget_window(
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    assert report["quarantined"] == 1
    assert store.retrieve_memories("unproven late summary", limit=3) == []
    with store._connect() as connection:
        audit = connection.execute(
            "SELECT payload_json FROM pcm_forget_quarantine WHERE item_type='memory'"
        ).fetchall()
    assert any("unproven late summary" in row[0] for row in audit)


def test_new_session_inherits_the_agent_forget_boundary(tmp_path, monkeypatch):
    from orchestrator.pcm_forget_memory import inherit_forget_fences

    root = tmp_path / "agent"
    session = tmp_path / "new-session"
    root.mkdir()
    session.mkdir()
    origin = "session:old:run:forgotten"
    agent_memory = BridgeMemoryStore(root)
    agent_memory.forget_window(
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
        origin_refs=(origin,),
    )
    (session / "transcript.jsonl").write_text(
        json.dumps({"role": "user", "source": "test", "ts": stamp(9, 5), "text": "bad request"}) + "\n"
        + json.dumps({"role": "assistant", "source": "test", "ts": stamp(9, 6), "text": "bad reply"}) + "\n",
        encoding="utf-8",
    )
    new_memory = BridgeMemoryStore(session)
    inherit_forget_fences(root, new_memory)
    assert new_memory.get_completed_exchanges(limit=10) == []
    monkeypatch.setattr(new_memory, "_now", lambda: stamp(12))
    assert new_memory.record_memory(
        "episodic", "test", "bad promoted idea", origin_ref=origin
    ) is None
    # Repeating the synchronization is safe and keeps newer memories.
    assert new_memory.record_memory("episodic", "test", "new safe note") is not None
    inherit_forget_fences(root, new_memory)
    assert len(new_memory.retrieve_memories("new safe note", limit=3)) == 1
    # A later unrelated forget must not reapply the old cutoff to new notes.
    agent_memory.forget_window(
        start=datetime.fromisoformat(stamp(15)), end=datetime.fromisoformat(stamp(16)),
        origin_refs=("another-forgotten-run",),
    )
    inherit_forget_fences(root, new_memory)
    assert len(new_memory.retrieve_memories("new safe note", limit=3)) == 1


def test_runtime_session_memory_inherits_agent_forget_fences(tmp_path):
    from types import SimpleNamespace
    from orchestrator import runtime_session
    from orchestrator.session_store import SessionStore

    home = tmp_path / "agent"
    home.mkdir()
    root_memory = BridgeMemoryStore(home)
    root_memory.forget_window(
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
        origin_refs=("forgotten-run",),
    )
    sessions = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.create_session(owner_id="user:7", agent_id="lily")
    runtime = SimpleNamespace(workspace_dir=home, session_store=sessions)
    item = SimpleNamespace(session_id=session["session_id"], context_generation=1)
    session_memory = runtime_session.session_memory_store(runtime, item)
    assert session_memory.record_completed_exchange(
        "bad request", "bad answer", "test",
        user_ts=stamp(9, 5), assistant_ts=stamp(9, 6), origin_ref="forgotten-run",
    ) is None


def test_recovery_capsule_cannot_be_replayed_after_its_interval_was_forgotten(tmp_path, monkeypatch):
    store = BridgeMemoryStore(tmp_path)
    monkeypatch.setattr(store, "_now", lambda: stamp(9, 30))
    assert store.record_recovery_capsule("bad idea in old unfinished work", origin_ref="old-run") is not None

    store.forget_window(start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))

    monkeypatch.setattr(store, "_now", lambda: stamp(12))
    assert store.record_recovery_capsule("bad idea in old unfinished work", origin_ref="old-run") is None
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE text LIKE '%bad idea%'"
        ).fetchone()[0] == 0
