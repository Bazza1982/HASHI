"""Habit reset reports the original failure and only claims verified restoration."""
from __future__ import annotations

import errno
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters import her_habits
from orchestrator import runtime_her_habits
from tests.test_runtime_her_habits import FakeRuntime, _callback_update, _seed_habit


def seeded_reset(tmp_path):
    runtime = FakeRuntime(tmp_path)
    adapter = runtime.backend_manager.current_backend
    habit_id = _seed_habit(adapter._her_habit_store(), "Fixture")
    adapter._her_meditation_journal().enqueue(
        job_id="c" * 32, request_id="fixture-reset", prompt="fixture maintenance", max_actions=1,
    )
    fingerprint = her_habits.habit_state_fingerprint(tmp_path)
    update, query = _callback_update("habit:confirm_reset:" + fingerprint)
    return runtime, update, query, habit_id


def query_text(query):
    return "\n".join([str(text or "") for text, _ in query.answers] + [row["text"] for row in query.edits])


def reset_failure_audit(tmp_path):
    rows = [json.loads(line) for line in (tmp_path / "backend_state" / "her_habit_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if row.get("event") == "habit_command_reset_failed"][-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,phase,reason,state", [
    ("prepare", "snapshot", "access_denied", "unchanged"),
    ("move", "move", "disk_full", "rolled_back"),
    ("finalize", "finalize", "io_error", "rolled_back"),
    ("rollback", "move", "disk_full", "incomplete"),
    ("move_ack", "move", "io_error", "rolled_back"),
])
async def test_reset_failure_has_true_restoration_state_and_one_audit_reference(
    tmp_path, monkeypatch, failure, phase, reason, state,
):
    runtime, update, query, _ = seeded_reset(tmp_path)
    original_inventory = her_habits.habit_state_inventory(tmp_path)
    original_replace = her_habits.os.replace
    original_mkdir = Path.mkdir
    original_write = her_habits._atomic_write_json

    def mkdir(self, *args, **kwargs):
        if failure == "prepare" and "her_habit_resets" in self.parts:
            raise PermissionError(errno.EACCES, "private-snapshot-path")
        return original_mkdir(self, *args, **kwargs)

    def replace(source, destination):
        source, destination = Path(source), Path(destination)
        if failure == "move_ack" and source == tmp_path / "habits":
            original_replace(source, destination)
            raise OSError(errno.EIO, "private-ack-lost-after-move")
        if failure in {"move", "rollback"} and source == tmp_path / "backend_state" / "her_habit_meditation":
            raise OSError(errno.ENOSPC, "private-second-move")
        if (failure == "rollback" and source.name == "habits"
                and source.parent.parent.name == "her_habit_resets" and destination == tmp_path / "habits"):
            raise PermissionError(errno.EACCES, "private-restore-path")
        return original_replace(source, destination)

    def write(destination, payload):
        if failure == "finalize" and Path(destination).name == "manifest.json" and payload.get("status") == "completed":
            raise OSError(errno.EIO, "private-manifest-finalize")
        return original_write(destination, payload)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(her_habits.os, "replace", replace)
    monkeypatch.setattr(her_habits, "_atomic_write_json", write)
    await runtime_her_habits.callback_habit(runtime, update, SimpleNamespace())
    snapshots_root = tmp_path / "backend_state" / "her_habit_resets"
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in snapshots_root.glob("*/manifest.json")]
    if state == "incomplete":
        assert len(manifests) == 1 and manifests[0]["status"] == "rollback_incomplete"
        assert not (tmp_path / "habits").exists()
        assert list(snapshots_root.glob("*/habits/*.json"))
    else:
        assert her_habits.habit_state_inventory(tmp_path) == original_inventory
    text = query_text(query)
    assert "habit_reset_" + phase + "_" + reason in text
    assert "private-" not in text
    reference = re.search(r"operr_[a-f0-9]{24}", text)
    assert reference is not None
    audit = reset_failure_audit(tmp_path)
    assert audit["diagnostic_reference"] == reference.group()
    assert audit["rollback_state"] == state
    assert "private-" not in json.dumps(audit)
    if manifests:
        assert manifests[0]["failure"]["reference"] == reference.group()
    if state == "incomplete":
        assert "could not fully restore" in text.lower()


@pytest.mark.asyncio
async def test_reset_recovery_never_overwrites_new_records_at_the_old_location(tmp_path, monkeypatch):
    runtime, update, query, _ = seeded_reset(tmp_path)
    original = her_habits.os.replace
    new_file = tmp_path / "habits" / "new-owner-record.json"
    def interrupted(source, destination):
        if Path(source) == tmp_path / "backend_state" / "her_habit_meditation":
            new_file.parent.mkdir()
            new_file.write_bytes(b'{"new":"owner record"}')
            raise OSError(errno.EIO, "private-concurrent-write")
        return original(source, destination)
    monkeypatch.setattr(her_habits.os, "replace", interrupted)
    await runtime_her_habits.callback_habit(runtime, update, SimpleNamespace())
    assert new_file.read_bytes() == b'{"new":"owner record"}'
    snapshots = tmp_path / "backend_state" / "her_habit_resets"
    assert list(snapshots.glob("*/habits/*.json"))
    manifest = json.loads(next(snapshots.glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["status"] == "rollback_incomplete"
    assert manifest["rollback_state"] == "incomplete"
    assert "could not fully restore" in query_text(query).lower()


@pytest.mark.asyncio
async def test_reset_does_not_claim_verified_restoration_when_verification_cannot_read(tmp_path, monkeypatch):
    runtime, update, query, habit_id = seeded_reset(tmp_path)
    target = tmp_path / "habits" / (habit_id + ".json")
    original_replace = her_habits.os.replace
    original_read = Path.read_bytes
    interrupted = False
    def replace(source, destination):
        nonlocal interrupted
        if Path(source) == tmp_path / "backend_state" / "her_habit_meditation":
            interrupted = True
            raise OSError(errno.ENOSPC, "private-second-move")
        return original_replace(source, destination)
    def denied(self):
        if interrupted and self == target:
            raise PermissionError(errno.EACCES, "private-verification")
        return original_read(self)
    monkeypatch.setattr(her_habits.os, "replace", replace)
    monkeypatch.setattr(Path, "read_bytes", denied)
    await runtime_her_habits.callback_habit(runtime, update, SimpleNamespace())
    manifest = json.loads(next((tmp_path / "backend_state" / "her_habit_resets").glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["status"] == "rollback_unconfirmed"
    assert manifest["rollback_state"] == "unconfirmed"
    assert "restoration is unconfirmed" in query_text(query).lower()
    assert reset_failure_audit(tmp_path)["rollback_state"] == "unconfirmed"
    assert original_read(target)


@pytest.mark.asyncio
async def test_reset_preflight_read_failure_is_visible_without_attempting_reset(tmp_path, monkeypatch):
    runtime, update, query, habit_id = seeded_reset(tmp_path)
    target = tmp_path / "habits" / (habit_id + ".json")
    original = Path.read_bytes
    def denied(self):
        if self == target:
            raise PermissionError(errno.EACCES, "private-preflight")
        return original(self)
    monkeypatch.setattr(Path, "read_bytes", denied)
    await runtime_her_habits.callback_habit(runtime, update, SimpleNamespace())
    text = query_text(query)
    assert "habit_reset_inventory_access_denied" in text and "operr_" in text
    assert "private-" not in text
    assert not (tmp_path / "backend_state" / "her_habit_resets").exists()
    assert reset_failure_audit(tmp_path)["rollback_state"] == "unchanged"
