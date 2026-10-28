from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar

import pytest

from orchestrator.agent_move import coordinator
from orchestrator.agent_move.package import AgentMoveError
from orchestrator.agent_move.source_guard import source_move_guard_state
from orchestrator.pcm import render_pcm_document
from orchestrator.session_store import SessionStore


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    workspace = root / "workspaces" / "zelda"
    workspace.mkdir(parents=True)
    (workspace / "agent.md").write_text(
        render_pcm_document(persona="Zelda", system="Follow policy", memory="Memory"),
        encoding="utf-8",
    )
    (workspace / "memory.md").write_text("durable", encoding="utf-8")
    _write_json(
        root / "agents.json",
        {
            "global": {"instance_id": "HASHI1"},
            "agents": [
                {
                    "name": "zelda",
                    "type": "flex",
                    "workspace_dir": "workspaces/zelda",
                    "active_backend": "codex-cli",
                    "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.5"}],
                    "is_active": True,
                },
                {
                    "name": "anchor",
                    "type": "flex",
                    "workspace_dir": "workspaces/anchor",
                    "active_backend": "codex-cli",
                    "allowed_backends": [
                        {"engine": "codex-cli", "model": "gpt-5.5"}
                    ],
                    "is_active": True,
                },
            ],
        },
    )
    _write_json(
        root / "secrets.json",
        {"hashi_remote_shared_token": "shared-secret", "zelda": "telegram-token"},
    )
    _write_json(
        root / "tasks.json",
        {
            "version": 1,
            "heartbeats": [{"id": "heartbeat", "agent": "zelda", "enabled": True}],
            "crons": [],
            "nudges": [],
        },
    )
    return root


class _Receiver:
    source_instance = "HASHI1"
    target_instance = "HASHI2"
    capabilities: ClassVar[dict[str, str]] = {
        "environment_kind": "windows",
        "capability": "agent_move_receive_v1",
        "max_package_bytes": str(256 * 1024 * 1024),
    }

    def __init__(
        self,
        *,
        fail_activate: bool = False,
        fail_rollback: bool = False,
        on_commit=None,
    ):
        self.calls = []
        self.fail_activate = fail_activate
        self.fail_rollback = fail_rollback
        self.on_commit = on_commit
        self.remote_status = "new"
        self.target_agent_id = "zelda"

    def resolve_target_id(
        self,
        source_agent_id,
        *,
        operation,
        requested_agent_id=None,
    ):
        self.target_agent_id = requested_agent_id or source_agent_id
        return {
            "target_agent_id": self.target_agent_id,
            "operation": operation,
            "renamed": self.target_agent_id != source_agent_id,
        }

    def stage(self, package_path, *, operation="move", target_agent_id=None):
        self.calls.append(("stage", Path(package_path)))
        self.remote_status = "staged"
        self.target_agent_id = target_agent_id or self.target_agent_id
        return {
            "status": "staged",
            "target_agent_id": self.target_agent_id,
            "credential_status": {"missing_keys": []},
            "warnings": [],
        }

    def commit(self, package_id):
        self.calls.append(("commit", package_id))
        if self.on_commit is not None:
            self.on_commit()
        self.remote_status = "committed_inactive"
        return {
            "status": "committed_inactive",
            "credential_status": {"missing_keys": []},
        }

    def activate(self, package_id):
        self.calls.append(("activate", package_id))
        if self.fail_activate:
            raise AgentMoveError("activation refused")
        self.remote_status = "activated_pending_reboot"
        return {"status": "activated_pending_reboot"}

    def start(self, package_id):
        self.calls.append(("start", package_id))
        return {"status": self.remote_status}

    def stop(self, package_id):
        self.calls.append(("stop", package_id))
        return {"status": self.remote_status}

    def finalize(self, package_id):
        self.calls.append(("finalize", package_id))
        self.remote_status = "completed"
        return {
            "status": "completed",
            "target_verified": True,
            "target_verification": {"runtime_online": True},
        }

    def status(self, package_id):
        return {
            "status": self.remote_status,
            "target_agent_id": self.target_agent_id,
            "target_verified": self.remote_status == "completed",
            "target_verification": {"runtime_online": True}
            if self.remote_status == "completed"
            else {},
        }

    def rollback(self, package_id):
        self.calls.append(("rollback", package_id))
        if self.fail_rollback:
            raise AgentMoveError("rollback response unavailable")
        self.remote_status = "rolled_back"
        return {"status": "rolled_back"}


def _install_receiver(monkeypatch, receiver):
    monkeypatch.setattr(
        coordinator,
        "connect_agent_move_receiver",
        lambda *args, **kwargs: receiver,
    )


