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


@pytest.mark.asyncio
async def test_real_openrouter_to_hashi_switch_recovers_completed_file_effect(tmp_path, monkeypatch):
    """Both concrete adapters execute across a genuine persisted interrupted Turn."""
    import asyncio
    from dataclasses import replace
    from tests.test_hashi_api import _adapter as hashi_adapter

    wire, effects = [], []
    target = tmp_path / "completed.txt"
    global_config = _global_config(tmp_path)

    def respond(request):
        payload = json.loads(request.content)
        wire.append(payload)
        if len(wire) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "write-original", "type": "function",
                     "function": {"name": "file_write", "arguments": json.dumps({
                         "path": str(target), "content": "Written exactly once."})}}]}
            body = _stream_event(delta) + _stream_event({}, "tool_calls")
        else:
            body = _stream_event({"content": "Continued safely."}) + _stream_event({}, "stop")
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=body + "data: [DONE]\n\n")

    class Manager:
        agent_mode = "fixed"
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            if engine == "hashi-api":
                backend = hashi_adapter(tmp_path, model=target_model)
            else:
                cfg = SimpleNamespace(name="agent", engine=engine, workspace_dir=tmp_path,
                                      model=target_model, system_md=None, extra={})
                backend = OpenRouterAdapter(cfg, global_config, api_key="fixture")
            backend.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
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
    registry = ToolRegistry(allowed_tools=["file_write"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={})
    execute = registry.execute

    async def counted(name, arguments, tool_call_id=""):
        effects.append(tool_call_id)
        return await execute(name, arguments, tool_call_id=tool_call_id)

    monkeypatch.setattr(registry, "execute", counted)
    config._hashi_runtime = runtime
    config._her_v2_stage_provider = HashiStageProvider(backend_manager=manager, tool_registry=registry)
    adapter = HERv2Adapter(config, global_config)
    assert await adapter.initialize()
    coordinator = adapter._session_coordinator
    persist = coordinator.record_provider_context_checkpoint

    def crash_once(accepted, checkpoint):
        result = persist(accepted, checkpoint)
        if checkpoint["boundary"] == "completed_tool_batch":
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(coordinator, "record_provider_context_checkpoint", crash_once)
    monkeypatch.setattr(adapter, "_close_interrupted_fixed_turn", lambda *args, **kwargs: None)

    def prepare(request_id, text):
        meta = {"request_id": request_id, "owner_id": "owner", "hashi_run_id": "run-" + request_id,
                "hashi_fencing_token": 1, "hashi_session_id": "conversation",
                "hashi_message_id": "message-" + request_id, "context_generation": 1,
                "session_workspace": str(tmp_path)}
        runtime._request_meta_by_id[request_id] = meta
        runtime.current_request_meta = meta
        config._her_v2_stage_provider = HashiStageProvider(backend_manager=manager, tool_registry=registry)
        return adapter.prepare_fixed_turn_input(
            prompt_payload={"transport_snapshot": {"version": 1, "sections": []}},
            user_message=text, request_id=request_id, request_meta=meta)[0]

    with pytest.raises(asyncio.CancelledError):
        await adapter.generate_response(prepare("source", "Write the report."), "source")
    assert target.read_text(encoding="utf-8") == "Written exactly once."
    assert effects == ["write-original"]
    assert coordinator.store.reconcile_interrupted(process_is_alive=lambda _pid: False) == 1
    monkeypatch.setattr(coordinator, "record_provider_context_checkpoint", persist)
    current = adapter._v2_config
    profiles = dict(current.profiles)
    profiles["main"] = replace(profiles["main"], engine="hashi-api", model="gpt-5.6-luna")
    adapter._v2_config = replace(current, profiles=profiles, routing_revision=current.routing_revision + 1)
    result = await adapter.generate_response(prepare("successor", "Continue the original task."), "successor")
    assert result.is_success, result.error
    assert effects == ["write-original"]
    assert target.read_text(encoding="utf-8") == "Written exactly once."
    outgoing = json.dumps(wire[-1]["messages"], ensure_ascii=False)
    assert "Continue the original task." in outgoing
    assert "write-original" in outgoing
    assert "completed.txt" in outgoing
    await adapter.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["deepseek", "ollama", "xai"])
