from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator import runtime_session
from orchestrator.session_store import SessionStore
from orchestrator.workbench_api import WorkbenchApiServer


@pytest.fixture
def api(tmp_path):
    workspace = tmp_path / "workspaces" / "momo"
    workspace.mkdir(parents=True)
    directories = [tmp_path / name for name in ("one", "two", "three")]
    for directory in directories:
        directory.mkdir()
        (directory / "keep.txt").write_text(directory.name, encoding="utf-8")
    config = tmp_path / "agents.json"
    config.write_text(json.dumps({"agents":[{"name":"momo","project_id":"p1"}]}), encoding="utf-8")
    runtime = SimpleNamespace(name="momo", workspace_dir=workspace, _workzone_state={"revision":0,"slots":[]})
    settings = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="test",
                               deployment_profile="personal", authorized_id=1)
    server = WorkbenchApiServer(config_path=config, global_config=settings,
        orchestrator=SimpleNamespace(runtimes=[runtime], scheduler=SimpleNamespace(state={})),
        secrets={"workbench_admin_token":"test-owner-token"}, reconcile_session_runs=False)
    runtime.session_store = server.session_store
    runtime.global_config = settings
    runtime.workzone_supported = True
    server._metadata_for_agent = lambda _row, _runtime: {
        "id":"momo", "name":"momo", "workspace_dir":str(workspace), "online":True,
        "workzone_revisioned_profile":bool(_runtime.workzone_supported),
        "workzone_state":_runtime._workzone_state,
    }
    return server, runtime, directories


def headers(token="test-owner-token"):
    return {"Authorization":f"Bearer {token}"}


@pytest.mark.asyncio
async def test_workzone_add_edit_remove_are_revision_checked_and_keep_directories(api):
    server, runtime, directories = api
    one, two, three = directories
    owner = SessionStore.owner_id_for(server.global_config)
    async with TestClient(TestServer(server.app)) as client:
        overview = await client.get("/api/agents/momo/overview", headers=headers())
        assert overview.status == 200
        assert (await overview.json())["overview"]["workzone"]["management_supported"] is True
        root = "/api/agents/momo/workzones"
        assert (await client.put(f"{root}/main", json={"expected_revision":0,"mode":"add","path":str(one)})).status == 401
        add = await client.put(f"{root}/main", headers=headers(),
                               json={"expected_revision":0,"mode":"add","path":str(one)})
        assert add.status == 200
        assert (await add.json())["revision"] == 1

        occupied = await client.put(f"{root}/main", headers=headers(),
                                    json={"expected_revision":1,"mode":"add","path":str(two)})
        assert occupied.status == 409
        assert (await occupied.json())["error_code"] == "workzone_slot_occupied"
        stale = await client.put(f"{root}/3", headers=headers(),
                                 json={"expected_revision":0,"mode":"add","path":str(two)})
        assert stale.status == 409
        assert (await stale.json())["error_code"] == "workzone_revision_conflict"
        add_three = await client.put(f"{root}/3", headers=headers(),
                                     json={"expected_revision":1,"mode":"add","path":str(two)})
        assert add_three.status == 200
        admitted = runtime_session.agent_workzone_state(runtime, owner=owner)
        edit = await client.put(f"{root}/3", headers=headers(),
                                json={"expected_revision":2,"mode":"replace","path":str(three)})
        assert edit.status == 200
        assert (await edit.json())["revision"] == 3
        assert runtime_session.session_workzone_state(runtime,
            SimpleNamespace(owner_id=owner,request_metadata={"workzone_snapshot":admitted}))["slots"][1]["path"] == str(two)
        latest = server.session_store.get_agent_workzone_set(owner_id=owner, agent_id="momo")
        assert [(item["slot_id"],item["path"]) for item in latest["slots"]] == [
            ("main",str(one)),("3",str(three)),
        ]
        for title in ("First session", "Second session"):
            session = server.session_store.create_session(owner_id=owner, agent_id="momo", title=title)
            state = runtime_session.session_workzone_state(runtime, session_id=session["session_id"])
            assert [(item["slot_id"],item["path"]) for item in state["slots"]] == [
                ("main",str(one)),("3",str(three)),
            ]
        pending = (await (await client.get("/api/agents/momo/overview", headers=headers())).json())["overview"]["workzone"]
        assert pending["pending_activation"] is True
        assert pending["management_supported"] is True

        no_confirm = await client.post(f"{root}/3/delete-if-unchanged", headers=headers(),
                                        json={"expected_revision":3})
        assert no_confirm.status == 403
        old_delete = await client.post(f"{root}/3/delete-if-unchanged", headers=headers(),
            json={"expected_revision":2,"authorization":"explicit_user_authorization"})
        assert old_delete.status == 409
        removed = await client.post(f"{root}/3/delete-if-unchanged", headers=headers(),
            json={"expected_revision":3,"authorization":"explicit_user_authorization"})
        assert removed.status == 200
        assert (await removed.json())["revision"] == 4
        latest = server.session_store.get_agent_workzone_set(owner_id=owner, agent_id="momo")
        assert [item["slot_id"] for item in latest["slots"]] == ["main"]
        assert (three / "keep.txt").read_text(encoding="utf-8") == "three"