def _install_local_lifecycle(monkeypatch, *running_agents: str) -> set[str]:
    running = set(running_agents)
    monkeypatch.setattr(
        coordinator,
        "_local_running_agents",
        lambda _root: set(running),
    )

    def lifecycle(_root, agent_id, action):
        if action == "start":
            running.add(agent_id)
        elif action == "stop":
            running.discard(agent_id)
        return {"ok": True, "agent": agent_id, "action": action}

    monkeypatch.setattr(coordinator, "_local_workbench_lifecycle", lifecycle)
    return running


def test_preview_is_disposable_and_does_not_create_outbound_state(
    tmp_path, monkeypatch
):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)

    result = coordinator.preview_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )

    assert result["preview"] is True
    assert result["target_environment"] == "windows"
    assert receiver.calls == []
    assert not (root / "state" / "agent_moves" / "outbound").exists()


def test_prepare_records_changed_workspace_metadata_without_staging(
    tmp_path, monkeypatch
):
    from orchestrator.agent_move import package as owner

    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"]["authorized_id"] = 7
    _write_json(root / "agents.json", config)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    workspace = root / "workspaces" / "zelda"
    write_file = owner._write_file

    def write_then_change(archive, name, source, checksums, *, mode):
        write_file(archive, name, source, checksums, mode=mode)
        if name == "workspace/memory.md":
            (workspace / "memory.md").write_text("new private memory content")

    monkeypatch.setattr(owner, "_write_file", write_then_change)
    with pytest.raises(AgentMoveError, match="workspace changed while packaging"):
        coordinator.prepare_outbound_move(
            root, {"hashi2": {}}, "zelda", "hashi2",
            source_instance="HASHI1", transfer_mode="workspace",
        )
    states = list((root / "state" / "agent_moves" / "outbound").glob("*/state.json"))
    assert len(states) == 1
    state = json.loads(states[0].read_text())
    assert state["status"] == "prepare_failed"
    assert state["transfer_mode"] == "workspace"
    assert state["workspace_changes"]["changed_count"] == 1
    assert state["workspace_changes"]["changes"][0]["path"] == "memory.md"
    assert state["workspace_changes"]["changes"][0]["before"]["size"] == len("durable")
    assert "new private memory content" not in json.dumps(state)
    assert receiver.calls == []
    assert (workspace / "memory.md").is_file()
    assert json.loads((root / "agents.json").read_text())["agents"][0]["is_active"] is True
    assert not list(states[0].parent.glob("*.hashi-agent"))


@pytest.mark.skipif(
    os.name == "nt",
    reason="agent.md and AGENT.md cannot coexist on a case-insensitive Windows tree",
)
def test_preview_reports_non_authoritative_retained_identity(tmp_path, monkeypatch):
    root = _source(tmp_path)
    retained = root / "workspaces" / "zelda" / "AGENT.md"
    retained.write_text("legacy retained identity", encoding="utf-8")
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)

    result = coordinator.preview_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )

    assert result["package_schema"] == 3
    assert result["retained_identity"]["authoritative"] is False
    assert result["retained_identity"]["original_path"] == "AGENT.md"
    assert result["retained_identity"]["target_policy"] == (
        "persistent transaction attachment; never installed as live PCM"
    )


def test_confirm_move_disables_source_only_after_target_commit(tmp_path, monkeypatch):
    root = _source(tmp_path)
    commit_guards = []
    receiver = _Receiver(
        on_commit=lambda: commit_guards.append(source_move_guard_state(root, "zelda"))
    )
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )

    result = coordinator.confirm_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )

    assert [call[0] for call in receiver.calls] == ["stage", "commit"]
    assert commit_guards == [
        {"status": "cutover_quiesce", "target_instance": "HASHI2"}
    ]
    assert result["status"] == "source_disabled_target_committed"
    assert result["reboot_order"] == ["HASHI1"]
    assert result["target_active"] is False
    assert (
        json.loads((root / "agents.json").read_text())["agents"][0]["is_active"]
        is False
    )
    assert (root / "workspaces" / "zelda" / "memory.md").read_text() == "durable"
    assert source_move_guard_state(root, "zelda") == {
        "status": "moved_out_pending_reboot",
        "target_instance": "HASHI2",
    }


def test_source_guard_supports_legacy_list_agents_file(tmp_path):
    root = _source(tmp_path)
    agents = json.loads((root / "agents.json").read_text())["agents"]
    agents[0].update(
        {
            "is_active": False,
            "transfer_state": "moved_out_pending_reboot",
            "transfer_target": "HASHI2",
        }
    )
    _write_json(root / "agents.json", agents)

    assert source_move_guard_state(root, "zelda") == {
        "status": "moved_out_pending_reboot",
        "target_instance": "HASHI2",
    }


