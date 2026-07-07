from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.background_jobs import BackgroundJobManager
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(self, payload=None, *, query=None, match_info=None):
        self._payload = payload or {}
        self.query = query or {}
        self.match_info = match_info or {}

    async def json(self):
        return self._payload


class _FakeRecord:
    def __init__(self, job_id="job-test", state="running"):
        self.job_id = job_id
        self.state = state

    def to_dict(self):
        return {"job_id": self.job_id, "state": self.state, "agent": "zelda"}


class _FakeBackgroundJobManager:
    def __init__(self):
        self.started = []
        self.records = [_FakeRecord()]
        self.cancelled = []

    async def start_job(self, **kwargs):
        self.started.append(kwargs)
        return self.records[0]

    def list(self, *, agent=None, states=None, limit=50):
        self.last_list = {"agent": agent, "states": states, "limit": limit}
        return self.records

    def get(self, job_id):
        return self.records[0] if job_id == "job-test" else None

    def tail(self, job_id, *, stream="stdout", lines=80):
        if job_id != "job-test":
            raise KeyError(job_id)
        return f"{stream}:{lines}:heartbeat"

    async def cancel(self, job_id):
        if job_id != "job-test":
            raise KeyError(job_id)
        self.cancelled.append(job_id)
        return _FakeRecord(job_id=job_id, state="cancelled")


def _server(tmp_path: Path, manager=None) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": []}), encoding="utf-8")
    global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        workbench_port=18800,
        api_gateway_port=18801,
    )
    orchestrator = SimpleNamespace(kernel=SimpleNamespace(background_job_manager=manager))
    return WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        orchestrator=orchestrator,
    )


@pytest.mark.asyncio
async def test_background_jobs_start_uses_live_manager(tmp_path):
    manager = _FakeBackgroundJobManager()
    server = _server(tmp_path, manager=manager)

    response = await server.handle_background_jobs_start(
        _FakeRequest(
            {
                "agent": "zelda",
                "argv": ["python3", "-c", "print('heartbeat')"],
                "cwd": str(tmp_path),
                "origin": {"source": "test"},
            }
        )
    )

    payload = json.loads(response.text)
    assert response.status == 201
    assert payload["ok"] is True
    assert payload["job"]["job_id"] == "job-test"
    assert manager.started[0]["agent"] == "zelda"
    assert manager.started[0]["argv"] == ["python3", "-c", "print('heartbeat')"]
    assert manager.started[0]["origin"]["api_path"] == "/api/background-jobs"


@pytest.mark.asyncio
async def test_background_jobs_start_with_real_manager_completes_and_notifies(tmp_path):
    sent = []

    async def send_long_message(**kwargs):
        sent.append(kwargs)
        return 0.0, 1

    runtime = SimpleNamespace(
        name="zelda",
        current_request_meta={
            "chat_id": 123,
            "request_id": "req-workbench-bg",
            "summary": "Workbench background smoke",
        },
        send_long_message=send_long_message,
    )
    kernel = SimpleNamespace(runtimes=[runtime], background_job_manager=None)
    manager = BackgroundJobManager(tmp_path / "background_jobs", kernel=kernel)
    await manager.start()
    kernel.background_job_manager = manager
    server = _server(tmp_path, manager=manager)

    response = await server.handle_background_jobs_start(
        _FakeRequest(
            {
                "agent": "zelda",
                "argv": [sys.executable, "-c", "print('workbench background done')"],
                "cwd": str(tmp_path),
                "origin": {"source": "test"},
            }
        )
    )

    payload = json.loads(response.text)
    assert response.status == 201
    assert payload["ok"] is True
    job_id = payload["job"]["job_id"]

    await manager._monitor_tasks[job_id]
    saved = manager.get(job_id)
    assert saved is not None
    assert saved.state == "succeeded"
    assert saved.notification["delivered"] is True
    assert sent and sent[0]["chat_id"] == 123
    assert sent[0]["request_id"] == "req-workbench-bg"
    assert "workbench background done" in manager.tail(job_id)


@pytest.mark.asyncio
async def test_background_jobs_list_get_tail_cancel(tmp_path):
    manager = _FakeBackgroundJobManager()
    server = _server(tmp_path, manager=manager)

    list_response = await server.handle_background_jobs_list(
        _FakeRequest(query={"agent": "zelda", "state": "running,created", "limit": "5"})
    )
    get_response = await server.handle_background_jobs_get(_FakeRequest(match_info={"job_id": "job-test"}))
    tail_response = await server.handle_background_jobs_tail(
        _FakeRequest(query={"stream": "stderr", "lines": "3"}, match_info={"job_id": "job-test"})
    )
    cancel_response = await server.handle_background_jobs_cancel(_FakeRequest(match_info={"job_id": "job-test"}))

    assert json.loads(list_response.text)["jobs"][0]["job_id"] == "job-test"
    assert manager.last_list == {"agent": "zelda", "states": {"running", "created"}, "limit": 5}
    assert json.loads(get_response.text)["job"]["state"] == "running"
    assert json.loads(tail_response.text)["tail"] == "stderr:3:heartbeat"
    assert json.loads(cancel_response.text)["job"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_background_jobs_return_503_without_manager(tmp_path):
    server = _server(tmp_path, manager=None)

    response = await server.handle_background_jobs_list(_FakeRequest())

    payload = json.loads(response.text)
    assert response.status == 503
    assert payload == {"ok": False, "error": "BackgroundJobManager is not running"}
