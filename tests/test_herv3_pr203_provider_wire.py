"""Provider wire regression through real HER, StageProvider and SQLite."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from adapters.her_v2 import HERv2Adapter
from adapters.her_v2_provider import HashiStageProvider
from adapters.openrouter_api import OpenRouterAdapter
from tests.test_her_v2_adapter import _agent_config, _global_config


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_checkpoint", [False, True])
@pytest.mark.parametrize("managed_owner", [False, True])
async def test_repeated_interrupted_successors_do_not_repeat_assembled_context(tmp_path, monkeypatch, legacy_checkpoint, managed_owner):
    calls = []
    message_wire_bytes, request_wire_bytes = [], []

    def answer(request):
        payload = json.loads(request.content)
        calls.append(copy.deepcopy(payload))
        encoded_messages = json.dumps(
            payload["messages"], ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        # Measure the exact serialized messages on the HTTP request, not a
        # later default-JSON re-encoding or the intermediate prompt projection.
        assert encoded_messages in request.content
        message_wire_bytes.append(len(encoded_messages))
        request_wire_bytes.append(len(request.content))
        event = {"id": f"response-{len(calls)}", "choices": [
            {"index": 0, "delta": {"content": "Work remains in progress."}, "finish_reason": None}
        ]}
        stop = {"id": f"response-{len(calls)}", "choices": [
            {"index": 0, "delta": {}, "finish_reason": "stop"}
        ]}
        body = "data: " + json.dumps(event) + "\n\ndata: " + json.dumps(stop) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    global_config = _global_config(tmp_path)

    class Manager:
        agent_mode = "fixed"
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            cfg = SimpleNamespace(
                name="agent", engine=engine, workspace_dir=tmp_path / "workspace",
                model=target_model, system_md=None, extra={},
            )
            backend = OpenRouterAdapter(cfg, global_config, api_key="isolated-test-key")
            backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
            return backend

    from orchestrator.session_store import SessionStore

    pao_store = SessionStore(tmp_path / "pao" / "sessions.sqlite3", instance_id="HASHI-test")
    pao_session = pao_store.ensure_default_session(owner_id="owner", agent_id="agent")
    manager = Manager()
    runtime = SimpleNamespace(backend_manager=manager, _request_meta_by_id={},
                              current_request_meta={}, session_store=pao_store)
    managed_turns = []
    if managed_owner:
        from orchestrator.permissions.her_binding import ManagedHerTurn

        class PermissionOwner:
            async def open_turn(self, adapter, accepted, request_id):
                binding = SimpleNamespace(context=SimpleNamespace(turn_id=accepted.turn_id))
                executor = SimpleNamespace(bind_provider=lambda _provider: None)
                turn = ManagedHerTurn(self, binding, executor, adapter=adapter, accepted=accepted)
                managed_turns.append(turn)
                return turn

            def is_park(self, _exc):
                return False

        runtime.permission_runs = PermissionOwner()
    config = _agent_config(tmp_path)
    config._hashi_runtime = runtime
    config._her_v2_stage_provider = HashiStageProvider(backend_manager=manager)
    adapter = HERv2Adapter(config, global_config)
    assert await adapter.initialize()

    # Simulate loss of the Function process immediately after the real
    # provider checkpoint transaction commits, before final delivery.
    coordinator = adapter._session_coordinator
    persist = coordinator.record_provider_context_checkpoint
    interruptions = {"remaining": 5}

    def crash_after_commit(accepted, checkpoint):
        # The original PR's legacy checkpoint had no assembly ownership.
        if legacy_checkpoint and interruptions["remaining"] == 5:
            checkpoint = {key: value for key, value in checkpoint.items() if key != "context_ownership"}
        result = persist(accepted, checkpoint)
        if checkpoint["boundary"] == "completed_provider_response" and interruptions["remaining"]:
            interruptions["remaining"] -= 1
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(coordinator, "record_provider_context_checkpoint", crash_after_commit)
    monkeypatch.setattr(adapter, "_close_interrupted_fixed_turn", lambda *args, **kwargs: None)
    marker = "UNCHANGED_CONTEXT_MARKER"
    sections = [
        {"key": "permanent", "title": "PERMANENT", "text": "Keep the original task.",
         "authority": "permanent_system", "rank": 0, "protected": True, "metadata": {}, "order": 0},
        {"key": "reference", "title": "REFERENCE", "text": marker + (" reference" * 500),
         "authority": "runtime_context", "rank": 3, "protected": False, "metadata": {}, "order": 1},
        {"key": "current_user_request", "title": "REQUEST", "text": "placeholder",
         "authority": "current_user", "rank": 3, "protected": True, "metadata": {}, "order": 2},
    ]
    payload = {"transport_snapshot": {"version": 1, "sections": sections}}

    for generation in range(6):
        if generation == 2:
            adapter.effort = "high"
            sections.insert(-1, {"key": "new-reference", "title": "NEW REFERENCE",
                                 "text": "NEW_UNIQUE_HISTORY_FACT",
                                 "authority": "runtime_context", "rank": 3,
                                 "protected": False, "metadata": {}, "order": 1})
        if generation == 3:
            from dataclasses import replace
            current = adapter._v2_config
            profiles = dict(current.profiles)
            profiles["main"] = replace(profiles["main"], model="configured/switched")
            adapter._v2_config = replace(
                current, profiles=profiles, routing_revision=current.routing_revision + 1,
            )
        # Production creates a fresh StageProvider for each accepted Turn.
        config._her_v2_stage_provider = HashiStageProvider(backend_manager=manager)
        request_id = f"req-{generation}"
        user = "Original goal: finish the isolated report" if not generation else f"Continue safely generation {generation}"
        run = pao_store.accept_run(
            session_id=pao_session["session_id"], owner_id="owner", agent_id="agent",
            request_id=request_id, text=user, source="test", idempotency_key=request_id,
        )
        fence = pao_store.mark_request_running(request_id, worker_id="isolated-worker")
        assert fence is not None
        meta = {"request_id": request_id, "owner_id": "owner", "hashi_run_id": run.run_id,
                "hashi_fencing_token": fence, "hashi_session_id": run.session_id,
                "hashi_message_id": run.message_id, "context_generation": run.context_generation,
                "session_workspace": str(tmp_path / "session")}
        runtime._request_meta_by_id[request_id] = meta
        runtime.current_request_meta = meta
        transport, _ = adapter.prepare_fixed_turn_input(
            prompt_payload=payload, user_message=user, request_id=request_id, request_meta=meta)
        if generation < 5:
            with pytest.raises(asyncio.CancelledError):
                await adapter.generate_response(transport, request_id)
            assert coordinator.store.reconcile_interrupted(process_is_alive=lambda _pid: False) == 1
            pao_store.finish_request(request_id, success=False, error_text="simulated worker loss", fencing_token=fence)
        else:
            response = await adapter.generate_response(transport, request_id)
            assert response.is_success, response.error
            pao_store.finish_request(request_id, success=True, assistant_text=response.text, fencing_token=fence)

    assert len(calls) == 6
    assert calls[1]["reasoning"]["effort"] == "low"
    assert calls[2]["reasoning"]["effort"] == "high"
    assert calls[2]["model"] != calls[3]["model"]
    assert calls[3]["model"] == "configured/switched"
    measurements = message_wire_bytes
    actual = json.dumps(calls[-1]["messages"], ensure_ascii=False)
    print("provider_message_wire_bytes:", measurements)
    print("provider_full_request_wire_bytes:", request_wire_bytes)
    print("provider_models:", [call["model"] for call in calls])
    print("provider_reasoning:", [call.get("reasoning") for call in calls])
    assert actual.count(marker) == 1, measurements
    assert "Original goal: finish the isolated report" in actual
    assert "Continue safely generation 5" in actual
    assert actual.count("NEW_UNIQUE_HISTORY_FACT") == 1
    if managed_owner:
        assert len(managed_turns) == 6
        assert managed_turns[0].recovery is None
        assert all(turn.successor_continuation["successor_turn"] is True
                   and turn.recovery["format"] == "herv3-provider-context-v1"
                   for turn in managed_turns[1:])
    await adapter.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["ready_provider_request", "pending_tool_batch", "completed_tool_batch", "completed_provider_response"])
async def test_permission_resume_of_successor_keeps_exact_request(tmp_path, boundary):
    from orchestrator.permissions.her_binding import ManagedHerTurn
    from tools.registry import ToolRegistry
    from tests.test_her_v2_control_plane import _run_claim

    captured = []
    global_config = _global_config(tmp_path)

    def answer(request):
        captured.append(json.loads(request.content)["messages"])
        events = [
            {"id": "resume", "choices": [{"index": 0, "delta": {"content": "Done."},
                                             "finish_reason": None}]},
            {"id": "resume", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

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
    config._hashi_runtime = runtime
    registry = ToolRegistry(allowed_tools=["file_write"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={})
    registry.permission_durable_wait = True
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
        return adapter.prepare_fixed_turn_input(prompt_payload=payload, user_message=text,
                                                request_id=request_id, request_meta=meta)[0]

    first = coordinator.accept(prepare("source", "Original task"))
    assert coordinator.store.reconcile_interrupted(process_is_alive=lambda _pid: False) == 1
    resumed_input = prepare("resume", "Continue original task")
    accepted = coordinator.accept(resumed_input)
    assert coordinator.store.consume_recovery_context(
        session_id=accepted.session_id, current_turn_id=accepted.turn_id) is not None
    exact = [{"role": "system", "content": "original system"},
             {"role": "user", "content": "exact frozen ready request"}]
    if boundary == "pending_tool_batch":
        exact.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "pending-once", "type": "function",
            "function": {"name": "file_write", "arguments": json.dumps({
                "path": str(tmp_path / "resumed.txt"), "content": "written once",
            })},
        }]})
    elif boundary == "completed_provider_response":
        exact.append({"role": "assistant", "content": "The original report is complete."})
    elif boundary == "completed_tool_batch":
        # Execute a real isolated side effect once and preserve its actual receipt.
        # A later change makes replay visible even if it would write the same bytes.
        effect_path = tmp_path / "completed.txt"
        arguments = {"path": str(effect_path), "content": "written once"}
        result = await registry.execute("file_write", arguments, tool_call_id="completed-once")
        assert not result.is_error, result.output
        assert effect_path.read_text(encoding="utf-8") == "written once"
        effect_path.write_text("changed after completion", encoding="utf-8")
        exact.extend([
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "completed-once", "type": "function",
                "function": {"name": "file_write", "arguments": json.dumps(arguments)},
            }]},
            {"role": "tool", "tool_call_id": "completed-once", "content": result.output},
        ])
    coordinator.record_provider_context_checkpoint(accepted, {
        "provider": "openrouter-api", "model": "configured/premium", "stage": "direct",
        "boundary": boundary, "messages": exact,
        "producer_run_claim": _run_claim("run-resume", "resume"),
    })

    class PermissionOwner:
        async def open_turn(self, _adapter, _accepted, request_id):
            binding = SimpleNamespace(context=SimpleNamespace(turn_id=accepted.turn_id))
            executor = SimpleNamespace(bind_provider=lambda _provider: None)
            return ManagedHerTurn(self, binding, executor,
                recovery=coordinator.store.provider_context_for_turn(accepted.session_id, accepted.turn_id),
                adapter=adapter, accepted=accepted)

        def is_park(self, _exc):
            return False

    runtime.permission_runs = PermissionOwner()
    response = await adapter.generate_response(resumed_input, "resume")
    assert response.is_success, response.error
    # Tool activity can also trigger a separate commentary call; check the
    # main provider request by the exact frozen protocol it carries.
    main = [messages for messages in captured if any(
        row.get("content") == "exact frozen ready request" for row in messages
    )]
    assert len(main) == 1 and main[0][:len(exact)] == exact
    if boundary == "pending_tool_batch":
        assert main[0][-1]["tool_call_id"] == "pending-once"
        assert (tmp_path / "resumed.txt").read_text(encoding="utf-8") == "written once"
    elif boundary == "completed_provider_response":
        # Keep the completed response and the exact previous conversation.
        # A small resume cue is allowed, but not another assembled PCM/history.
        assert len(main[0]) == len(exact) + 1
        assert main[0][-1]["role"] == "user"
        assert len(main[0][-1]["content"]) < 512
        assert "Context and reference material" not in main[0][-1]["content"]
    else:
        assert main[0] == exact
        if boundary == "completed_tool_batch":
            assert (tmp_path / "completed.txt").read_text(encoding="utf-8") == "changed after completion"
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_successor_new_images_preserve_prior_provider_evidence(tmp_path):
    from dataclasses import replace
    from orchestrator.her_v2.config import ProviderProfile
    from orchestrator.multimodal_contract import attachment_manifest
    from tests.test_openrouter_multimodal import _adapter, _two_images

    captured = []
    def answer(request):
        captured.append(json.loads(request.content))
        events = [
            {"id": "image-response", "choices": [{"index": 0, "delta": {"content": "Compared."}, "finish_reason": None}]},
            {"id": "image-response", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    backend = _adapter(tmp_path)
    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    class Manager:
        privacy_level = 1
        def create_ephemeral_backend(self, engine, target_model=None):
            return backend

    content = _two_images(tmp_path)
    request = replace(_direct_request(), goal="Compare both.",
                      request_content=content, attachment_manifest=attachment_manifest(content))
    request = replace(request, context={**request.context, "pcm_input": {
        "current_request": "Compare both.", "runtime_context": {}, "sections": [], "history": [],
    }})
    history = [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "Original task"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "old-read", "type": "function",
             "function": {"name": "file_read", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "old-read", "content": "Important old file result"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,T0xE"}}]},
    ]
    stage = HashiStageProvider(backend_manager=Manager())
    stage.bind_provider_context(observer=lambda _: None, recovery={
        **_context("completed_tool_batch", history), "model": "google/gemini-2.5-pro",
    }, successor_turn=True)
    await stage.invoke(ProviderProfile("main", "openrouter-api", "google/gemini-2.5-pro"), request)
    assert len(captured) == 1
    outgoing = captured[0]["messages"]
    encoded = json.dumps(outgoing)
    assert "Important old file result" in encoded
    assert "data:image/png;base64,T0xE" in encoded
    new_images = [part for message in outgoing if isinstance(message.get("content"), list)
                  for part in message["content"] if part.get("type") == "image_url"
                  and part.get("image_url", {}).get("url") != "data:image/png;base64,T0xE"]
    assert len(new_images) == 2
# HASHI-Fingerprint: hfp_08e1baa68f7444b5ad4bd5a8c03afe76


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["completed_tool_batch", "completed_provider_response"])
async def test_exact_completed_resume_preserves_original_media_and_tool_result(tmp_path, boundary):
    from dataclasses import replace
    from orchestrator.her_v2.config import ProviderProfile
    from orchestrator.multimodal_contract import attachment_manifest
    from tests.test_openrouter_multimodal import _adapter, _two_images

    captured = []

    def answer(request):
        captured.append(json.loads(request.content)["messages"])
        events = [
            {"id": "media-resume", "choices": [{"index": 0, "delta": {"content": "Done."}, "finish_reason": None}]},
            {"id": "media-resume", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    backend = _adapter(tmp_path)
    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))

    class Manager:
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            return backend

    content = _two_images(tmp_path)
    request = replace(_direct_request(), goal="Compare both.",
                      request_content=content, attachment_manifest=attachment_manifest(content))
    request = replace(request, context={**request.context, "pcm_input": {
        "current_request": "Compare both.", "runtime_context": {}, "sections": [], "history": [],
    }})
    history, _ = backend._initial_messages("Compare both.", content)
    history.extend([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "completed-read", "type": "function",
             "function": {"name": "file_read", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "completed-read", "content": "Important completed result"},
    ])
    if boundary == "completed_provider_response":
        history.append({"role": "assistant", "content": "Comparison complete."})
    checkpoint = {**_context(boundary, history), "model": "google/gemini-2.5-pro"}
    stage = HashiStageProvider(backend_manager=Manager())
    stage.bind_provider_context(observer=lambda _: None, recovery=checkpoint, successor_turn=False)
    await stage.invoke(ProviderProfile("main", "openrouter-api", "google/gemini-2.5-pro"), request)
    assert len(captured) == 1 and captured[0][:len(history)] == history
    assert len(captured[0]) == len(history) + (boundary == "completed_provider_response")
    assert len([part for part in history[1]["content"] if part.get("type") == "image_url"]) == 2


@pytest.mark.asyncio
async def test_pending_successor_media_follows_all_tool_results(tmp_path):
    from dataclasses import replace
    from orchestrator.her_v2.config import ProviderProfile
    from orchestrator.multimodal_contract import attachment_manifest
    from tests.test_openrouter_multimodal import _adapter, _two_images
    from tools.registry import ToolRegistry

    captured = []
    def answer(request):
        captured.append(json.loads(request.content))
        events = [
            {"id": "pending-image", "choices": [{"index": 0, "delta": {"content": "Done."}, "finish_reason": None}]},
            {"id": "pending-image", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    backend = _adapter(tmp_path)
    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    class Manager:
        privacy_level = 1
        def create_ephemeral_backend(self, engine, target_model=None):
            return backend

    registry = ToolRegistry(allowed_tools=["file_write"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={})
    registry.permission_durable_wait = True
    content = _two_images(tmp_path)
    request = replace(_direct_request(), goal="Compare both.",
                      request_content=content, attachment_manifest=attachment_manifest(content))
    request = replace(request, context={**request.context, "pcm_input": {
        "current_request": "Compare both.", "runtime_context": {
            "active_turn_recovery": {"safe_to_resume": False, "side_effects": {"unresolved_total": 1}}
        }, "sections": [], "history": [],
    }})
    call = {"id": "pending-write", "type": "function", "function": {
        "name": "file_write", "arguments": json.dumps({
            "path": str(tmp_path / "pending.txt"), "content": "written once"
        })}}
    checkpoint = _context("pending_tool_batch", [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "Original task"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
    ], deferred_followups=[
        {"role": "user", "content": [{"type": "image_url",
                                       "image_url": {"url": "data:image/png;base64,T0xE"}}]},
        {"role": "system", "content": "Older tool advisory"},
    ])
    checkpoint["model"] = "google/gemini-2.5-pro"
    stage = HashiStageProvider(backend_manager=Manager(), tool_registry=registry)
    stage.bind_provider_context(observer=lambda _: None, recovery=checkpoint,
                                successor_turn=True, resume_pending_tools=True)
    await stage.invoke(ProviderProfile("main", "openrouter-api", "google/gemini-2.5-pro"), request)
    outgoing = captured[0]["messages"]
    assert not (tmp_path / "pending.txt").exists()
    tool_index = next(i for i, row in enumerate(outgoing) if row.get("tool_call_id") == "pending-write")
    assert "verify" in outgoing[tool_index]["content"].lower()
    old_index = next(i for i, row in enumerate(outgoing) if "T0xE" in json.dumps(row))
    advisory_index = next(i for i, row in enumerate(outgoing) if row.get("content") == "Older tool advisory")
    assert tool_index < old_index < advisory_index < len(outgoing) - 1
    assert outgoing[-1]["role"] == "user"
    encoded = json.dumps(outgoing, ensure_ascii=False)
    assert encoded.count("data:image/png;base64,") == 3
    assert "Compare both." in encoded and "unresolved_total" in encoded
    assert "_hashi_successor_" not in encoded


@pytest.mark.asyncio
async def test_openrouter_successor_audio_preserves_old_and_new_parts(tmp_path):
    from tests.test_native_audio_chat import _voice_content, _wav_bytes

    wav = tmp_path / "new.wav"
    _wav_bytes(wav, sample=7)
    content = _voice_content(wav)
    config = SimpleNamespace(
        name="audio-test", engine="openrouter-api", model="configured-audio-model",
        workspace_dir=tmp_path, system_md=None,
        extra={"input_modalities": ["text", "audio"], "input_transports": {"audio": ["inline"]},
               "input_formats": {"audio": ["wav"]}, "output_modalities": ["text"],
               "api_surface": "chat_completions", "output_streaming": "sse"},
    )
    backend = OpenRouterAdapter(config, SimpleNamespace(
        base_media_dir=tmp_path, openrouter_url="https://openrouter.invalid/v1/chat/completions"
    ), api_key="test-key")
    captured = []
    def answer(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "audio-response", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "Heard."}, "finish_reason": "stop"}
        ]})

    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": [{"type": "input_audio",
                                       "input_audio": {"data": "T0xE", "format": "wav"}}]},
        {"role": "assistant", "content": "Old audio was heard."},
        {"role": "user", "content": "Listen to the new audio"},
    ]
    response = await backend.generate_response(
        "Listen to the new audio", "audio-successor", request_content=content,
        continuation_messages=history, continuation_request_content=True,
    )
    assert response.is_success, response.error
    outgoing = captured[0]["messages"]
    audio = [part["input_audio"] for row in outgoing if isinstance(row.get("content"), list)
             for part in row["content"] if part.get("type") == "input_audio"]
    assert len(audio) == 2
    assert audio[0]["data"] == "T0xE"
    assert audio[1]["format"] == "wav" and audio[1]["data"] != "T0xE"


@pytest.mark.asyncio
async def test_ready_provider_request_recovers_from_real_sqlite_without_replaying_tools(tmp_path):
    from orchestrator.her_v2.config import ProviderProfile
    from tests.test_herv3_deferred_recovery import _accept, _claim, _new_store
    from tests.test_openrouter_multimodal import _adapter

    coordinator, source = _new_store(tmp_path)
    history = [
        {"role": "system", "content": "Old system"},
        {"role": "user", "content": "Original user request"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "read-1", "type": "function",
             "function": {"name": "file_read", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "read-1", "content": "Durable read result"},
        {"role": "user", "content": "Previously supplied resolution"},
    ]
    coordinator.record_provider_context_checkpoint(source, {
        "provider": "openrouter-api", "model": "google/gemini-2.5-pro", "stage": "direct",
        "boundary": "ready_provider_request", "messages": history,
        "producer_run_claim": _claim(),
    })
    assert coordinator.store.reconcile_interrupted() == 1
    successor = _accept(coordinator, "test-successor", "New user instruction")
    recovery = coordinator.store.consume_recovery_context(
        session_id=successor.session_id, current_turn_id=successor.turn_id)
    assert recovery["provider_context_checkpoint"]["available"] is True
    checkpoint = coordinator.store.provider_context_for_turn(successor.session_id, successor.turn_id)
    captured = []
    def answer(request):
        captured.append(json.loads(request.content))
        events = [
            {"id": "ready-response", "choices": [{"index": 0, "delta": {"content": "Done."}, "finish_reason": None}]},
            {"id": "ready-response", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    backend = _adapter(tmp_path)
    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    class Manager:
        privacy_level = 1
        def create_ephemeral_backend(self, engine, target_model=None):
            return backend
    stage = HashiStageProvider(backend_manager=Manager())
    stage.bind_provider_context(observer=lambda _: None, recovery=checkpoint,
                                successor_turn=True, resume_pending_tools=True)
    await stage.invoke(ProviderProfile("main", "openrouter-api", "google/gemini-2.5-pro"), _direct_request())
    assert len(captured) == 1
    outgoing = captured[0]["messages"]
    assert sum(row.get("tool_call_id") == "read-1" for row in outgoing) == 1
    assert any(row.get("content") == "Durable read result" for row in outgoing)
    assert outgoing[-1]["role"] == "user"
    assert "New user instruction" in outgoing[-1]["content"]
    assert '"safe_to_resume": false' in outgoing[-1]["content"]


@pytest.mark.asyncio
async def test_real_her_agent_can_retrieve_omitted_receipt_with_its_tool(tmp_path):
    from tests.test_herv3_p0_checkpoint_recovery_bounds import _runtime_event
    from tools.registry import ToolRegistry

    config = _agent_config(tmp_path)
    global_config = _global_config(tmp_path)
    global_config.instance_id = "HASHI-test"
    workspace = config.workspace_dir
    session_workspace = tmp_path / "session"
    captured, fragments = [], []
    source = {}

    def sse(delta, finish, serial):
        events = [
            {"id": f"receipt-{serial}", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": f"receipt-{serial}", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        ]
        return "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"

    def answer(request):
        payload = json.loads(request.content)
        captured.append(payload)
        offset, digest = 0, ""
        tools = [row for row in payload["messages"] if row.get("role") == "tool"]
        if tools:
            page = json.loads(tools[-1]["content"])["recovery_evidence"]
            assert page["status"] == "available"
            fragments.append(page["content"])
            if page["next_offset"] is None:
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=sse({"content": "Receipt retrieved."}, "stop", len(captured)))
            offset, digest = page["next_offset"], page["digest"]
        arguments = {
            "request_id": "source", "recovery_evidence_ref": source["ref"],
            "recovery_item_id": "read-old", "offset": offset, "expected_digest": digest,
        }
        delta = {"tool_calls": [{
            "index": 0, "id": f"fetch-{offset}", "type": "function",
            "function": {"name": "request_diagnostics", "arguments": json.dumps(arguments)},
        }]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(delta, "tool_calls", len(captured)))

    class Manager:
        agent_mode = "fixed"
        privacy_level = 1
        def create_ephemeral_backend(self, engine, target_model=None):
            cfg = SimpleNamespace(name="agent", engine=engine, workspace_dir=workspace,
                                  model=target_model, system_md=None, extra={})
            backend = OpenRouterAdapter(cfg, global_config, api_key="isolated-test-key")
            backend.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
            return backend

    class PaoRuns:
        def get_run(self, run_id, *, owner_id=None):
            return {"run_id": run_id, "request_id": run_id.removeprefix("run-"),
                    "agent_id": "agent", "attempt": 1, "fencing_token": 1,
                    "request_digest": "sha256:" + "a" * 64, "state": "running", "parent_run_id": None}

    manager = Manager()
    runtime = SimpleNamespace(backend_manager=manager, _request_meta_by_id={},
                              current_request_meta={}, session_store=PaoRuns())
    # Exercise the actual request-context refresh, including removal of stale
    # Session identity, rather than manufacturing the final tool authority.
    registry = ToolRegistry(
        allowed_tools=["request_diagnostics"], access_root=workspace, workspace_dir=workspace, secrets={},
        audit_context={"agent_name": "agent", "context_generation": 99,
                       "session_workspace": str(tmp_path / "stale-session")},
    )
    config._hashi_runtime = runtime
    config._her_v2_stage_provider = HashiStageProvider(backend_manager=manager, tool_registry=registry)
    adapter = HERv2Adapter(config, global_config)
    assert await adapter.initialize()
    payload = {"transport_snapshot": {"version": 1, "sections": []}}

    def prepare(request_id, message):
        meta = {"request_id": request_id, "owner_id": "owner", "hashi_run_id": "run-" + request_id,
                "hashi_fencing_token": 1, "hashi_session_id": "isolated-session",
                "hashi_message_id": "message-" + request_id, "context_generation": 1,
                "session_workspace": str(session_workspace)}
        runtime._request_meta_by_id[request_id] = meta
        runtime.current_request_meta = meta
        return adapter.prepare_fixed_turn_input(prompt_payload=payload, user_message=message,
                                                request_id=request_id, request_meta=meta)[0]

    first = prepare("source", "Read the important report")
    accepted = adapter._session_coordinator.accept(first)
    full = "Original important fact " + ("details " * 900) + " FINAL-PAGE-FACT"
    adapter._session_coordinator.record_runtime_event(accepted, _runtime_event(
        accepted.turn_id, "source:receipt", "tool_receipt",
        receipt={"operation_id": "read-old", "tool_call_id": "read-old",
                 "status": "completed", "completed": True, "output": full}))
    source["ref"] = f"her_active_turn_recovery:tool_receipts:{accepted.session_id}:{accepted.turn_id}"
    assert adapter._session_coordinator.store.reconcile_interrupted(process_is_alive=lambda _pid: False) == 1
    second = prepare("successor", "Retrieve the rest of the report evidence")
    from orchestrator.flexible_backend_manager import FlexibleBackendManager
    runtime.name = "agent"
    runtime.global_config = global_config
    context_manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    context_manager.config = SimpleNamespace(active_backend="openrouter-api")
    context_manager.current_backend = SimpleNamespace(tool_registry=registry)
    context_manager.runtime = runtime
    context_manager._refresh_tool_runtime_context("successor")
    assert registry.audit_context["context_generation"] == 1
    assert registry.audit_context["session_workspace"] == str(session_workspace)
    response = await adapter.generate_response(second, "successor")
    assert response.is_success, response.error
    assert len(captured) >= 2
    assert json.loads("".join(fragments))["output"] == full
    runtime._request_meta_by_id["unbound"] = {"request_id": "unbound"}
    context_manager._refresh_tool_runtime_context("unbound")
    assert "context_generation" not in registry.audit_context
    assert "session_workspace" not in registry.audit_context
    await adapter.shutdown()


def _direct_request():
    from orchestrator.her_v2.models import Effort, Stage, StageRequest, TriageClassification

    return StageRequest(
        turn_id="new-turn", request_ref="new-request", stage=Stage.DIRECT, role="direct",
        attempt=1, goal="New user instruction",
        classification=TriageClassification.COMPLEX_TASK, effort=Effort.HIGH,
        context={"her_v3": True, "pcm_input": {
            "current_request": "New user instruction",
            "runtime_context": {"active_turn_recovery": {"safe_to_resume": False}},
        }}, allow_tools=True, allow_side_effects=True,
    )


def _context(boundary, messages, **extra):
    return {"format": "herv3-provider-context-v1", "provider": "openrouter-api",
            "model": "configured/old", "stage": "direct", "boundary": boundary,
            "messages": messages, "deferred_followups": [], **extra}


@pytest.mark.parametrize("change", ["model", "stage", "missing_messages"])
@pytest.mark.parametrize("boundary", ["completed_tool_batch", "completed_provider_response"])
def test_exact_completed_boundary_fails_closed_if_checkpoint_cannot_be_resumed(change, boundary):
    from adapters.her_v2_provider import ProviderContextObserverError
    from orchestrator.her_v2.config import ProviderProfile

    checkpoint = _context(boundary, [
        {"role": "system", "content": "original system"},
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "completed", "type": "function",
             "function": {"name": "file_read", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "completed", "content": "needed result"},
    ])
    if boundary == "completed_provider_response":
        checkpoint["messages"].append({"role": "assistant", "content": "Finished."})
    if change == "stage":
        checkpoint["stage"] = "plan"
    elif change == "missing_messages":
        checkpoint["messages"] = []
    model = "configured/new" if change == "model" else "configured/old"
    stage = HashiStageProvider(backend_manager=SimpleNamespace())
    stage.bind_provider_context(observer=lambda _: None, recovery=checkpoint,
                                successor_turn=False, resume_pending_tools=True)
    with pytest.raises(ProviderContextObserverError):
        stage._provider_recovery_messages(
            profile=ProviderProfile("main", "openrouter-api", model),
            request=_direct_request(), stage_prompt="newly compiled context",
        )


def test_changed_owned_message_is_not_deleted_or_misclassified():
    from orchestrator.her_v2.config import ProviderProfile

    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": "Do not discard this original evidence"}]
    checkpoint = _context("completed_provider_response", messages, context_ownership={
        "format": "herv3-assembled-context-owner-v1", "message_index": 1, "part_index": None,
        "original_request": "Original task", "content_sha256": "sha256:" + "0" * 64,
    })
    stage = HashiStageProvider(backend_manager=SimpleNamespace())
    stage.bind_provider_context(observer=lambda _: None, recovery=checkpoint, successor_turn=True)
    result = stage._provider_recovery_messages(
        profile=ProviderProfile("main", "openrouter-api", "configured/old"),
        request=_direct_request(), stage_prompt="Full repeated context",
        original_request="New user instruction", system_prompt="updated system")
    assert result[1]["content"] == "Do not discard this original evidence"
    assert "Full repeated context" not in json.dumps(result)
    assert "New user instruction" in result[-1]["content"]


def test_pending_successor_closes_old_call_before_latest_instruction():
    from orchestrator.her_v2.config import ProviderProfile
    from orchestrator.her_v2.provider_continuation import pending_tool_calls

    pending = [{"role": "system", "content": "system"},
               {"role": "user", "content": "Original task"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "c1", "type": "function",
                    "function": {"name": "file_write", "arguments": "{}"}}
               ]}]
    stage = HashiStageProvider(backend_manager=SimpleNamespace())
    stage.bind_provider_context(observer=lambda _: None,
        recovery=_context("pending_tool_batch", pending),
        resume_pending_tools=True, successor_turn=True)
    messages = stage._provider_recovery_messages(
        profile=ProviderProfile("main", "openrouter-api", "configured/old"),
        request=_direct_request(), stage_prompt="Assembled context",
        original_request="New user instruction", system_prompt="new system")
    assert messages[1:3] == pending[1:3]
    assert messages[3]["role"] == "tool" and messages[3]["tool_call_id"] == "c1"
    assert "verify" in messages[3]["content"].lower()
    assert messages[-1]["role"] == "user"
    assert "New user instruction" in messages[-1]["content"]
    assert '"safe_to_resume": false' in messages[-1]["content"]
    assert stage._provider_recovery_deferred_followups == []
    with pytest.raises(ValueError, match="no pending Tool call"):
        pending_tool_calls(messages)


def test_model_switch_closes_pending_batch_without_executing_it():
    from orchestrator.her_v2.config import ProviderProfile
    from orchestrator.her_v2.provider_continuation import pending_tool_calls

    messages = [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "Original task"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "pending-write", "type": "function",
             "function": {"name": "file_write", "arguments": "{}"}}
        ]},
    ]
    stage = HashiStageProvider(backend_manager=SimpleNamespace())
    stage.bind_provider_context(observer=lambda _: None,
        recovery=_context("pending_tool_batch", messages), successor_turn=True, resume_pending_tools=True)
    result = stage._provider_recovery_messages(
        profile=ProviderProfile("main", "openrouter-api", "configured/new"),
        request=_direct_request(), stage_prompt="Assembled context",
        original_request="New user instruction", system_prompt="new system")
    assert any(row.get("tool_call_id") == "pending-write" for row in result)
    assert "verify" in next(row["content"] for row in result if row.get("tool_call_id") == "pending-write").lower()
    assert "New user instruction" in result[-1]["content"]
    with pytest.raises(ValueError, match="no pending Tool call"):
        pending_tool_calls(result)
    assert stage._provider_recovery_resume_pending is False


def test_model_switch_keeps_completed_tool_and_structured_media():
    from orchestrator.her_v2.config import ProviderProfile

    history = [{"role": "system", "content": "old system"},
               {"role": "user", "content": "Original task"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "read", "type": "function",
                    "function": {"name": "file_read", "arguments": "{}"}}
               ]},
               {"role": "tool", "tool_call_id": "read", "content": "Useful completed result"},
               {"role": "user", "content": [
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
               ]}]
    stage = HashiStageProvider(backend_manager=SimpleNamespace())
    stage.bind_provider_context(observer=lambda _: None,
        recovery=_context("completed_tool_batch", history),
        resume_pending_tools=True, successor_turn=True)
    messages = stage._provider_recovery_messages(
        profile=ProviderProfile("main", "openrouter-api", "configured/new"),
        request=_direct_request(), stage_prompt="Assembled context",
        original_request="New user instruction", system_prompt="new system")
    assert messages is not None
    assert any(row.get("tool_call_id") == "read" and row.get("content") == "Useful completed result"
               for row in messages)
    assert any(isinstance(row.get("content"), list) and row["content"][0]["type"] == "image_url"
               for row in messages)
    assert "New user instruction" in messages[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary,steered,model_switched", [
    ("pending_tool_batch", False, False), ("completed_tool_batch", False, False),
    ("pending_tool_batch", True, False), ("pending_tool_batch", False, True),
])
async def test_real_file_effects_survive_interrupted_recovery(tmp_path, monkeypatch, boundary, steered, model_switched):
    from tests.test_herv3_p0_checkpoint_recovery_bounds import _runtime_event
    from tools.registry import ToolRegistry

    wire = []
    counts = {}
    global_config = _global_config(tmp_path)

    def answer(request):
        payload = json.loads(request.content)
        wire.append(payload)
        if len(wire) == 1:
            tool_calls = [
                {"index": n, "id": f"write-{n}", "type": "function",
                 "function": {"name": "file_write", "arguments": json.dumps({
                     "path": str(tmp_path / f"effect-{n}.txt"), "content": f"effect-{n}"
                 })}}
                for n in range(2)
            ]
            delta = {"tool_calls": tool_calls}
            finish = "tool_calls"
        elif (boundary == "pending_tool_batch" and not steered and not model_switched
              and any(row.get("tool_call_id") == "write-1" for row in payload["messages"])
              and not any(row.get("tool_call_id") == "write-reconsidered" for row in payload["messages"])):
            # A newly accepted instruction is considered first; only the
            # model's new decision may request the remaining write.
            delta = {"tool_calls": [{"index": 0, "id": "write-reconsidered", "type": "function",
                     "function": {"name": "file_write", "arguments": json.dumps({
                         "path": str(tmp_path / "effect-1.txt"), "content": "effect-1"
                     })}}]}
            finish = "tool_calls"
        else:
            delta = {"content": "Both files are written."}
            finish = "stop"
        events = [
            {"id": f"response-{len(wire)}", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": f"response-{len(wire)}", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

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
        source_state = "running"
        def get_run(self, run_id, *, owner_id=None):
            request_id = run_id.removeprefix("run-")
            return {"run_id": run_id, "request_id": request_id, "agent_id": "agent",
                    "attempt": 1, "fencing_token": 1, "request_digest": "sha256:" + "b" * 64,
                    "state": self.source_state if request_id == "first" else "running",
                    "parent_run_id": "run-first" if steered and request_id == "second" else None}

    manager = Manager()
    pao_runs = PaoRuns()
    runtime = SimpleNamespace(backend_manager=manager, _request_meta_by_id={},
                              current_request_meta={}, session_store=pao_runs)
    config = _agent_config(tmp_path)
    registry = ToolRegistry(allowed_tools=["file_write"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={})
    registry.permission_durable_wait = True
    execute = registry.execute
    async def counted(name, arguments, tool_call_id=""):
        counts[tool_call_id] = counts.get(tool_call_id, 0) + 1
        return await execute(name, arguments, tool_call_id=tool_call_id)
    monkeypatch.setattr(registry, "execute", counted)
    config._hashi_runtime = runtime
    config._her_v2_stage_provider = HashiStageProvider(backend_manager=manager, tool_registry=registry)
    adapter = HERv2Adapter(config, global_config)
    assert await adapter.initialize()
    coordinator = adapter._session_coordinator
    persist = coordinator.record_provider_context_checkpoint
    interrupted = {"done": False, "turn": None, "again": False}

    def crash_after_first_write(accepted, checkpoint):
        result = persist(accepted, checkpoint)
        if (
            not steered and not model_switched and boundary == "pending_tool_batch"
            and accepted.turn_id == "second" and checkpoint["boundary"] == "completed_tool_batch"
            and not interrupted["again"]
        ):
            interrupted["again"] = True
            raise asyncio.CancelledError()
        if (not interrupted["done"] and checkpoint["boundary"] == boundary
                and any(m.get("role") == "tool" and m.get("tool_call_id") == "write-0"
                        for m in checkpoint["messages"])):
            interrupted.update(done=True, turn=accepted)
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(coordinator, "record_provider_context_checkpoint", crash_after_first_write)
    monkeypatch.setattr(adapter, "_close_interrupted_fixed_turn", lambda *args, **kwargs: None)
    payload = {"transport_snapshot": {"version": 1, "sections": [
        {"key": "reference", "title": "REFERENCE", "text": "PENDING_STATIC_REFERENCE",
         "authority": "runtime_context", "rank": 3, "protected": False, "metadata": {}, "order": 0},
        {"key": "current_user_request", "title": "REQUEST", "text": "placeholder",
         "authority": "current_user", "rank": 3, "protected": True, "metadata": {}, "order": 1}
    ]}}

    def prepare(request_id, user, evidence=None):
        meta = {"request_id": request_id, "owner_id": "owner", "hashi_run_id": "run-" + request_id,
                "hashi_fencing_token": 1, "hashi_session_id": "isolated-session",
                "hashi_message_id": "message-" + request_id, "context_generation": 1,
                "session_workspace": str(tmp_path / "session")}
        if evidence is not None:
            meta["source"] = "steer"
            meta["request_metadata"] = {
                "parent_run_id": "run-first", "steer_parent_request_id": "first",
                "steer_continuation_evidence": {
                    **evidence, "old_run_id": "run-first", "relationship": "linked_successor",
                },
            }
        runtime._request_meta_by_id[request_id] = meta
        runtime.current_request_meta = meta
        config._her_v2_stage_provider = HashiStageProvider(backend_manager=manager, tool_registry=registry)
        return adapter.prepare_fixed_turn_input(prompt_payload=payload, user_message=user,
                                                request_id=request_id, request_meta=meta)[0]

    first = prepare("first", "Write both isolated files")
    with pytest.raises(asyncio.CancelledError):
        await adapter.generate_response(first, "first")
    assert interrupted["done"], counts
    accepted = interrupted["turn"]
    coordinator.record_runtime_event(accepted, _runtime_event(
        accepted.turn_id, "uncertain-side-effect", "tool_intent",
        operation_id="unknown-other", tool_call_id="unknown-other",
        tool_name="external_write", read_only=False))
    evidence = None
    if steered:
        coordinator.steer(accepted, reason="Stop the remaining write and inspect.")
        pao_runs.source_state = "superseded"
        evidence = adapter.steer_continuation_evidence("first")
        assert evidence is not None
    else:
        assert coordinator.store.reconcile_interrupted(process_is_alive=lambda _pid: False) == 1

    payload["transport_snapshot"]["sections"].insert(1, {
        "key": "new-reference", "title": "NEW", "text": "PENDING_NEW_FACT",
        "authority": "runtime_context", "rank": 3, "protected": False,
        "metadata": {}, "order": 1,
    })
    if model_switched:
        from dataclasses import replace
        current = adapter._v2_config
        profiles = dict(current.profiles)
        profiles["main"] = replace(profiles["main"], model="configured/switched")
        adapter._v2_config = replace(current, profiles=profiles, routing_revision=current.routing_revision + 1)
    second = prepare("second", "Continue and verify both files", evidence=evidence)
    if not steered and not model_switched and boundary == "pending_tool_batch":
        with pytest.raises(asyncio.CancelledError):
            await adapter.generate_response(second, "second")
        assert interrupted["again"]
        assert coordinator.store.reconcile_interrupted(process_is_alive=lambda _pid: False) == 1
        third = prepare("third", "Finish checking the files")
        response = await adapter.generate_response(third, "third")
    else:
        response = await adapter.generate_response(second, "second")
    assert response.is_success, response.error
    if steered or model_switched:
        assert counts == {"write-0": 1}
    elif boundary == "pending_tool_batch":
        assert counts == {"write-0": 1, "write-reconsidered": 1}
        reconsideration = next(call["messages"] for call in wire
                               if any(row.get("tool_call_id") == "write-1" for row in call["messages"]))
        assert "Continue and verify both files" in json.dumps(reconsideration)
    else:
        assert counts == {"write-0": 1, "write-1": 1}
    assert (tmp_path / "effect-0.txt").read_text(encoding="utf-8") == "effect-0"
    if steered or model_switched:
        assert not (tmp_path / "effect-1.txt").exists()
    else:
        assert (tmp_path / "effect-1.txt").read_text(encoding="utf-8") == "effect-1"
    outgoing = wire[-1]["messages"]
    tool_ids = [m.get("tool_call_id") for m in outgoing if m.get("role") == "tool"]
    assert tool_ids == (["write-0", "write-1", "write-reconsidered"]
                        if boundary == "pending_tool_batch" and not steered and not model_switched
                        else ["write-0", "write-1"])
    serialized = json.dumps(outgoing, ensure_ascii=False)
    assert "unknown-other" in serialized
    assert "Continue and verify both files" in serialized
    assert serialized.count("PENDING_STATIC_REFERENCE") == 1
    assert serialized.count("PENDING_NEW_FACT") == 1
    if not steered and not model_switched and boundary == "pending_tool_batch":
        assert "Finish checking the files" in serialized
    assert "_hashi_successor_" not in serialized
    assert outgoing[-1]["role"] == "user"
    await adapter.shutdown()
