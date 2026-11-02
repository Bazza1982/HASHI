"""Read-only Session/history endpoints must leave the shared HTTP loop free."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.workbench_api import WorkbenchApiServer


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["history", "messages", "sessions", "session", "run", "events"])
async def test_slow_history_read_does_not_queue_unrelated_http_request(tmp_path, kind):
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(1.2), "bounded test cleanup did not release read"
        return {
            "history": {"messages": [], "history_complete": True, "next_boundary": None},
            "messages": [], "sessions": [], "session": {"session_id": "session"},
            "run": {"session_id": "session", "final_message_id": None}, "events": [],
        }[kind]
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server._v1_owner_id = lambda request: "user:7"
    server._runtime_map = lambda: {"alice": SimpleNamespace()}
    server.global_config = SimpleNamespace(instance_id="SB-HTTP")
    server.admin_token = "synthetic"
    server.config_path = tmp_path / "agents.json"
    server.session_store = SimpleNamespace(
        agent_history_page=slow, messages=slow, list_sessions=slow,
        get_session=slow, get_run=slow, events=slow,
        snapshot=lambda *a, **kw: {"earliest_available_sequence":0,"latest_sequence":0})
    routes = {
        "history": ("/agents/{agent_id}/history", "/agents/alice/history", server.handle_v1_agent_history),
        "messages": ("/sessions/{session_id}/messages", "/sessions/session/messages", server.handle_v1_session_messages),
        "sessions": ("/sessions", "/sessions", server.handle_v1_sessions_list),
        "session": ("/sessions/{session_id}", "/sessions/session", server.handle_v1_session_get),
        "run": ("/sessions/{session_id}/runs/{run_id}", "/sessions/session/runs/run", server.handle_v1_session_run_get),
        "events": ("/sessions/{session_id}/events", "/sessions/session/events", server.handle_v1_session_events),
    }
    path, target, handler = routes[kind]
    app = web.Application()
    app.router.add_get(path, handler)
    async def independent(request):
        return web.json_response({"ready": True})
    app.router.add_get("/independent", independent)
    timer = threading.Timer(.8, release.set)
    timer.start()
    try:
        async with TestClient(TestServer(app)) as client:
            started = time.monotonic()
            task = asyncio.create_task(client.get(target))
            assert await asyncio.to_thread(entered.wait, 1.5)
            response = await client.get("/independent")
            duration = time.monotonic() - started
            assert response.status == 200
            release.set()
            assert (await task).status == 200
            assert duration < .5, "Session read blocked the shared HTTP event loop"
    finally:
        release.set()
        timer.cancel()
        timer.join()
