"""Forced reboot: stop what still runs, keep exactly what the stop removed, restart."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import ui_language
from orchestrator.agent_stop_fence import AgentStopFenceStore, stop_fence_path
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


def _capture_file(tmp_path, operation_id, agent="zelda"):
    directory = tmp_path / "state" / "reboot_resume"
    return directory / f"capture-{operation_id}-{agent}.json"


def _manifests(tmp_path):
    directory = tmp_path / "state" / "reboot_resume"
    if not directory.exists():
        return []
    return [path for path in directory.glob("*.json") if not path.name.startswith("capture-")]


def _write_capture(tmp_path, operation_id, agent, payload):
    path = _capture_file(tmp_path, operation_id, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "operation_id": operation_id,
                "agent": agent,
                "pending": list(payload.get("pending") or ()),
                "delayed": list(payload.get("delayed") or ()),
                "interrupted": list(payload.get("interrupted") or ()),
                "unconfirmed": list(payload.get("unconfirmed") or ()),
                # Only a finished record is evidence; the stop writes this last.
                "phase": payload.get("phase", "stopped"),
                "broken": bool(payload.get("broken", False)),
            }
        ),
        encoding="utf-8",
    )


def _worker(handle, tmp_path, payload=None, *, stop=True, seen=None):
    """Fake the Worker contract: arming succeeds and /stop writes its record.

    The record is written by the stop, exactly as the Worker writes it at the
    removal point; nothing else is inspected afterwards.
    """
    name = handle.name
    state = {"operation_id": ""}

    async def arm(operation_id, **_kwargs):
        state["operation_id"] = str(operation_id)
        return {"armed": True, "operation_id": str(operation_id), "durable": True}

    async def execute(text, *, source_channel, **_kwargs):
        if seen is not None:
            seen.append(text)
        if stop:
            handle.client.metadata["is_generating"] = False
            handle.client.metadata["queue_depth"] = 0
        _write_capture(tmp_path, state["operation_id"], name, payload or {})
        return {"ok": True}

    handle.arm_reboot_capture = arm
    handle.execute_slash_command = execute
    return state


def _stop_clears_busy(handle, seen):
    async def execute(text, *, source_channel, **_kwargs):
        seen.append(text)
        handle.client.metadata["is_generating"] = False
        handle.client.metadata["queue_depth"] = 0
        return {"ok": True}

    return execute


def _resuming(handle, *, result=None):
    calls: list[dict] = []

    async def resume(pending=(), *, interrupted=(), operation_id="", **_kw):
        calls.append(
            {
                "pending": list(pending),
                "interrupted": list(interrupted),
                "operation_id": operation_id,
            }
        )
        return result or {
            "pending_resumed": len(pending),
            "turns_resumed": len(interrupted),
        }

    handle.resume_after_reboot = resume
    return calls


@pytest.mark.asyncio
async def test_force_reboot_stops_the_busy_agent_before_the_cutover(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    seen: list[str] = []
    _worker(handle, tmp_path, seen=seen)

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

    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    _worker(handle, tmp_path, stop=False)

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
    monkeypatch.setattr("orchestrator.reboot_manager.REBOOT_DELAY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        "orchestrator.reboot_manager.REBOOT_DELAY_IDLE_WINDOW_SECONDS", 0.05
    )
    handle = kernel.runtimes[0]
    handle.client.metadata["queue_depth"] = 3
    seen: list[str] = []
    _worker(handle, tmp_path, seen=seen)

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
    _worker(kernel.runtimes[0], tmp_path)

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

    queued = {"request_id": "req-1", "chat_id": 42, "prompt": "queued", "session_id": "s1"}
    turn = {
        "session_id": "s1",
        "request_id": "req-2",
        "chat_id": 42,
        "prompt": "long task",
        "source": "telegram",
        "summary": "long task",
    }
    _worker(handle, tmp_path, {"pending": [queued], "interrupted": [turn]})
    calls = _resuming(handle)

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    manifests = _manifests(tmp_path)
    assert len(manifests) == 1
    written = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert written["phase"] == "stopped"
    assert written["agents"]["zelda"]["pending"] == [queued]
    assert written["agents"]["zelda"]["interrupted"] == [turn]
    assert kernel._restart_request["resume_manifest"] == str(manifests[0])

    totals = await manager.resume_restored_work(kernel._restart_request)

    assert totals == {"pending": 1, "turns": 1, "delayed": 0, "unconfirmed": 0}
    assert calls[0]["interrupted"] == [turn]
    assert calls[0]["operation_id"] == accepted["record"]["id"]
    assert _manifests(tmp_path) == [], "nothing left once every item is confirmed"
    record = manager.receipts.get(accepted["record"]["id"])
    assert record["resume"] == {"pending": 1, "turns": 1, "delayed": 0, "unconfirmed": 0}
    rendered = render_notice(record, locale="en")
    assert "1 queued request" in rendered and "1 interrupted turn" in rendered


@pytest.mark.asyncio
async def test_the_manifest_is_exactly_what_the_stop_recorded(tmp_path, monkeypatch):
    """No before/after interpretation: the stop's own record is the manifest."""
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True

    recorded = {
        "pending": [
            {"request_id": "req-gone", "chat_id": 42, "prompt": "enqueued after the arm"}
        ],
        "delayed": [
            {"id": "d1", "chat_id": 42, "prompt": "later", "due_at": 1_900_000_000.0,
             "record": {"id": "d1", "agent": "zelda", "chat_id": 42, "prompt": "later",
                        "due_at": 1_900_000_000.0}}
        ],
        "interrupted": [],
    }
    _worker(handle, tmp_path, recorded)
    _resuming(handle)

    manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    written = json.loads(_manifests(tmp_path)[0].read_text(encoding="utf-8"))
    assert written["agents"]["zelda"]["pending"] == recorded["pending"]
    assert written["agents"]["zelda"]["delayed"] == recorded["delayed"]


