from __future__ import annotations

import json
import os
import asyncio
from pathlib import Path
from types import SimpleNamespace
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
    monkeypatch.setattr(coordinator, "_local_runtime_presence", lambda _root: set(running))

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
    cancelled = coordinator.cancel_outbound_move(root, {"hashi2": {}}, state["package_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["remote_status"] == "not_submitted"
    assert receiver.calls == []
    assert (workspace / "memory.md").read_text() == "new private memory content"


def test_cancel_failed_move_reconciles_lost_stage_ack_before_rollback(tmp_path, monkeypatch):
    class LostAcknowledgement(_Receiver):
        def stage(self, package_path, *, operation="move", target_agent_id=None):
            super().stage(package_path, operation=operation, target_agent_id=target_agent_id)
            raise AgentMoveError("stage acknowledgement lost")

    root = _source(tmp_path)
    receiver = LostAcknowledgement()
    _install_receiver(monkeypatch, receiver)
    with pytest.raises(AgentMoveError, match="acknowledgement lost"):
        coordinator.prepare_outbound_move(
            root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1"
        )
    state_path = next((root / "state" / "agent_moves" / "outbound").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    assert state["status"] == "prepare_failed"
    assert state["sha256"]
    cancelled = coordinator.cancel_outbound_move(root, {"hashi2": {}}, state["package_id"])
    assert cancelled["status"] == "cancelled"
    assert receiver.remote_status == "rolled_back"
    assert [call[0] for call in receiver.calls] == ["stage", "rollback"]
    assert json.loads((root / "agents.json").read_text())["agents"][0]["is_active"] is True


@pytest.mark.parametrize("remote_status", ["committed_inactive", "unavailable"])
def test_cancel_failed_move_preserves_uncertain_or_committed_target(tmp_path, monkeypatch, remote_status):
    class LostAcknowledgement(_Receiver):
        def stage(self, package_path, *, operation="move", target_agent_id=None):
            super().stage(package_path, operation=operation, target_agent_id=target_agent_id)
            raise AgentMoveError("stage acknowledgement lost")

        def status(self, package_id):
            if remote_status == "unavailable":
                raise AgentMoveError("receiver unavailable")
            return {"status": remote_status}

    root = _source(tmp_path)
    receiver = LostAcknowledgement()
    _install_receiver(monkeypatch, receiver)
    with pytest.raises(AgentMoveError, match="acknowledgement lost"):
        coordinator.prepare_outbound_move(
            root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1"
        )
    state_path = next((root / "state" / "agent_moves" / "outbound").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    with pytest.raises(AgentMoveError, match="reconcile"):
        coordinator.cancel_outbound_move(root, {"hashi2": {}}, state["package_id"])
    assert json.loads(state_path.read_text())["status"] == "prepare_failed"
    assert [call[0] for call in receiver.calls] == ["stage"]
    assert json.loads((root / "agents.json").read_text())["agents"][0]["is_active"] is True


def test_cancel_failed_preparation_cannot_clear_another_move_quiesce(tmp_path, monkeypatch):
    from orchestrator.agent_move.source_guard import begin_source_quiesce

    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)

    def fail_package(*_args, **_kwargs):
        raise AgentMoveError("packaging failed")

    monkeypatch.setattr(coordinator, "create_agent_move_package", fail_package)
    with pytest.raises(AgentMoveError, match="packaging failed"):
        coordinator.prepare_outbound_move(
            root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1"
        )
    state_path = next((root / "state" / "agent_moves" / "outbound").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    begin_source_quiesce(root, "zelda", "another-operation", target_instance="HASHI2")
    with pytest.raises(AgentMoveError, match="another Agent move"):
        coordinator.cancel_outbound_move(root, {"hashi2": {}}, state["package_id"])
    assert source_move_guard_state(root, "zelda")["status"] == "cutover_quiesce"
    assert json.loads(state_path.read_text())["status"] == "prepare_failed"
    assert receiver.calls == []


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


def test_prepare_exposes_destination_access_scope_preview(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    original_stage = receiver.stage
    def stage_with_scope(*args, **kwargs):
        result = original_stage(*args, **kwargs)
        result["access_scope_plan"] = {
            "requested": "drive", "target_maximum": "project", "effective": "project",
        }
        result["warnings"] = ["target policy will clamp access_scope from drive to project"]
        return result
    receiver.stage = stage_with_scope
    _install_receiver(monkeypatch, receiver)

    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1",
    )
    assert prepared["access_scope_plan"]["effective"] == "project"
    assert any("drive to project" in warning for warning in prepared["warnings"])


def test_changed_access_policy_is_cancellable_before_move_commit(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    receiver.capabilities = dict(receiver.capabilities, max_access_scope="project")
    original_stage = receiver.stage
    def stage_with_scope(*args, **kwargs):
        result = original_stage(*args, **kwargs)
        result["access_scope_plan"] = {
            "requested": "drive", "target_maximum": "project", "effective": "project",
        }
        return result
    receiver.stage = stage_with_scope
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1",
    )
    receiver.capabilities["max_access_scope"] = "workspace"

    with pytest.raises(AgentMoveError, match="policy changed after preview"):
        coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert [call[0] for call in receiver.calls] == ["stage"]
    assert json.loads((root / "agents.json").read_text())["agents"][0]["is_active"] is True
    assert source_move_guard_state(root, "zelda") is None
    cancelled = coordinator.cancel_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert cancelled["status"] == "cancelled"
    assert receiver.calls[-1][0] == "rollback"


@pytest.mark.parametrize("operation", ["move", "clone"])
@pytest.mark.parametrize("lost_reply", [False, True])
def test_scope_change_after_intent_safely_releases_only_after_staged_abort(
    tmp_path, monkeypatch, operation, lost_reply,
):
    from orchestrator.agent_move.package import AccessScopePreviewChanged
    from types import SimpleNamespace
    from orchestrator.agent_move.source_guard import runtime_move_guard, scope_abort_released

    root = _source(tmp_path)

    class ScopeReceiver(_Receiver):
        capabilities = dict(_Receiver.capabilities, max_access_scope="project")
        abort_attempts = 0
        status_lost = False
        def stage(self, *args, **kwargs):
            result = super().stage(*args, **kwargs)
            result["access_scope_plan"] = {
                "requested": "drive", "target_maximum": "project", "effective": "project",
            }
            return result
        def commit(self, package_id):
            self.calls.append(("commit", package_id))
            raise AccessScopePreviewChanged("target access scope policy changed after preview")
        def abort_staged(self, package_id):
            self.calls.append(("abort_staged", package_id))
            self.abort_attempts += 1
            self.remote_status = "rolled_back"
            if lost_reply and self.abort_attempts == 1:
                self.status_lost = True
                raise AgentMoveError("abort acknowledgement lost")
            self.status_lost = False
            return {"status": "rolled_back", "staged_abort": True}
        def status(self, package_id):
            if self.status_lost:
                raise AgentMoveError("status unavailable")
            return {"status": self.remote_status, "staged_abort": self.remote_status == "rolled_back"}

    receiver = ScopeReceiver()
    _install_receiver(monkeypatch, receiver)
    prepare = coordinator.prepare_outbound_move if operation == "move" else coordinator.prepare_outbound_clone
    prepared = prepare(root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1")
    package = prepared["package_id"]
    runtime = SimpleNamespace(_agent_move_quiesced=True, _agent_move_target_instance="HASHI2",
                              _agent_move_quiesce_package_id=package)
    assert runtime_move_guard(runtime, root, "zelda")["status"] == "cutover_quiesce"
    assert runtime._agent_move_quiesced is True
    if lost_reply:
        with pytest.raises(AgentMoveError, match="recovery is pending"):
            coordinator.confirm_outbound_move(root, {"hashi2": {}}, package)
        pending = coordinator.get_outbound_move(root, package)
        assert pending["scope_abort_pending"] is True
        assert not scope_abort_released(root, "zelda", package)
        if operation == "move":
            assert source_move_guard_state(root, "zelda")["status"] == "cutover_quiesce"
        receiver.status_lost = False
    with pytest.raises(AgentMoveError, match="staged target was safely cancelled"):
        coordinator.confirm_outbound_move(root, {"hashi2": {}}, package)
    state = coordinator.get_outbound_move(root, package)
    assert state["status"] == "cancelled"
    assert state["scope_abort_target_confirmed"] is True
    assert state["rollback_completed"] is True
    assert not state["forward_only"]
    assert scope_abort_released(root, "zelda", package)
    assert not scope_abort_released(root, "other", package)
    assert source_move_guard_state(root, "zelda") is None
    assert runtime_move_guard(runtime, root, "zelda") is None
    assert runtime._agent_move_quiesced is False
    assert json.loads((root / "agents.json").read_text())["agents"][0]["is_active"] is True


def test_scope_abort_never_reverts_a_target_that_committed_in_the_race(tmp_path, monkeypatch):
    from orchestrator.agent_move.package import AccessScopePreviewChanged

    root = _source(tmp_path)
    class RacingReceiver(_Receiver):
        attempts = 0
        def commit(self, package_id):
            self.calls.append(("commit", package_id))
            self.attempts += 1
            self.remote_status = "committed_inactive"
            if self.attempts == 1:
                raise AccessScopePreviewChanged("policy changed")
            return {"status": "committed_inactive"}
        def abort_staged(self, package_id):
            self.calls.append(("abort_staged", package_id))
            raise AgentMoveError("target is already committed")
    receiver = RacingReceiver()
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1")
    package = prepared["package_id"]
    with pytest.raises(AgentMoveError, match="committed.*retry"):
        coordinator.confirm_outbound_move(root, {"hashi2": {}}, package)
    pending = coordinator.get_outbound_move(root, package)
    assert pending["forward_only"] is True
    assert not pending.get("scope_abort_pending")
    assert source_move_guard_state(root, "zelda")["status"] == "cutover_quiesce"
    result = coordinator.confirm_outbound_move(root, {"hashi2": {}}, package)
    assert result["status"] == "source_disabled_target_committed"
    assert "rollback" not in [call[0] for call in receiver.calls]


def test_unverified_scope_rejection_never_triggers_staged_abort(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver(on_commit=lambda: (_ for _ in ()).throw(
        AgentMoveError("unsigned claim of scope policy change")))
    receiver.abort_staged = lambda package_id: pytest.fail("unverified rejection must not abort")
    _install_receiver(monkeypatch, receiver)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1")
    with pytest.raises(AgentMoveError, match="retry pending"):
        coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert source_move_guard_state(root, "zelda")["status"] == "cutover_quiesce"


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


def test_move_uses_packaged_source_owner_for_cleanup_after_owner_change(tmp_path, monkeypatch):
    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text(encoding="utf-8"))
    config["global"]["owner_scope_id"] = "owner:source"
    _write_json(root / "agents.json", config)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    original = store.ensure_default_session(owner_id="owner:source", agent_id="zelda")
    accepted = store.accept_run(
        session_id=original["session_id"], owner_id="owner:source", agent_id="zelda",
        request_id="move-owner", text="original history", source="workbench",
        idempotency_key="move-owner",
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(
        accepted.request_id, success=True, assistant_text="original answer",
        assistant_source="fixture",
    )
    unrelated = store.ensure_default_session(owner_id="owner:other", agent_id="zelda")
    _install_receiver(monkeypatch, _Receiver())
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "hashi2",
        source_instance="HASHI1", transfer_mode="workspace",
    )
    config = json.loads((root / "agents.json").read_text(encoding="utf-8"))
    config["global"]["owner_scope_id"] = "owner:other"
    _write_json(root / "agents.json", config)
    coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    completed = coordinator.continue_outbound_move(
        root, {"hashi2": {}}, prepared["package_id"]
    )
    assert completed["status"] == "completed"
    assert store.get_session(original["session_id"], owner_id="owner:source")["status"] == "archived"
    assert store.get_session(unrelated["session_id"], owner_id="owner:other")["status"] == "active"


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


@pytest.mark.parametrize("change", ["reactivated", "different_transfer", "different_lifecycle"])
def test_move_does_not_activate_target_after_source_handoff_state_changes(tmp_path, monkeypatch, change):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1"
    )
    coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    path = root / "agents.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    source = next(row for row in data["agents"] if row["name"] == "zelda")
    if change == "reactivated":
        source["is_active"] = True
    elif change == "different_transfer":
        source["transfer_package_id"] = "different-transfer"
    else:
        source["agent_lifecycle_id"] = "0" * 32
    _write_json(path, data)

    with pytest.raises(AgentMoveError, match="source Agent.*changed"):
        coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])

    assert [call[0] for call in receiver.calls] == ["stage", "commit"]
    assert json.loads(path.read_text(encoding="utf-8")) == data
    assert (root / "workspaces" / "zelda" / "memory.md").is_file()


def test_move_can_continue_after_the_disabled_source_was_already_removed(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1"
    )
    coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    path = root / "agents.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agents"] = [row for row in data["agents"] if row["name"] != "zelda"]
    _write_json(path, data)

    completed = coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])

    assert completed["status"] == "completed"
    assert [call[0] for call in receiver.calls] == ["stage", "commit", "activate", "start", "finalize"]


