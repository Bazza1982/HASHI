from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import orchestrator.agent_move.history_backfill as history_backfill
from orchestrator.agent_move.history_backfill import (
    apply_history_backfill,
    dry_run_history_backfill,
    export_history_capsules,
    history_backfill_status,
    rollback_history_backfill,
    validate_manifest,
)
from orchestrator.agent_move.package import AgentMoveError
from orchestrator.instance_lock import InstanceLock
from orchestrator.pathing import build_bridge_paths
from orchestrator.session_store import SessionStore


OWNER = "user:7"


def _write_root(root: Path, instance_id: str, agents: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agents.json").write_text(
        json.dumps(
            {
                "global": {"instance_id": instance_id, "authorized_id": 7},
                "agents": [
                    {
                        "name": name,
                        "is_active": True,
                        "agent_lifecycle_id": hashlib.sha256(
                            f"lifecycle:{name}".encode()
                        ).hexdigest()[:32],
                    }
                    for name in agents
                ],
            }
        ),
        encoding="utf-8",
    )


def _complete(
    store: SessionStore,
    *,
    agent_id: str,
    session_id: str,
    request_id: str,
    question: str,
    answer: str,
) -> None:
    store.accept_run(
        session_id=session_id,
        owner_id=OWNER,
        agent_id=agent_id,
        request_id=request_id,
        text=question,
        source="test",
        idempotency_key=request_id,
    )
    assert store.mark_request_running(request_id, worker_id="test-worker") == 1
    store.finish_request(
        request_id,
        success=True,
        assistant_text=answer,
        assistant_source="test-backend",
    )


def _fixture(tmp_path: Path, *, entry_count: int = 2):
    h1_count = min(12, entry_count)
    h1_agents = [f"agent-{index:02d}" for index in range(h1_count)]
    h2_agents = [f"agent-{index:02d}" for index in range(h1_count, entry_count)]
    target_agents = [*h1_agents, *h2_agents]
    h1 = tmp_path / "hashi1"
    h2 = tmp_path / "hashi2"
    target = tmp_path / "hashi4"
    _write_root(h1, "HASHI1", h1_agents)
    _write_root(h2, "HASHI2", h2_agents)
    _write_root(target, "HASHI4", target_agents)
    entries = []
    for source_root, instance_id, agents in (
        (h1, "HASHI1", h1_agents),
        (h2, "HASHI2", h2_agents),
    ):
        store = SessionStore(
            source_root / "state" / "sessions.sqlite3",
            instance_id=instance_id,
        )
        for agent in agents:
            session = store.ensure_default_session(owner_id=OWNER, agent_id=agent)
            _complete(
                store,
                agent_id=agent,
                session_id=session["session_id"],
                request_id=f"source-{agent}",
                question=f"old question {agent}",
                answer=f"old answer {agent}",
            )
            entries.append(
                {
                    "source_instance": instance_id,
                    "source_agent_id": agent,
                    "source_agent_lifecycle_id": hashlib.sha256(
                        f"lifecycle:{agent}".encode()
                    ).hexdigest()[:32],
                    "target_agent_id": agent,
                    "target_agent_lifecycle_id": hashlib.sha256(
                        f"lifecycle:{agent}".encode()
                    ).hexdigest()[:32],
                }
            )
    target_store = SessionStore(
        target / "state" / "sessions.sqlite3", instance_id="HASHI4"
    )
    for agent in target_agents:
        session = target_store.ensure_default_session(owner_id=OWNER, agent_id=agent)
        _complete(
            target_store,
            agent_id=agent,
            session_id=session["session_id"],
            request_id=f"target-{agent}",
            question=f"new question {agent}",
            answer=f"new answer {agent}",
        )
        workspace = target / "state" / "session_workspaces" / session["session_id"]
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "marker.txt").write_text("keep", encoding="utf-8")
    legacy = target / "workspaces" / target_agents[0] / "transcript.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("LEGACY MUST NOT BE READ\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "batch_id": "h4-history-backfill-test",
        "target_instance": "HASHI4",
        "owner_id": OWNER,
        "expected_entry_count": entry_count,
        "entries": entries,
    }
    capsule_dir = tmp_path / "frozen-capsules"
    export_history_capsules(h1, "HASHI1", manifest, capsule_dir)
    if h2_agents:
        export_history_capsules(h2, "HASHI2", manifest, capsule_dir)
    return h1, h2, target, target_store, manifest, capsule_dir, legacy


def _db_sha(root: Path) -> str:
    return hashlib.sha256((root / "state" / "sessions.sqlite3").read_bytes()).hexdigest()