async def test_real_adapter_forwards_structured_successor_with_completed_result(tmp_path, provider):
    """Concrete subclasses carry the successor content through their actual transport."""
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from orchestrator.her_v2.config import ProviderProfile
    from orchestrator.multimodal_contract import canonical_request_content
    from tests.test_herv3_pr203_provider_wire import _context, _direct_request

    if provider == "deepseek":
        from tests.test_deepseek_api import _adapter
        backend = _adapter(tmp_path)
        engine = "deepseek-api"
    elif provider == "ollama":
        from tests.test_ollama_context_pilot import _adapter
        backend = _adapter(tmp_path, enabled=True, capacity=49152)
        engine = "ollama-api"
    else:
        from adapters.xai_api import XaiApiAdapter
        cfg = SimpleNamespace(name="agent", engine="xai-api", model="unit-chat",
                              workspace_dir=tmp_path, system_md=None, extra={})
        backend = XaiApiAdapter(cfg, SimpleNamespace(), api_key="test-key")
        backend._resolve_bearer = AsyncMock()
        engine = "xai-api"

    captured = []

    def answer(request):
        if request.method == "GET":
            return httpx.Response(200, json={"version": "0.35.0"})
        payload = json.loads(request.content)
        captured.append((request.url.path, payload))
        if request.url.path.endswith("/api/chat"):
            output_tokens = 1 if payload.get("options", {}).get("num_predict") == 1 else 2
            return httpx.Response(200, json={"message": {"content": "Done."},
                "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": output_tokens})
        body = _stream_event({"content": "Done."}) + _stream_event({}, "stop")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body + "data: [DONE]\n\n")

    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))

    class Manager:
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            return backend

    content = canonical_request_content([{"type": "text", "item_index": 1, "text": "Continue with the report."}])
    request = replace(_direct_request(), goal="Continue with the report.", request_content=content)
    request = replace(request, context={**request.context, "pcm_input": {
        "current_request": "Continue with the report.", "runtime_context": {}, "sections": [], "history": [],
    }})
    history = [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "Original task"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "old-read", "type": "function",
            "function": {"name": "file_read", "arguments": '{"path":"old.txt"}'},
        }]},
        {"role": "tool", "tool_call_id": "old-read", "content": "Useful completed result"},
    ]
    checkpoint = {**_context("completed_tool_batch", history),
                  "provider": engine, "model": backend.config.model}
    stage = HashiStageProvider(backend_manager=Manager())
    stage.bind_provider_context(observer=lambda _: None, recovery=checkpoint, successor_turn=True)
    response = await stage.invoke(ProviderProfile("main", engine, backend.config.model), request)
    assert response.text == "Done."
    assert captured
    sent = json.dumps(captured[0][1], ensure_ascii=False)
    assert "Useful completed result" in sent and "Continue with the report." in sent
    if provider == "ollama":
        assert captured[0][0].endswith("/api/chat")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["deepseek", "ollama"])
