from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.agent_overview import _system_prompt_overview
from orchestrator.bridge_memory import SysPromptManager
from orchestrator.skill_manager import SkillManager
from orchestrator.workbench_api import WorkbenchApiServer


class Request:
    def __init__(self, *, name="momo", job_id="", slot="", query=None, payload=None):
        self.match_info = {"name": name, "job_id": job_id, "slot": slot}
        self.query = query or {}
        self.headers = {}
        self.payload = payload or {}

    async def json(self):
        return self.payload


@pytest.fixture
def environment(tmp_path):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({
        "crons": [{"id": "momo-daily", "agent": "momo", "enabled": False,
                   "schedule": "0 9 * * *", "prompt": "old work"},
                  {"id": "other-daily", "agent": "other", "enabled": False}],
        "heartbeats": [], "nudges": [],
    }), encoding="utf-8")
    manager = SkillManager(tmp_path, tasks)
    workspace = tmp_path / "momo"
    workspace.mkdir()
    prompts = SysPromptManager(workspace)
    global_prompts = SysPromptManager(state_path=tmp_path / "global.json", shared=True, scope="global")
    runtime = SimpleNamespace(name="momo", skill_manager=manager,
                              sys_prompt_manager=prompts, global_sys_prompt_manager=global_prompts)
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.global_config = SimpleNamespace(deployment_profile="personal", authorized_id=1)
    server.orchestrator = SimpleNamespace(scheduler=SimpleNamespace(state={}))
    server._runtime_map = lambda: {"momo": runtime}
    return server, manager, prompts, global_prompts


async def job_revision(server, job_id, kind="cron"):
    result = await server.handle_agent_scheduler_jobs(Request(query={"kind": kind}))
    jobs = json.loads(result.text)["jobs"]
    return next(item["deletion_revision"] for item in jobs if item["job_id"] == job_id)


def delete_request(job_id, revision, *, kind="cron", only_disabled=False, name="momo"):
    return Request(name=name, job_id=job_id, payload={
        "kind": kind, "authorization": "explicit_user_authorization",
        "expected_revision": revision, "only_disabled": only_disabled,
    })


@pytest.mark.asyncio
async def test_guarded_delete_preserves_a_task_reenabled_after_confirmation(environment):
    server, manager, _prompts, _global = environment
    revision = await job_revision(server, "momo-daily")
    assert manager.set_job_enabled("cron", "momo-daily", True)[0]

    result = await server.handle_agent_scheduler_conditional_delete(
        delete_request("momo-daily", revision, only_disabled=True))

    assert result.status == 409
    assert manager.get_job("cron", "momo-daily")["enabled"] is True
    assert manager.get_job("cron", "other-daily") is not None


@pytest.mark.asyncio
async def test_guarded_delete_requires_confirmation_and_preserves_new_owner(environment):
    server, manager, _prompts, _global = environment
    revision = await job_revision(server, "momo-daily")
    denied = await server.handle_agent_scheduler_conditional_delete(
        Request(job_id="momo-daily", payload={"kind": "cron", "expected_revision": revision}))
    assert denied.status == 403

    tasks = manager._load_tasks()
    tasks["crons"][0]["agent"] = "other"
    manager._save_tasks(tasks)
    result = await server.handle_agent_scheduler_conditional_delete(
        delete_request("momo-daily", revision))
    assert result.status in (404, 409)
    assert manager.get_job("cron", "momo-daily")["agent"] == "other"


@pytest.mark.asyncio
async def test_guarded_delete_persists_only_the_confirmed_task(environment, tmp_path):
    server, manager, _prompts, _global = environment
    revision = await job_revision(server, "momo-daily")
    result = await server.handle_agent_scheduler_conditional_delete(
        delete_request("momo-daily", revision, only_disabled=True))
    assert result.status == 200
    reloaded = SkillManager(tmp_path, tmp_path / "tasks.json")
    assert reloaded.get_job("cron", "momo-daily") is None
    assert reloaded.get_job("cron", "other-daily") is not None


