from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.skill_manager import SkillManager
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(self, *, name="momo", query=None, payload=None):
        self.match_info = {"name": name}
        self.query = query or {}
        self._payload = payload or {}

    async def json(self):
        return self._payload


class _FakeRuntime:
    name = "momo"

    def __init__(self, workspace_dir: Path, skill_manager: SkillManager):
        self.workspace_dir = workspace_dir
        self.skill_manager = skill_manager
        self.reruns = []

    async def _run_job_now(self, job, *, kind=None):
        self.reruns.append((kind, dict(job)))
        return True, f"Queued cron task [{job['id']}]"


def _server(tmp_path: Path) -> tuple[WorkbenchApiServer, _FakeRuntime]:
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "crons": [
                    {
                        "id": "daily-report",
                        "agent": "momo",
                        "enabled": True,
                        "schedule": "0 9 * * *",
                        "prompt": "send report",
                    },
                    {
                        "id": "other-agent",
                        "agent": "lily",
                        "enabled": True,
                        "schedule": "0 10 * * *",
                    },
                ],
                "heartbeats": [],
                "nudges": [],
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspaces" / "momo"
    workspace.mkdir(parents=True)
    manager = SkillManager(tmp_path, tasks_path)
    runtime = _FakeRuntime(workspace, manager)
    scheduler = SimpleNamespace(
        state={
            "crons": {"daily-report": 1_723_456_789.0},
            "heartbeats": {},
            "nudges": {},
            "recovery_batches": {
                "batch-1": {
                    "batch_id": "batch-1",
                    "agent": "momo",
                    "status": "pending",
                    "items": [
                        {
                            "kind": "cron",
                            "task_id": "daily-report",
                            "missed_count": 2,
                            "replay_limit": 1,
                            "due_at": [1.0, 2.0],
                        }
                    ],
                }
            },
        }
    )
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps({"global": {}, "agents": [{"name": "momo"}]}),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        deployment_profile="personal",
    )
    orchestrator = SimpleNamespace(runtimes=[runtime], scheduler=scheduler)
    return (
        WorkbenchApiServer(
            config_path=config_path,
            global_config=global_config,
            orchestrator=orchestrator,
        ),
        runtime,
    )


@pytest.mark.asyncio
async def test_scheduler_list_and_status_use_hashi_authority(tmp_path):
    server, _runtime = _server(tmp_path)

    listed = await server.handle_agent_scheduler_jobs(
        _FakeRequest(query={"kind": "cron", "enabled": "true"})
    )
    list_payload = json.loads(listed.text)
    assert listed.status == 200
    assert list_payload["authority"] == "HASHI Scheduler"
    assert [job["job_id"] for job in list_payload["jobs"]] == ["daily-report"]
    assert list_payload["jobs"][0]["pending_recovery"] == [
        {
            "batch_id": "batch-1",
            "status": "pending",
            "missed_count": 2,
            "replayable_count": 1,
        }
    ]
    assert list_payload["jobs"][0]["her_v2_effort_policy"] == {
        "effective": "zero",
        "source": "scheduled_direct_policy",
        "applies_to": "her-v2",
    }

    status = await server.handle_agent_scheduler_status(
        _FakeRequest(query={"kind": "cron", "job_id": "daily-report"})
    )
    status_payload = json.loads(status.text)
    assert status.status == 200
    assert status_payload["status"]["last_run"] == 1_723_456_789.0
    assert status_payload["status"]["job"]["schedule"] == "0 9 * * *"