def test_dry_run_and_apply_twelve_agents_preserve_new_history_and_are_idempotent(
    tmp_path,
):
    h1, _h2, target, store, manifest, capsule_dir, legacy = _fixture(
        tmp_path, entry_count=12
    )
    before_sha = _db_sha(target)

    plan = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)

    assert plan["target_writes"] == 0
    assert len(plan["entries"]) == 12
    assert sum(item["eligible_messages"] for item in plan["entries"]) == 24
    assert sum(item["target_messages_before"] for item in plan["entries"]) == 24
    assert _db_sha(target) == before_sha
    assert legacy.read_text(encoding="utf-8") == "LEGACY MUST NOT BE READ\n"

    # Target planning consumes the frozen capsules, never the source live DB/WAL.
    h1_store = SessionStore(h1 / "state" / "sessions.sqlite3", instance_id="HASHI1")
    source_session = h1_store.ensure_default_session(owner_id=OWNER, agent_id="agent-00")
    _complete(
        h1_store,
        agent_id="agent-00",
        session_id=source_session["session_id"],
        request_id="post-export-source-change",
        question="must not enter frozen capsule",
        answer="must not enter target",
    )
    assert dry_run_history_backfill(
        target, manifest, capsule_dir=capsule_dir
    )["plan_digest"] == plan["plan_digest"]

    applied = apply_history_backfill(
        target,
        manifest,
        capsule_dir=capsule_dir,
        expected_plan_digest=plan["plan_digest"],
        backup_dir=tmp_path / "backup-1",
    )
    assert len(applied["entries"]) == 12
    assert (tmp_path / "backup-1" / "sessions.sqlite3").is_file()
    assert (tmp_path / "backup-1" / "session_workspaces").is_dir()
    assert json.loads(
        (tmp_path / "backup-1" / "manifest.json").read_text(encoding="utf-8")
    )["expected_entry_count"] == 12
    first = store.resolve_session(
        owner_id=OWNER,
        agent_id="agent-00",
        surface="workbench",
        channel_key="default",
    )
    assert [row["text"] for row in store.messages(first["session_id"], owner_id=OWNER)] == [
        "old question agent-00",
        "old answer agent-00",
        "new question agent-00",
        "new answer agent-00",
    ]
    assert all(
        item["status"] == "imported"
        for item in history_backfill_status(target, manifest)["entries"]
    )

    replay_plan = dry_run_history_backfill(
        target, manifest, capsule_dir=capsule_dir
    )
    replayed = apply_history_backfill(
        target,
        manifest,
        capsule_dir=capsule_dir,
        expected_plan_digest=replay_plan["plan_digest"],
        backup_dir=tmp_path / "backup-2",
    )
    assert all(item["replayed"] for item in replayed["entries"])
    assert all(item["imported_messages"] == 0 for item in replayed["entries"])

    rolled_back = rollback_history_backfill(target, manifest)
    assert sum(item["removed_messages"] for item in rolled_back["entries"]) == 24
    assert [row["text"] for row in store.messages(first["session_id"], owner_id=OWNER)] == [
        "new question agent-00",
        "new answer agent-00",
    ]


def test_apply_rejects_running_instance_and_nonterminal_run(tmp_path):
    _h1, _h2, target, store, manifest, capsule_dir, _legacy = _fixture(tmp_path)
    plan = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    paths = build_bridge_paths(target, bridge_home=target, canonical_home=True)
    held = InstanceLock(
        paths.lock_path, pid_path=paths.pid_path, instance_id=paths.instance_id
    )
    held.acquire()
    try:
        with pytest.raises(AgentMoveError, match="target instance is running"):
            apply_history_backfill(
                target,
                manifest,
                capsule_dir=capsule_dir,
                expected_plan_digest=plan["plan_digest"],
                backup_dir=tmp_path / "blocked-backup",
            )
    finally:
        held.release()
    assert not (tmp_path / "blocked-backup").exists()

    session = store.ensure_default_session(owner_id="user:8", agent_id="agent-00")
    store.accept_run(
        session_id=session["session_id"],
        owner_id="user:8",
        agent_id="agent-00",
        request_id="still-queued",
        text="queued",
        source="test",
        idempotency_key="still-queued",
    )
    with pytest.raises(AgentMoveError, match="non-terminal Runs"):
        apply_history_backfill(
            target,
            manifest,
            capsule_dir=capsule_dir,
            expected_plan_digest=plan["plan_digest"],
            backup_dir=tmp_path / "active-run-backup",
        )
    assert not (tmp_path / "active-run-backup").exists()


