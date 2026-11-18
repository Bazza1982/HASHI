from __future__ import annotations

import asyncio
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
async def test_move_retries_forward_past_legacy_attempt_limit(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.setattr(move_manager, "_MAX_EXECUTION_ATTEMPTS", 1)
    monkeypatch.setattr(move_manager, "_CROSS_PROCESS_RECOVERY_SECONDS", 0.01)
    attempts = 0
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
    def confirm(*_args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("network lost")
        return {
            "package_id": "move-3",
            "agent_id": "source",
            "operation": "move",
            "status": "completed",
        }

    monkeypatch.setattr(move_manager, "confirm_outbound_move", confirm)

    manager = move_manager.AgentMoveManager(kernel)
    await manager.start()
    try:
        manager.submit("move-3", {"hashi2": {"instance_id": "HASHI2"}})
        result = await manager.wait("move-3")
    finally:
        await manager.stop()

    assert result["status"] == "completed"
    assert result["execution_attempts"] == 2
    assert attempts == 2
    reopened = move_manager.AgentMoveManager(kernel)
    assert reopened.status("move-3")["status"] == "completed"


@pytest.mark.parametrize("rollback_completed", [False, True])
@pytest.mark.asyncio
async def test_transient_failure_retries_with_persisted_attempt_count(
    tmp_path, monkeypatch, rollback_completed
):
    kernel, _stopped = _kernel(tmp_path)
    attempts = []
    rolled_back = {}
    monkeypatch.setattr(move_manager, "_CROSS_PROCESS_RECOVERY_SECONDS", 0.01)
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


@pytest.mark.parametrize("operation", ["move", "clone"])
@pytest.mark.asyncio
async def test_scope_abort_pending_is_retried_even_when_error_is_not_transient(
    tmp_path, monkeypatch, operation,
):
    kernel, _stopped = _kernel(tmp_path)
    outbound = {"package_id": "scope-recovery", "agent_id": "source", "operation": operation,
                "status": "committing_target", "scope_abort_pending": True}
    monkeypatch.setattr(move_manager, "get_outbound_move", lambda *_args: dict(outbound))
    calls = []
    def confirm(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise move_manager.AgentMoveError("scope abort outcome is unknown")
        outbound.update(status="cancelled", rollback_completed=True, scope_abort_pending=False)
        raise move_manager.AgentMoveError("target policy changed; prepare a fresh transfer")
    monkeypatch.setattr(move_manager, "confirm_outbound_move", confirm)
    manager = move_manager.AgentMoveManager(kernel)
    manager.submit("scope-recovery", {}, _schedule=False)
    await manager._execute("scope-recovery")
    assert manager.status("scope-recovery")["status"] == "retry_wait"
    await manager._execute("scope-recovery")
    result = manager.status("scope-recovery")
    assert result["status"] == "needs_recovery"
    assert "prepare a fresh transfer" in result["last_error"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_clone_transient_failure_recovers_beyond_five_with_same_package(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    calls = []
    monkeypatch.setattr(move_manager, "get_outbound_move", lambda *_args: {
        "package_id": "clone-long", "agent_id": "source", "operation": "clone",
        "status": "staged_remote",
    })

    def confirm(_root, _instances, package_id):
        calls.append(package_id)
        if len(calls) <= 6:
            raise OSError("temporary disconnect")
        return {"package_id": package_id, "status": "completed"}

    monkeypatch.setattr(move_manager, "confirm_outbound_move", confirm)
    manager = move_manager.AgentMoveManager(kernel)
    manager.submit("clone-long", {}, _schedule=False)
    for _attempt in range(7):
        manager = move_manager.AgentMoveManager(kernel)
        await manager._execute("clone-long")
    assert calls == ["clone-long"] * 7
    assert manager.status("clone-long")["status"] == "completed"


@pytest.mark.asyncio
async def test_permanent_clone_failure_does_not_retry_forever(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.setattr(move_manager, "get_outbound_move", lambda *_args: {
        "package_id": "clone-invalid", "agent_id": "source", "operation": "clone",
        "status": "staged_remote",
    })
    def reject(*_args):
        raise move_manager.AgentMoveError("target rejected the package")

    monkeypatch.setattr(move_manager, "confirm_outbound_move", reject)
    manager = move_manager.AgentMoveManager(kernel)
    manager.submit("clone-invalid", {}, _schedule=False)
    await manager._execute("clone-invalid")
    assert manager.status("clone-invalid")["status"] == "needs_recovery"


@pytest.mark.asyncio
async def test_unconfirmed_source_stop_never_activates_target(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.setattr(move_manager, "_MAX_EXECUTION_ATTEMPTS", 1)
    monkeypatch.setattr(move_manager, "_CROSS_PROCESS_RECOVERY_SECONDS", 0.01)
    stop_attempts = 0

    async def fail_stop(_agent_name: str, reason: str = ""):
        nonlocal stop_attempts
        stop_attempts += 1
        if stop_attempts == 1:
            return False, "source could not be stopped because Telegram is draining"
        return True, "stopped"

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
        lambda *_args: continued.append(True)
        or {"package_id": "move-4", "operation": "move", "status": "completed"},
    )

    manager = move_manager.AgentMoveManager(kernel)
    await manager.start()
    try:
        manager.submit("move-4", {})
        result = await manager.wait("move-4")
    finally:
        await manager.stop()

    assert result["status"] == "completed"
    assert stop_attempts == 2
    assert continued == [True]


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
        started = asyncio.get_running_loop().time()
        accepted = move_manager.enqueue_agent_move(
            tmp_path,
            {},
            "clone-cli",
            requested_by="cli",
            origin={"surface": "cli"},
        )
        completed = await asyncio.wait_for(manager.wait("clone-cli"), timeout=3.0)
        latency = asyncio.get_running_loop().time() - started
    finally:
        await manager.stop()

    assert accepted["accepted"] is True
    assert completed["status"] == "completed"
    assert completed["origin"] == {"surface": "cli"}
    assert calls == ["confirm"]
    assert latency < 2.5


@pytest.mark.asyncio
async def test_idle_manager_reads_recovery_state_once_until_bounded_fallback(
    tmp_path, monkeypatch
):
    kernel, _stopped = _kernel(tmp_path)
    manager = move_manager.AgentMoveManager(kernel)
    calls = 0
    original = manager._load_operations

    def counted_load():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(manager, "_load_operations", counted_load)
    monkeypatch.setattr(move_manager, "_CROSS_PROCESS_RECOVERY_SECONDS", 60.0)

    await manager.start()
    try:
        await __import__("asyncio").sleep(0.05)
    finally:
        await manager.stop()

    assert calls == 1


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


@pytest.mark.asyncio
async def test_retryable_notice_survives_six_failures_without_rerunning_transfer(tmp_path, monkeypatch):
    from orchestrator import frontend_notice_delivery

    kernel, _stopped = _kernel(tmp_path)
    manager = move_manager.AgentMoveManager(kernel)
    with manager._state_lock():
        manager._write_record_unlocked("clone-1", {
            "package_id": "clone-1", "status": "completed", "agent_id": "source",
            "origin": {"session_id": "session-1", "surface": "workbench"},
            "delivery": {"status": "pending", "attempts": 0, "next_attempt_at": 0},
        })
    publications = []

    async def publish(*_args, **kwargs):
        publications.append(kwargs["publication_id"])
        if len(publications) <= 6:
            return {"sent": False, "retry_safe": True, "retry_after": 1}
        return {"sent": True, "message_id": "published"}

    monkeypatch.setattr(frontend_notice_delivery, "deliver_notice", publish)
    for _attempt in range(7):
        # Simulate the persisted deadline passing and a new manager process.
        manager = move_manager.AgentMoveManager(kernel)
        record = manager._require_record("clone-1")
        record["delivery"]["next_attempt_at"] = 0
        manager._update("clone-1", delivery=record["delivery"])
        await manager._deliver_result("clone-1", manager._require_record("clone-1"))

    assert publications == ["agent-transfer:clone-1:completed"] * 7
    result = manager.status("clone-1")
    assert result["status"] == "completed"
    assert result["delivery"]["status"] == "sent"
    assert result["delivery"]["attempts"] == 7


@pytest.mark.asyncio
async def test_uncertain_notice_is_recorded_without_blind_retry(tmp_path, monkeypatch):
    from orchestrator import frontend_notice_delivery

    kernel, _stopped = _kernel(tmp_path)
    manager = move_manager.AgentMoveManager(kernel)
    with manager._state_lock():
        manager._write_record_unlocked("clone-1", {
            "package_id": "clone-1", "status": "completed", "agent_id": "source",
            "origin": {"session_id": "session-1", "surface": "workbench"},
            "delivery": {"status": "pending", "attempts": 0, "next_attempt_at": 0},
        })
    calls = []

    async def publish(*_args, **_kwargs):
        calls.append(True)
        return {"sent": False, "delivery_state": "unknown"}

    monkeypatch.setattr(frontend_notice_delivery, "deliver_notice", publish)
    await manager._deliver_result("clone-1", manager._require_record("clone-1"))
    await manager._deliver_result("clone-1", manager._require_record("clone-1"))
    assert calls == [True]
    assert manager.status("clone-1")["status"] == "completed"
    assert manager.status("clone-1")["delivery"]["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
async def test_configured_notice_budget_does_not_count_offline_time(tmp_path, monkeypatch, deferred):
    from orchestrator import frontend_notice_delivery

    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.setenv("HASHI_AGENT_MOVE_DELIVERY_MAX_ATTEMPTS", "1")
    manager = move_manager.AgentMoveManager(kernel)
    with manager._state_lock():
        manager._write_record_unlocked("clone-1", {
            "package_id": "clone-1", "status": "completed", "agent_id": "source",
            "origin": {"session_id": "session-1", "surface": "workbench"},
            "delivery": {"status": "pending", "attempts": 0, "next_attempt_at": 0},
        })

    async def publish(*_args, **_kwargs):
        return {"sent": False, "retry_safe": True, "deferred": deferred}

    monkeypatch.setattr(frontend_notice_delivery, "deliver_notice", publish)
    await manager._deliver_result("clone-1", manager._require_record("clone-1"))
    delivery = manager.status("clone-1")["delivery"]
    assert delivery["status"] == ("pending" if deferred else "exhausted")
    assert delivery["attempts"] == (0 if deferred else 1)


@pytest.mark.asyncio
async def test_explicit_confirmation_resumes_safely_exhausted_notice_only(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    manager = move_manager.AgentMoveManager(kernel)
    with manager._state_lock():
        manager._write_record_unlocked("clone-1", {
            "package_id": "clone-1", "status": "completed",
            "delivery": {"status": "exhausted", "attempts": 1, "retry_safe": True},
        })
    monkeypatch.setattr(move_manager, "get_outbound_move", lambda *_args: pytest.fail("transfer was rerun"))

    duplicate = manager.submit("clone-1", {}, _schedule=False)
    assert duplicate["duplicate"] is True
    result = manager.status("clone-1")
    assert result["status"] == "completed"
    assert result["delivery"]["status"] == "pending"
    assert result["delivery"]["attempts"] == 0


@pytest.mark.asyncio
async def test_direct_notice_interrupted_during_send_is_not_automatically_replayed(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    manager = move_manager.AgentMoveManager(kernel)
    with manager._state_lock():
        manager._write_record_unlocked("clone-1", {
            "package_id": "clone-1", "status": "completed", "agent_id": "source",
            "origin": {"surface": "telegram", "chat_id": 42},
            "delivery": {"status": "pending", "attempts": 0, "next_attempt_at": 0},
        })
    calls = []

    async def interrupted(*_args, **_kwargs):
        calls.append(True)
        raise OSError("interrupted after the send may have started")

    monkeypatch.setattr(move_manager, "send_runtime_notice", interrupted)
    with pytest.raises(OSError):
        await manager._deliver_result("clone-1", manager._require_record("clone-1"))
    restarted = move_manager.AgentMoveManager(kernel)
    await restarted._deliver_result("clone-1", restarted._require_record("clone-1"))
    assert calls == [True]
    assert restarted.status("clone-1")["delivery"]["status"] == "unknown"


def test_legacy_empty_lock_does_not_block_or_require_deletion(tmp_path):
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
    assert lock_path.read_bytes() == b""


def test_live_finalizer_writer_is_not_displaced_by_old_lock_mtime(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    kernel, _stopped = _kernel(tmp_path)
    first = move_manager.AgentMoveManager(kernel)
    second = move_manager.AgentMoveManager(kernel)
    entered = Event()

    def read_after_lock():
        entered.set()
        return second.status("missing")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with first._state_lock():
            lock = next(first.state_path.parent.glob("*lock"))
            old = time.time() - 10
            os.utime(lock, (old, old))
            pending = pool.submit(read_after_lock)
            assert entered.wait(5)
            time.sleep(0.03)
            assert not pending.done()
        assert pending.result(timeout=5) is None


def test_many_pending_and_undelivered_receipts_survive_restart_and_updates(tmp_path):
    kernel, _stopped = _kernel(tmp_path)
    first = move_manager.AgentMoveManager(kernel)
    records = {
        f"move-{index}": {
            "package_id": f"move-{index}",
            "status": "accepted",
            "instances": {"peer": {"metadata": "x" * 4300}},
            "delivery": {"status": "pending", "publication_id": f"publication-{index}"},
        }
        for index in range(150)
    }
    records["undelivered"] = {
        "package_id": "undelivered",
        "status": "completed",
        "delivery": {"status": "exhausted", "publication_id": "publication-unsent"},
    }
    with first._state_lock():
        first._write_operations_unlocked(records)

    reopened = move_manager.AgentMoveManager(kernel)
    assert len(reopened._load_operations()) == 151
    reopened._update("move-149", status="running")
    assert move_manager.AgentMoveManager(kernel).status("move-149")["status"] == "running"
    assert reopened.status("move-0")["delivery"]["publication_id"] == "publication-0"
    assert reopened.status("undelivered")["delivery"]["publication_id"] == "publication-unsent"


def test_oversized_legacy_journal_is_migrated_without_losing_original(tmp_path):
    kernel, _stopped = _kernel(tmp_path)
    manager = move_manager.AgentMoveManager(kernel)
    manager.state_path.parent.mkdir(parents=True)
    operations = {
        f"move-{index}": {"package_id": f"move-{index}", "status": "accepted", "detail": "x" * 4300}
        for index in range(150)
    }
    original = json.dumps({"schema_version": 1, "operations": operations}).encode()
    manager.state_path.write_bytes(original)

    assert len(manager._load_operations()) == 150
    manager._update("move-0", status="running")
    assert move_manager.AgentMoveManager(kernel).status("move-0")["status"] == "running"
    assert manager.state_path.read_bytes() == original


def test_interrupted_legacy_migration_can_be_retried(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    manager = move_manager.AgentMoveManager(kernel)
    manager.state_path.parent.mkdir(parents=True)
    original = json.dumps({"schema_version": 1, "operations": {
        "move-1": {"status": "accepted", "delivery": {"status": "pending"}},
    }}).encode()
    manager.state_path.write_bytes(original)
    real_replace = move_manager.os.replace

    def fail_install(*_args):
        raise OSError("simulated interrupted migration")

    monkeypatch.setattr(move_manager.os, "replace", fail_install)
    with pytest.raises(OSError, match="interrupted migration"):
        manager.status("move-1")
    assert not manager.journal_path.exists()
    assert manager.state_path.read_bytes() == original
    monkeypatch.setattr(move_manager.os, "replace", real_replace)
    assert manager.status("move-1")["delivery"]["status"] == "pending"


def test_old_writer_changing_legacy_after_migration_is_reported_without_overwrite(tmp_path):
    kernel, _stopped = _kernel(tmp_path)
    manager = move_manager.AgentMoveManager(kernel)
    manager.state_path.parent.mkdir(parents=True)
    manager.state_path.write_text(json.dumps({"schema_version": 1, "operations": {
        "move-1": {"status": "accepted"},
    }}), encoding="utf-8")
    assert manager.status("move-1")["status"] == "accepted"
    newer_legacy = json.dumps({"schema_version": 1, "operations": {
        "move-1": {"status": "accepted"}, "move-2": {"status": "accepted"},
    }})
    manager.state_path.write_text(newer_legacy, encoding="utf-8")

    with pytest.raises(move_manager.AgentMoveError, match="legacy Agent move state changed"):
        manager._update("move-1", status="running")
    assert manager.state_path.read_text(encoding="utf-8") == newer_legacy
    assert manager.journal_path.exists()


def test_concurrent_managers_preserve_each_others_receipts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    kernel, _stopped = _kernel(tmp_path)
    move_manager.AgentMoveManager(kernel)._load_operations()

    def save(index):
        manager = move_manager.AgentMoveManager(kernel)
        with manager._state_lock():
            manager._write_record_unlocked(f"move-{index}", {
                "package_id": f"move-{index}", "status": "accepted",
            })

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save, range(20)))
    operations = move_manager.AgentMoveManager(kernel)._load_operations()
    assert set(operations) == {f"move-{index}" for index in range(20)}


def test_configured_receipt_retention_never_removes_undelivered_results(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.setenv("HASHI_AGENT_MOVE_DELIVERED_RECEIPT_RETENTION", "1")
    manager = move_manager.AgentMoveManager(kernel)
    records = {
        "old": {"status": "completed", "updated_at": "2026-01-01", "delivery": {"status": "sent"}},
        "new": {"status": "completed", "updated_at": "2026-02-01", "delivery": {"status": "sent"}},
        "pending": {"status": "completed", "delivery": {"status": "pending"}},
        "unconfirmed": {"status": "completed", "delivery": {"status": "exhausted"}},
        "active": {"status": "running"},
    }
    with manager._state_lock():
        manager._write_operations_unlocked(records)

    assert manager.status("old") is None
    for package_id in ("new", "pending", "unconfirmed", "active"):
        assert manager.status(package_id) is not None


def test_invalid_retention_does_not_block_recovery_or_remove_records(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    manager = move_manager.AgentMoveManager(kernel)
    with manager._state_lock():
        manager._write_operations_unlocked({
            "completed": {"status": "completed", "delivery": {"status": "sent"}},
            "active": {"status": "accepted"},
        })
    monkeypatch.setenv("HASHI_AGENT_MOVE_DELIVERED_RECEIPT_RETENTION", "invalid")
    manager._update("active", status="running")
    assert manager.status("active")["status"] == "running"
    assert manager.status("completed")["status"] == "completed"


def test_completed_receipts_are_not_pruned_without_retention_policy(tmp_path, monkeypatch):
    kernel, _stopped = _kernel(tmp_path)
    monkeypatch.delenv("HASHI_AGENT_MOVE_DELIVERED_RECEIPT_RETENTION", raising=False)
    manager = move_manager.AgentMoveManager(kernel)
    with manager._state_lock():
        manager._write_operations_unlocked({
            "old": {"status": "completed", "updated_at": "2026-01-01", "delivery": {"status": "sent"}, "result": {"detail": "x" * 1000}},
            "new": {"status": "accepted", "updated_at": "2026-10-29"},
        })
    assert manager.status("new")["status"] == "accepted"
    assert manager.status("old")["status"] == "completed"


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