@pytest.mark.asyncio
async def test_guarded_delete_rejects_a_change_between_read_and_commit(environment, monkeypatch):
    server, manager, _prompts, _global = environment
    revision = await job_revision(server, "momo-daily")
    save = manager._save_tasks

    def race(stale):
        other = SkillManager(manager.project_root, manager.tasks_path)
        assert other.set_job_enabled("cron", "momo-daily", True)[0]
        return save(stale)

    monkeypatch.setattr(manager, "_save_tasks", race)
    result = await server.handle_agent_scheduler_conditional_delete(
        delete_request("momo-daily", revision, only_disabled=True))
    assert result.status == 409
    assert manager.get_job("cron", "momo-daily")["enabled"] is True


@pytest.mark.asyncio
async def test_active_heartbeat_is_rechecked_using_its_own_persistent_snapshot(environment):
    server, manager, _prompts, _global = environment
    manager._save_active_heartbeats([{
        "id": "momo-active-heartbeat", "agent": "momo", "enabled": False,
        "managed_by": "active-command", "interval_seconds": 60,
    }])
    revision = await job_revision(server, "momo-active-heartbeat", "heartbeat")
    assert manager.set_job_enabled("heartbeat", "momo-active-heartbeat", True)[0]

    result = await server.handle_agent_scheduler_conditional_delete(
        delete_request("momo-active-heartbeat", revision, kind="heartbeat", only_disabled=True))
    assert result.status == 409
    assert manager.get_job("heartbeat", "momo-active-heartbeat")["enabled"] is True


@pytest.mark.asyncio
async def test_guarded_delete_supports_legacy_active_heartbeat_array(environment):
    server, manager, _prompts, _global = environment
    manager.active_heartbeats_path.write_text(json.dumps([{
        "id": "momo-active-heartbeat", "agent": "momo", "enabled": False,
        "managed_by": "active-command", "interval_seconds": 60,
    }]), encoding="utf-8")
    revision = await job_revision(server, "momo-active-heartbeat", "heartbeat")
    result = await server.handle_agent_scheduler_conditional_delete(
        delete_request("momo-active-heartbeat", revision, kind="heartbeat", only_disabled=True))
    assert result.status == 200
    assert manager.get_job("heartbeat", "momo-active-heartbeat") is None


@pytest.mark.asyncio
async def test_active_heartbeat_commit_uses_the_revision_it_read(environment, monkeypatch):
    server, manager, _prompts, _global = environment
    manager._save_active_heartbeats([{
        "id": "momo-active-heartbeat", "agent": "momo", "enabled": False,
        "managed_by": "active-command", "interval_seconds": 60,
    }])
    revision = await job_revision(server, "momo-active-heartbeat", "heartbeat")
    save = manager._save_json

    def race(path, payload):
        if path == manager.active_heartbeats_path:
            other = SkillManager(manager.project_root, manager.tasks_path)
            assert other.set_job_enabled("heartbeat", "momo-active-heartbeat", True)[0]
        return save(path, payload)

    monkeypatch.setattr(manager, "_save_json", race)
    result = await server.handle_agent_scheduler_conditional_delete(
        delete_request("momo-active-heartbeat", revision, kind="heartbeat", only_disabled=True))
    assert result.status == 409
    assert manager.get_job("heartbeat", "momo-active-heartbeat")["enabled"] is True


@pytest.mark.asyncio
async def test_guarded_operations_respect_governed_agent_visibility(environment, monkeypatch):
    server, manager, prompts, _global = environment
    prompts.save("1", "private")
    job_token = await job_revision(server, "momo-daily")
    prompt_token = _system_prompt_overview(prompts, agent_name="momo")["slots"][0]["deletion_revision"]
    monkeypatch.setattr(server, "_is_governed_profile", lambda: True)
    monkeypatch.setattr(server, "_enterprise_user_from_request", lambda _request: SimpleNamespace(id="user"))
    monkeypatch.setattr(server, "_load_agent_rows", lambda: [{"name": "momo"}])
    monkeypatch.setattr(server, "_filter_enterprise_agent_rows_for_user", lambda _user, _rows: [])

    job_result = await server.handle_agent_scheduler_conditional_delete(delete_request("momo-daily", job_token))
    prompt_result = await server.handle_agent_sys_prompt_conditional_delete(Request(
        slot="1", payload={"authorization": "explicit_user_authorization", "expected_revision": prompt_token}))
    assert job_result.status == 404
    assert prompt_result.status == 404
    assert manager.get_job("cron", "momo-daily") is not None
    assert prompts.get_slot("1")["text"] == "private"


