"""Regressions for PR 203's reviewed recovery continuation boundaries."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from adapters.her_v2 import HERv2Adapter
from adapters.her_v2_provider import HashiStageProvider
from adapters.openrouter_api import OpenRouterAdapter
from tests.test_her_v2_adapter import _agent_config, _global_config
from tests.test_her_v2_control_plane import _run_claim
from tools.registry import ToolRegistry


def _stream_event(delta, finish=None):
    event = {"id": "targeted", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return "data: " + json.dumps(event) + "\n\n"


@pytest.mark.asyncio
async def test_new_user_turn_reconsiders_pending_effect_before_executing(tmp_path, monkeypatch):
    """A new ordinary instruction reaches the model before an old unfinished write."""
    captured = []
    global_config = _global_config(tmp_path)
    file_a, file_b = tmp_path / "A.txt", tmp_path / "B.txt"

    def answer(request):
        captured.append(json.loads(request.content)["messages"])
        if len(captured) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "inspect-a", "type": "function",
                "function": {"name": "file_read", "arguments": json.dumps({"path": str(file_a)})}}]}
            body = _stream_event(delta) + _stream_event({}, "tool_calls")
        else:
            body = _stream_event({"content": "A inspected; B was not modified."}) + _stream_event({}, "stop")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body + "data: [DONE]\n\n")

    class Manager:
        agent_mode = "fixed"
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            cfg = SimpleNamespace(name="agent", engine=engine, workspace_dir=tmp_path / "workspace",
                                  model=target_model, system_md=None, extra={})
            backend = OpenRouterAdapter(cfg, global_config, api_key="isolated-test-key")
            backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
            return backend

    class PaoRuns:
        def get_run(self, run_id, *, owner_id=None):
            return {"run_id": run_id, "request_id": run_id.removeprefix("run-"),
                    "agent_id": "agent", "attempt": 1, "fencing_token": 1,
                    "request_digest": "sha256:input", "state": "running", "parent_run_id": None}

    manager = Manager()
    runtime = SimpleNamespace(backend_manager=manager, _request_meta_by_id={},
                              current_request_meta={}, session_store=PaoRuns())
    config = _agent_config(tmp_path)
    registry = ToolRegistry(allowed_tools=["file_write", "file_read"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={})
    registry.permission_durable_wait = True
    counts = []
    execute = registry.execute

    async def counted(name, arguments, tool_call_id=""):
        counts.append(tool_call_id)
        return await execute(name, arguments, tool_call_id=tool_call_id)

    monkeypatch.setattr(registry, "execute", counted)
    config._hashi_runtime = runtime
    config._her_v2_stage_provider = HashiStageProvider(backend_manager=manager, tool_registry=registry)
    adapter = HERv2Adapter(config, global_config)
    assert await adapter.initialize()
    coordinator = adapter._session_coordinator
    payload = {"transport_snapshot": {"version": 1, "sections": []}}

    def prepare(request_id, text):
        meta = {"request_id": request_id, "owner_id": "owner", "hashi_run_id": "run-" + request_id,
                "hashi_fencing_token": 1, "hashi_session_id": "isolated-session",
                "hashi_message_id": "message-" + request_id, "context_generation": 1,
                "session_workspace": str(tmp_path / "session")}
        runtime._request_meta_by_id[request_id] = meta
        runtime.current_request_meta = meta
        config._her_v2_stage_provider = HashiStageProvider(backend_manager=manager, tool_registry=registry)
        return adapter.prepare_fixed_turn_input(prompt_payload=payload, user_message=text,
                                                request_id=request_id, request_meta=meta)[0]

    first = coordinator.accept(prepare("first", "Write files A and B."))
    args_a = {"path": str(file_a), "content": "A is complete"}
    args_b = {"path": str(file_b), "content": "B should not be written"}
    receipt_a = await registry.execute("file_write", args_a, tool_call_id="write-a")
    assert not receipt_a.is_error and file_a.read_text(encoding="utf-8") == "A is complete"
    calls = [{"id": call_id, "type": "function", "function": {
        "name": "file_write", "arguments": json.dumps(arguments)}}
        for call_id, arguments in (("write-a", args_a), ("write-b", args_b))]
    coordinator.record_provider_context_checkpoint(first, {
        "provider": "openrouter-api", "model": "configured/premium", "stage": "direct",
        "boundary": "pending_tool_batch",
        "messages": [
            {"role": "system", "content": "original system"},
            {"role": "user", "content": "Write files A and B."},
            {"role": "assistant", "content": "", "tool_calls": calls},
            {"role": "tool", "tool_call_id": "write-a", "content": receipt_a.output},
        ],
        "producer_run_claim": _run_claim("run-first", "first"),
    })
    assert coordinator.store.reconcile_interrupted(process_is_alive=lambda _pid: False) == 1
    response = await adapter.generate_response(prepare("second", "Do not modify B. Inspect A instead."), "second")
    assert response.is_success, response.error
    assert counts == ["write-a", "inspect-a"], counts
    assert not file_b.exists()
    outgoing = json.dumps(captured[0], ensure_ascii=False)
    assert "Do not modify B. Inspect A instead." in outgoing
    assert any(row.get('role') == 'tool' and row.get('tool_call_id') == 'write-a'
               and row.get('content') == receipt_a.output for row in captured[0])
    assert "write-b" in outgoing
    assert any(row.get('tool_call_id') == 'inspect-a' and 'A is complete' in row.get('content', '')
               for messages in captured for row in messages)
    await adapter.shutdown()
