"""Forced reboot: stop what still runs, capture it, restart, hand it back."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator.reboot_manager import RebootManager
from orchestrator.reboot_ui import render_notice

from tests.test_reboot_manager import _Kernel


def _setup(tmp_path, monkeypatch, *, confirm=0.2, poll=0.01):
    monkeypatch.setattr(
        "orchestrator.reboot_manager.REBOOT_FORCE_STOP_CONFIRM_SECONDS", confirm
    )
    monkeypatch.setattr(
        "orchestrator.reboot_manager.REBOOT_FORCE_STOP_POLL_SECONDS", poll
    )
    kernel = _Kernel(names=("zelda",))
    kernel.paths = SimpleNamespace(bridge_home=tmp_path)
    kernel.shutdown_event = asyncio.Event()
    kernel._restart_request = None
    manager = RebootManager(kernel, None)
    request = {
        "mode": "min",
        "agent_name": "zelda",
        "request_key": "force-1",
        "origin": {"chat_id": 42, "actor_id": "42", "thread_id": 7},
        "locale": "en",
    }
    return kernel, manager, request


def _stop_clears_busy(handle, seen):
    async def execute(text, *, source_channel, **_kwargs):
        seen.append(text)
        handle.client.metadata["is_generating"] = False
        handle.client.metadata["queue_depth"] = 0
        return {"ok": True}

    return execute


@pytest.mark.asyncio
async def test_force_reboot_stops_the_busy_agent_before_the_cutover(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    seen: list[str] = []
    handle.execute_slash_command = _stop_clears_busy(handle, seen)

    accepted = manager.submit({**request, "force": True})
    assert accepted["accepted"] and accepted["stopping"]

    await asyncio.sleep(0.25)
    assert seen == ["/stop"]
    assert kernel.shutdown_event.is_set()
    assert kernel._restart_request["operation_id"] == accepted["record"]["id"]
    assert kernel._restart_request["targets"] == ["zelda"]


@pytest.mark.asyncio
async def test_force_reboot_keeps_the_old_instance_when_stop_is_unconfirmed(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch, confirm=0.15)

    async def never_stops(text, *, source_channel, **_kwargs):
        return {"ok": True}

    kernel.runtimes[0].client.metadata["is_generating"] = True
    kernel.runtimes[0].execute_slash_command = never_stops

    accepted = manager.submit({**request, "force": True})
    assert accepted["stopping"]

    await asyncio.sleep(0.4)
    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "force_stop_unconfirmed"
    assert record["busy_targets"] == ["zelda"]
    assert not kernel.shutdown_event.is_set()
    assert kernel._restart_request is None
    assert manager.active_operation is None
    assert "could not be stopped" in render_notice(record, locale="en")


@pytest.mark.asyncio
async def test_delayed_force_reboot_forces_once_the_wait_budget_runs_out(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "orchestrator.reboot_manager.REBOOT_DELAY_POLL_SECONDS", 0.01
    )
    monkeypatch.setattr(
        "orchestrator.reboot_manager.REBOOT_DELAY_IDLE_WINDOW_SECONDS", 0.05
    )
    handle = kernel.runtimes[0]
    handle.client.metadata["queue_depth"] = 3
    seen: list[str] = []
    handle.execute_slash_command = _stop_clears_busy(handle, seen)

    accepted = manager.submit({**request, "force": True, "delay_seconds": 0.2})
    assert accepted["waiting"]
    await asyncio.sleep(0.05)
    assert not kernel.shutdown_event.is_set()

    await asyncio.sleep(0.6)
    assert seen == ["/stop"]
    assert kernel.shutdown_event.is_set()
    assert kernel._restart_request["operation_id"] == accepted["record"]["id"]


@pytest.mark.asyncio
async def test_forced_wait_is_not_cancellable_once_stopping_began(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch, confirm=1.0)
    kernel.runtimes[0].client.metadata["is_generating"] = True

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.05)
    manager.receipts.update(accepted["record"]["id"], phase="stopping_targets")

    result = manager.submit({"cancel": True})
    assert result["accepted"] is False
    assert result["reason"] == "not_waiting"


@pytest.mark.asyncio
async def test_resume_restores_captured_work_after_the_cutover(tmp_path, monkeypatch):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    handle.execute_slash_command = _stop_clears_busy(handle, [])

    queued = {"request_id": "req-1", "chat_id": 42, "prompt": "queued", "session_id": "s1"}
    turn = {
        "session_id": "s1",
        "request_id": "req-2",
        "chat_id": 42,
        "prompt": "long task",
        "source": "telegram",
        "summary": "long task",
    }
    handle.capture_reboot_state = _sequence_capture(
        {"pending": [queued], "delayed": [], "interrupted": []},
        {"pending": [], "delayed": [], "interrupted": [turn]},
    )
    results: list[dict] = []

    async def resume(
        pending=(), *, delayed=(), interrupted=(), operation_id="", **_kw
    ):
        results.append(
            {
                "pending": list(pending),
                "delayed": list(delayed),
                "interrupted": list(interrupted),
                "operation_id": operation_id,
            }
        )
        return {
            "pending_resumed": len(pending),
            "delayed_resumed": len(delayed),
            "turns_resumed": len(interrupted),
            "turn_resumed": bool(interrupted),
        }

    handle.resume_after_reboot = resume

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)
    manifests = list((tmp_path / "state" / "reboot_resume").glob("*.json"))
    assert len(manifests) == 1
    written = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert written["phase"] == "stopped"
    assert written["agents"]["zelda"]["pending"] == [queued]
    assert written["agents"]["zelda"]["interrupted"] == [turn]
    assert kernel._restart_request["resume_manifest"] == str(manifests[0])

    totals = await manager.resume_restored_work(kernel._restart_request)

    assert totals == {"pending": 1, "turns": 1, "delayed": 0, "unconfirmed": 0}
    assert results[0]["interrupted"] == [turn]
    assert results[0]["operation_id"] == accepted["record"]["id"]
    assert not list((tmp_path / "state" / "reboot_resume").glob("*.json"))
    record = manager.receipts.get(accepted["record"]["id"])
    assert record["resume"] == {"pending": 1, "turns": 1, "delayed": 0, "unconfirmed": 0}
    rendered = render_notice(record, locale="en")
    assert "1 queued request" in rendered and "1 interrupted turn" in rendered


@pytest.mark.asyncio
async def test_force_capture_reads_the_interrupted_turn_after_the_stop(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    stopped: list[str] = []

    old_turn = {"session_id": "s-old", "request_id": "req-old", "prompt": "old stop"}
    new_turn = {
        "session_id": "s1",
        "request_id": "req-new",
        "chat_id": 42,
        "prompt": "long task",
    }

    async def capture(*_args, **_kwargs):
        return {
            "pending": [],
            "delayed": [],
            "interrupted": [old_turn, new_turn] if stopped else [old_turn],
        }

    async def stop(text, *, source_channel, **_kwargs):
        stopped.append(text)
        handle.client.metadata["is_generating"] = False
        handle.client.metadata["queue_depth"] = 0
        return {"ok": True}

    handle.capture_reboot_state = capture
    handle.execute_slash_command = stop

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    manifests = list((tmp_path / "state" / "reboot_resume").glob("*.json"))
    assert len(manifests) == 1, "a stopped turn must be captured for the resume"
    written = json.loads(manifests[0].read_text(encoding="utf-8"))
    interrupted = written["agents"]["zelda"]["interrupted"]
    assert interrupted == [new_turn], "an older manual /stop must stay stopped"
    assert kernel._restart_request["operation_id"] == accepted["record"]["id"]


@pytest.mark.asyncio
async def test_force_does_not_restore_work_that_survived_the_stop(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    stopped = {"req-keep": True, "req-gone": True}

    def payload():
        return {
            "pending": [
                {"request_id": name, "chat_id": 42, "prompt": name}
                for name, alive in stopped.items()
                if alive
            ],
            "delayed": [],
            "interrupted": [],
        }

    async def capture(*_args, **_kwargs):
        return payload()

    async def stop(text, *, source_channel, **_kwargs):
        stopped["req-gone"] = False
        handle.client.metadata["is_generating"] = False
        handle.client.metadata["queue_depth"] = 0
        return {"ok": True}

    handle.capture_reboot_state = capture
    handle.execute_slash_command = stop

    manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    manifests = list((tmp_path / "state" / "reboot_resume").glob("*.json"))
    written = json.loads(manifests[0].read_text(encoding="utf-8"))
    pending = written["agents"]["zelda"]["pending"]
    assert [entry["request_id"] for entry in pending] == ["req-gone"]


@pytest.mark.asyncio
async def test_force_reboot_stops_nothing_when_capture_fails(tmp_path, monkeypatch):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    seen: list[str] = []
    handle.execute_slash_command = _stop_clears_busy(handle, seen)

    async def capture(*_args, **_kwargs):
        raise TimeoutError("worker did not answer")

    handle.capture_reboot_state = capture

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "force_capture_failed"
    assert seen == []
    assert not kernel.shutdown_event.is_set()
    assert not list((tmp_path / "state" / "reboot_resume").glob("*.json"))


@pytest.mark.asyncio
async def test_force_reboot_stops_nothing_when_the_capture_cannot_be_stored(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    seen: list[str] = []
    handle.execute_slash_command = _stop_clears_busy(handle, seen)
    handle.capture_reboot_state = _fixed_capture(
        {"pending": [{"request_id": "req-1", "chat_id": 42, "prompt": "queued"}]}
    )

    def broken(*_args, **_kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr(manager, "_write_resume_manifest", broken)

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "force_manifest_unavailable"
    assert seen == []
    assert not kernel.shutdown_event.is_set()


@pytest.mark.asyncio
async def test_unconfirmed_restore_keeps_the_manifest(tmp_path, monkeypatch):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    handle.execute_slash_command = _stop_clears_busy(handle, [])
    queued = {"request_id": "req-1", "chat_id": 42, "prompt": "queued"}
    handle.capture_reboot_state = _sequence_capture(
        {"pending": [queued]},
        {"pending": [], "delayed": [], "interrupted": []},
    )
    handle.resume_after_reboot = _always_rejected

    manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)
    totals = await manager.resume_restored_work(kernel._restart_request)

    assert totals["pending"] == 0
    assert totals["unconfirmed"] == 1
    manifests = list((tmp_path / "state" / "reboot_resume").glob("*.json"))
    assert len(manifests) == 1, "unconfirmed work must stay on disk"
    written = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert written["agents"]["zelda"]["pending"] == [queued]


@pytest.mark.asyncio
async def test_a_failed_restore_keeps_the_record_and_reports_it(tmp_path, monkeypatch):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    handle.execute_slash_command = _stop_clears_busy(handle, [])
    queued = {"request_id": "req-1", "chat_id": 42, "prompt": "queued"}
    turn = {"session_id": "s1", "request_id": "req-2", "prompt": "long task"}
    handle.capture_reboot_state = _sequence_capture(
        {"pending": [queued]},
        {"pending": [], "delayed": [], "interrupted": [turn]},
    )

    async def broken(*_args, **_kwargs):
        raise TimeoutError("worker went away")

    handle.resume_after_reboot = broken

    manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)
    totals = await manager.resume_restored_work(kernel._restart_request)

    assert totals["unconfirmed"] == 2
    manifests = list((tmp_path / "state" / "reboot_resume").glob("*.json"))
    assert len(manifests) == 1, "work whose restore failed must stay recoverable"
    written = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert written["agents"]["zelda"]["pending"] == [queued]
    assert written["agents"]["zelda"]["interrupted"] == [turn]


def test_interrupted_payloads_include_persisted_sessions(tmp_path):
    from orchestrator.function_worker_host import _interrupted_task_payloads
    from orchestrator import runtime_retry

    workspace = tmp_path / "agent"
    (workspace / "state").mkdir(parents=True)
    (workspace / "state" / "retry_state.json").write_text(
        json.dumps(
            {
                "version": runtime_retry.RETRY_STATE_VERSION,
                "unfinished_task_by_session": {
                    "ses_restarted": {
                        "prompt": "finish the deploy",
                        "request_id": "req-restarted",
                        "session_id": "ses_restarted",
                        "reason": "user_stop",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(workspace_dir=workspace)

    payloads = _interrupted_task_payloads(runtime, runtime_retry)

    assert [item.request_id for item in payloads] == ["req-restarted"]


def _fixed_capture(payload):
    async def capture(*_args, **_kwargs):
        return dict(payload)

    return capture


def _sequence_capture(*payloads):
    """Capture answers in order: before the stop, then after it."""
    remaining = [dict(payload) for payload in payloads]

    async def capture(*_args, **_kwargs):
        if remaining:
            return remaining.pop(0)
        return {"pending": [], "delayed": [], "interrupted": []}

    return capture


async def _always_rejected(pending=(), *, delayed=(), interrupted=(), **_kwargs):
    return {
        "pending_resumed": 0,
        "delayed_resumed": 0,
        "turns_resumed": 0,
        "turn_resumed": False,
        "unconfirmed": [
            *({"kind": "pending", "entry": entry} for entry in pending),
            *({"kind": "delayed", "entry": entry} for entry in delayed),
            *({"kind": "turn", "entry": entry} for entry in interrupted),
        ],
    }


def test_parser_accepts_force_alongside_delay():
    from orchestrator.runtime_reboot import parse_request

    assert parse_request("max force") == ("max", {"force": True})
    assert parse_request("max delay=15 force") == (
        "max",
        {"delay_seconds": 900.0, "force": True},
    )
    assert parse_request("max force delay") == ("max", {"delay_seconds": 1800.0, "force": True})
    assert parse_request("max bogus") == (None, {})
# HASHI-Fingerprint: hfp_f35bc599f6634309bdcfa884403aa717