@pytest.mark.parametrize("change", ["config", "runtime"])
def test_move_rechecks_source_after_remote_activation_before_target_start(tmp_path, monkeypatch, change):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    running = _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1"
    )
    coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    original_activate = receiver.activate

    def change_source_during_activation(package_id):
        result = original_activate(package_id)
        if change == "runtime":
            running.add("zelda")
        else:
            path = root / "agents.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            next(row for row in data["agents"] if row["name"] == "zelda")["is_active"] = True
            _write_json(path, data)
        return result

    receiver.activate = change_source_during_activation
    with pytest.raises(AgentMoveError, match="source Agent"):
        coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])

    assert [call[0] for call in receiver.calls] == ["stage", "commit", "activate"]


def test_verified_move_resumes_interrupted_source_cleanup_without_restarting_target(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1"
    )
    coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    original_cleanup = coordinator._finish_source_cleanup

    def crash_before_cleanup(*_args):
        raise SystemExit("simulated process interruption")

    monkeypatch.setattr(coordinator, "_finish_source_cleanup", crash_before_cleanup)
    with pytest.raises(SystemExit, match="simulated process interruption"):
        coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    _, state = coordinator._load_outbound(root, prepared["package_id"])
    assert state["status"] == "activating_target" and state["target_verified"] is True
    calls = list(receiver.calls)

    journal = root / "state" / "agent_moves" / "source" / (prepared["package_id"] + ".json")
    source_state = json.loads(journal.read_text(encoding="utf-8"))
    source_state["status"] = "source_cleanup_in_progress"
    _write_json(journal, source_state)
    monkeypatch.setattr(coordinator, "_finish_source_cleanup", original_cleanup)
    completed = coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])

    assert completed["status"] == "completed"
    assert receiver.calls == calls