def test_activation_failure_keeps_forward_move_and_retries_without_rollback(
    tmp_path, monkeypatch
):
    root = _source(tmp_path)
    session_path = root / "workspaces" / "zelda" / ".runtime_session.json"
    original_session = b'{"engine_session_id":"original-session"}\n'
    session_path.write_bytes(original_session)
    receiver = _Receiver(fail_activate=True)
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )

    first_phase = coordinator.confirm_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )
    assert first_phase["status"] == "source_disabled_target_committed"

    with pytest.raises(AgentMoveError, match="forward retry"):
        coordinator.continue_outbound_move(
            root, {"hashi2": {}}, prepared["package_id"]
        )

    assert [call[0] for call in receiver.calls] == [
        "stage",
        "commit",
        "activate",
    ]
    source_agent = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source_agent["is_active"] is False
    assert source_agent["transfer_package_id"] == prepared["package_id"]
    assert (
        json.loads((root / "tasks.json").read_text())["heartbeats"][0]["enabled"]
        is False
    )
    assert session_path.read_bytes() == original_session
    pending = coordinator.get_outbound_move(root, prepared["package_id"])
    assert pending["status"] == "activating_target"
    assert pending["source_disabled"] is True

    receiver.fail_activate = False
    completed = coordinator.continue_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )
    assert completed["status"] == "completed"
    assert "rollback" not in [call[0] for call in receiver.calls]


def test_repeated_activation_failure_never_attempts_target_rollback(
    tmp_path, monkeypatch
):
    root = _source(tmp_path)
    receiver = _Receiver(fail_activate=True, fail_rollback=True)
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )

    coordinator.confirm_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )
    with pytest.raises(AgentMoveError, match="forward retry"):
        coordinator.continue_outbound_move(
            root,
            {"hashi2": {}},
            prepared["package_id"],
        )

    assert [call[0] for call in receiver.calls] == [
        "stage",
        "commit",
        "activate",
    ]
    source_agent = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source_agent["is_active"] is False
    assert source_agent["transfer_package_id"] == prepared["package_id"]
    assert source_move_guard_state(root, "zelda") == {
        "status": "moved_out_pending_reboot",
        "target_instance": "HASHI2",
    }

    receiver.fail_activate = False
    recovered = coordinator.confirm_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )
    assert recovered["status"] == "activating_target"
    completed = coordinator.continue_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )
    assert completed["status"] == "completed"


def test_cancel_is_rejected_after_forward_move_intent_is_committed(
    tmp_path, monkeypatch
):
    root = _source(tmp_path)
    receiver = _Receiver(fail_activate=True, fail_rollback=True)
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )
    coordinator.confirm_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )
    with pytest.raises(AgentMoveError, match="forward retry"):
        coordinator.continue_outbound_move(
            root,
            {"hashi2": {}},
            prepared["package_id"],
        )

    with pytest.raises(AgentMoveError, match="must continue forward"):
        coordinator.cancel_outbound_move(
            root,
            {"hashi2": {}},
            prepared["package_id"],
        )
    source_agent = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source_agent["is_active"] is False
    assert source_agent["transfer_package_id"] == prepared["package_id"]
    assert source_move_guard_state(root, "zelda") == {
        "status": "moved_out_pending_reboot",
        "target_instance": "HASHI2",
    }


def test_copy_mode_never_disables_source_or_activates_target(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
        keep_source=True,
    )

    result = coordinator.confirm_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )

    assert [call[0] for call in receiver.calls] == ["stage", "commit"]
    assert result["status"] == "copied_inactive"
    assert (
        json.loads((root / "agents.json").read_text())["agents"][0]["is_active"] is True
    )


def test_confirm_resumes_after_interruption_during_target_commit(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )
    state_path = (
        root
        / "state"
        / "agent_moves"
        / "outbound"
        / prepared["package_id"]
        / "state.json"
    )
    state = json.loads(state_path.read_text())
    state["status"] = "committing_target"
    _write_json(state_path, state)

    result = coordinator.confirm_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )

    assert result["status"] == "source_disabled_target_committed"
    assert [call[0] for call in receiver.calls] == ["stage", "commit"]


def test_prepare_rejects_second_unfinished_move_for_same_agent(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    first = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )

    with pytest.raises(AgentMoveError, match=first["package_id"]):
        coordinator.prepare_outbound_move(
            root,
            {"hashi2": {}},
            "zelda",
            "hashi2",
            source_instance="HASHI1",
        )


