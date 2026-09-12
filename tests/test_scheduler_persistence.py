from __future__ import annotations

import json

from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.scheduler import TaskScheduler


def _legacy(value: dict) -> bytes:
    rendered = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\r\n")
    return b"\xef\xbb\xbf" + (rendered + "\r\n").encode("utf-8")


def _state() -> dict:
    return {
        "heartbeats": {},
        "crons": {},
        "nudges": {},
        "missed_crons": {},
        "missed_heartbeats": {},
        "recovery_batches": {},
        "delayed_messages": {},
        "extension": {"keep": True},
    }


def _scheduler(tmp_path, *, state_bytes: bytes | None = None, tasks_bytes: bytes | None = None):
    tasks = tmp_path / "tasks.json"
    state = tmp_path / "state" / "scheduler.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    if state_bytes is not None:
        state.write_bytes(state_bytes)
    if tasks_bytes is not None:
        tasks.write_bytes(tasks_bytes)
    return TaskScheduler(tasks, state, [], 0), tasks, state


def test_scheduler_state_and_tasks_accept_legacy_bytes_then_publish_utf8_lf(tmp_path):
    scheduler, tasks_path, state_path = _scheduler(
        tmp_path,
        state_bytes=_legacy(_state()),
        tasks_bytes=_legacy(
            {
                "heartbeats": [],
                "crons": [],
                "nudges": [],
                "extension": "kept",
            }
        ),
    )

    scheduler.state["nudges"]["changed"] = 1
    assert scheduler._save_state() is True
    tasks = scheduler._load_tasks()
    tasks["nudges"].append({"id": "n1", "agent": "a", "enabled": False})
    assert scheduler._save_tasks(tasks) is True

    for path in (state_path, tasks_path):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw
        assert raw.endswith(b"\n")
    assert read_config_json(state_path)["extension"] == {"keep": True}
    assert read_config_json(tasks_path)["extension"] == "kept"


def test_scheduler_does_not_replace_corrupt_saved_state_or_tasks(tmp_path):
    scheduler, tasks_path, state_path = _scheduler(
        tmp_path,
        state_bytes=b'{"broken":',
        tasks_bytes=b'{"also_broken":',
    )

    assert scheduler._save_state() is False
    tasks = scheduler._load_tasks()
    assert scheduler._save_tasks(tasks) is False
    assert state_path.read_bytes() == b'{"broken":'
    assert tasks_path.read_bytes() == b'{"also_broken":'


def test_scheduler_rejects_a_stale_state_snapshot(tmp_path):
    scheduler, _tasks_path, state_path = _scheduler(
        tmp_path,
        state_bytes=_legacy(_state()),
    )
    winner = read_config_json(state_path)
    winner["external"] = "winner"
    write_config_json(state_path, winner)

    scheduler.state["external"] = "stale"
    assert scheduler._save_state() is False
    assert read_config_json(state_path)["external"] == "winner"