def test_move_waits_for_source_worker_presence_even_if_not_reported_running(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1"
    )
    coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    monkeypatch.setattr(coordinator, "_local_running_agents", lambda _root: set())
    monkeypatch.setattr(coordinator, "_local_runtime_presence", lambda _root: {"zelda"})

    with pytest.raises(AgentMoveError, match="still running"):
        coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])

    assert [call[0] for call in receiver.calls] == ["stage", "commit"]


@pytest.mark.asyncio
async def test_move_of_last_active_agent_keeps_transfer_recovery_after_source_stops(
    tmp_path,
    monkeypatch,
):
    root = _source(tmp_path)
    data = json.loads((root / "agents.json").read_text())
    data["agents"] = [data["agents"][0]]
    _write_json(root / "agents.json", data)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    running = _install_local_lifecycle(monkeypatch, "zelda")

    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1"
    )
    assert prepared["status"] == "staged_remote"
    committed = coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert committed["status"] == "source_disabled_target_committed"
    with pytest.raises(AgentMoveError, match="still running"):
        coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert [call[0] for call in receiver.calls] == ["stage", "commit"]

    from types import SimpleNamespace
    from orchestrator.agent_move.manager import AgentMoveManager

    async def stop_agent(agent_id, *, reason):
        assert reason == "agent-move-cutover"
        running.remove(agent_id)
        return True, "stopped"

    manager = AgentMoveManager(SimpleNamespace(paths=SimpleNamespace(bridge_home=root), stop_agent=stop_agent))
    await manager.start()
    try:
        manager.submit(prepared["package_id"], {"hashi2": {}})
        completed = await manager.wait(prepared["package_id"])
    finally:
        await manager.stop()
    assert completed["status"] == "completed"
    assert completed["result"]["status"] == "completed"
    assert not running
    assert [call[0] for call in receiver.calls] == ["stage", "commit", "activate", "start", "finalize"]


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_case", ["included", "omitted", "changed", "archive_unavailable"])
@pytest.mark.parametrize("source_active", [False, True])
async def test_inactive_move_finishes_without_starting_either_source_or_target(tmp_path, monkeypatch, credential_case, source_active):
    from orchestrator.agent_move import service
    from orchestrator.agent_move.package import package_sha256

    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["agents"][0]["is_active"] = source_active
    _write_json(root / "agents.json", config)
    target = tmp_path / "target"
    _write_json(target / "agents.json", {"global": {"instance_id": "HASHI2"}, "agents": []})
    _write_json(target / "secrets.json", {})
    _write_json(target / "tasks.json", {"version": 1, "heartbeats": [], "crons": [], "nudges": []})

    class Receiver(_Receiver):
        def stage(self, package_path, *, operation="move", target_agent_id=None):
            self.calls.append(("stage", package_path))
            return service.stage_agent_move(target, package_path, expected_sha256=package_sha256(package_path),
                source_instance="HASHI1", target_instance="HASHI2", operation=operation,
                target_agent_id=target_agent_id, secret_passphrase="shared-secret")

        def commit(self, package_id):
            self.calls.append(("commit", package_id))
            return service.commit_agent_move(target, package_id, secret_passphrase="shared-secret")

        def finalize(self, package_id):
            self.calls.append(("finalize", package_id))
            return service.finalize_agent_move(target, package_id, runtime_online=False)

        def status(self, package_id):
            return service.get_agent_move_status(target, package_id)

    receiver = Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    create_package = coordinator.create_agent_move_package

    def create_with_selected_credentials(*args, **kwargs):
        kwargs["include_agent_secrets"] = credential_case != "omitted"
        return create_package(*args, **kwargs)

    monkeypatch.setattr(coordinator, "create_agent_move_package", create_with_selected_credentials)
    prepared = coordinator.prepare_outbound_move(root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1", desired_target_active=False)
    assert prepared["desired_target_active"] is False
    source_row = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source_row["is_active"] is source_active and "transfer_state" not in source_row
    committed = coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert committed["source_disabled"] is True
    if credential_case == "changed":
        source_secrets = json.loads((root / "secrets.json").read_text())
        source_secrets["zelda"] = "newer-token"
        _write_json(root / "secrets.json", source_secrets)
    elif credential_case == "archive_unavailable":
        directory, _ = coordinator._load_outbound(root, prepared["package_id"])
        (directory / "zelda.hashi-agent").unlink()
    from types import SimpleNamespace
    from orchestrator.agent_move.manager import AgentMoveManager

    async def stop_agent(agent_id, *, reason):
        assert agent_id == "zelda" and reason == "agent-move-cutover"
        return False, "Agent 'zelda' is not running."

    manager = AgentMoveManager(SimpleNamespace(paths=SimpleNamespace(bridge_home=root), stop_agent=stop_agent))
    await manager.start()
    try:
        manager.submit(prepared["package_id"], {"hashi2": {}})
        receipt = await manager.wait(prepared["package_id"])
    finally:
        await manager.stop()
    assert receipt["status"] == "completed"
    result = receipt["result"]
    assert result["status"] == "completed"
    assert result["target_active"] is False
    assert result["target_verification"]["config_inactive"] is True
    assert json.loads((target / "agents.json").read_text())["agents"][0]["is_active"] is False
    assert [call[0] for call in receiver.calls] == ["stage", "commit", "finalize"]
    source_secrets = json.loads((root / "secrets.json").read_text())
    target_secrets = json.loads((target / "secrets.json").read_text())
    assert ("zelda" in source_secrets) is (credential_case != "included")
    assert ("zelda" in target_secrets) is (credential_case != "omitted")
    if credential_case == "changed":
        assert source_secrets["zelda"] == "newer-token"
    assert result["preserved_source_secret_keys"] == ([] if credential_case == "included" else ["zelda"])


def test_inactive_move_does_not_cleanup_on_unverified_target_receipt(tmp_path, monkeypatch):
    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["agents"][0]["is_active"] = False
    _write_json(root / "agents.json", config)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1")
    coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    with pytest.raises(AgentMoveError, match="pending forward retry"):
        coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    source_row = json.loads((root / "agents.json").read_text())["agents"][0]
    assert source_row["transfer_state"] == "moved_out_pending_reboot"
    assert (root / "workspaces" / "zelda").is_dir()
    assert [call[0] for call in receiver.calls] == ["stage", "commit", "finalize"]


def test_inactive_move_presence_requires_the_complete_runtime_list(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "_local_workbench_json", lambda *_args: {"agents": []})
    with pytest.raises(AgentMoveError, match="runtime presence is unavailable"):
        coordinator._local_runtime_presence(tmp_path)
    monkeypatch.setattr(coordinator, "_local_workbench_json", lambda *_args: {
        "agents": [], "agent_runtime_presence": ["starting", "failed"]})
    assert coordinator._local_runtime_presence(tmp_path) == {"starting", "failed"}


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
async def test_telegram_delegate_local_clone_keeps_only_its_own_history(tmp_path, monkeypatch):
    from orchestrator.telegram_actor_scope import (
        _delegated_owner, process_telegram_update, runtime_snapshot,
    )

    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"].update(
        owner_scope_id="user:7",
        telegram_owner_id=7,
        authorized_telegram_user_ids=[100],
    )
    config["agents"][0]["is_active"] = False
    _write_json(root / "agents.json", config)
    runtime = SimpleNamespace(
        name="zelda",
        global_config=SimpleNamespace(
            bridge_home=root, config_path=root / "agents.json",
            secrets_path=root / "secrets.json", instance_id="HASHI1",
            owner_scope_id="user:7", authorized_id=7, deployment_profile="personal",
        ),
    )
    delegate = _delegated_owner(runtime_snapshot(runtime), 100)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    for owner, message in (("user:7", "primary private"), (delegate, "delegate private")):
        session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
        accepted = store.accept_run(
            session_id=session["session_id"], owner_id=owner, agent_id="zelda",
            request_id=message, text=message, source="telegram", idempotency_key=message,
        )
        store.mark_request_running(accepted.request_id, worker_id="fixture")
        store.finish_request(
            accepted.request_id, success=True, assistant_text=message + " answer",
            assistant_source="fixture",
        )

    results = {}
    release = asyncio.Event()
    pending = []

    class App:
        async def process_update(self, _update):
            prepared = await asyncio.to_thread(
                coordinator.prepare_outbound_clone, root, {}, "zelda", "HASHI1",
                source_instance="HASHI1", history_mode="copy", desired_target_active=False,
            )
            directory, outbound = coordinator._load_outbound(root, prepared["package_id"])
            package = coordinator.read_agent_move_package(directory / "zelda.hashi-agent", verify=True)
            incoming = json.loads((
                root / "state/agent_moves/incoming" / prepared["package_id"] / "state.json"
            ).read_text())
            results["source_owner"] = outbound["history_source_owner_id"]
            results["package"] = package.conversation_continuity
            results["target_owner"] = incoming["conversation_target_owner_id"]

            async def finish_from_authenticated_journal():
                await release.wait()
                return await asyncio.to_thread(
                    coordinator.confirm_outbound_move, root, {}, prepared["package_id"],
                )

            pending.append(asyncio.create_task(finish_from_authenticated_journal()))

    runtime.app = App()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=100),
        effective_chat=SimpleNamespace(id=100, type="private"),
    )
    _install_local_lifecycle(monkeypatch)
    assert await process_telegram_update(runtime, update)
    release.set()
    results["completed"] = await pending[0]
    assert results["source_owner"] == results["target_owner"] == delegate
    assert "delegate private" in json.dumps(results["package"])
    assert "primary private" not in json.dumps(results["package"])
    assert results["completed"]["status"] == "completed"
    sessions = store.list_sessions(owner_id=delegate, agent_id="zelda_1")
    assert len(sessions) == 1
    assert [m["text"] for m in store.messages(sessions[0]["session_id"], owner_id=delegate)] == [
        "delegate private", "delegate private answer",
    ]
    assert store.list_sessions(owner_id="user:7", agent_id="zelda_1") == []


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