def test_move_of_last_active_agent_is_rejected_before_target_or_outbound_mutation(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    data = json.loads((root / "agents.json").read_text())
    data["agents"] = [data["agents"][0]]
    _write_json(root / "agents.json", data)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)

    with pytest.raises(AgentMoveError, match="last active Agent"):
        coordinator.prepare_outbound_move(
            root,
            {"hashi2": {}},
            "zelda",
            "hashi2",
            source_instance="HASHI1",
        )

    assert receiver.calls == []
    assert not (root / "state" / "agent_moves" / "outbound").exists()


@pytest.mark.asyncio
async def test_last_active_agent_can_be_cloned_locally_and_source_stays_unchanged(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    data = json.loads((root / "agents.json").read_text())
    data["agents"] = [data["agents"][0]]
    _write_json(root / "agents.json", data)
    running = _install_local_lifecycle(monkeypatch, "zelda")

    workspace = root / "workspaces" / "zelda"
    (workspace / "transcript.jsonl").write_text('{"message":"retain history"}\n')
    (workspace / "artifact.bin").write_bytes(b"ordinary work")
    prepared = coordinator.prepare_outbound_clone(
        root,
        {},
        "zelda",
        "HASHI1",
        source_instance="HASHI1",
        transfer_mode="identity_memory",
    )
    assert prepared["transfer_mode"] == "identity_memory"
    assert any(item["path"] == "artifact.bin" for item in prepared["discarded_files"])
    assert prepared["target_agent_id"] == "zelda_1"
    from types import SimpleNamespace
    from orchestrator.agent_move.manager import AgentMoveManager
    manager = AgentMoveManager(SimpleNamespace(paths=SimpleNamespace(bridge_home=root)))
    await manager.start()
    try:
        manager.submit(prepared["package_id"], {})
        receipt = await manager.wait(prepared["package_id"])
        assert receipt["status"] == "completed"
        result = receipt["result"]
    finally:
        await manager.stop()

    assert result["status"] == "completed"
    assert result["target_agent_id"] == "zelda_1"
    rows = json.loads((root / "agents.json").read_text())["agents"]
    assert {row["name"] for row in rows} == {"zelda", "zelda_1"}
    assert all(row["is_active"] is True for row in rows)
    assert {"zelda", "zelda_1"}.issubset(running)
    assert (root / "workspaces" / "zelda" / "memory.md").read_text() == "durable"
    target = root / "workspaces" / "zelda_1"
    assert (target / "transcript.jsonl").read_bytes() == (workspace / "transcript.jsonl").read_bytes()
    assert not (target / "artifact.bin").exists()
    assert (workspace / "artifact.bin").read_bytes() == b"ordinary work"
    secrets = json.loads((root / "secrets.json").read_text())
    assert secrets["zelda"] == "telegram-token"
    assert "zelda_1" not in secrets
    clone_tasks = [
        item
        for item in json.loads((root / "tasks.json").read_text())["heartbeats"]
        if item.get("agent") == "zelda_1"
    ]
    assert len(clone_tasks) == 1
    assert clone_tasks[0]["enabled"] is False


@pytest.mark.asyncio
async def test_local_clone_copies_stable_history_without_activating_inactive_source(
    tmp_path, monkeypatch
):
    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"].update(
        owner_scope_id="user:7", authorized_id=99, telegram_owner_id=99
    )
    config["agents"][0]["is_active"] = False
    _write_json(root / "agents.json", config)
    running = _install_local_lifecycle(monkeypatch)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    source_session = store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    store.ensure_default_session(owner_id="user:99", agent_id="zelda")
    accepted = store.accept_run(
        session_id=source_session["session_id"],
        owner_id="user:7",
        agent_id="zelda",
        request_id="stable-history",
        text="same human",
        source="workbench",
        idempotency_key="stable-history",
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="retained answer",
        assistant_source="fixture",
    )
    prepared = coordinator.prepare_outbound_clone(
        root,
        {},
        "zelda",
        "HASHI1",
        source_instance="HASHI1",
        history_mode="copy",
    )
    from types import SimpleNamespace
    from orchestrator.agent_move.manager import AgentMoveManager

    manager = AgentMoveManager(SimpleNamespace(paths=SimpleNamespace(bridge_home=root)))
    await manager.start()
    try:
        manager.submit(prepared["package_id"], {})
        receipt = await manager.wait(prepared["package_id"])
    finally:
        await manager.stop()
    assert receipt["status"] == "completed"
    target_session = store.resolve_session(
        owner_id="user:7",
        agent_id="zelda_1",
        surface="workbench",
        channel_key="default",
    )
    assert target_session["session_id"] != source_session["session_id"]
    assert [
        m["text"]
        for m in store.messages(target_session["session_id"], owner_id="user:7")
    ] == [
        "same human",
        "retained answer",
    ]
    assert len(store.messages(source_session["session_id"], owner_id="user:7")) == 2
    assert store.list_sessions(owner_id="user:99", agent_id="zelda_1") == []
    agents = {
        row["name"]: row
        for row in json.loads((root / "agents.json").read_text())["agents"]
    }
    assert agents["zelda"]["is_active"] is False
    assert agents["zelda_1"]["is_active"] is True
    assert "zelda_1" in running and "zelda" not in running
    assert "zelda_1" not in json.loads((root / "secrets.json").read_text())


