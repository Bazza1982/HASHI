from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.pcm import render_pcm_document
from orchestrator.workbench_api import WorkbenchApiServer


@pytest.fixture
def document_api(tmp_path):
    workspace = tmp_path / "workspaces" / "momo"
    workspace.mkdir(parents=True)
    original = render_pcm_document(persona="before", system="system")
    edited = render_pcm_document(persona="after", system="system")
    (workspace / "agent.md").write_text(original, encoding="utf-8", newline="")
    config = tmp_path / "agents.json"
    config.write_text(json.dumps({"agents": [{"name": "momo"}]}), encoding="utf-8")
    runtime = SimpleNamespace(name="momo", workspace_dir=workspace, startup_success=True)
    settings = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="test",
                               deployment_profile="personal", authorized_id=1)
    server = WorkbenchApiServer(config_path=config, global_config=settings,
        orchestrator=SimpleNamespace(runtimes=[runtime], scheduler=SimpleNamespace(state={})),
        secrets={"workbench_admin_token": "test-owner-token"}, reconcile_session_runs=False)
    server._metadata_for_agent = lambda _row, _runtime: {"workspace_dir": str(workspace)}
    headers = {"Authorization": "Bearer test-owner-token"}
    return SimpleNamespace(
        workspace=workspace, original=original, edited=edited, server=server, headers=headers,
    )


@pytest.mark.asyncio
async def test_external_agent_document_access_is_authorized_and_revision_checked(document_api):
    workspace = document_api.workspace
    original, edited = document_api.original, document_api.edited
    server, headers = document_api.server, document_api.headers
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


@pytest.mark.asyncio
async def test_agent_document_lock_wait_does_not_block_unrelated_api_requests(
    document_api, monkeypatch,
):
    from orchestrator import pcm
    from orchestrator.config_json import file_write_lock

    locked = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    writer_finished = threading.Event()
    document = document_api.workspace / "agent.md"

    def hold_document_lock():
        with file_write_lock(document):
            locked.set()
            if not release.wait(15):
                raise TimeoutError("test did not release the Agent document lock")

    @contextmanager
    def observe_writer_lock(path, *, timeout=5.0):
        waiting.set()
        try:
            with file_write_lock(path, timeout=timeout):
                yield
        finally:
            writer_finished.set()

    async with TestClient(TestServer(document_api.server.app)) as client:
        root = "/api/agents/momo/agent-document"
        opened = await client.get(root, headers=document_api.headers)
        assert opened.status == 200
        snapshot = await opened.json()
        assert (await client.get("/api/health")).status == 200
        monkeypatch.setattr(pcm, "file_write_lock", observe_writer_lock)

        async def save_document():
            return await client.put(root, headers=document_api.headers, json={
                "expected_revision": snapshot["revision"], "content": document_api.edited,
            })

        holder = asyncio.create_task(asyncio.to_thread(hold_document_lock))
        pending = None
        try:
            assert await asyncio.to_thread(locked.wait, 10)
            pending = asyncio.create_task(save_document())
            assert await asyncio.to_thread(waiting.wait, 10)
            health = await asyncio.wait_for(client.get("/api/health"), 10)
            assert health.status == 200
            # The health request must complete while the real writer lock is
            # still held and the document request is still waiting for it.
            assert not writer_finished.is_set(), "document lock wait blocked the API event loop"
            assert not pending.done()
        finally:
            release.set()
            await asyncio.wait_for(holder, 10)
            if pending is not None:
                saved = await asyncio.wait_for(pending, 10)

        assert saved.status == 200
        assert document.read_text(encoding="utf-8") == document_api.edited


@pytest.mark.asyncio
async def test_hcc_only_change_preserves_external_agent_document_conflict(document_api):
    from orchestrator.pcm import update_pcm_text

    async with TestClient(TestServer(document_api.server.app)) as client:
        root = "/api/agents/momo/agent-document"
        opened = await client.get(root, headers=document_api.headers)
        assert opened.status == 200
        snapshot = await opened.json()
        refreshed = render_pcm_document(
            persona="before", system="system", hcc="refreshed temporary context",
        )
        await asyncio.to_thread(
            update_pcm_text, document_api.workspace, lambda _document: refreshed,
        )
        stale = await client.put(root, headers=document_api.headers, json={
            "expected_revision": snapshot["revision"], "content": document_api.edited,
        })
        assert stale.status == 409
        assert (await stale.json())["error_code"] == "pcm_conflict"
        assert (document_api.workspace / "agent.md").read_text(encoding="utf-8") == refreshed