def test_clone_history_retry_ignores_legacy_config_after_data_binding(tmp_path):
    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"]["owner_scope_id"] = "owner:one"
    _write_json(root / "agents.json", config)
    first = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1", history_mode="copy",
    )
    repeated = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1", history_mode="copy",
    )
    assert repeated["package_id"] == first["package_id"]

    # Once the instance has a stable data binding, a legacy configuration
    # edit cannot silently select a different source user's history.
    config["global"]["owner_scope_id"] = "owner:two"
    _write_json(root / "agents.json", config)
    second = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1", history_mode="copy",
    )
    assert second["package_id"] == first["package_id"]
    assert second["target_agent_id"] == first["target_agent_id"]
    directory, state = coordinator._load_outbound(root, second["package_id"])
    assert state["history_source_owner_id"] == "owner:one"
    package = coordinator.read_agent_move_package(directory / "zelda.hashi-agent", verify=True)
    assert package.conversation_continuity["owner_id"] == "owner:one"


@pytest.mark.asyncio
async def test_frontend_clone_preview_and_retry_use_bound_owner_across_thread(tmp_path, monkeypatch):
    from orchestrator.frontend_command_context import bind_frontend_command_principal

    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"]["owner_scope_id"] = "owner:legacy"
    _write_json(root / "agents.json", config)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    for owner, text in (("owner:one", "history-one"), ("owner:two", "history-two")):
        session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
        accepted = store.accept_run(session_id=session["session_id"], owner_id=owner,
                                    agent_id="zelda", request_id=text, text=text,
                                    source="workbench", idempotency_key=text)
        store.mark_request_running(accepted.request_id, worker_id="fixture")
        store.finish_request(accepted.request_id, success=True, assistant_text=text + " answer",
                             assistant_source="fixture")
    _install_receiver(monkeypatch, _Receiver())
    runtime = SimpleNamespace(global_config=SimpleNamespace(
        project_root=root, instance_id="HASHI1", deployment_profile="personal"))
    args = (root, {"hashi2": {}}, "zelda", "HASHI2")
    kwargs = {"source_instance": "HASHI1", "history_mode": "copy"}

    with bind_frontend_command_principal(runtime, "owner:one"):
        preview = await asyncio.to_thread(coordinator.preview_outbound_clone, *args, **kwargs)
        first = await asyncio.to_thread(coordinator.prepare_outbound_clone, *args, **kwargs)
        repeated = await asyncio.to_thread(coordinator.prepare_outbound_clone, *args, **kwargs)
    assert preview["conversation_continuity_summary"]["eligible_message_count"] == 2
    assert repeated["package_id"] == first["package_id"]

    with bind_frontend_command_principal(runtime, "owner:two"):
        second = await asyncio.to_thread(coordinator.prepare_outbound_clone, *args, **kwargs)
    assert second["package_id"] != first["package_id"]
    for result, owner, expected, foreign in ((first, "owner:one", "history-one", "history-two"),
                                              (second, "owner:two", "history-two", "history-one")):
        directory, state = coordinator._load_outbound(root, result["package_id"])
        package = coordinator.read_agent_move_package(directory / "zelda.hashi-agent", verify=True)
        assert state["history_source_owner_id"] == owner
        assert package.conversation_continuity["owner_id"] == owner
        assert expected in json.dumps(package.conversation_continuity)
        assert foreign not in json.dumps(package.conversation_continuity)