@pytest.mark.asyncio
async def test_force_reboot_stops_nothing_when_capture_cannot_be_armed(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    seen: list[str] = []
    handle.execute_slash_command = _stop_clears_busy(handle, seen)

    async def arm(*_args, **_kwargs):
        raise TimeoutError("worker did not answer")

    handle.arm_reboot_capture = arm

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "force_capture_failed"
    assert seen == []
    assert not kernel.shutdown_event.is_set()
    assert _manifests(tmp_path) == []


@pytest.mark.asyncio
async def test_an_armed_agent_is_disarmed_when_a_later_arm_fails(
    tmp_path, monkeypatch
):
    kernel = _Kernel(names=("zelda", "sunny"))
    kernel.paths = SimpleNamespace(bridge_home=tmp_path)
    kernel.shutdown_event = asyncio.Event()
    kernel._restart_request = None
    manager = RebootManager(kernel, None)
    first, second = kernel.runtimes
    for handle in (first, second):
        handle.client.metadata["is_generating"] = True
    seen: list[str] = []
    disarmed: list[tuple[str, str]] = []

    async def arm(operation_id, **_kwargs):
        return {"armed": True, "operation_id": str(operation_id), "durable": True}

    async def disarm(operation_id, **_kwargs):
        disarmed.append((first.name, str(operation_id)))
        return {"disarmed": True}

    async def arm_too_late(*_args, **_kwargs):
        raise TimeoutError("worker did not answer")

    first.arm_reboot_capture = arm
    first.disarm_reboot_capture = disarm
    first.execute_slash_command = _stop_clears_busy(first, seen)
    second.arm_reboot_capture = arm_too_late
    second.execute_slash_command = _stop_clears_busy(second, seen)

    accepted = manager.submit(
        {
            "mode": "max",
            "force": True,
            "request_key": "force-2",
            "origin": {"chat_id": 42, "actor_id": "42", "thread_id": 7},
            "locale": "en",
        }
    )
    await asyncio.sleep(0.25)

    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "force_capture_failed"
    # The Agent that armed first must give its record back: left armed, a later
    # plain /stop would be filed as work this reboot removed.
    assert [name for name, _ in disarmed] == [first.name]
    assert seen == []
    assert not kernel.shutdown_event.is_set()
    assert _manifests(tmp_path) == []
    assert list((tmp_path / "state" / "reboot_resume").glob("capture-*")) == []


@pytest.mark.asyncio
async def test_a_missing_record_stops_the_cutover_and_keeps_what_exists(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    seen: list[str] = []

    async def arm(operation_id, **_kwargs):
        return {"armed": True, "operation_id": str(operation_id), "durable": True}

    async def execute(text, *, source_channel, **_kwargs):
        seen.append(text)
        handle.client.metadata["is_generating"] = False
        handle.client.metadata["queue_depth"] = 0
        return {"ok": True}

    handle.arm_reboot_capture = arm
    handle.execute_slash_command = execute  # never writes a record

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "force_capture_unconfirmed"
    assert seen == ["/stop"]
    assert not kernel.shutdown_event.is_set()
    assert kernel._restart_request is None


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_stored_hands_the_work_back(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    queued = {"request_id": "req-1", "chat_id": 42, "prompt": "queued"}
    _worker(handle, tmp_path, {"pending": [queued]})
    calls = _resuming(handle)

    def broken(*_args, **_kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr(manager, "_write_resume_manifest", broken)

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "force_manifest_unavailable"
    assert not kernel.shutdown_event.is_set()
    assert calls and calls[0]["pending"] == [queued], "stopped work goes back"
    # The stop's own record is what made the hand-back possible; it is only
    # cleared once every item is confirmed back in the queue.
    assert not _capture_file(tmp_path, accepted["record"]["id"]).exists()


@pytest.mark.asyncio
async def test_an_abort_keeps_the_record_when_the_hand_back_is_unconfirmed(
    tmp_path, monkeypatch
):
    """Regression: an unconfirmed hand-back must not delete the manifest."""
    kernel, manager, request = _setup(tmp_path, monkeypatch, confirm=0.15)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    queued = {"request_id": "req-1", "chat_id": 42, "prompt": "queued"}
    _worker(handle, tmp_path, {"pending": [queued]}, stop=False)
    _resuming(handle, result={"pending_resumed": 0})

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.4)

    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "force_stop_unconfirmed"
    assert record["resume"]["unconfirmed"] == 1
    written = json.loads(_manifests(tmp_path)[0].read_text(encoding="utf-8"))
    assert written["agents"]["zelda"]["pending"] == [queued]


@pytest.mark.asyncio
async def test_unconfirmed_restore_keeps_the_manifest(tmp_path, monkeypatch):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    queued = {"request_id": "req-1", "chat_id": 42, "prompt": "queued"}
    _worker(handle, tmp_path, {"pending": [queued]})
    _resuming(handle, result={"pending_resumed": 0})

    manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)
    totals = await manager.resume_restored_work(kernel._restart_request)

    assert totals["pending"] == 0
    assert totals["unconfirmed"] == 1
    manifests = _manifests(tmp_path)
    assert len(manifests) == 1, "unconfirmed work must stay on disk"
    written = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert written["agents"]["zelda"]["pending"] == [queued]


@pytest.mark.asyncio
async def test_a_failed_restore_keeps_the_record_and_reports_it(tmp_path, monkeypatch):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    queued = {"request_id": "req-1", "chat_id": 42, "prompt": "queued"}
    _worker(handle, tmp_path, {"pending": [queued]})

    async def broken(*_args, **_kwargs):
        raise RuntimeError("worker is not there yet")

    handle.resume_after_reboot = broken

    manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)
    totals = await manager.resume_restored_work(kernel._restart_request)

    assert totals == {"pending": 0, "turns": 0, "delayed": 0, "unconfirmed": 1}
    written = json.loads(_manifests(tmp_path)[0].read_text(encoding="utf-8"))
    assert written["agents"]["zelda"]["pending"] == [queued]


@pytest.mark.asyncio
async def test_a_capture_time_unconfirmed_entry_is_reported_and_kept(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    _worker(
        handle,
        tmp_path,
        {
            "delayed": [],
            "unconfirmed": [
                {
                    "kind": "delayed",
                    "entry": {"id": "d1", "prompt": "later"},
                    "reason": "cancel_not_confirmed",
                }
            ],
        },
    )
    calls = _resuming(handle)

    manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)
    totals = await manager.resume_restored_work(kernel._restart_request)

    assert totals["unconfirmed"] == 1
    assert calls == [], "nothing was removed, so there is nothing to re-admit"
    written = json.loads(_manifests(tmp_path)[0].read_text(encoding="utf-8"))
    assert written["agents"]["zelda"]["unconfirmed"][0]["entry"]["id"] == "d1"


class _DelayScheduler:
    """The Scheduler's real restore contract: a receipt, or nothing."""

    def __init__(self, *, accept=True):
        self.records: list[dict] = []
        self.accept = accept

    async def restore_delayed_message(self, record):
        if not self.accept:
            return None
        self.records.append(dict(record))
        return dict(record)


@pytest.mark.asyncio
async def test_a_cancelled_delayed_message_comes_back_through_the_scheduler(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    AgentStopFenceStore(stop_fence_path(tmp_path)).advance("zelda")
    scheduler = _DelayScheduler()
    kernel.scheduler = scheduler
    due_at = 1_900_000_000.0
    recorded = {
        "delayed": [
            {
                "id": "d1",
                "chat_id": 42,
                "prompt": "later",
                "due_at": due_at,
                "record": {
                    "id": "d1",
                    "agent": "zelda",
                    "chat_id": 42,
                    "prompt": "later",
                    "created_at": 1_700_000_000.0,
                    "due_at": due_at,
                    "request_metadata": {"agent_stop_epoch": 0, "owner_id": "42"},
                },
            }
        ]
    }
    _worker(handle, tmp_path, recorded)
    calls = _resuming(handle)

    manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)
    totals = await manager.resume_restored_work(kernel._restart_request)

    assert totals == {"pending": 0, "turns": 0, "delayed": 1, "unconfirmed": 0}
    assert calls == [], "Core restores the schedule; the Worker is not asked"
    restored = scheduler.records[0]
    assert restored["id"] == "d1"
    assert restored["created_at"] == 1_700_000_000.0
    assert restored["due_at"] == due_at
    assert restored["request_metadata"]["agent_stop_epoch"] == 1
    assert restored["request_metadata"]["owner_id"] == "42"
    assert _manifests(tmp_path) == []


@pytest.mark.asyncio
async def test_a_delayed_message_the_scheduler_rejects_stays_unconfirmed(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    kernel.scheduler = _DelayScheduler(accept=False)
    entry = {"id": "d1", "prompt": "later", "due_at": 1_900_000_000.0, "record": {}}
    _worker(handle, tmp_path, {"delayed": [entry]})
    _resuming(handle)

    manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)
    totals = await manager.resume_restored_work(kernel._restart_request)

    assert totals == {"pending": 0, "turns": 0, "delayed": 0, "unconfirmed": 1}
    written = json.loads(_manifests(tmp_path)[0].read_text(encoding="utf-8"))
    assert written["agents"]["zelda"]["delayed"] == [entry]


@pytest.mark.asyncio
async def test_only_the_rejected_delayed_message_stays_unconfirmed(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True

    class _PartialScheduler:
        """Accepts the first delayed record and refuses the second."""

        def __init__(self):
            self.restored: list[str] = []

        async def restore_delayed_message(self, record):
            if record.get("id") != "d1":
                return None
            self.restored.append(str(record.get("id")))
            return dict(record)

    scheduler = _PartialScheduler()
    kernel.scheduler = scheduler
    restored = {"id": "d1", "prompt": "still due", "due_at": 1_900_000_000.0, "record": {}}
    refused = {"id": "d2", "prompt": "later", "due_at": 1_900_000_100.0, "record": {}}
    _worker(handle, tmp_path, {"delayed": [restored, refused]})
    _resuming(handle)

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)
    totals = await manager.resume_restored_work(kernel._restart_request)

    # One delayed message came back, one did not: exactly one is unconfirmed,
    # and only the one that did not come back stays recorded.
    assert totals == {"pending": 0, "turns": 0, "delayed": 1, "unconfirmed": 1}
    reply = manager.receipts.get(accepted["record"]["id"])
    assert reply["resume"] == {
        "pending": 0,
        "turns": 0,
        "delayed": 1,
        "unconfirmed": 1,
    }
    # The final notice reports all four numbers off that same receipt.
    summary = ui_language.tr("reboot.resume_summary", locale="en", **reply["resume"])
    assert "1 delayed message(s)" in summary
    assert "1 unconfirmed" in summary
    assert scheduler.restored == ["d1"]
    written = json.loads(_manifests(tmp_path)[0].read_text(encoding="utf-8"))
    agent = written["agents"]["zelda"]
    assert agent["delayed"] == [refused]
    assert agent["unconfirmed"] == []


@pytest.mark.asyncio
async def test_an_unfinished_record_stops_the_cutover_and_is_kept(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    queued = {"request_id": "req-1", "chat_id": 42, "prompt": "queued"}
    seen: list[str] = []
    # The stop armed the record and wrote it, but never finished: no evidence.
    _worker(handle, tmp_path, {"pending": [queued], "phase": "recording"}, seen=seen)

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "force_capture_unconfirmed"
    assert seen == ["/stop"]
    assert not kernel.shutdown_event.is_set()
    assert kernel._restart_request is None
    assert record["resume"]["unconfirmed"] == 1
    assert _capture_file(tmp_path, accepted["record"]["id"]).exists()


def test_parser_accepts_force_alongside_delay():
    from orchestrator.runtime_reboot import parse_request

    assert parse_request("max delay=15 force") == (
        "max",
        {"delay_seconds": 900.0, "force": True},
    )
    assert parse_request("max force delay") == ("max", {"delay_seconds": 1800.0, "force": True})
    assert parse_request("max bogus") == (None, {})
# HASHI-Fingerprint: hfp_a098c810a7a4475282a2b62e57f1b229
