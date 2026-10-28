from __future__ import annotations

import asyncio
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator import runtime_lifecycle
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.request_activity import RequestActivityStore
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_runtime_pipeline import _item, _runtime


@pytest.mark.asyncio
async def test_question_stop_runs_real_stop_path_and_rejects_late_card(tmp_path, monkeypatch):
    cfg = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="HASHI1",
                          authorized_id=7, workbench_port=18800, api_gateway_port=18801,
                          deployment_profile="personal", ui_language="en")
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": [{"name": "zelda"}]}))
    server = WorkbenchApiServer(config_path, cfg, secrets={"workbench_admin_token": "test-secret"},
                                reconcile_session_runs=False)
    runtime = _runtime()
    runtime.global_config = cfg
    runtime.session_store = server.session_store
    runtime.cmd_stop = MethodType(FlexibleAgentRuntime.cmd_stop, runtime)
    runtime.request_activity = RequestActivityStore()
    runtime.queue = asyncio.Queue()
    runtime._request_listeners = {}
    runtime._pending_request_results = {}
    runtime._background_request_ids = set()
    runtime._request_meta_by_id = {}
    runtime._session_executions = {}
    runtime._session_execution_tasks = {}
    runtime._notify_request_listeners = MethodType(FlexibleAgentRuntime._notify_request_listeners, runtime)
    runtime._remote_backend_block_reason = lambda _source: None
    runtime._notify_right_brain_interrupted = lambda *args, **kwargs: None
    runtime._is_authorized_user = lambda user: user == 7
    runtime._reply_text = AsyncMock()
    runtime.error_logger.exception = lambda _message: None
    server._runtime_map = lambda: {"zelda": runtime}

    session = server.session_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    accepted = server.session_store.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="zelda",
        request_id="req-askme-stop", text="Run until stopped", source="workbench",
        idempotency_key="askme-stop",
    )
    item = _item(request_id=accepted.request_id, session_id=session["session_id"],
                 run_id=accepted.run_id, context_generation=1, owner_id="user:7",
                 source="session-api", session_surface="workbench", session_channel_key="default")
    runtime.request_activity.start(item.request_id)
    entered = asyncio.Event()

    async def hold_prompt(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime_lifecycle.runtime_workzone, "activate_backend_state", AsyncMock())
    monkeypatch.setattr(runtime_lifecycle.runtime_pipeline, "build_turn_prompt", hold_prompt)
    monkeypatch.setattr("orchestrator.runtime_media.finish_native_voice_transcript_path", AsyncMock())
    monkeypatch.setattr("orchestrator.frontend_whatsapp_mirror.deliver_whatsapp_mirror", AsyncMock())
    monkeypatch.setattr("orchestrator.runtime_debug_reporting.schedule_terminal_diagnostic", lambda *a, **k: None)
    monkeypatch.setattr("orchestrator.runtime_debug_reporting.schedule_failure_report", lambda *a, **k: None)
    await runtime.queue.put(item)
    execution_task = asyncio.create_task(runtime_lifecycle._process_single_item(runtime, await runtime.queue.get()))
    runtime._session_execution_tasks[item.request_id] = execution_task
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        question = server._questions().create(request_id=accepted.request_id, agent_id="zelda",
                                              payload={"question": "Proceed?", "idempotency_key": "q"})
        url = f"/api/v1/sessions/{session['session_id']}/questions/{question['question_id']}/answer"
        response = await client.post(url, headers={"X-Workbench-Token": "test-secret"},
                                     json={"idempotency_key": "stop", "option_id": "__hashi_stop__"})
        assert response.status == 200, await response.text()
        assert execution_task.done()
        assert server.session_store.get_run(accepted.run_id, owner_id="user:7")["state"] == "stopped"
        assert (await response.json())["receipt"] == "saved"
        later = server.session_store.accept_run(
            session_id=session["session_id"], owner_id="user:7", agent_id="zelda",
            request_id="req-later", text="Another task", source="workbench",
            idempotency_key="later",
        )
        server.session_store.mark_request_running(later.request_id, worker_id="later-worker")
        late = await client.post(url, headers={"X-Workbench-Token": "test-secret"},
                                 json={"idempotency_key": "late", "option_id": "__hashi_stop__"})
        assert late.status == 409
        assert server.session_store.get_run(later.run_id, owner_id="user:7")["state"] == "running"
    finally:
        if not execution_task.done():
            execution_task.cancel()
            await asyncio.gather(execution_task, return_exceptions=True)
        await client.close()