def test_legacy_clone_history_retry_recovers_owner_from_verified_package(tmp_path):
    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"]["owner_scope_id"] = "owner:one"
    _write_json(root / "agents.json", config)
    first = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1", history_mode="copy",
    )
    directory, state = coordinator._load_outbound(root, first["package_id"])
    state.pop("history_source_owner_id")
    _write_json(directory / "state.json", state)
    repeated = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1", history_mode="copy",
    )
    assert repeated["package_id"] == first["package_id"]
    assert repeated["reused"] is True


def test_history_free_clone_retry_is_not_partitioned_by_owner(tmp_path):
    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"]["owner_scope_id"] = "owner:one"
    _write_json(root / "agents.json", config)
    first = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    config["global"]["owner_scope_id"] = "owner:two"
    _write_json(root / "agents.json", config)
    repeated = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", source_instance="HASHI1")
    assert repeated["package_id"] == first["package_id"]


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
    retained = Path(completed["retained_source_workspace"])
    assert (retained / "memory.md").read_text() == "durable"
    assert completed["source_workspace_retention"]["status"] == "preserved"
    assert "zelda" not in json.loads((root / "secrets.json").read_text())
    moved = json.loads(
        (root / "state" / "agent_moves" / "moved_agents.json").read_text()
    )
    assert moved["agents"]["zelda"]["target_instance"] == "HASHI2"


def test_move_preserves_source_schedules_changed_after_packaging(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1"
    )
    coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    tasks = json.loads((root / "tasks.json").read_text())
    tasks["heartbeats"][0]["interval_seconds"] = 91
    added = {"id": "later", "agent": "zelda", "enabled": True, "schedule": "0 1 * * *"}
    tasks["crons"].append(added)
    _write_json(root / "tasks.json", tasks)

    completed = coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])

    assert completed["status"] == "completed"
    assert completed["preserved_source_schedule_ids"] == {
        "heartbeats": ["heartbeat"], "crons": ["later"], "nudges": []
    }
    remaining = json.loads((root / "tasks.json").read_text())
    assert remaining["heartbeats"][0]["interval_seconds"] == 91
    assert remaining["heartbeats"][0]["enabled"] is False
    assert remaining["crons"][0]["enabled"] is False
    journal = json.loads(
        (root / "state" / "agent_moves" / "source" / f"{prepared['package_id']}.json").read_text()
    )
    assert journal["preserved_source_schedules"]["heartbeats"][0]["interval_seconds"] == 91
    assert journal["preserved_source_schedules"]["crons"] == [added]


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
# HASHI-Fingerprint: hfp_df95428a15e3499d91f855d20f532ccb