def test_remote_clone_runs_full_lifecycle_without_disabling_source(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_clone(
        root,
        {"hashi2": {}},
        "zelda",
        "HASHI2",
        source_instance="HASHI1",
        target_agent_id="sheik",
    )

    assert prepared["transfer_mode"] == "workspace"
    result = coordinator.confirm_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )

    assert result["status"] == "completed"
    assert result["target_agent_id"] == "sheik"
    assert [call[0] for call in receiver.calls] == [
        "stage",
        "commit",
        "activate",
        "start",
        "finalize",
    ]
    source = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source["is_active"] is True
    assert "transfer_package_id" not in source


@pytest.mark.parametrize("explicit", [False, True])
def test_repeating_clone_reopens_same_prepared_transaction(tmp_path, explicit):
    root = _source(tmp_path)
    options = {"target_agent_id": "sheik"} if explicit else {}
    first = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1", **options,
    )
    second = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "hashi1", source_instance="hashi1", **options,
    )
    assert second["package_id"] == first["package_id"]
    assert second["target_agent_id"] == first["target_agent_id"]
    assert second["status"] == "staged_remote"
    assert second["reused"] is True
    assert len(list((root / "state/agent_moves/incoming").glob("*/state.json"))) == 1


def test_independent_clones_and_move_do_not_serialize_source_snapshots(tmp_path, monkeypatch):
    root = _source(tmp_path)
    first = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1",
    )
    second = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1", target_agent_id="sheik",
    )
    assert first["package_id"] != second["package_id"]
    assert second["target_agent_id"] == "sheik"
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    move = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1",
    )
    assert move["status"] == "staged_remote"


def test_rolled_back_clone_does_not_block_new_clone(tmp_path, monkeypatch):
    root = _source(tmp_path)
    first = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1",
    )
    coordinator.cancel_outbound_move(root, {}, first["package_id"])
    directory, state = coordinator._load_outbound(root, first["package_id"])
    state.update(status="clone_failed", rollback_completed=True)
    _write_json(directory / "state.json", state)
    second = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1",
    )
    assert second["package_id"] != first["package_id"]
    assert second["target_agent_id"] == first["target_agent_id"]


def test_clone_auto_id_is_allocated_atomically_at_staging(tmp_path, monkeypatch):
    root = _source(tmp_path)
    build = coordinator.create_agent_move_package

    def build_then_occupy(*args, **kwargs):
        package = build(*args, **kwargs)
        config = json.loads((root / "agents.json").read_text())
        config["agents"].append({"name": "zelda_1", "is_active": False})
        _write_json(root / "agents.json", config)
        return package

    monkeypatch.setattr(coordinator, "create_agent_move_package", build_then_occupy)
    prepared = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1",
    )
    assert prepared["target_agent_id"] == "zelda_2"
    assert prepared["status"] == "staged_remote"


def test_clone_lost_stage_ack_reuses_identical_package_and_reservation(tmp_path, monkeypatch):
    root = _source(tmp_path)
    stage = coordinator._LocalAgentTransferClient.stage
    failed_once = False

    def stage_then_lose_ack(client, package_path, *, operation, target_agent_id=None):
        nonlocal failed_once
        result = stage(client, package_path, operation=operation, target_agent_id=target_agent_id)
        if not failed_once:
            failed_once = True
            raise OSError("stage acknowledgement lost")
        return result

    monkeypatch.setattr(coordinator._LocalAgentTransferClient, "stage", stage_then_lose_ack)
    with pytest.raises(coordinator.ClonePreparationError, match="acknowledgement lost"):
        coordinator.prepare_outbound_clone(
            root, {}, "zelda", "HASHI1", source_instance="HASHI1",
        )
    receipts = list((root / "state/agent_moves/outbound").glob("*/state.json"))
    first = json.loads(receipts[0].read_text())
    resumed = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1",
    )
    assert resumed["package_id"] == first["package_id"]
    assert resumed["sha256"] == first["sha256"]
    assert resumed["target_agent_id"] == "zelda_1"
    assert len(list((root / "state/agent_moves/incoming").glob("*/state.json"))) == 1


