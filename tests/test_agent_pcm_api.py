from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.pcm import render_pcm_document
from orchestrator.workbench_api import WorkbenchApiServer


@pytest.mark.asyncio
async def test_external_agent_document_access_is_authorized_and_revision_checked(tmp_path):
    workspace = tmp_path / "workspaces" / "momo"
    workspace.mkdir(parents=True)
    original = render_pcm_document(persona="before", system="system")
    edited = render_pcm_document(persona="after", system="system")
    (workspace / "agent.md").write_text(original, encoding="utf-8", newline="")
    config = tmp_path / "agents.json"
    config.write_text(json.dumps({"agents": [{"name": "momo"}]}), encoding="utf-8")
    runtime = SimpleNamespace(name="momo", workspace_dir=workspace)
    settings = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="test",
                               deployment_profile="personal", authorized_id=1)
    server = WorkbenchApiServer(config_path=config, global_config=settings,
        orchestrator=SimpleNamespace(runtimes=[runtime], scheduler=SimpleNamespace(state={})),
        secrets={"workbench_admin_token": "test-owner-token"}, reconcile_session_runs=False)
    server._metadata_for_agent = lambda _row, _runtime: {"workspace_dir": str(workspace)}
    headers = {"Authorization": "Bearer test-owner-token"}
    async with TestClient(TestServer(server.app)) as client:
        root = "/api/agents/momo/agent-document"
        assert (await client.get(root)).status == 401
        opened = await client.get(root, headers=headers)
        assert opened.status == 200
        snapshot = await opened.json()
        assert snapshot["content"] == original
        assert "no-store" in opened.headers["Cache-Control"]
        assert (await client.put(root, headers=headers, json={
            "expected_revision": snapshot["revision"], "content": "invalid",
        })).status == 400
        saved = await client.put(root, headers=headers, json={
            "expected_revision": snapshot["revision"], "content": edited,
        })
        assert saved.status == 200
        stale = await client.put(root, headers=headers, json={
            "expected_revision": snapshot["revision"], "content": original,
        })
        assert stale.status == 409
        assert (await stale.json())["error_code"] == "pcm_conflict"
        assert (workspace / "agent.md").read_text(encoding="utf-8") == edited
        assert (await client.get("/api/agents/other/agent-document", headers=headers)).status == 404