def test_move_retain_source_policy_survives_preview_prepare_and_cutover(tmp_path, monkeypatch):
    root = _source(tmp_path)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    session = store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    store.append_presentation_message(
        session_id=session["session_id"], owner_id="user:7", agent_id="zelda",
        role="assistant", text="history left on source", source="fixture",
        idempotency_key="retained-history", history_eligible=True,
    )
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    kwargs = dict(source_instance="HASHI1", transfer_mode="workspace", history_mode="retain_source")
    preview = coordinator.preview_outbound_move(root, {"hashi2": {}}, "zelda", "HASHI2", **kwargs)
    assert preview["history_mode"] == "retain_source"
    prepared = coordinator.prepare_outbound_move(root, {"hashi2": {}}, "zelda", "HASHI2", **kwargs)
    assert prepared["history_mode"] == "retain_source"
    assert prepared["history_source_owner_id"] is None
    first = coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert first["status"] == "source_disabled_target_committed"
    assert first["history_mode"] == "retain_source"
    completed = coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert completed["status"] == "completed"
    assert completed["history_mode"] == "retain_source"
    assert store.get_session(session["session_id"], owner_id="user:7")["status"] == "archived"
    assert [item["text"] for item in store.messages(session["session_id"], owner_id="user:7")] == [
        "history left on source"
    ]
    fresh = store.resolve_primary_session(owner_id="user:7", agent_id="zelda", establish=True)
    assert fresh["session_id"] != session["session_id"]

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "first_owner", "second_owner"),
    [
        ("personal", "owner:one", "owner:two"),
        ("enterprise", "enterprise:one", "enterprise:two"),
    ],
)
async def test_local_frontend_clone_keeps_authenticated_owner_through_later_commit(
    tmp_path, monkeypatch, profile, first_owner, second_owner
):
    from orchestrator.agent_move import service
    from orchestrator.agent_move.manager import AgentMoveManager
    from orchestrator.frontend_command_context import bind_frontend_command_principal

    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text())
    config["global"]["owner_scope_id"] = "owner:legacy"
    config["global"]["deployment_profile"] = profile
    if profile != "personal":
        config["global"]["organization_id"] = "test-org"
    _write_json(root / "agents.json", config)
    _install_local_lifecycle(monkeypatch)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    for owner, message in ((first_owner, "one-private"), (second_owner, "two-private")):
        session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
        accepted = store.accept_run(
            session_id=session["session_id"], owner_id=owner, agent_id="zelda",
            request_id=message, text=message, source="workbench", idempotency_key=message,
        )
        store.mark_request_running(accepted.request_id, worker_id="fixture")
        store.finish_request(
            accepted.request_id, success=True, assistant_text=message + " answer",
            assistant_source="fixture",
        )

    runtime = SimpleNamespace(global_config=SimpleNamespace(
        project_root=root, instance_id="HASHI1", deployment_profile=profile
    ))
    with bind_frontend_command_principal(runtime, first_owner):
        prepared = await asyncio.to_thread(
            coordinator.prepare_outbound_clone, root, {}, "zelda", "HASHI1",
            source_instance="HASHI1", history_mode="copy",
        )
    _, staged = service._load_record(root, prepared["package_id"])
    assert staged["conversation_owner_binding"]["target_owner_id"] == first_owner

    with bind_frontend_command_principal(runtime, second_owner):
        with pytest.raises(AgentMoveError, match="authenticated.*owner"):
            service.commit_agent_move(
                root, prepared["package_id"],
                secret_passphrase=coordinator._transfer_passphrase(root, local=True, persist=False),
            )
    assert not store.list_sessions(owner_id=second_owner, agent_id="zelda_1")

    manager = AgentMoveManager(SimpleNamespace(paths=SimpleNamespace(bridge_home=root)))
    await manager.start()
    try:
        manager.submit(prepared["package_id"], {})
        receipt = await manager.wait(prepared["package_id"])
    finally:
        await manager.stop()
    assert receipt["status"] == "completed"
    sessions = store.list_sessions(owner_id=first_owner, agent_id="zelda_1")
    assert len(sessions) == 1
    messages = store.messages(sessions[0]["session_id"], owner_id=first_owner)
    assert [message["text"] for message in messages] == ["one-private", "one-private answer"]
    assert store.list_sessions(owner_id="owner:legacy", agent_id="zelda_1") == []
    assert store.list_sessions(owner_id=second_owner, agent_id="zelda_1") == []


def test_identity_move_reports_and_retains_pending_source_delivery(tmp_path, monkeypatch):
    from orchestrator.telegram_delivery_state import telegram_bot_fingerprint

    root = _source(tmp_path)
    lifecycle_id = "4" * 32
    config = json.loads((root / "agents.json").read_text())
    config["agents"][0]["agent_lifecycle_id"] = lifecycle_id
    _write_json(root / "agents.json", config)
    workspace = root / "workspaces" / "zelda" / "undelivered"
    workspace.mkdir()
    (workspace / "req-owned.md").write_text("source-only response", encoding="utf-8")
    _write_json(workspace / "req-owned.json", {"request_id": "req-owned", "chat_id": 123})
    delivery = {
        "owner": {"instance_id": "HASHI1", "agent_lifecycle_id": lifecycle_id,
                  "telegram_bot_fingerprint": telegram_bot_fingerprint("telegram-token")},
        "status": "blocked", "per_chat": {"123": {"undelivered_request_ids": ["req-owned"]}},
    }
    _write_json(root / "state" / "telegram_delivery_health.json",
                {"version": 2, "agents": {"zelda": delivery}, "quarantine": []})
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    _install_local_lifecycle(monkeypatch)
    preview = coordinator.preview_outbound_move(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1",
        transfer_mode="identity_memory", history_mode="retain_source",
    )
    assert preview["source_retained_pending_count"] == 1
    prepared = coordinator.prepare_outbound_move(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1",
        transfer_mode="identity_memory", history_mode="retain_source",
    )
    assert prepared["source_retained_pending_count"] == 1
    assert any("undelivered" in warning for warning in prepared["warnings"])
    coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    completed = coordinator.continue_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert completed["status"] == "completed"
    assert completed["source_retained_pending_count"] == 1
    assert "retained_source_delivery" not in completed
    retained = Path(completed["retained_source_workspace"])
    assert (retained / "undelivered" / "req-owned.md").read_text() == "source-only response"
    journal = json.loads((root / "state" / "agent_moves" / "source" /
                          f'{prepared["package_id"]}.json').read_text())
    assert journal["retained_source_delivery"]["record"]["owner"] == delivery["owner"]
    assert journal["retained_source_delivery"]["record"]["per_chat"] == delivery["per_chat"]


def test_inactive_clone_backup_finishes_without_starting_target(tmp_path, monkeypatch):
    from orchestrator.agent_move.package import decrypt_agent_secrets, read_agent_move_package

    root = _source(tmp_path)

    class InactiveReceiver(_Receiver):
        def stage(self, package_path, *, operation="clone", target_agent_id=None):
            package = read_agent_move_package(package_path)
            assert package.manifest["desired_target_active"] is False
            assert package.manifest["retain_telegram_backup"] is True
            assert decrypt_agent_secrets(package, "shared-secret")["zelda"] == "telegram-token"
            return super().stage(package_path, operation=operation, target_agent_id=target_agent_id)

        def finalize(self, package_id):
            self.calls.append(("finalize", package_id))
            self.remote_status = "completed"
            return {"status": "completed", "desired_target_active": False,
                    "retained_telegram_backup": {"status": "quarantined",
                        "target_agent_id": self.target_agent_id, "live_connector": False},
                    "target_verified": True, "target_verification": {
                        "runtime_online": False, "runtime_present": False,
                        "presence_checked": True, "desired_active": False,
                        "config_inactive": True, "telegram_backup_recoverable": True,
                    }}

    receiver = InactiveReceiver()
    _install_receiver(monkeypatch, receiver)
    preview = coordinator.preview_outbound_clone(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1",
        desired_target_active=False, retain_telegram_backup=True,
    )
    assert preview["desired_target_active"] is False
    assert preview["retain_telegram_backup"] is True
    prepared = coordinator.prepare_outbound_clone(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1",
        desired_target_active=False, retain_telegram_backup=True,
    )
    assert prepared["desired_target_active"] is False
    assert prepared["retain_telegram_backup"] is True
    completed = coordinator.confirm_outbound_move(root, {"hashi2": {}}, prepared["package_id"])
    assert completed["status"] == "completed"
    assert completed["target_active"] is False
    assert preview["telegram_backup_present"] is True
    assert prepared["telegram_backup_present"] is True
    assert completed["telegram_backup_confirmed"] is True
    assert [call[0] for call in receiver.calls] == ["stage", "commit", "finalize"]
    assert json.loads((root / "agents.json").read_text())["agents"][0]["is_active"] is True


