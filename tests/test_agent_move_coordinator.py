from __future__ import annotations

import json
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
                }
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

    def stage(self, package_path):
        self.calls.append(("stage", Path(package_path)))
        return {
            "status": "staged",
            "credential_status": {"missing_keys": []},
            "warnings": [],
        }

    def commit(self, package_id):
        self.calls.append(("commit", package_id))
        if self.on_commit is not None:
            self.on_commit()
        return {
            "status": "committed_inactive",
            "credential_status": {"missing_keys": []},
        }

    def activate(self, package_id):
        self.calls.append(("activate", package_id))
        if self.fail_activate:
            raise AgentMoveError("activation refused")
        return {"status": "activated_pending_reboot"}

    def rollback(self, package_id):
        self.calls.append(("rollback", package_id))
        if self.fail_rollback:
            raise AgentMoveError("rollback response unavailable")
        return {"status": "rolled_back"}


def _install_receiver(monkeypatch, receiver):
    monkeypatch.setattr(
        coordinator,
        "connect_agent_move_receiver",
        lambda *args, **kwargs: receiver,
    )


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

    assert [call[0] for call in receiver.calls] == ["stage", "commit", "activate"]
    assert commit_guards == [
        {"status": "cutover_quiesce", "target_instance": "HASHI2"}
    ]
    assert result["status"] == "moved_pending_reboots"
    assert result["reboot_order"] == ["HASHI1", "HASHI2"]
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
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )

    with pytest.raises(AgentMoveError, match="cutover failed"):
        coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])

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
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )

    with pytest.raises(AgentMoveError, match="source remains disabled"):
        coordinator.confirm_outbound_move(
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
    assert recovered["status"] == "moved_pending_reboots"


def test_cancel_reconciles_uncertain_cutover_before_restoring_source(
    tmp_path, monkeypatch
):
    root = _source(tmp_path)
    receiver = _Receiver(fail_activate=True, fail_rollback=True)
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root,
        {"hashi2": {}},
        "zelda",
        "hashi2",
        source_instance="HASHI1",
    )
    with pytest.raises(AgentMoveError):
        coordinator.confirm_outbound_move(
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

    assert result["status"] == "moved_pending_reboots"
    assert [call[0] for call in receiver.calls] == ["stage", "commit", "activate"]


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