async def test_real_adapter_successor_media_and_completed_result(tmp_path, provider):
    from dataclasses import replace
    from orchestrator.her_v2.config import ProviderProfile
    from orchestrator.multimodal_contract import attachment_manifest
    from tests.test_herv3_pr203_provider_wire import _context, _direct_request
    from tests.test_openrouter_multimodal import _two_images

    if provider == "deepseek":
        from tests.test_deepseek_api import _adapter
        backend = _adapter(tmp_path, model="deepseek-flash")
        engine = "deepseek-api"
    else:
        from tests.test_ollama_context_pilot import _adapter
        # The existing native Ollama capacity pilot is text-only. Its
        # OpenAI-compatible vision route supports structured image content.
        backend = _adapter(tmp_path, enabled=False, capacity=None)
        backend.config.extra.update({
            "input_modalities": ["text", "image"],
            "input_transports": {"image": ["data_url"]},
            "input_formats": {"image": ["png"]},
        })
        engine = "ollama-api"
    backend.global_config.base_media_dir = tmp_path
    wire = []

    def answer(request):
        if request.method == "GET":
            return httpx.Response(200, json={"version": "0.35.0"})
        payload = json.loads(request.content)
        wire.append(payload)
        if request.url.path.endswith("/api/chat"):
            output_tokens = 1 if payload.get("options", {}).get("num_predict") == 1 else 2
            return httpx.Response(200, json={"message": {"content": "Seen."}, "done": True,
                "done_reason": "stop", "prompt_eval_count": 10, "eval_count": output_tokens})
        body = _stream_event({"content": "Seen."}) + _stream_event({}, "stop") + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))

    class Manager:
        privacy_level = 1
        def create_ephemeral_backend(self, engine, target_model=None):
            return backend

    content = _two_images(tmp_path)
    request = replace(_direct_request(), goal="Compare both.", request_content=content,
                      attachment_manifest=attachment_manifest(content))
    request = replace(request, context={**request.context, "pcm_input": {
        "current_request": "Compare both.", "runtime_context": {}, "sections": [], "history": []}})
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Original task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "old-read", "type": "function",
            "function": {"name": "file_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "old-read", "content": "Completed historical result."},
    ]
    checkpoint = {**_context("completed_tool_batch", history), "provider": engine,
                  "model": backend.config.model}
    stage = HashiStageProvider(backend_manager=Manager())
    stage.bind_provider_context(observer=lambda _: None, recovery=checkpoint, successor_turn=True)
    result = await stage.invoke(ProviderProfile("main", engine, backend.config.model), request)
    assert result.text == "Seen."
    sent = json.dumps(wire[-1], ensure_ascii=False)
    assert "Completed historical result." in sent and "Compare both." in sent
    assert sent.count("data:image/png;base64,") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["deepseek", "ollama-native"])