def test_plan_binds_channels_and_apply_backs_up_before_live_store_init(
    tmp_path, monkeypatch
):
    _h1, _h2, target, store, manifest, capsule_dir, _legacy = _fixture(tmp_path)
    plan = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    session = store.ensure_default_session(owner_id=OWNER, agent_id="agent-00")
    store.bind_channel(
        owner_id=OWNER,
        agent_id="agent-00",
        surface="workbench",
        channel_key="new-binding",
        session_id=session["session_id"],
    )
    changed = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    assert changed["plan_digest"] != plan["plan_digest"]

    backup = tmp_path / "binding-backup"
    original_init = SessionStore.__init__
    live_database = (target / "state" / "sessions.sqlite3").resolve()

    def assert_backup_before_live_init(self, db_path, *args, **kwargs):
        if Path(db_path).resolve() == live_database:
            assert (backup / "sessions.sqlite3").is_file()
        original_init(self, db_path, *args, **kwargs)

    monkeypatch.setattr(SessionStore, "__init__", assert_backup_before_live_init)
    apply_history_backfill(
        target,
        manifest,
        capsule_dir=capsule_dir,
        expected_plan_digest=changed["plan_digest"],
        backup_dir=backup,
    )


def test_status_never_initializes_the_live_session_store(tmp_path, monkeypatch):
    _h1, _h2, target, _store, manifest, _capsules, _legacy = _fixture(tmp_path)
    original_init = SessionStore.__init__
    live_database = (target / "state" / "sessions.sqlite3").resolve()

    def reject_live_init(self, db_path, *args, **kwargs):
        assert Path(db_path).resolve() != live_database
        original_init(self, db_path, *args, **kwargs)

    monkeypatch.setattr(SessionStore, "__init__", reject_live_init)
    result = history_backfill_status(target, manifest)
    assert all(item["status"] == "not_imported" for item in result["entries"])


def test_apply_failure_compensates_prior_entries_without_overwriting_target_work(
    tmp_path, monkeypatch
):
    _h1, _h2, target, store, manifest, capsule_dir, _legacy = _fixture(tmp_path)
    plan = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    original = SessionStore.import_conversation_continuity

    def fail_second(self, capsule, **kwargs):
        if self.db_path == store.db_path and kwargs["agent_id"] == "agent-01":
            raise RuntimeError("injected second import failure")
        return original(self, capsule, **kwargs)

    monkeypatch.setattr(SessionStore, "import_conversation_continuity", fail_second)
    with pytest.raises(AgentMoveError, match="failed and was compensated"):
        apply_history_backfill(
            target,
            manifest,
            capsule_dir=capsule_dir,
            expected_plan_digest=plan["plan_digest"],
            backup_dir=tmp_path / "failure-backup",
        )

    assert all(
        item["status"] == "not_imported"
        for item in history_backfill_status(target, manifest)["entries"]
    )
    for agent in ("agent-00", "agent-01"):
        session = store.ensure_default_session(owner_id=OWNER, agent_id=agent)
        assert [row["text"] for row in store.messages(session["session_id"], owner_id=OWNER)] == [
            f"new question {agent}",
            f"new answer {agent}",
        ]


def test_failure_compensation_preserves_a_preexisting_same_batch_claim(
    tmp_path, monkeypatch
):
    _h1, _h2, target, store, manifest, capsule_dir, _legacy = _fixture(tmp_path)
    first_plan = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    apply_history_backfill(
        target,
        manifest,
        capsule_dir=capsule_dir,
        expected_plan_digest=first_plan["plan_digest"],
        backup_dir=tmp_path / "initial-backup",
    )
    normalized = validate_manifest(manifest)
    second_transfer = normalized["entries"][1]["transfer_id"]
    store.rollback_conversation_continuity(second_transfer)
    retry_plan = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    original = SessionStore.import_conversation_continuity

    def fail_second(self, capsule, **kwargs):
        if self.db_path == store.db_path and kwargs["agent_id"] == "agent-01":
            raise RuntimeError("injected retry failure")
        return original(self, capsule, **kwargs)

    monkeypatch.setattr(SessionStore, "import_conversation_continuity", fail_second)
    with pytest.raises(AgentMoveError, match="failed and was compensated"):
        apply_history_backfill(
            target,
            manifest,
            capsule_dir=capsule_dir,
            expected_plan_digest=retry_plan["plan_digest"],
            backup_dir=tmp_path / "retry-backup",
        )
    status = history_backfill_status(target, manifest)["entries"]
    assert status[0]["status"] == "imported"
    assert status[1]["status"] == "not_imported"