def test_clone_lost_start_ack_is_resumable_not_rolled_back(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    original = receiver.start

    def lose_ack(package_id):
        receiver.start = original
        receiver.calls.append(("start", package_id))
        raise AgentMoveError("start acknowledgement lost")

    receiver.start = lose_ack
    prepared = coordinator.prepare_outbound_clone(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1",
    )
    with pytest.raises(AgentMoveError, match="retry"):
        coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    pending = coordinator.get_outbound_move(root, prepared["package_id"])
    assert pending["status"] == "clone_activating"
    assert not pending.get("rollback_completed")
    assert {"stop", "rollback"}.isdisjoint(call[0] for call in receiver.calls)
    completed = coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert completed["status"] == "completed"
    assert json.loads((root / "agents.json").read_text())["agents"][0]["is_active"]


def test_duplicate_clone_during_preparation_does_not_stage_another_copy(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    root = _source(tmp_path)
    entered, release = Event(), Event()
    original = coordinator.create_agent_move_package

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(coordinator, "create_agent_move_package", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(coordinator.prepare_outbound_clone, root, {}, "zelda", "HASHI1", source_instance="HASHI1")
        try:
            assert entered.wait(5)
            duplicate = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
            assert duplicate["status"] == "packaging"
            assert duplicate["reused"]
        finally:
            release.set()
        completed = first.result(timeout=10)
    assert duplicate["package_id"] == completed["package_id"]
    assert len(list((root / "state/agent_moves/incoming").glob("*/state.json"))) == 1


def test_legacy_staged_clone_reopens_and_new_history_policy_is_independent(tmp_path):
    root = _source(tmp_path)
    data = json.loads((root / "agents.json").read_text())
    data["global"]["owner_scope_id"] = "user:7"
    _write_json(root / "agents.json", data)
    first = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    directory, state = coordinator._load_outbound(root, first["package_id"])
    state.pop("requested_target_agent_id", None)  # Journal written by the deployed version.
    _write_json(directory / "state.json", state)
    reopened = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    assert reopened["package_id"] == first["package_id"]
    second = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1", history_mode="copy")
    assert second["package_id"] != first["package_id"]
    assert second["target_agent_id"] == "zelda_2"
    assert second["history_mode"] == "copy"


@pytest.mark.parametrize("phase", ["packaging", "uploading"])
def test_abandoned_clone_preparation_is_recovered_without_pid_guessing(tmp_path, phase):
    root = _source(tmp_path)
    first = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    directory, state = coordinator._load_outbound(root, first["package_id"])
    state.update(status=phase, preparing_pid=os.getpid())  # Process survived; its old task did not.
    if phase == "packaging":
        state.pop("sha256")
        (directory / "zelda.hashi-agent").unlink()
        # A packaging task cannot already have uploaded a package. Reproduce
        # the interrupted pre-upload state in this disposable instance.
        coordinator.rollback_agent_move(root, first["package_id"])
        incoming = root / "state/agent_moves/incoming" / first["package_id"]
        import shutil
        shutil.rmtree(incoming)
    _write_json(directory / "state.json", state)
    resumed = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    assert resumed["package_id"] == first["package_id"]
    assert resumed["status"] == "staged_remote"


def test_old_empty_lock_files_do_not_disable_clone(tmp_path):
    root = _source(tmp_path)
    directory = root / "state/agent_moves"
    directory.mkdir(parents=True)
    (directory / ".outbound.lock").touch()
    (directory / ".mutation.lock").touch()
    result = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    assert result["status"] == "staged_remote"


def test_clone_preparation_does_not_require_source_identity_write(tmp_path, monkeypatch):
    root = _source(tmp_path)
    before = (root / "agents.json").read_bytes()

    def source_write(*args):
        pytest.fail("clone must not upgrade or write the source Agent configuration")

    monkeypatch.setattr(coordinator, "ensure_agent_lifecycle_id", source_write)
    prepared = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    assert prepared["status"] == "staged_remote"
    assert (root / "agents.json").read_bytes() == before


def test_rejected_clone_snapshot_is_rebuilt_after_source_configuration_fix(tmp_path):
    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["agents"][0]["active_backend"] = "invalid-backend"
    _write_json(root / "agents.json", config)
    with pytest.raises(coordinator.ClonePreparationError) as failed:
        coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    config["agents"][0]["active_backend"] = "codex-cli"
    _write_json(root / "agents.json", config)
    prepared = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    assert prepared["package_id"] != failed.value.package_id
    assert prepared["target_agent_id"] == "zelda_1"
    assert prepared["status"] == "staged_remote"


def test_slow_remote_name_resolution_does_not_hold_instance_clone_lock(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    root = _source(tmp_path)
    receiver = _Receiver()
    resolve = receiver.resolve_target_id
    entered, release = Event(), Event()

    def slow_resolve(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return resolve(*args, **kwargs)

    receiver.resolve_target_id = slow_resolve
    _install_receiver(monkeypatch, receiver)
    with ThreadPoolExecutor(max_workers=1) as pool:
        remote = pool.submit(
            coordinator.prepare_outbound_clone, root, {"hashi2": {}}, "zelda", "HASHI2",
            source_instance="HASHI1", target_agent_id="sheik",
        )
        try:
            assert entered.wait(5)
            local = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
            assert local["status"] == "staged_remote"
        finally:
            release.set()
        assert remote.result(timeout=15)["status"] == "staged_remote"


def test_successful_move_cleans_source_only_after_target_verification(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "HASHI2",
        source_instance="HASHI1",
    )
    first = coordinator.confirm_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )
    assert first["status"] == "source_disabled_target_committed"
    assert (root / "workspaces" / "zelda").exists()

    completed = coordinator.continue_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )

    assert completed["status"] == "completed"
    assert [call[0] for call in receiver.calls] == [
        "stage",
        "commit",
        "activate",
        "start",
        "finalize",
    ]
    assert [
        row["name"]
        for row in json.loads((root / "agents.json").read_text())["agents"]
    ] == ["anchor"]
    assert not (root / "workspaces" / "zelda").exists()
    assert "zelda" not in json.loads((root / "secrets.json").read_text())
    moved = json.loads(
        (root / "state" / "agent_moves" / "moved_agents.json").read_text()
    )
    assert moved["agents"]["zelda"]["target_instance"] == "HASHI2"


def test_source_cleanup_failure_never_reactivates_verified_move_source(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "HASHI2",
        source_instance="HASHI1",
    )
    coordinator.confirm_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )
    monkeypatch.setattr(
        coordinator,
        "cleanup_source_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cleanup disk error")),
    )

    with pytest.raises(AgentMoveError, match="source cleanup is pending"):
        coordinator.continue_outbound_move(
            root, {"hashi2": {}}, prepared["package_id"]
        )

    state = coordinator.get_outbound_move(root, prepared["package_id"])
    assert state["status"] == "move_completed_cleanup_pending"
    assert state["source_disabled"] is True
    assert state["target_active"] is True
    source = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source["is_active"] is False
    assert "rollback" not in [call[0] for call in receiver.calls]


