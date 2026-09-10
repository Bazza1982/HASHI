from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from orchestrator.agent_move import manager as move_manager


def _kernel(tmp_path):
    stopped: list[tuple[str, str]] = []

    async def stop_agent(agent_name: str, reason: str = ""):
        stopped.append((agent_name, reason))
        return True, "stopped"

    return (
        SimpleNamespace(
            paths=SimpleNamespace(bridge_home=tmp_path),
            stop_agent=stop_agent,
        ),
        stopped,
    )


@pytest.mark.asyncio
async def test_one_confirmation_finishes_move_after_source_worker_stops(
    tmp_path, monkeypatch
):
    kernel, stopped = _kernel(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "move-1",
            "agent_id": "self-agent",
            "operation": "move",
            "status": "staged_remote",
        },
    )
    monkeypatch.setattr(
        move_manager,
        "confirm_outbound_move",
        lambda *_args: calls.append("confirm")
        or {
            "package_id": "move-1",
            "agent_id": "self-agent",
            "operation": "move",
            "status": "source_disabled_target_committed",
        },
    )
    monkeypatch.setattr(
        move_manager,
        "continue_outbound_move",
        lambda *_args: calls.append("continue")
        or {
            "package_id": "move-1",
            "agent_id": "self-agent",
            "operation": "move",
            "status": "completed",
        },
    )

    manager = move_manager.AgentMoveManager(kernel)
    accepted = manager.submit("move-1", {"hashi2": {"instance_id": "HASHI2"}})

    assert accepted["accepted"] is True
    assert stopped == []  # submit returns before a self-moving Worker is stopped
    result = await manager.wait("move-1")
    assert result["status"] == "completed"
    assert calls == ["confirm", "continue"]
    assert stopped == [("self-agent", "agent-move-cutover")]


@pytest.mark.asyncio
async def test_duplicate_confirmation_is_idempotent(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "clone-1",
            "agent_id": "source",
            "operation": "clone",
            "status": "staged_remote",
        },
    )
    monkeypatch.setattr(
        move_manager,
        "confirm_outbound_move",
        lambda *_args: calls.append("confirm")
        or {
            "package_id": "clone-1",
            "agent_id": "source",
            "operation": "clone",
            "status": "completed",
            "source_disabled": False,
        },
    )

    manager = move_manager.AgentMoveManager(kernel)
    first = manager.submit("clone-1", {"hashi1": {"instance_id": "HASHI1"}})
    duplicate = manager.submit(
        "clone-1", {"hashi1": {"instance_id": "HASHI1"}}
    )
    result = await manager.wait("clone-1")

    assert first["accepted"] is True
    assert duplicate["accepted"] is True
    assert duplicate["duplicate"] is True
    assert result["status"] == "completed"
    assert result["result"]["source_disabled"] is False
    assert calls == ["confirm"]


