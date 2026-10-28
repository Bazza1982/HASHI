from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.agent_overview import _system_prompt_overview
from orchestrator.bridge_memory import SysPromptManager
from orchestrator.skill_manager import SkillManager, scheduler_job_deletion_revision
from orchestrator.workbench_api import WorkbenchApiServer


@pytest.fixture
def api(tmp_path):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({
        "crons": [{"id": "daily", "agent": "momo", "enabled": False,
                   "schedule": "0 9 * * *", "prompt": "do work"}],
        "heartbeats": [], "nudges": [],
    }), encoding="utf-8")
    workspace = tmp_path / "workspaces" / "momo"
    workspace.mkdir(parents=True)
    prompts = SysPromptManager(workspace)
    prompts.save("1", "private instruction")
    manager = SkillManager(tmp_path, tasks)
    runtime = SimpleNamespace(name="momo", skill_manager=manager, sys_prompt_manager=prompts)
    config = tmp_path / "agents.json"
    config.write_text(json.dumps({"agents": [{"name": "momo", "project_id": "p1"}]}),
                      encoding="utf-8")
    settings = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path,
                               deployment_profile="personal", authorized_id=1)
    server = WorkbenchApiServer(
        config_path=config, global_config=settings,
        orchestrator=SimpleNamespace(runtimes=[runtime], scheduler=SimpleNamespace(state={})),
        secrets={"workbench_admin_token": "test-owner-token"},
    )
    return server, manager, prompts


def headers(token="test-owner-token"):
    return {"Authorization": f"Bearer {token}"}


def job_payload(manager):
    return {"kind": "cron", "authorization": "explicit_user_authorization",
            "expected_revision": scheduler_job_deletion_revision(manager.get_job("cron", "daily"))}


def prompt_payload(prompts):
    return {"authorization": "explicit_user_authorization", "expected_revision":
            _system_prompt_overview(prompts, agent_name="momo")["slots"][0]["deletion_revision"]}


@pytest.mark.asyncio
async def test_http_deletion_requires_credentials_even_with_valid_revisions(api):
    server, manager, prompts = api
    confirmed_job = job_payload(manager)
    confirmed_prompt = prompt_payload(prompts)
    async with TestClient(TestServer(server.app)) as client:
        for token_headers in ({}, headers("wrong")):
            job = await client.post("/api/agents/momo/scheduler/jobs/daily/delete-if-unchanged",
                                    headers=token_headers, json=confirmed_job)
            prompt = await client.post("/api/agents/momo/system-prompts/1/delete-if-unchanged",
                                       headers=token_headers, json=confirmed_prompt)
            legacy = await client.delete("/api/agents/momo/scheduler/jobs/daily",
                                         headers=token_headers, json=confirmed_job)
            assert (job.status, prompt.status, legacy.status) == (401, 401, 401)
        assert manager.get_job("cron", "daily") is not None
        assert prompts.get_slot("1")["text"] == "private instruction"


@pytest.mark.asyncio
async def test_http_authorized_delete_requires_confirmation_and_current_revision(api):
    server, manager, prompts = api
    async with TestClient(TestServer(server.app)) as client:
        body = job_payload(manager)
        job_url = "/api/agents/momo/scheduler/jobs/daily/delete-if-unchanged"
        denied = await client.post(job_url, headers=headers(),
                                   json={"kind": "cron", "expected_revision": body["expected_revision"]})
        assert denied.status == 403
        assert manager.set_job_enabled("cron", "daily", True)[0]
        stale = await client.post(job_url, headers=headers(), json=body)
        assert stale.status == 409
        success = await client.post(job_url, headers=headers(), json=job_payload(manager))
        assert success.status == 200
        assert manager.get_job("cron", "daily") is None

        body = prompt_payload(prompts)
        prompts.replace("1", "changed")
        url = "/api/agents/momo/system-prompts/1/delete-if-unchanged"
        stale = await client.post(url, headers=headers(), json=body)
        assert stale.status == 409
        success = await client.post(url, headers=headers(), json=prompt_payload(prompts))
        assert success.status == 200
        assert prompts.get_slot("1")["text"] == ""