def test_lost_target_start_response_stays_forward_only_until_retry(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)

    original_start = receiver.start
    lost_once = True

    def start_with_lost_response(package_id):
        nonlocal lost_once
        receiver.calls.append(("start", package_id))
        if lost_once:
            lost_once = False
            raise AgentMoveError("start response lost")
        return {"status": receiver.remote_status}

    receiver.start = start_with_lost_response
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "HASHI2",
        source_instance="HASHI1",
    )
    coordinator.confirm_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )

    with pytest.raises(AgentMoveError, match="forward retry"):
        coordinator.continue_outbound_move(
            root, {"hashi2": {}}, prepared["package_id"]
        )

    assert [call[0] for call in receiver.calls] == [
        "stage",
        "commit",
        "activate",
        "start",
    ]
    source = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source["is_active"] is False
    assert source["transfer_package_id"] == prepared["package_id"]

    receiver.start = original_start
    completed = coordinator.continue_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )
    assert completed["status"] == "completed"
    assert "stop" not in [call[0] for call in receiver.calls]
    assert "rollback" not in [call[0] for call in receiver.calls]


def test_lost_finalize_response_never_rolls_back_completed_move_target(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)

    def finalize_with_lost_response(package_id):
        receiver.calls.append(("finalize", package_id))
        receiver.remote_status = "completed"
        raise AgentMoveError("finalize response lost")

    receiver.finalize = finalize_with_lost_response
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "HASHI2",
        source_instance="HASHI1",
    )
    coordinator.confirm_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )

    with pytest.raises(AgentMoveError, match="source cleanup is pending"):
        coordinator.continue_outbound_move(
            root, {"hashi2": {}}, prepared["package_id"]
        )
    pending = coordinator.get_outbound_move(root, prepared["package_id"])
    assert pending["status"] == "move_completed_cleanup_pending"
    assert "stop" not in [call[0] for call in receiver.calls]
    assert "rollback" not in [call[0] for call in receiver.calls]

    completed = coordinator.continue_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )
    assert completed["status"] == "completed"
    assert not (root / "workspaces" / "zelda").exists()