@pytest.mark.asyncio
async def test_shared_function_restart_recovers_running_operation(
    tmp_path, monkeypatch
):
    kernel, stopped = _kernel(tmp_path)
    state_path = (
        tmp_path / "state" / "instance" / "agent-move-autofinalize.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operations": {
                    "move-2": {
                        "package_id": "move-2",
                        "status": "running",
                        "instances": {"hashi2": {"instance_id": "HASHI2"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "move-2",
            "agent_id": "source",
            "operation": "move",
            "status": "source_disabled_target_committed",
        },
    )
    monkeypatch.setattr(
        move_manager,
        "confirm_outbound_move",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("recovery must not recommit a committed target")
        ),
    )
    monkeypatch.setattr(
        move_manager,
        "continue_outbound_move",
        lambda *_args: {
            "package_id": "move-2",
            "agent_id": "source",
            "operation": "move",
            "status": "completed",
        },
    )

    manager = move_manager.AgentMoveManager(kernel)
    await manager.start()
    result = await manager.wait("move-2")

    assert result["status"] == "completed"
    assert stopped == [("source", "agent-move-cutover")]


@pytest.mark.asyncio
async def test_failure_stays_persistent_for_explicit_recovery(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.setattr(move_manager, "_MAX_EXECUTION_ATTEMPTS", 1)
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "move-3",
            "agent_id": "source",
            "operation": "move",
            "status": "staged_remote",
        },
    )
    monkeypatch.setattr(
        move_manager,
        "confirm_outbound_move",
        lambda *_args: (_ for _ in ()).throw(OSError("network lost")),
    )

    manager = move_manager.AgentMoveManager(kernel)
    manager.submit("move-3", {"hashi2": {"instance_id": "HASHI2"}})
    result = await manager.wait("move-3")

    assert result["status"] == "needs_recovery"
    assert result["last_error"] == "network lost"
    persisted = json.loads(manager.state_path.read_text(encoding="utf-8"))
    assert persisted["operations"]["move-3"]["status"] == "needs_recovery"


@pytest.mark.parametrize("rollback_completed", [False, True])
@pytest.mark.asyncio
async def test_transient_failure_retries_with_persisted_attempt_count(
    tmp_path, monkeypatch, rollback_completed
):
    kernel, _stopped = _kernel(tmp_path)
    attempts = []
    rolled_back = {}
    monkeypatch.setattr(move_manager, "_WATCH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "clone-retry",
            "agent_id": "source",
            "operation": "clone",
            "status": "staged_remote",
            **rolled_back,
        },
    )

    def confirm(*_args):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            if rollback_completed:
                rolled_back.update(status="cutover_failed", rollback_completed=True)
            raise OSError("temporary disconnect")
        return {
            "package_id": "clone-retry",
            "agent_id": "source",
            "operation": "clone",
            "status": "completed",
        }

    monkeypatch.setattr(move_manager, "confirm_outbound_move", confirm)
    manager = move_manager.AgentMoveManager(kernel)
    await manager.start()
    try:
        manager.submit("clone-retry", {})
        result = await manager.wait("clone-retry")
    finally:
        await manager.stop()

    assert result["status"] == ("needs_recovery" if rollback_completed else "completed")
    assert result["execution_attempts"] == (1 if rollback_completed else 2)
    assert attempts == ([1] if rollback_completed else [1, 2])


@pytest.mark.asyncio
async def test_unconfirmed_source_stop_never_activates_target(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.setattr(move_manager, "_MAX_EXECUTION_ATTEMPTS", 1)

    async def fail_stop(_agent_name: str, reason: str = ""):
        return False, "source could not be stopped because Telegram is draining"

    kernel.stop_agent = fail_stop
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "move-4",
            "agent_id": "source",
            "operation": "move",
            "status": "source_disabled_target_committed",
        },
    )
    continued = []
    monkeypatch.setattr(
        move_manager,
        "continue_outbound_move",
        lambda *_args: continued.append(True),
    )

    manager = move_manager.AgentMoveManager(kernel)
    manager.submit("move-4", {})
    result = await manager.wait("move-4")

    assert result["status"] == "needs_recovery"
    assert "could not be stopped" in result["last_error"]
    assert continued == []


@pytest.mark.asyncio
async def test_cli_inbox_submission_is_picked_up_after_submitter_exits(
    tmp_path, monkeypatch
):
    kernel, _stopped = _kernel(tmp_path)
    calls = []
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "clone-cli",
            "agent_id": "source",
            "operation": "clone",
            "status": "staged_remote",
        },
    )
    monkeypatch.setattr(
        move_manager,
        "confirm_outbound_move",
        lambda *_args: calls.append("confirm")
        or {
            "package_id": "clone-cli",
            "agent_id": "source",
            "operation": "clone",
            "status": "completed",
            "source_disabled": False,
        },
    )
    manager = move_manager.AgentMoveManager(kernel)
    await manager.start()
    try:
        accepted = move_manager.enqueue_agent_move(
            tmp_path,
            {},
            "clone-cli",
            requested_by="cli",
            origin={"surface": "cli"},
        )
        completed = await manager.wait("clone-cli")
    finally:
        await manager.stop()

    assert accepted["accepted"] is True
    assert completed["status"] == "completed"
    assert completed["origin"] == {"surface": "cli"}
    assert calls == ["confirm"]


