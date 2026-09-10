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


def test_activation_failure_restores_source_and_rolls_back_target(
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

    with pytest.raises(AgentMoveError, match="activation failed"):
        coordinator.continue_outbound_move(
            root, {"hashi2": {}}, prepared["package_id"]
        )

    assert [call[0] for call in receiver.calls] == [
        "stage",
        "commit",
        "activate",
        "rollback",
    ]
    source_agent = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source_agent["is_active"] is True
    assert "transfer_package_id" not in source_agent
    assert (
        json.loads((root / "tasks.json").read_text())["heartbeats"][0]["enabled"]
        is True
    )
    assert session_path.read_bytes() == original_session


def test_uncertain_target_rollback_keeps_source_disabled(tmp_path, monkeypatch):
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
    with pytest.raises(AgentMoveError, match="source remains disabled"):
        coordinator.continue_outbound_move(
            root,
            {"hashi2": {}},
            prepared["package_id"],
        )

    assert [call[0] for call in receiver.calls] == [
        "stage",
        "commit",
        "activate",
        "rollback",
    ]
    source_agent = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source_agent["is_active"] is False
    assert source_agent["transfer_package_id"] == prepared["package_id"]
    assert source_move_guard_state(root, "zelda") == {
        "status": "moved_out_pending_reboot",
        "target_instance": "HASHI2",
    }

    receiver.fail_activate = False
    receiver.fail_rollback = False
    recovered = coordinator.confirm_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )
    assert recovered["status"] == "source_disabled_target_committed"
    completed = coordinator.continue_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )
    assert completed["status"] == "completed"


def test_cancel_reconciles_uncertain_cutover_before_restoring_source(
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
    with pytest.raises(AgentMoveError):
        coordinator.continue_outbound_move(
            root,
            {"hashi2": {}},
            prepared["package_id"],
        )

    receiver.fail_rollback = False
    cancelled = coordinator.cancel_outbound_move(
        root,
        {"hashi2": {}},
        prepared["package_id"],
    )

    assert cancelled["status"] == "cancelled"
    source_agent = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source_agent["is_active"] is True
    assert "transfer_package_id" not in source_agent
    assert source_move_guard_state(root, "zelda") is None


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


def test_last_active_agent_can_be_cloned_locally_and_source_stays_unchanged(
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
    result = coordinator.confirm_outbound_move(root, {}, prepared["package_id"])

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


def test_lost_target_start_response_is_stopped_before_source_restore(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)

    def start_with_lost_response(package_id):
        receiver.calls.append(("start", package_id))
        raise AgentMoveError("start response lost")

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

    with pytest.raises(AgentMoveError, match="activation failed"):
        coordinator.continue_outbound_move(
            root, {"hashi2": {}}, prepared["package_id"]
        )

    assert [call[0] for call in receiver.calls] == [
        "stage",
        "commit",
        "activate",
        "start",
        "stop",
        "rollback",
    ]
    source = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source["is_active"] is True
    assert "transfer_package_id" not in source


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


def test_confirm_rejects_stale_source_snapshot_and_rolls_back_target(
    tmp_path, monkeypatch
):
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
    (root / "workspaces" / "zelda" / "memory.md").write_text(
        "newer durable memory",
        encoding="utf-8",
    )

    with pytest.raises(AgentMoveError, match="durable state changed"):
        coordinator.confirm_outbound_move(
            root,
            {"hashi2": {}},
            prepared["package_id"],
        )

    assert [call[0] for call in receiver.calls] == ["stage", "commit", "rollback"]
    assert (
        json.loads((root / "agents.json").read_text())["agents"][0]["is_active"]
        is True
    )
    assert source_move_guard_state(root, "zelda") is None


def test_confirm_ignores_append_only_slash_audit_written_by_callback(
    tmp_path, monkeypatch
):
    root = _source(tmp_path)
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
    )

    with audit.open("a", encoding="utf-8") as handle:
        handle.write('{"event":"confirmation_callback"}\n')

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
def test_confirm_rejects_changed_retained_identity_and_rolls_back_target(
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

    with pytest.raises(AgentMoveError, match="durable state changed"):
        coordinator.confirm_outbound_move(
            root,
            {"hashi2": {}},
            prepared["package_id"],
        )

    assert [call[0] for call in receiver.calls] == ["stage", "commit", "rollback"]
    assert (
        json.loads((root / "agents.json").read_text())["agents"][0]["is_active"]
        is True
    )
    assert source_move_guard_state(root, "zelda") is None
