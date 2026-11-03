from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.workbench_api import WorkbenchApiServer


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["roster", "overview"])
async def test_slow_agent_projection_does_not_block_independent_http_request(tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entered, release = threading.Event(), threading.Event()
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server._runtime_map = lambda: {}
    server._load_agent_rows = lambda **kwargs: [{"name": "agent"}]
    server._is_governed_profile = lambda: False
    server._v1_owner_id = lambda _request: "user:7"
    server._workzone_profile_ready = lambda *_: False
    server._can_view_deletion_revision = lambda *_: False
    server._parked_topic_guarded_writes_ready = lambda *_: False
    server.session_store = SimpleNamespace(
        get_agent_workzone_set=lambda **kwargs: {"revision": 0, "slots": []},
    )

    def slow_projection(*_args, **_kwargs):
        entered.set()
        assert release.wait(1.2), "Bounded cleanup timer must release projection"
        return {"id": "agent", "name": "agent", "workspace_dir": str(workspace), "online": False}

    server._metadata_for_agent = slow_projection
    app = web.Application()
    app.router.add_get("/agents", server.handle_agents)
    app.router.add_get("/agents/{name}/overview", server.handle_agent_overview)
    async def independent(_request):
        return web.json_response({"ready": True})

    app.router.add_get("/independent", independent)
    timer = threading.Timer(0.8, release.set)
    timer.start()
    try:
        async with TestClient(TestServer(app)) as client:
            started = time.monotonic()
            slow = asyncio.create_task(client.get("/agents" if kind == "roster" else "/agents/agent/overview"))
            assert await asyncio.to_thread(entered.wait, 1.5)
            probe = await client.get("/independent")
            elapsed = time.monotonic() - started
            assert probe.status == 200
            assert await probe.json() == {"ready": True}
            release.set()
            assert (await slow).status == 200
            assert elapsed < 0.5, "Unrelated HTTP request queued behind synchronous projection"
    finally:
        release.set()
        timer.cancel()
        timer.join()