def test_pending_clone_intent_distinguishes_active_from_inactive_backup(tmp_path, monkeypatch):
    root = _source(tmp_path)
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    first = coordinator.prepare_outbound_clone(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1",
    )
    second = coordinator.prepare_outbound_clone(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1",
        desired_target_active=False, retain_telegram_backup=True,
    )
    assert first["package_id"] != second["package_id"]
    repeat = coordinator.prepare_outbound_clone(
        root, {"hashi2": {}}, "zelda", "HASHI2", source_instance="HASHI1",
        desired_target_active=False, retain_telegram_backup=True,
    )
    assert repeat["package_id"] == second["package_id"]


def test_inactive_clone_requires_target_backup_receipt_before_success():
    state = {"operation": "clone", "desired_target_active": False,
             "retain_telegram_backup": True, "telegram_backup_present": True,
             "target_agent_id": "zelda_copy"}
    target = {"status": "completed", "desired_target_active": False, "target_verified": True,
              "target_verification": {"runtime_online": False, "runtime_present": False,
                  "presence_checked": True, "desired_active": False, "config_inactive": True}}
    assert coordinator._move_target_verified(state, target) is False
    target["retained_telegram_backup"] = {"status": "quarantined",
        "target_agent_id": "another", "live_connector": False}
    assert coordinator._move_target_verified(state, target) is False
    target["retained_telegram_backup"]["target_agent_id"] = "zelda_copy"
    target["retained_telegram_backup"]["live_connector"] = True
    assert coordinator._move_target_verified(state, target) is False
    target["retained_telegram_backup"]["live_connector"] = False
    assert coordinator._move_target_verified(state, target) is False
    target["target_verification"]["telegram_backup_recoverable"] = True
    assert coordinator._move_target_verified(state, target) is True


def test_local_inactive_backup_clone_uses_real_receiver_and_keeps_source_running(tmp_path, monkeypatch):
    from orchestrator.agent_move.package import _decrypt_json
    from orchestrator.agent_move.service import _recover_quarantined_telegram_token, get_agent_move_status

    root = _source(tmp_path)
    running = _install_local_lifecycle(monkeypatch, "zelda")
    prepared = coordinator.prepare_outbound_clone(
        root, {}, "zelda", "HASHI1", source_instance="HASHI1",
        desired_target_active=False, retain_telegram_backup=True,
    )
    completed = coordinator.confirm_outbound_move(root, {}, prepared["package_id"])
    assert completed["status"] == "completed"
    assert completed["target_active"] is False
    assert completed["telegram_backup_confirmed"] is True
    target = completed["target_agent_id"]
    assert running == {"zelda"}
    state = get_agent_move_status(root, prepared["package_id"])
    backup = state["retained_telegram_backup"]
    blob = root / backup["transport_backup_path"]
    assert _decrypt_json(blob.read_bytes(), "shared-secret")["zelda"] == "telegram-token"
    assert _recover_quarantined_telegram_token(root, prepared["package_id"]) == "telegram-token"
    secrets = json.loads((root / "secrets.json").read_text())
    assert target not in secrets and secrets["zelda"] == "telegram-token"
    rows = {row["name"]: row for row in json.loads((root / "agents.json").read_text())["agents"]}
    assert rows["zelda"]["is_active"] is True and rows[target]["is_active"] is False


def test_preview_and_prepare_use_receiver_archive_member_budget(tmp_path, monkeypatch):
    from orchestrator.agent_move import package as owner

    root = _source(tmp_path)
    for index in range(6):
        (root / "workspaces/zelda" / f"file-{index}.txt").write_text(str(index))
    monkeypatch.setattr(owner, "MAX_ARCHIVE_MEMBERS", 8)
    receiver = _Receiver()
    receiver.capabilities = dict(receiver.capabilities, max_archive_members=64)
    _install_receiver(monkeypatch, receiver)
    preview = coordinator.preview_outbound_clone(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1",
        transfer_mode="workspace", history_mode="none",
    )
    assert preview["receiver"]["max_archive_members"] == 64
    prepared = coordinator.prepare_outbound_clone(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1",
        transfer_mode="workspace", history_mode="none",
    )
    assert prepared["status"] == "staged_remote"
    state = json.loads((root / "state" / "agent_moves" / "outbound" /
                        prepared["package_id"] / "state.json").read_text())
    assert state["max_archive_members"] == 64
    assert [name for name, *_ in receiver.calls] == ["stage"]

def test_preview_and_prepare_use_receiver_expanded_budget(tmp_path, monkeypatch):
    from orchestrator.agent_move import package as owner

    root = _source(tmp_path)
    (root / "workspaces/zelda/big.txt").write_text("synthetic data " * 1024)
    monkeypatch.setattr(owner, "MAX_UNPACKED_BYTES", 2048)
    receiver = _Receiver()
    receiver.capabilities = dict(receiver.capabilities, max_unpacked_bytes=128 * 1024)
    _install_receiver(monkeypatch, receiver)
    preview = coordinator.preview_outbound_clone(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1",
        transfer_mode="workspace", history_mode="none",
    )
    assert preview["receiver"]["max_unpacked_bytes"] == 128 * 1024
    prepared = coordinator.prepare_outbound_clone(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1",
        transfer_mode="workspace", history_mode="none",
    )
    state = json.loads((root / "state/agent_moves/outbound" /
                        prepared["package_id"] / "state.json").read_text())
    assert state["max_unpacked_bytes"] == 128 * 1024
    assert [name for name, *_ in receiver.calls] == ["stage"]

def test_preview_and_prepare_use_receiver_workspace_budget(tmp_path, monkeypatch):
    from orchestrator.agent_move import package as owner

    root = _source(tmp_path)
    (root / "workspaces/zelda/big.txt").write_text("synthetic work " * 1024)
    monkeypatch.setattr(owner, "WORKSPACE_LIMIT_BYTES", 1024)
    receiver = _Receiver()
    receiver.capabilities = dict(receiver.capabilities, max_workspace_bytes=128 * 1024)
    _install_receiver(monkeypatch, receiver)
    preview = coordinator.preview_outbound_clone(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1",
        transfer_mode="workspace", history_mode="none",
    )
    assert preview["receiver"]["max_workspace_bytes"] == 128 * 1024
    prepared = coordinator.prepare_outbound_clone(
        root, {"hashi2": {}}, "zelda", "hashi2", source_instance="HASHI1",
        transfer_mode="workspace", history_mode="none",
    )
    state = json.loads((root / "state/agent_moves/outbound" /
                        prepared["package_id"] / "state.json").read_text())
    assert state["max_workspace_bytes"] == 128 * 1024
    assert [name for name, *_ in receiver.calls] == ["stage"]