def test_lost_finalize_response_is_reconciled_as_completed_clone(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)

    def finalize_with_lost_response(package_id):
        receiver.calls.append(("finalize", package_id))
        receiver.remote_status = "completed"
        raise AgentMoveError("finalize response lost")

    receiver.finalize = finalize_with_lost_response
    prepared = coordinator.prepare_outbound_clone(
        root,
        {"hashi2": {}},
        "zelda",
        "HASHI2",
        source_instance="HASHI1",
    )

    completed = coordinator.confirm_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )

    assert completed["status"] == "completed"
    assert completed["source_disabled"] is False
    assert "stop" not in [call[0] for call in receiver.calls]
    assert "rollback" not in [call[0] for call in receiver.calls]


@pytest.mark.parametrize("changed_kind", ["memory", "history"])
def test_confirm_does_not_revalidate_source_after_preflight(
    tmp_path, monkeypatch, changed_kind
):
    from orchestrator.session_store import SessionStore

    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"]["authorized_id"] = 7
    _write_json(root / "agents.json", config)
    store = SessionStore(root / "state" / "sessions.sqlite3", instance_id="HASHI1")
    session = store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
        transfer_mode="workspace",
    )
    if changed_kind == "memory":
        (root / "workspaces" / "zelda" / "memory.md").write_text(
            "newer durable memory", encoding="utf-8",
        )
    else:
        store.append_presentation_message(
            session_id=session["session_id"], owner_id="user:7", agent_id="zelda",
            role="assistant", text="newer durable history", source="fixture",
            idempotency_key="new-business-history", history_eligible=True,
        )

    result = coordinator.confirm_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )

    assert result["status"] == "source_disabled_target_committed"
    assert [call[0] for call in receiver.calls] == ["stage", "commit"]
    assert (
        json.loads((root / "agents.json").read_text())["agents"][0]["is_active"]
        is False
    )
    assert source_move_guard_state(root, "zelda") == {
        "status": "moved_out_pending_reboot",
        "target_instance": "HASHI2",
    }


def test_confirm_ignores_append_only_slash_audit_written_by_callback(
    tmp_path, monkeypatch
):
    from orchestrator.session_store import SessionStore

    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"]["authorized_id"] = 7
    _write_json(root / "agents.json", config)
    store = SessionStore(root / "state" / "sessions.sqlite3", instance_id="HASHI1")
    session = store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    store.append_presentation_message(
        session_id=session["session_id"], owner_id="user:7", agent_id="zelda",
        role="assistant", text="Durable business response", source="fixture",
        idempotency_key="business-response", history_eligible=True,
    )
    audit = root / "workspaces" / "zelda" / "slash_command_audit.jsonl"
    audit.write_text('{"event":"command_started"}\n', encoding="utf-8")
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
        transfer_mode="workspace",
    )

    with audit.open("a", encoding="utf-8") as handle:
        handle.write('{"event":"confirmation_callback"}\n')
    store.append_presentation_message(
        session_id=session["session_id"], owner_id="user:7", agent_id="zelda",
        role="assistant", text="Move is staged; awaiting confirmation",
        source="telegram.send", idempotency_key="move-staged",
        history_eligible=False,
    )

    result = coordinator.confirm_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )

    assert result["status"] == "source_disabled_target_committed"
    assert [call[0] for call in receiver.calls] == ["stage", "commit"]
    moved = json.loads((root / "agents.json").read_text())["agents"][0]
    assert moved["is_active"] is False


@pytest.mark.skipif(
    os.name == "nt",
    reason="agent.md and AGENT.md cannot coexist on a case-insensitive Windows tree",
)
def test_confirm_does_not_revalidate_retained_identity_after_preflight(
    tmp_path, monkeypatch
):
    root = _source(tmp_path)
    retained = root / "workspaces" / "zelda" / "AGENT.md"
    retained.write_text("historical identity one", encoding="utf-8")
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )
    retained.write_text("historical identity two", encoding="utf-8")

    result = coordinator.confirm_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )

    assert result["status"] == "source_disabled_target_committed"
    assert [call[0] for call in receiver.calls] == ["stage", "commit"]
    assert (
        json.loads((root / "agents.json").read_text())["agents"][0]["is_active"]
        is False
    )
    assert source_move_guard_state(root, "zelda") == {
        "status": "moved_out_pending_reboot",
        "target_instance": "HASHI2",
    }