@pytest.mark.asyncio
async def test_shared_worker_facade_uses_the_agent_local_prompt_store(environment, monkeypatch):
    server, _manager, prompts, _global = environment
    prompts.save("1", "facade prompt")
    token = _system_prompt_overview(prompts, agent_name="momo")["slots"][0]["deletion_revision"]
    facade = SimpleNamespace(name="momo")
    monkeypatch.setattr(server, "_runtime_map", lambda: {"momo": facade})
    monkeypatch.setattr(server, "_load_agent_rows", lambda: [{"name": "momo"}])
    monkeypatch.setattr(server, "_metadata_for_agent", lambda _row, _runtime: {
        "workspace_dir": str(prompts.state_path.parent),
    })

    result = await server.handle_agent_sys_prompt_conditional_delete(Request(
        slot="1", payload={"authorization": "explicit_user_authorization", "expected_revision": token}))
    assert result.status == 200
    assert prompts.get_slot("1")["text"] == ""


def test_prompt_confirmation_revision_is_bound_to_agent_and_slot(environment):
    _server, _manager, prompts, _global = environment
    prompts.save("1", "same")
    prompts.save("2", "same")
    momo = _system_prompt_overview(prompts, agent_name="momo")["slots"]
    other = _system_prompt_overview(prompts, agent_name="other")["slots"]
    assert momo[0]["deletion_revision"] != momo[1]["deletion_revision"]
    assert momo[0]["deletion_revision"] != other[0]["deletion_revision"]


@pytest.mark.asyncio
async def test_prompt_guard_rejects_changed_content_and_never_touches_global(environment):
    server, _manager, prompts, global_prompts = environment
    original = "x" * 70 + "A"
    prompts.save("1", original)
    prompts.activate("1")
    global_prompts.save("1", "global instruction")
    snapshot = _system_prompt_overview(prompts, agent_name="momo")["slots"][0]
    assert original not in json.dumps(snapshot)

    prompts.replace("1", "x" * 70 + "B")
    request = Request(slot="1", payload={
        "authorization": "explicit_user_authorization",
        "expected_revision": snapshot["deletion_revision"],
    })
    result = await server.handle_agent_sys_prompt_conditional_delete(request)
    assert result.status == 409
    assert prompts.get_slot("1") == {"text": "x" * 70 + "B", "active": True}
    assert global_prompts.get_slot("1")["text"] == "global instruction"

    current = _system_prompt_overview(prompts, agent_name="momo")["slots"][0]
    request.payload["expected_revision"] = current["deletion_revision"]
    result = await server.handle_agent_sys_prompt_conditional_delete(request)
    assert result.status == 200
    assert SysPromptManager(prompts.state_path.parent).get_slot("1") == {"text": "", "active": False}
    assert global_prompts.get_slot("1")["text"] == "global instruction"


@pytest.mark.asyncio
async def test_prompt_guard_rechecks_at_persistence_after_a_concurrent_edit(environment, monkeypatch):
    server, _manager, prompts, _global = environment
    prompts.save("1", "before")
    snapshot = _system_prompt_overview(prompts, agent_name="momo")["slots"][0]
    delete = prompts.delete

    def race(slot, *, expected_slot):
        prompts.replace(slot, "newer")
        return delete(slot, expected_slot=expected_slot)

    monkeypatch.setattr(prompts, "delete", race)
    result = await server.handle_agent_sys_prompt_conditional_delete(Request(
        slot="1", payload={"authorization": "explicit_user_authorization",
                           "expected_revision": snapshot["deletion_revision"]}))
    assert result.status == 409
    assert prompts.get_slot("1")["text"] == "newer"
