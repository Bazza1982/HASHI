"""Ollama reference projection and tool-loop capacity boundaries."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import httpx
import pytest

from adapters.ollama_api import OllamaAdapter
from orchestrator.her_v2.v3_prompt import compile_main_prompt
from tests.test_ollama_context_pilot import _adapter


@pytest.mark.parametrize("detail", ["archived evidence line. " * 220, "历史详情逐轮累积。" * 180])
def test_direct_small_model_bounds_accumulated_history_and_retains_sources(tmp_path, detail):
    from pathlib import Path
    from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
    from orchestrator.pcm import render_pcm_document
    from tools.registry import ToolRegistry

    pcm = tmp_path / "agent.md"
    pcm.write_text(render_pcm_document(
        persona="KEEP_PERSONA", system="KEEP_SYSTEM", memory="",
    ), encoding="utf-8")
    adapter = _adapter(tmp_path, capacity=8192)
    adapter.config.extra["model_capabilities"]["local-qwen"].update({
        "ollama_native_context": True, "response_headroom_tokens": 2048,
        "ollama_max_input_chars": 25000,
    })
    adapter.tool_registry = ToolRegistry(
        allowed_tools=["file_read", "log_query"], access_root=tmp_path,
        workspace_dir=tmp_path, secrets={},
    )
    assembler = BridgeContextAssembler(BridgeMemoryStore(tmp_path), pcm)
    history = []
    projected_history = False
    for sequence in range(1, 13):
        history.append({
            "sequence": sequence, "user_text": f"Completed step {sequence}",
            "assistant_text": detail + f" LATEST_DETAILS_{sequence}",
            "user_ts": "2026-11-04T00:00:00Z",
            "assistant_ts": "2026-11-04T00:00:01Z",
        })
        source = assembler.build_prompt_payload(
            "KEEP_CURRENT_TASK", "ollama-api", recent_exchanges=history,
            extra_sections=[("ACTIVE RECOVERY", "UNRESOLVED_EFFECT_RECEIPT", {
                "key": "active_recovery", "protected": True,
            })],
        )
        original = deepcopy(source)
        projected = adapter.project_pcm_payload(source, render_prompt=assembler._render_pcm_prompt)
        wire = adapter._build_payload(adapter._initial_messages(projected["final_prompt"], None)[0])
        chars = len(json.dumps({k: v for k, v in wire.items()
                               if k not in {"options", "keep_alive", "stream"}}))
        assert chars <= 25000, (sequence, chars)
        assert source == original
        for marker in ("KEEP_CURRENT_TASK", "KEEP_SYSTEM", "KEEP_PERSONA", "UNRESOLVED_EFFECT_RECEIPT"):
            assert marker in projected["final_prompt"]
        assert f"LATEST_DETAILS_{sequence}" in projected["final_prompt"]
        projection = projected.get("audit", {}).get("model_reference_projection", {})
        history_keys = [key for key in projection.get("projected_keys", [])
                        if key.startswith("recent_exchange:")]
        if history_keys:
            projected_history = True
            saved = Path(projection["saved_reference"]).read_bytes()
            assert hashlib.sha256(saved).hexdigest() == projection["sha256"]
            sources = {row["key"]: row["text"] for row in json.loads(saved)}
            original_rows = {row["key"]: row["text"] for row in source["transport_snapshot"]["sections"]}
            assert all(sources[key] == original_rows[key] for key in history_keys)
            assert "not complete summaries" in projected["final_prompt"]
            assert str(projection["saved_reference"]) in projected["final_prompt"]
    assert projected_history
    adapter.config.model = "another-model"
    assert adapter.project_pcm_payload(source, render_prompt=assembler._render_pcm_prompt) == source


def test_small_model_preserves_oversized_protected_input(tmp_path):
    from adapters.openrouter_api import ProviderContextCapacityPreflightError
    from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
    from orchestrator.pcm import render_pcm_document

    pcm = tmp_path / "agent.md"
    pcm.write_text(render_pcm_document(persona="KEEP_PERSONA", system="KEEP_SYSTEM", memory=""), encoding="utf-8")
    adapter = _adapter(tmp_path, capacity=8192)
    adapter.config.extra["model_capabilities"]["local-qwen"].update({
        "ollama_native_context": True, "response_headroom_tokens": 2048,
        "ollama_max_input_chars": 25000,
    })
    assembler = BridgeContextAssembler(BridgeMemoryStore(tmp_path), pcm)
    current = "当前任务必须保留。" * 1000
    source = assembler.build_prompt_payload(current, "ollama-api", recent_exchanges=[])
    projected = adapter.project_pcm_payload(source, render_prompt=assembler._render_pcm_prompt)
    assert current in projected["final_prompt"]
    wire = adapter._build_payload(adapter._initial_messages(projected["final_prompt"], None)[0])
    with pytest.raises(ProviderContextCapacityPreflightError):
        adapter._preflight_payload_capacity(wire)


def test_small_model_retains_usable_one_line_search_result(tmp_path):
    from adapters.ollama_capacity import page_new_tool_results

    adapter = _adapter(tmp_path, capacity=8192)
    adapter.config.extra["model_capabilities"]["local-qwen"].update({
        "ollama_native_context": True, "response_headroom_tokens": 2048,
        "ollama_max_input_chars": 25000,
    })
    adapter._ollama_prompt_tokens = 4085
    adapter._ollama_last_output_tokens = 345
    evidence = json.dumps({
        "matches": [{"term": "EARLIER_KEY", "excerpt":
                     "历史上下文。" * 20 + " EARLIER_KEY=RECOVERED_RANDOM_VALUE " + "后续上下文。" * 20}],
        "coverage": {"complete": False}, "can_continue": True,
        "metadata": "safe source metadata " * 30,
    }, ensure_ascii=False)
    messages = [{"role": "user", "content": "Find earlier evidence."},
                {"role": "tool", "tool_call_id": "read-1", "content": evidence}]
    page_new_tool_results(adapter, messages, 1)
    assert "EARLIER_KEY=RECOVERED_RANDOM_VALUE" in messages[1]["content"]
    assert json.loads(messages[1]["content"])["matches"][0]["excerpt"].endswith("后续上下文。" * 20)


@pytest.mark.asyncio
async def test_small_model_can_read_history_references_before_closing_tools(tmp_path):
    from tools.registry import ToolRegistry

    adapter = _adapter(tmp_path, capacity=8192)
    adapter.config.extra["model_capabilities"]["local-qwen"].update({
        "ollama_native_context": True, "response_headroom_tokens": 2048,
        "ollama_max_input_chars": 25000,
    })
    marker = tmp_path / "older-evidence.txt"
    marker.write_text("EARLIER_VALUE_FOUND_BY_READ", encoding="utf-8")
    adapter.tool_registry = ToolRegistry(
        allowed_tools=["file_read"], access_root=tmp_path,
        workspace_dir=tmp_path, secrets={},
    )
    reads = 0

    def respond(request):
        nonlocal reads
        payload = json.loads(request.content)
        probe = payload["options"]["num_predict"] == 1
        result = next((m["content"] for m in payload["messages"] if m["role"] == "tool"), None)
        message = {"content": ""}
        if not probe:
            if result is not None:
                message["content"] = result
            elif payload.get("tools"):
                reads += 1
                message["tool_calls"] = [{"function": {
                    "name": "file_read", "arguments": {"path": str(marker)},
                }}]
            else:
                message["content"] = "Reference cannot be read because tools were closed."
        return httpx.Response(200, json={
            "message": message, "done": True, "done_reason": "length" if probe else "stop",
            "prompt_eval_count": 4800 if result else 4500,
            "eval_count": 1 if probe else 40,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("Read the earlier saved evidence.", "small-history-read")
    assert response.is_success, response.error
    assert "EARLIER_VALUE_FOUND_BY_READ" in response.text
    assert response.tool_call_count == reads == 1


def test_reference_projection_preserves_authority_recovery_and_full_source(tmp_path):
    adapter = _adapter(tmp_path)
    recovery = {
        "active_turn_recovery": {
            "remaining_work": {"goal": "Review the interface"},
            "side_effects": [{"operation_id": "write-1", "status": "UNKNOWN_SIDE_EFFECT"}],
        },
        "settled_checkpoint": {
            "remaining_work": {"pending": "inspect source"},
            "tool_receipts": [{"id": "read-1", "sha256": "evidence"}],
            "route_snapshot": {"routes": {"unused": "metadata" * 2000}},
        },
    }
    pcm = {
        "current_request": "Continue the interface review",
        "sections": [
            {"key": "local", "authority": "local_system", "text": "Never disclose secrets."},
            {"key": "persona", "authority": "persona", "text": "Use the configured Chinese voice."},
            {"key": "extra:scheduler_recovery", "authority": "runtime_context",
             "text": "PENDING RECOVERY BATCHES\n- Batch recovery-1 · status=pending\n"
                     "  - task_id=export kind=cron missed_count=144 replayable_count=1\n"
                     "    task_prompt=" + "Private full instructions. " * 1500 +
                     "\n    first_due=2026-10-01; last_due=2026-10-16\n"},
            {"key": "recent_exchange:notice", "authority": "history",
             "text": "Exchange sequence=8\nUSER: ⏰ HASHI offline recovery\n"
                     "Recovery batch: historical-batch\n• old-task\n  Content: " +
                     "Old recovery detail. " * 500 +
                     "\nASSISTANT: Awaiting the user's decision."},
            {"key": "tools_catalogue", "authority": "runtime_context",
             "text": "- file_read: Read files with offset and limit. " + "Details. " * 1000},
        ],
        "history": [{
            "sequence": 1, "user_message": "Locate the project",
            "assistant_text": "The project is available.", "status": "completed",
            "route_snapshot": {"all_routes": "metadata" * 2000},
            "execution_owner_id": "internal-owner",
        }],
        "runtime_context": recovery,
    }
    original = deepcopy(pcm)
    cloud_system, cloud_user = compile_main_prompt(
        pcm_input=pcm, fallback_request="fallback", context={}
    )
    system, user = compile_main_prompt(
        pcm_input=pcm, fallback_request="fallback", context={},
        reference_projector=adapter.project_main_references,
    )
    refs = json.JSONDecoder().raw_decode(user[user.index("{"):])[0]
    assert system == cloud_system
    assert user.endswith("Current user request:\nContinue the interface review")
    assert refs["session_history_context_only"][0]["assistant_text"] == "The project is available."
    assert "execution_owner_id" not in refs["session_history_context_only"][0]
    assert "route_snapshot" not in refs["session_history_context_only"][0]
    assert refs["runtime_recovery_context_only"]["active_turn_recovery"] == recovery["active_turn_recovery"]
    assert refs["runtime_recovery_context_only"]["settled_checkpoint"]["tool_receipts"] == recovery["settled_checkpoint"]["tool_receipts"]
    index = next(r["text"] for r in refs["context_only"] if r["source"] == "extra:scheduler_recovery")
    assert "recovery-1" in index and "task_id=export" in index
    historic = next(r["text"] for r in refs["context_only"] if r["source"] == "recent_exchange:notice")
    assert "historical-batch" in historic and "Old recovery detail." not in historic
    saved = refs["full_reference"]
    from pathlib import Path
    raw = Path(saved["saved_reference"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == saved["sha256"]
    full = json.loads(raw)
    assert full["session_history_context_only"] == pcm["history"]
    assert full["runtime_recovery_context_only"] == recovery
    assert full["context_only"][0]["text"] == pcm["sections"][2]["text"]
    assert len(user) < len(cloud_user) // 3
    assert pcm == original
    legacy = _adapter(tmp_path / "legacy", enabled=False)
    assert legacy.project_main_references(full) == full


@pytest.mark.asyncio
async def test_capacity_recovery_closes_tools_and_preserves_completed_write(tmp_path):
    from tools.registry import ToolRegistry

    sentinel = tmp_path / "completed.txt"
    adapter = _adapter(tmp_path)
    adapter.tool_registry = ToolRegistry(
        allowed_tools=["file_write"], access_root=tmp_path,
        workspace_dir=tmp_path, secrets={},
    )
    requests = []
    writes = 0

    def respond(request):
        nonlocal writes
        payload = json.loads(request.content)
        requests.append(payload)
        probe = payload["options"]["num_predict"] == 1
        has_result = any(m["role"] == "tool" for m in payload["messages"])
        count = 37000 if not has_result else (41149 if payload.get("tools") else 38200)
        message = {"content": ""}
        if not probe:
            if not has_result:
                writes += 1
                message["tool_calls"] = [{
                    "function": {"name": "file_write", "arguments": {
                        "path": str(sentinel), "content": "completed exactly once",
                    }},
                }]
            else:
                message["content"] = "Completed the write; remaining inspection is pending."
        return httpx.Response(200, json={
            "message": message, "done": True,
            "done_reason": "length" if probe else "stop",
            "prompt_eval_count": count, "eval_count": 1 if probe else 80,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("Write once, then report.", "close-loop")

    assert response.is_success, response.error
    assert sentinel.read_text(encoding="utf-8") == "completed exactly once"
    assert response.tool_call_count == writes == 1
    assert len(requests) == 5
    assert requests[2]["tools"] and "tools" not in requests[3] and "tools" not in requests[4]
    assert requests[0]["messages"][0] == requests[-1]["messages"][0]
    assert requests[0]["messages"][1] == requests[-1]["messages"][1]
    old_results = [m for m in requests[2]["messages"] if m["role"] == "tool"]
    assert old_results == [m for m in requests[-1]["messages"] if m["role"] == "tool"]
    assert all(p["options"]["num_ctx"] == 49152 for p in requests)
    assert all(p["options"]["num_predict"] in {1, 8192} for p in requests)


@pytest.mark.asyncio
async def test_low_space_read_batch_returns_source_and_finishes_without_page_recursion(tmp_path):
    from tools.registry import ToolRegistry

    files = []
    for i in range(3):
        p = tmp_path / f"source-{i}.txt"
        p.write_text(f"VISIBLE-SOURCE-{i}\n" + "Useful source information.\n" * 1000, encoding="utf-8")
        files.append(p)
    adapter = _adapter(tmp_path)
    adapter.tool_registry = ToolRegistry(
        allowed_tools=["file_read"], access_root=tmp_path,
        workspace_dir=tmp_path, secrets={},
    )
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        probe = payload["options"]["num_predict"] == 1
        results = [m for m in payload["messages"] if m["role"] == "tool"]
        message = {"content": ""}
        if not probe:
            if not results:
                message["tool_calls"] = [{
                    "function": {"name": "file_read", "arguments": {"path": str(p)}},
                } for p in files]
            else:
                message["content"] = "Visible markers checked; the unshown source remains unreviewed."
        return httpx.Response(200, json={
            "message": message, "done": True,
            "done_reason": "length" if probe else "stop",
            "prompt_eval_count": 37000 if not results else 39500,
            "eval_count": 1 if probe else 80,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("Inspect these three files.", "read-batch")

    assert response.is_success, response.error
    assert response.tool_call_count == 3
    final = requests[-1]
    assert "tools" not in final
    result_rows = [m for m in final["messages"] if m["role"] == "tool"]
    assert len(result_rows) == 3
    for i, row in enumerate(result_rows):
        assert f"VISIBLE-SOURCE-{i}" in row["content"]
        assert "HASHI_OLLAMA_RESULT_PAGE" in row["content"]
    saved = list((tmp_path / "backend_state" / "ollama_tool_results").glob("*.txt"))
    assert len(saved) == 3


@pytest.mark.asyncio
async def test_tool_request_after_capacity_closure_cannot_execute(tmp_path):
    from tools.registry import ToolRegistry

    sentinel = tmp_path / "must-not-be-written.txt"
    adapter = _adapter(tmp_path)
    adapter.tool_registry = ToolRegistry(
        allowed_tools=["file_write"], access_root=tmp_path,
        workspace_dir=tmp_path, secrets={},
    )

    def respond(request):
        payload = json.loads(request.content)
        probe = payload["options"]["num_predict"] == 1
        has_result = any(m["role"] == "tool" for m in payload["messages"])
        message = {"content": ""}
        if not probe and not has_result:
            # An unadvertised structured tool call is still untrusted.
            message["tool_calls"] = [{
                "function": {"name": "file_write", "arguments": {
                    "path": str(sentinel), "content": "unexpected",
                }},
            }]
        elif not probe:
            message["content"] = "done"
        return httpx.Response(200, json={
            "message": message, "done": True,
            "done_reason": "length" if probe else "stop",
            "prompt_eval_count": 40756 if payload.get("tools") else 38000,
            "eval_count": 1 if probe else 40,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("Inspect only.", "closed-tools")
    assert not sentinel.exists()
    assert not response.is_success
    assert response.error_code == "CONTEXT_CAPACITY_REJECTED"