@pytest.mark.parametrize("position", ["message", "deferred"])
async def test_cross_adapter_does_not_send_unsupported_prior_media(tmp_path, target, position):
    from dataclasses import replace
    from orchestrator.her_v2.config import ProviderProfile
    from tests.test_herv3_pr203_provider_wire import _context, _direct_request

    if target == "deepseek":
        from tests.test_deepseek_api import _adapter
        backend = _adapter(tmp_path, model="deepseek-v4-pro")
        engine = "deepseek-api"
    else:
        from tests.test_ollama_context_pilot import _adapter
        backend = _adapter(tmp_path, enabled=True, capacity=49152)
        # The model may be configured for images even though the native
        # context pilot's actual transport accepts only text.
        backend.config.extra.update({"input_modalities": ["text", "image"],
                                     "input_transports": {"image": ["data_url"]}})
        engine = "ollama-api"
    wire = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"version": "0.35.0"})
        payload = json.loads(request.content)
        wire.append(payload)
        if request.url.path.endswith("/api/chat"):
            count = 1 if payload.get("options", {}).get("num_predict") == 1 else 2
            return httpx.Response(200, json={"message": {"content": "Done."}, "done": True,
                "done_reason": "stop", "prompt_eval_count": 10, "eval_count": count})
        body = _stream_event({"content": "Done."}) + _stream_event({}, "stop") + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    class Manager:
        privacy_level = 1
        def create_ephemeral_backend(self, engine, target_model=None):
            return backend

    image = {"role": "user", "content": [{"type": "image_url",
        "image_url": {"url": "data:image/png;base64,U0VDUkVU"}}]}
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Original task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "old-read", "type": "function",
            "function": {"name": "file_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "old-read", "content": "Completed earlier result"},
    ]
    deferred = []
    (history if position == "message" else deferred).append(image)
    checkpoint = _context("completed_tool_batch", history, deferred_followups=deferred)
    request = replace(_direct_request(), context={"her_v3": True, "pcm_input": {
        "current_request": "Continue with the report.", "sections": [], "history": [],
        "runtime_context": {"active_turn_recovery": {"source_request_id": "old-request",
            "provider_context_checkpoint": {"items_evidence_ref": "her_provider_context_items:session:turn"}}},
    }})
    stage = HashiStageProvider(backend_manager=Manager())
    stage.bind_provider_context(observer=lambda _: None, recovery=checkpoint, successor_turn=True)
    result = await stage.invoke(ProviderProfile("main", engine, backend.config.model), request)
    assert result.text == "Done."
    sent = json.dumps(wire[-1], ensure_ascii=False)
    assert "U0VDUkVU" not in sent
    assert "Completed earlier result" in sent and "Continue with the report." in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target", [
    ("openrouter-api", "hashi-api"),
    ("hashi-api", "openrouter-api"),
    ("deepseek-api", "ollama-api"),
    ("ollama-api", "hli-api"),
    ("hli-api", "deepseek-api"),
])
async def test_actual_cross_adapter_successor_preserves_recovery_evidence(tmp_path, source, target):
    from dataclasses import replace
    from orchestrator.her_v2.config import ProviderProfile
    from tests.test_herv3_pr203_provider_wire import _context, _direct_request

    if target == "hashi-api":
        from tests.test_hashi_api import _adapter
        backend = _adapter(tmp_path)
    elif target == "openrouter-api":
        from tests.test_openrouter_multimodal import _adapter
        backend = _adapter(tmp_path)
    elif target == "deepseek-api":
        from tests.test_deepseek_api import _adapter
        backend = _adapter(tmp_path)
    elif target == "ollama-api":
        from tests.test_ollama_context_pilot import _adapter
        backend = _adapter(tmp_path, enabled=True, capacity=49152)
    else:
        from adapters.hli_api import HLIAdapter
        cfg = SimpleNamespace(name="agent", engine="hli-api", model="local-model",
                              workspace_dir=tmp_path, system_md=None, extra={})
        backend = HLIAdapter(cfg, SimpleNamespace())

    captured = []

    def answer(request):
        if request.method == "GET":
            return httpx.Response(200, json={"version": "0.35.0"})
        payload = json.loads(request.content)
        captured.append(payload)
        if request.url.path.endswith("/api/chat"):
            output_tokens = 1 if payload.get("options", {}).get("num_predict") == 1 else 2
            return httpx.Response(200, json={"message": {"content": "Done."},
                "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": output_tokens})
        body = _stream_event({"content": "Done."}) + _stream_event({}, "stop")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body + "data: [DONE]\n\n")

    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))

    class Manager:
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            return backend

    history = [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "Original task"},
        {"role": "assistant", "content": "", "reasoning_content": "private vendor reasoning",
         "tool_calls": [{"id": "old-read", "type": "function",
                         "function": {"name": "file_read", "arguments": '{"path":"old.txt"}'}}]},
        {"role": "tool", "tool_call_id": "old-read", "content": "CHECKPOINT_ONLY_RESULT"},
    ]
    checkpoint = {**_context("completed_tool_batch", history),
                  "provider": source, "model": "previous-model"}
    request = replace(_direct_request(), context={"her_v3": True, "pcm_input": {
        "current_request": "Continue the original task.", "sections": [], "history": [],
        "runtime_context": {"active_turn_recovery": {
            "source_request_id": "old-request", "remaining_work": {"goal": "Original task"},
            "side_effects": [{"operation_id": "UNKNOWN_EFFECT", "state": "unknown"}],
            "provider_context_checkpoint": {"items_evidence_ref": "her_provider_context_items:session:turn"},
        }},
    }})
    stage = HashiStageProvider(backend_manager=Manager())
    stage.bind_provider_context(observer=lambda _: None, recovery=checkpoint, successor_turn=True)
    result = await stage.invoke(ProviderProfile("main", target, backend.config.model), request)
    assert result.text == "Done."
    sent = json.dumps(captured[-1], ensure_ascii=False)
    assert "Continue the original task." in sent
    assert "CHECKPOINT_ONLY_RESULT" in sent
    assert "UNKNOWN_EFFECT" in sent
    assert "private vendor reasoning" not in sent