@pytest.mark.parametrize("history_mode", ["copy", "inherit_read_only"])
def test_personal_clone_with_unidentified_old_history_keeps_source(
    tmp_path, monkeypatch, history_mode,
):
    from orchestrator.agent_move import service

    root = _source(tmp_path)
    _install_local_lifecycle(monkeypatch)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    legacy = store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    store.append_presentation_message(
        session_id=legacy["session_id"], owner_id="user:7", agent_id="zelda",
        role="assistant", text="unidentified historical conversation",
        source="fixture", idempotency_key="legacy-history", history_eligible=True,
    )

    kwargs = dict(source_instance="HASHI1", transfer_mode="identity_memory",
                  history_mode=history_mode)
    preview = coordinator.preview_outbound_clone(root, {}, "zelda", "HASHI1", **kwargs)
    assert preview["conversation_continuity_summary"]["eligible_message_count"] == 0
    assert any("other local conversation history" in warning for warning in preview["warnings"])
    prepared = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", **kwargs)
    assert any("other local conversation history" in warning for warning in prepared["warnings"])
    committed = service.commit_agent_move(
        root, prepared["package_id"],
        secret_passphrase=coordinator._transfer_passphrase(root, local=True, persist=False),
    )

    assert committed["conversation_continuity"]["imported_messages"] == 0
    assert store.list_sessions(owner_id="user:7", agent_id=prepared["target_agent_id"]) == []
    assert [row["text"] for row in store.messages(
        legacy["session_id"], owner_id="user:7",
    )] == ["unidentified historical conversation"]



def test_personal_clone_exports_only_selected_history_with_other_scopes(tmp_path, monkeypatch):
    from orchestrator.agent_move import service

    root = _source(tmp_path)
    _install_local_lifecycle(monkeypatch)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    for owner, text in (("user:0", "selected history"), ("user:7", "other history")):
        session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
        store.append_presentation_message(
            session_id=session["session_id"], owner_id=owner, agent_id="zelda",
            role="assistant", text=text, source="fixture", idempotency_key=owner,
            history_eligible=True,
        )
    kwargs = dict(source_instance="HASHI1", transfer_mode="identity_memory", history_mode="copy")

    preview = coordinator.preview_outbound_clone(root, {}, "zelda", "HASHI1", **kwargs)
    prepared = coordinator.prepare_outbound_clone(root, {}, "zelda", "HASHI1", **kwargs)
    committed = service.commit_agent_move(
        root, prepared["package_id"],
        secret_passphrase=coordinator._transfer_passphrase(root, local=True, persist=False),
    )

    assert preview["conversation_continuity_summary"]["eligible_message_count"] == 1
    assert committed["conversation_continuity"]["imported_messages"] == 1
    sessions = store.list_sessions(owner_id="user:0", agent_id=prepared["target_agent_id"])
    assert len(sessions) == 1
    assert [row["text"] for row in store.messages(
        sessions[0]["session_id"], owner_id="user:0",
    )] == ["selected history"]
    assert store.list_sessions(owner_id="user:7", agent_id=prepared["target_agent_id"]) == []


@pytest.mark.parametrize("profile", ["team", "enterprise"])
def test_governed_clone_does_not_infer_an_unknown_history_owner(tmp_path, profile):
    root = _source(tmp_path)
    config = json.loads((root / "agents.json").read_text(encoding="utf-8"))
    config["global"].update(deployment_profile=profile, organization_id="test-org")
    _write_json(root / "agents.json", config)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    foreign = store.ensure_default_session(owner_id="enterprise:other", agent_id="zelda")
    store.append_presentation_message(
        session_id=foreign["session_id"], owner_id="enterprise:other", agent_id="zelda",
        role="assistant", text="other user's history", source="fixture",
        idempotency_key="foreign", history_eligible=True,
    )

    with pytest.raises(AgentMoveError, match="authorized conversation owner is not configured"):
        coordinator.preview_outbound_clone(
            root, {}, "zelda", "HASHI1", source_instance="HASHI1",
            transfer_mode="identity_memory", history_mode="copy",
        )



def test_personal_move_with_empty_selected_history_preserves_other_owners(tmp_path, monkeypatch):
    from orchestrator.agent_move import service
    from orchestrator.agent_move.package import read_agent_move_package

    root = _source(tmp_path)
    store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI1")
    legacy = store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    store.append_presentation_message(
        session_id=legacy["session_id"], owner_id="user:7", agent_id="zelda",
        role="assistant", text="unidentified old conversation",
        source="fixture", idempotency_key="legacy-move", history_eligible=True,
    )
    receiver = _Receiver()
    _install_receiver(monkeypatch, receiver)
    kwargs = dict(source_instance="HASHI1", transfer_mode="identity_memory", history_mode="move")

    preview = coordinator.preview_outbound_move(root, {"hashi2": {}}, "zelda", "hashi2", **kwargs)
    assert preview["conversation_continuity_summary"]["eligible_message_count"] == 0
    assert any("other local conversation history" in warning for warning in preview["warnings"])

    prepared = coordinator.prepare_outbound_move(root, {"hashi2": {}}, "zelda", "hashi2", **kwargs)
    assert any("other local conversation history" in warning for warning in prepared["warnings"])
    archive = read_agent_move_package(receiver.calls[-1][1], verify=True)
    assert archive.conversation_continuity["owner_id"] == "user:0"
    assert archive.conversation_continuity["summary"]["eligible_message_count"] == 0

    service.deactivate_source_agent(
        root, "zelda", prepared["package_id"], target_instance="HASHI2",
        history_mode="move", history_source_owner_id="user:0",
    )
    assert [row["text"] for row in store.messages(
        legacy["session_id"], owner_id="user:7",
    )] == ["unidentified old conversation"]
    cleaned = service.cleanup_source_agent(
        root, prepared["package_id"], source_secret_keys=[],
        target_instance="HASHI2", target_agent_id="zelda",
    )
    assert cleaned["status"] == "source_cleaned"
    assert cleaned["conversation_history_retirement"]["session_ids"] == []
    assert store.get_session(legacy["session_id"], owner_id="user:7")["status"] == "active"
    assert [row["text"] for row in store.messages(
        legacy["session_id"], owner_id="user:7",
    )] == ["unidentified old conversation"]
