"""End-to-end HTTP response retention with disposable loopback services."""
from __future__ import annotations

import asyncio
import gc
import json
import threading
import tracemalloc

import httpx
import pytest
from aiohttp import web

from tools.registry import ToolRegistry
from tools.tool_output_store import run_binding


def _registry(tmp_path, *, run="run-http", request="request-http"):
    home = tmp_path / "agent"
    home.mkdir(exist_ok=True)
    context = {
        "instance_id": "NP", "agent_name": "fixture", "owner_id": "user:1",
        "hashi_session_id": "session-1", "hashi_run_id": run, "request_id": request,
    }
    registry = ToolRegistry(
        ["http_request"], access_root=tmp_path, workspace_dir=home, secrets={},
        audit_context=context, search_scope={"agent_id": "fixture", "agent_home": str(home)},
    )
    return registry, context


async def _serve(handler):
    app = web.Application()
    app.router.add_post("/effect", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/effect"


@pytest.mark.asyncio
async def test_large_http_post_is_paged_after_registry_replacement_without_replay(tmp_path):
    assert httpx.AsyncClient is not None
    unit = b"entry-" * 8192
    repetitions = 400
    suffix = "中文-FINAL-HTTP-SENTINEL".encode("utf-8")
    size = len(unit) * repetitions + len(suffix)
    assert size > 16 * 1024 * 1024
    received = []

    async def respond(request):
        received.append(await request.text())
        response = web.StreamResponse(headers={
            "Content-Type": "text/plain; charset=utf-8", "Content-Length": str(size),
        })
        await response.prepare(request)
        for _ in range(repetitions):
            await response.write(unit)
        # Deliberately split a multi-byte character across writes.
        await response.write(suffix[:1])
        await response.write(suffix[1:])
        return response

    runner, url = await _serve(respond)
    registry, context = _registry(tmp_path)
    try:
        gc.collect()
        tracemalloc.start()
        try:
            result = await registry.execute("http_request", {
                "url": url, "method": "POST", "body": "once",
            })
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert not result.is_error, result.output
        assert received == ["once"]
        assert result.details["response_complete"] is True
        assert result.details["response_limit_bytes"] is None
        assert result.details["response_bytes"] == size
        assert peak < size // 2, f"HTTP used {peak} traced bytes for {size} response bytes"
        reference = result.details["output_continuation"]
        replacement, _ = _registry(tmp_path)
        binding = run_binding(context)
        retained = replacement._tool_output_store.lookup(binding, reference)
        last = json.loads(replacement._tool_output_store.page(
            binding, reference, max(0, retained.characters - 500),
        ))
        assert "中文-FINAL-HTTP-SENTINEL" in last["text"]
        assert received == ["once"]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_retention_cancellation_keeps_other_runs_and_never_reposts(tmp_path, monkeypatch):
    body = "data " * 30000
    received = []

    async def respond(request):
        received.append(await request.text())
        return web.Response(text=body)

    runner, url = await _serve(respond)
    registry, context = _registry(tmp_path)
    store = registry._tool_output_store
    other_registry, other_context = _registry(tmp_path, run="other", request="other")
    other_binding = run_binding(other_context)
    other_ref = other_registry._tool_output_store.retain(other_binding, "fixture", "other survives")
    original = store.retain_text_chunks
    entered, release = threading.Event(), threading.Event()

    def pause(binding, source_tool, chunks):
        def pieces():
            for index, chunk in enumerate(chunks):
                if index == 1:
                    entered.set()
                    assert release.wait(5)
                yield chunk
        return original(binding, source_tool, pieces())

    monkeypatch.setattr(store, "retain_text_chunks", pause)
    task = asyncio.create_task(registry.execute("http_request", {
        "url": url, "method": "POST", "body": "once",
    }))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        page = json.loads(other_registry._tool_output_store.page(other_binding, other_ref, 0))
        assert page["text"] == "other survives"
        cancelled_dir = store._run_dir(store.private_root, run_binding(context))
        assert not list(cancelled_dir.glob("*.data"))
        assert received == ["once"]
    finally:
        release.set()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_capture_and_retention_failures_report_effect_truthfully(tmp_path, monkeypatch):
    received = []

    async def respond(request):
        received.append(await request.text())
        return web.Response(text="response " * 15000)

    runner, url = await _serve(respond)
    registry, _ = _registry(tmp_path)
    store = registry._tool_output_store

    def fail(*_args, **_kwargs):
        raise OSError("fixture disk unavailable")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, "temporary_capture_file", fail)
            before = await registry.execute("http_request", {
                "url": url, "method": "POST", "body": "once",
            })
        assert before.is_error
        assert before.details["request_effect"] == "not_dispatched"
        assert received == []

        with monkeypatch.context() as patch:
            patch.setattr(store, "retain_text_chunks", fail)
            after = await registry.execute("http_request", {
                "url": url, "method": "POST", "body": "once",
            })
        assert not after.is_error
        assert after.details["response_complete"] is True
        assert after.details["complete_content"] is False
        assert after.details["output_retention"] == "unavailable"
        assert "retention is unavailable" in after.output
        assert received == ["once"]
    finally:
        await runner.cleanup()
# HASHI-Fingerprint: hfp_ca814e5babb24670a8a8fc6d06ff36c9