@pytest.mark.asyncio
async def test_scheduler_run_history_reads_isolated_scheduler_receipts(tmp_path):
    server, runtime = _server(tmp_path)
    state_dir = runtime.workspace_dir / "state"
    state_dir.mkdir()
    (state_dir / "cross_session_receipts.json").write_text(
        json.dumps(
            {
                "version": 1,
                "next_sequence": 3,
                "receipts": [
                    {
                        "request_id": "req-direct",
                        "source": "telegram",
                        "summary": "direct",
                        "last_sequence": 1,
                    },
                    {
                        "request_id": "req-cron",
                        "source": "scheduler",
                        "summary": "Cron Task [daily-report]",
                        "status": "completed",
                        "completion_status": "completed",
                        "stop_reason": "end_turn",
                        "delivered": True,
                        "assistant_text": "report sent",
                        "last_sequence": 2,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    response = await server.handle_agent_scheduler_runs(
        _FakeRequest(query={"kind": "cron", "job_id": "daily-report", "limit": "5"})
    )
    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["count"] == 1
    assert payload["runs"][0]["request_id"] == "req-cron"
    assert payload["runs"][0]["result"] == "report sent"


@pytest.mark.asyncio
async def test_scheduler_gateway_rerun_requires_exact_single_job_authorization(tmp_path):
    server, runtime = _server(tmp_path)

    denied = await server.handle_agent_run_job(
        _FakeRequest(
            payload={
                "kind": "cron",
                "job_id": "daily-report",
                "requested_by": "hashi_tool_gateway",
            }
        )
    )
    assert denied.status == 403
    assert runtime.reruns == []

    accepted = await server.handle_agent_run_job(
        _FakeRequest(
            payload={
                "kind": "cron",
                "job_id": "daily-report",
                "requested_by": "hashi_tool_gateway",
                "authorization": "explicit_user_authorization",
            }
        )
    )
    payload = json.loads(accepted.text)
    assert accepted.status == 200
    assert payload["ok"] is True
    assert [(kind, job["id"]) for kind, job in runtime.reruns] == [
        ("cron", "daily-report")
    ]


@pytest.mark.asyncio
async def test_scheduler_agent_can_create_update_and_delete_owned_tasks(tmp_path):
    server, _runtime = _server(tmp_path)

    created = await server.handle_agent_scheduler_create(
        _FakeRequest(
            payload={
                "kind": "heartbeat",
                "task_id": "momo-loop-1",
                "interval_seconds": 60,
                "prompt": "check the work",
                "loop_max": 5,
            }
        )
    )
    created_payload = json.loads(created.text)
    assert created.status == 201
    assert created_payload["job"]["job"]["id"] == "momo-loop-1"

    updated = await server.handle_agent_scheduler_update(
        type(
            "Request",
            (),
            {
                "match_info": {"name": "momo", "job_id": "momo-loop-1"},
                "json": _FakeRequest(
                    payload={
                        "kind": "heartbeat",
                        "enabled": False,
                        "interval_seconds": 120,
                        "prompt": "check the work again",
                    }
                ).json,
            },
        )()
    )
    updated_payload = json.loads(updated.text)
    assert updated.status == 200
    assert updated_payload["job"]["job"]["enabled"] is False
    assert updated_payload["job"]["job"]["interval_seconds"] == 120

    deleted = await server.handle_agent_scheduler_delete(
        type(
            "Request",
            (),
            {
                "match_info": {"name": "momo", "job_id": "momo-loop-1"},
                "json": _FakeRequest(
                    payload={
                        "kind": "heartbeat",
                        "requested_by": "hashi_tool_gateway",
                        "authorization": "explicit_user_authorization",
                    }
                ).json,
            },
        )()
    )
    assert deleted.status == 200
    assert json.loads(deleted.text)["job_id"] == "momo-loop-1"


@pytest.mark.asyncio
async def test_scheduler_mutation_rejects_other_agent_job(tmp_path):
    server, _runtime = _server(tmp_path)
    denied = await server.handle_agent_scheduler_update(
        type(
            "Request",
            (),
            {
                "match_info": {"name": "momo", "job_id": "other-agent"},
                "json": _FakeRequest(payload={"kind": "cron", "enabled": False}).json,
            },
        )()
    )
    assert denied.status == 404


@pytest.mark.asyncio
async def test_scheduler_nudge_create_update_delete_uses_typed_owner_path(tmp_path):
    server, _runtime = _server(tmp_path)

    created = await server.handle_agent_scheduler_create(
        _FakeRequest(
            payload={
                "kind": "nudge",
                "task_id": "momo-nudge-1",
                "interval_minutes": 5,
                "exit_condition": "the report is sent",
                "max_nudges": 3,
            }
        )
    )
    created_payload = json.loads(created.text)
    assert created.status == 201
    assert created_payload["job"]["job"]["nudge_meta"]["max"] == 3
    assert "NUDGE_COMPLETE:momo-nudge-1" in created_payload["job"]["job"]["prompt"]

    updated = await server.handle_agent_scheduler_update(
        type(
            "Request",
            (),
            {
                "match_info": {"name": "momo", "job_id": "momo-nudge-1"},
                "json": _FakeRequest(
                    payload={
                        "kind": "nudge",
                        "interval_seconds": 480,
                        "exit_condition": "the report is sent and acknowledged",
                        "max_nudges": 4,
                    }
                ).json,
            },
        )()
    )
    updated_payload = json.loads(updated.text)
    assert updated.status == 200
    updated_job = updated_payload["job"]["job"]
    assert updated_job["interval_seconds"] == 480
    assert updated_job["nudge_meta"]["max"] == 4
    assert "acknowledged" in updated_job["prompt"]

    deleted = await server.handle_agent_scheduler_delete(
        type(
            "Request",
            (),
            {
                "match_info": {"name": "momo", "job_id": "momo-nudge-1"},
                "json": _FakeRequest(
                    payload={
                        "kind": "nudge",
                        "authorization": "explicit_user_authorization",
                    }
                ).json,
            },
        )()
    )
    assert deleted.status == 200


@pytest.mark.asyncio
async def test_superloop_agent_api_binds_create_manage_delete_to_owner(tmp_path):
    server, _runtime = _server(tmp_path)

    created = await server.handle_agent_superloop_create(
        _FakeRequest(payload={"goal": "keep the migration moving"})
    )
    created_payload = json.loads(created.text)
    assert created.status == 201
    assert created_payload["authority"] == "HASHI Superloop"
    loop_id = created_payload["loop_id"]

    listed = await server.handle_agent_superloops(_FakeRequest())
    listed_payload = json.loads(listed.text)
    assert listed.status == 200
    assert [item["loop_id"] for item in listed_payload["loops"]] == [loop_id]

    fetched = await server.handle_agent_superloop_get(
        type(
            "Request",
            (),
            {"match_info": {"name": "momo", "loop_id": loop_id}},
        )()
    )
    fetched_payload = json.loads(fetched.text)
    assert fetched.status == 200
    assert fetched_payload["loop"]["owner_agent"] == "momo"

    updated = await server.handle_agent_superloop_update(
        type(
            "Request",
            (),
            {
                "match_info": {"name": "momo", "loop_id": loop_id},
                "json": _FakeRequest(
                    payload={
                        "action": "task_add",
                        "title": "verify the next checkpoint",
                    }
                ).json,
            },
        )()
    )
    updated_payload = json.loads(updated.text)
    assert updated.status == 200
    assert updated_payload["task_id"]

    denied = await server.handle_agent_superloop_delete(
        type(
            "Request",
            (),
            {
                "match_info": {"name": "momo", "loop_id": loop_id},
                "json": _FakeRequest(
                    payload={"requested_by": "hashi_tool_gateway"}
                ).json,
            },
        )()
    )
    assert denied.status == 403

    deleted = await server.handle_agent_superloop_delete(
        type(
            "Request",
            (),
            {
                "match_info": {"name": "momo", "loop_id": loop_id},
                "json": _FakeRequest(
                    payload={
                        "requested_by": "hashi_tool_gateway",
                        "authorization": "explicit_user_authorization",
                    }
                ).json,
            },
        )()
    )
    deleted_payload = json.loads(deleted.text)
    assert deleted.status == 200
    assert deleted_payload["status"] == "deleted"

    listed_after = await server.handle_agent_superloops(_FakeRequest())
    assert json.loads(listed_after.text)["loops"] == []