def test_commit_then_raise_is_discovered_and_incomplete_compensation_is_truthful(
    tmp_path, monkeypatch
):
    _h1, _h2, target, store, manifest, capsule_dir, _legacy = _fixture(tmp_path)
    plan = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    normalized = validate_manifest(manifest)
    first_transfer = normalized["entries"][0]["transfer_id"]
    original_import = SessionStore.import_conversation_continuity
    original_rollback = SessionStore.rollback_conversation_continuity

    def commit_then_raise(self, capsule, **kwargs):
        result = original_import(self, capsule, **kwargs)
        if self.db_path == store.db_path and kwargs["agent_id"] == "agent-01":
            raise RuntimeError("raised after committed import")
        return result

    def fail_first_rollback(self, transfer_id):
        if self.db_path == store.db_path and transfer_id == first_transfer:
            raise RuntimeError("injected rollback failure")
        return original_rollback(self, transfer_id)

    monkeypatch.setattr(
        SessionStore, "import_conversation_continuity", commit_then_raise
    )
    monkeypatch.setattr(
        SessionStore, "rollback_conversation_continuity", fail_first_rollback
    )
    with pytest.raises(
        AgentMoveError,
        match=f"compensation incomplete.*{first_transfer}.*manual recovery required",
    ):
        apply_history_backfill(
            target,
            manifest,
            capsule_dir=capsule_dir,
            expected_plan_digest=plan["plan_digest"],
            backup_dir=tmp_path / "commit-then-raise-backup",
        )
    status = history_backfill_status(target, manifest)["entries"]
    assert status[0]["status"] == "imported"
    assert status[1]["status"] == "not_imported"


def test_manifest_owner_source_and_shape_fail_closed_before_target_write(tmp_path):
    h1, _h2, target, _store, manifest, capsule_dir, _legacy = _fixture(tmp_path)
    before = _db_sha(target)
    replayed_export = export_history_capsules(h1, "HASHI1", manifest, capsule_dir)
    assert all(item["replayed"] for item in replayed_export["entries"])
    invalid = dict(manifest)
    invalid["unexpected"] = True
    with pytest.raises(AgentMoveError, match="manifest keys"):
        dry_run_history_backfill(target, invalid, capsule_dir=capsule_dir)

    wrong_owner = dict(manifest)
    wrong_owner["owner_id"] = "user:8"
    with pytest.raises(AgentMoveError, match="target owner"):
        dry_run_history_backfill(target, wrong_owner, capsule_dir=capsule_dir)

    target_config = json.loads((target / "agents.json").read_text(encoding="utf-8"))
    expected_lifecycle = target_config["agents"][0]["agent_lifecycle_id"]
    target_config["agents"][0]["agent_lifecycle_id"] = "f" * 32
    (target / "agents.json").write_text(json.dumps(target_config), encoding="utf-8")
    with pytest.raises(AgentMoveError, match="target Agent .* lifecycle"):
        dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    target_config["agents"][0]["agent_lifecycle_id"] = expected_lifecycle
    (target / "agents.json").write_text(json.dumps(target_config), encoding="utf-8")

    wrong_source = json.loads(json.dumps(manifest))
    wrong_source["entries"][0]["source_instance"] = "HASHI9"
    with pytest.raises(AgentMoveError, match="capsule set"):
        dry_run_history_backfill(target, wrong_source, capsule_dir=capsule_dir)

    mismatched_lifecycle = json.loads(json.dumps(manifest))
    mismatched_lifecycle["entries"][0]["target_agent_lifecycle_id"] = "e" * 32
    with pytest.raises(AgentMoveError, match="lifecycle identities differ"):
        dry_run_history_backfill(
            target, mismatched_lifecycle, capsule_dir=capsule_dir
        )

    plan = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    with pytest.raises(AgentMoveError, match="overlaps target state"):
        apply_history_backfill(
            target,
            manifest,
            capsule_dir=capsule_dir,
            expected_plan_digest=plan["plan_digest"],
            backup_dir=target / "state" / "recursive-backup",
        )
    assert _db_sha(target) == before


def test_backup_inventory_permission_failure_is_fail_closed(tmp_path, monkeypatch):
    _h1, _h2, target, _store, manifest, capsule_dir, _legacy = _fixture(tmp_path)
    plan = dry_run_history_backfill(target, manifest, capsule_dir=capsule_dir)
    original_walk = history_backfill.os.walk
    workspace = (target / "state" / "session_workspaces").absolute()

    def denied_walk(top, *args, **kwargs):
        if Path(top).absolute() == workspace:
            kwargs["onerror"](PermissionError("denied"))
            return iter(())
        return original_walk(top, *args, **kwargs)

    monkeypatch.setattr(history_backfill.os, "walk", denied_walk)
    with pytest.raises(AgentMoveError, match="could not inspect"):
        apply_history_backfill(
            target,
            manifest,
            capsule_dir=capsule_dir,
            expected_plan_digest=plan["plan_digest"],
            backup_dir=tmp_path / "denied-backup",
        )