@pytest.mark.asyncio
async def test_legacy_delete_cannot_bypass_revision_confirmation(api):
    server, manager, _ = api
    async with TestClient(TestServer(server.app)) as client:
        url = "/api/agents/momo/scheduler/jobs/daily"
        no_revision = await client.delete(url, headers=headers(),
            json={"kind": "cron", "authorization": "explicit_user_authorization"})
        assert no_revision.status in (400, 428)
        assert manager.get_job("cron", "daily") is not None
        old = job_payload(manager)
        assert manager.set_job_enabled("cron", "daily", True)[0]
        stale = await client.delete(url, headers=headers(), json=old)
        assert stale.status == 409
        success = await client.delete(url, headers=headers(), json=job_payload(manager))
        assert success.status == 200


@pytest.mark.asyncio
async def test_revision_reads_are_not_exposed_without_authentication(api):
    server, _manager, prompts = api
    server._metadata_for_agent = lambda _row, _runtime: {
        "id": "momo", "name": "momo", "workspace_dir": str(prompts.state_path.parent),
    }
    async with TestClient(TestServer(server.app)) as client:
        for url in ("/api/agents/momo/overview",
                    "/api/agents/momo/scheduler/jobs?kind=cron",
                    "/api/agents/momo/scheduler/status?kind=cron&job_id=daily"):
            anonymous = await client.get(url)
            if anonymous.status == 200:
                assert "deletion_revision" not in json.dumps(await anonymous.json())
            else:
                assert anonymous.status == 401
            authorized = await client.get(url, headers=headers())
            assert authorized.status == 200
            assert "deletion_revision" in json.dumps(await authorized.json())


@pytest.mark.asyncio
async def test_unconfigured_personal_token_fails_closed(api):
    server, manager, prompts = api
    server.admin_token = ""
    async with TestClient(TestServer(server.app)) as client:
        job = await client.post("/api/agents/momo/scheduler/jobs/daily/delete-if-unchanged",
                                headers=headers(), json=job_payload(manager))
        prompt = await client.post("/api/agents/momo/system-prompts/1/delete-if-unchanged",
                                   headers=headers(), json=prompt_payload(prompts))
        assert (job.status, prompt.status) == (401, 401)
        assert manager.get_job("cron", "daily") is not None
        assert prompts.get_slot("1")["text"] == "private instruction"


class GovernedIdentity:
    def get_session_user(self, token):
        valid = {"admin", "member", "outside", "team", "other-team", "security"}
        return SimpleNamespace(id=token) if token in valid else None

    def list_project_memberships(self, *, user_id):
        if user_id == "admin":
            return [{"role": "org_admin", "project_id": "p1"}]
        if user_id == "team":
            return [{"role": "team_admin", "project_id": "p1"}]
        if user_id == "other-team":
            return [{"role": "team_admin", "project_id": "p2"}]
        if user_id == "security":
            return [{"role": "security_admin", "project_id": "p1"}]
        if user_id == "member":
            return [{"role": "member", "project_id": "p1"}]
        return [{"role": "member", "project_id": "p2"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("admin_token", ["admin", "team"])
async def test_governed_delete_requires_management_role_and_visibility(api, admin_token):
    server, manager, prompts = api
    server.global_config.deployment_profile = "governed"
    server.identity_service = GovernedIdentity()
    async with TestClient(TestServer(server.app)) as client:
        read_url = "/api/agents/momo/scheduler/jobs?kind=cron"
        for token, can_view in (("member", True), ("outside", False), ("invalid", False)):
            response = await client.get(read_url, headers=headers(token))
            assert ("deletion_revision" in json.dumps(await response.json())) is can_view
        for token, status in (("member", 403), ("outside", 404),
                              ("other-team", 403), ("security", 403)):
            job = await client.post("/api/agents/momo/scheduler/jobs/daily/delete-if-unchanged",
                                    headers=headers(token), json=job_payload(manager))
            prompt = await client.post("/api/agents/momo/system-prompts/1/delete-if-unchanged",
                                       headers=headers(token), json=prompt_payload(prompts))
            legacy = await client.delete("/api/agents/momo/scheduler/jobs/daily",
                                         headers=headers(token), json=job_payload(manager))
            assert (job.status, prompt.status, legacy.status) == (status, status, status)
        assert manager.get_job("cron", "daily") is not None
        assert prompts.get_slot("1")["text"] == "private instruction"
        job = await client.post("/api/agents/momo/scheduler/jobs/daily/delete-if-unchanged",
                                headers=headers(admin_token), json=job_payload(manager))
        prompt = await client.post("/api/agents/momo/system-prompts/1/delete-if-unchanged",
                                   headers=headers(admin_token), json=prompt_payload(prompts))
        assert (job.status, prompt.status) == (200, 200)