@pytest.mark.asyncio
async def test_workzone_rejects_invalid_paths_duplicates_and_missing_targets(api, tmp_path):
    server, _runtime, directories = api
    one, two, _three = directories
    async with TestClient(TestServer(server.app)) as client:
        root = "/api/agents/momo/workzones"
        for invalid in (tmp_path / "missing", one / "keep.txt", tmp_path / "workspaces" / "momo"):
            result = await client.put(f"{root}/1", headers=headers(),
                json={"expected_revision":0,"mode":"add","path":str(invalid)})
            assert result.status == 400
        assert (await client.put(f"{root}/1", headers=headers(),
            json={"expected_revision":0,"mode":"add","path":str(one)})).status == 200
        duplicate = await client.put(f"{root}/2", headers=headers(),
            json={"expected_revision":1,"mode":"add","path":str(one)})
        assert duplicate.status == 400
        missing = await client.put(f"{root}/2", headers=headers(),
            json={"expected_revision":1,"mode":"replace","path":str(two)})
        assert missing.status == 409
        assert (await missing.json())["error_code"] == "workzone_slot_missing"
        for slot in ("0", "10", "other"):
            assert (await client.put(f"{root}/{slot}", headers=headers(),
                json={"expected_revision":1,"mode":"add","path":str(two)})).status == 400
        for invalid in (
            {"expected_revision":True,"mode":"add","path":str(two)},
            {"expected_revision":1,"mode":[],"path":str(two)},
            {"expected_revision":1,"mode":"add","path":[]},
            {"expected_revision":1,"mode":"add","path":"bad\npath"},
            {"expected_revision":1,"mode":"add","path":str(two),"extra":True},
        ):
            assert (await client.put(f"{root}/2", headers=headers(), json=invalid)).status == 400


@pytest.mark.asyncio
async def test_old_worker_does_not_advertise_or_accept_workzone_management(api):
    server, runtime, directories = api
    runtime.workzone_supported = False
    async with TestClient(TestServer(server.app)) as client:
        overview = await client.get("/api/agents/momo/overview", headers=headers())
        assert "management_supported" not in (await overview.json())["overview"]["workzone"]
        result = await client.put("/api/agents/momo/workzones/1", headers=headers(),
            json={"expected_revision":0,"mode":"add","path":str(directories[0])})
        assert result.status == 503
    owner = SessionStore.owner_id_for(server.global_config)
    assert server.session_store.get_agent_workzone_set(owner_id=owner,agent_id="momo")["slots"] == []



@pytest.mark.asyncio
async def test_concurrent_workzone_change_is_not_overwritten(api, monkeypatch):
    server, _runtime, directories = api
    owner = SessionStore.owner_id_for(server.global_config)
    original = server.session_store.set_agent_workzone_slot
    calls = 0

    def interleaved(**kwargs):
        nonlocal calls
        calls += 1
        original(owner_id=owner, agent_id="momo", slot_id="2", path=str(directories[1]),
                 enabled=True, expected_revision=0, source="test")
        return original(**kwargs)

    monkeypatch.setattr(server.session_store, "set_agent_workzone_slot", interleaved)
    async with TestClient(TestServer(server.app)) as client:
        result = await client.put("/api/agents/momo/workzones/1", headers=headers(),
            json={"expected_revision":0,"mode":"add","path":str(directories[0])})
        assert result.status == 409
        assert (await result.json())["error_code"] == "workzone_revision_conflict"
    state = server.session_store.get_agent_workzone_set(owner_id=owner, agent_id="momo")
    assert [(item["slot_id"],item["path"]) for item in state["slots"]] == [("2",str(directories[1]))]
    assert calls == 1



class GovernedIdentity:
    def get_session_user(self, token):
        return SimpleNamespace(id=token) if token in {"admin", "member", "outside"} else None

    def list_project_memberships(self, *, user_id):
        if user_id == "admin":
            return [{"role":"org_admin","project_id":"p1"}]
        if user_id == "member":
            return [{"role":"member","project_id":"p1"}]
        return [{"role":"member","project_id":"p2"}]


@pytest.mark.asyncio
async def test_governed_workzone_management_requires_access_and_management_role(api):
    server, _runtime, directories = api
    server.global_config.deployment_profile = "governed"
    server.identity_service = GovernedIdentity()
    body = {"expected_revision":0,"mode":"add","path":str(directories[0])}
    async with TestClient(TestServer(server.app)) as client:
        member = await client.get("/api/agents/momo/overview", headers=headers("member"))
        assert member.status == 200
        assert "management_supported" not in (await member.json())["overview"]["workzone"]
        assert (await client.put("/api/agents/momo/workzones/1", headers=headers("member"), json=body)).status == 403
        assert (await client.put("/api/agents/momo/workzones/1", headers=headers("outside"), json=body)).status == 404
        assert (await client.put("/api/agents/momo/workzones/1", headers=headers("admin"), json=body)).status == 200
    state = server.session_store.get_agent_workzone_set(owner_id="enterprise:admin",agent_id="momo")
    assert state["slots"][0]["path"] == str(directories[0])