@pytest.mark.asyncio
async def test_terminal_result_uses_persisted_delivery_origin(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    notices = []
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "clone-notice",
            "agent_id": "source",
            "target_agent_id": "source_1",
            "target_instance": "HASHI2",
            "operation": "clone",
            "status": "staged_remote",
        },
    )
    monkeypatch.setattr(
        move_manager,
        "confirm_outbound_move",
        lambda *_args: {
            "package_id": "clone-notice",
            "agent_id": "source",
            "target_agent_id": "source_1",
            "target_instance": "HASHI2",
            "operation": "clone",
            "status": "completed",
        },
    )

    async def send_notice(_kernel, **kwargs):
        notices.append(kwargs)
        return {"sent": True, "sender": "fallback", "message_id": 42}

    monkeypatch.setattr(move_manager, "send_runtime_notice", send_notice)
    manager = move_manager.AgentMoveManager(kernel)
    await manager.start()
    try:
        manager.submit(
            "clone-notice",
            {},
            requested_by="source",
            origin={"surface": "telegram", "chat_id": 77, "thread_id": 8},
            locale="zh-CN",
        )
        await manager.wait("clone-notice")
        for _attempt in range(100):
            if notices:
                break
            await __import__("asyncio").sleep(0.01)
    finally:
        await manager.stop()

    assert len(notices) == 1
    assert notices[0]["chat_id"] == 77
    assert notices[0]["thread_id"] == 8
    assert "source_1" in notices[0]["render_text"]("fallback", "Fallback")
    assert manager.status("clone-notice")["delivery"]["status"] == "sent"


def test_empty_lock_left_by_crash_is_reclaimed_after_stale_window(tmp_path):
    state_path = (
        tmp_path / "state" / "instance" / "agent-move-autofinalize.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"schema_version": 1, "operations": {}}),
        encoding="utf-8",
    )
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.write_bytes(b"")
    old = time.time() - 10
    os.utime(lock_path, (old, old))
    kernel, _stopped = _kernel(tmp_path)

    assert move_manager.AgentMoveManager(kernel).status("missing") is None
    assert not lock_path.exists()


@pytest.mark.asyncio
async def test_explicit_retry_reopens_exhausted_operation(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.setattr(move_manager, "_MAX_EXECUTION_ATTEMPTS", 1)
    succeeding = False
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "retry-manual",
            "agent_id": "source",
            "operation": "clone",
            "status": "staged_remote",
        },
    )

    def confirm(*_args):
        if not succeeding:
            raise OSError("offline")
        return {"package_id": "retry-manual", "status": "completed"}

    monkeypatch.setattr(move_manager, "confirm_outbound_move", confirm)
    manager = move_manager.AgentMoveManager(kernel)
    manager.submit("retry-manual", {})
    assert (await manager.wait("retry-manual"))["status"] == "needs_recovery"

    succeeding = True
    duplicate = manager.submit("retry-manual", {})
    result = await manager.wait("retry-manual")

    assert duplicate["duplicate"] is True
    assert result["status"] == "completed"


def test_admin_cancel_reconciles_background_receipt(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.setattr(
        move_manager,
        "get_outbound_move",
        lambda *_args: {
            "package_id": "cancel-admin",
            "agent_id": "source",
            "operation": "move",
            "status": "staged_remote",
        },
    )
    move_manager.enqueue_agent_move(tmp_path, {}, "cancel-admin")

    move_manager.record_agent_move_admin_outcome(
        tmp_path,
        "cancel-admin",
        {"package_id": "cancel-admin", "status": "cancelled"},
    )

    assert move_manager.get_agent_move_execution_status(
        tmp_path, "cancel-admin"
    )["status"] == "cancelled"


@pytest.mark.asyncio
async def test_corrupt_recovery_receipt_does_not_block_hashi_startup(tmp_path):
    kernel, _stopped = _kernel(tmp_path)
    state_path = (
        tmp_path / "state" / "instance" / "agent-move-autofinalize.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{", encoding="utf-8")
    manager = move_manager.AgentMoveManager(kernel)

    await manager.start()
    await manager.stop()

    assert manager._last_scan_error is not None
