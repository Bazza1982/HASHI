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
        ["http_request", "web_fetch"], access_root=tmp_path, workspace_dir=home, secrets={},
        audit_context=context, search_scope={"agent_id": "fixture", "agent_home": str(home)},
    )
    return registry, context


async def _serve(handler):
    app = web.Application()
    app.router.add_post("/effect", handler)
    app.router.add_get("/effect", handler)
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



@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["http_request", "web_fetch"])
@pytest.mark.parametrize("coding", ["gzip", "deflate", "raw_deflate", "gzip_deflate", "deflate_gzip"])
async def test_large_compressed_response_does_not_inflate_whole_body_in_memory(tmp_path, tool, coding):
    import gzip
    import zlib

    payload_size = 24 * 1024 * 1024
    sentinel = b"COMPRESSED-HTTP-COMPLETE"
    raw = b"x" * payload_size + sentinel
    if coding == "gzip":
        compressed, header = gzip.compress(raw, mtime=0), "gzip"
    elif coding == "gzip_deflate":
        compressed, header = zlib.compress(gzip.compress(raw, mtime=0)), "gzip, deflate"
    elif coding == "deflate_gzip":
        compressed, header = gzip.compress(zlib.compress(raw), mtime=0), "deflate, gzip"
    else:
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS if coding == "raw_deflate" else zlib.MAX_WBITS)
        compressed = compressor.compress(raw) + compressor.flush()
        header = "deflate"
    del raw
    received = []

    async def respond(request):
        received.append(await request.text())
        return web.Response(
            body=compressed,
            headers={"Content-Type": "text/plain; charset=utf-8", "Content-Encoding": header},
        )

    runner, url = await _serve(respond)
    registry, context = _registry(tmp_path)
    try:
        gc.collect()
        tracemalloc.start()
        try:
            arguments = {"url": url}
            if tool == "http_request":
                arguments.update(method="POST", body="once")
            result = await registry.execute(tool, arguments)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert not result.is_error, result.output
        if tool == "http_request":
            assert result.details["response_complete"] is True
        assert received == (["once"] if tool == "http_request" else [""])
        reference = result.details["output_continuation"]
        binding = run_binding(context)
        item = registry._tool_output_store.lookup(binding, reference)
        last = json.loads(registry._tool_output_store.page(
            binding, reference, max(0, item.characters - 300),
        ))
        assert sentinel.decode("ascii") in last["text"]
        assert peak < payload_size // 2, f"{coding} inflated with {peak} traced bytes"
    finally:
        await runner.cleanup()



@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["http_request", "web_fetch"])
async def test_gzip_members_preserve_unicode_and_truncated_stream_fails(tmp_path, tool):
    import gzip
    compressed = gzip.compress(b"first ", mtime=0) + gzip.compress("中文 last".encode(), mtime=0)
    truncated = False

    async def respond(_request):
        return web.Response(
            body=compressed[:-2] if truncated else compressed,
            headers={"Content-Type": "text/plain; charset=utf-8", "Content-Encoding": "gzip"},
        )

    runner, url = await _serve(respond)
    registry, _ = _registry(tmp_path)
    try:
        result = await registry.execute(tool, {"url": url})
        assert not result.is_error, result.output
        assert "first 中文 last" in result.output
        truncated = True
        incomplete = await registry.execute(tool, {"url": url})
        assert incomplete.is_error
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("stacked", [False, True])
async def test_explicit_http_budget_applies_to_decompressed_gzip_bytes(tmp_path, stacked):
    import gzip
    import zlib

    body = gzip.compress(b"abcdefghijk", mtime=0)
    if stacked:
        body = zlib.compress(body)
    header = "gzip, deflate" if stacked else "gzip"

    async def respond(_request):
        return web.Response(
            body=body,
            headers={"Content-Type": "text/plain; charset=utf-8", "Content-Encoding": header},
        )

    runner, url = await _serve(respond)
    home = tmp_path / "agent"
    home.mkdir()
    registry = ToolRegistry(
        ["http_request"], access_root=tmp_path, workspace_dir=home, secrets={},
        tool_options={"http_request": {"max_response_bytes": 5}},
    )
    try:
        result = await registry.execute("http_request", {"url": url})
        assert not result.is_error
        assert result.details["response_complete"] is False
        assert result.details["response_bytes"] == 5
        assert "abcde" in result.output
        assert "fgh" not in result.output
        assert "No automatic retry" in result.output
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["http_request", "web_fetch"])
async def test_other_http_content_encodings_keep_existing_support(tmp_path, tool):
    import zlib

    async def respond(_request):
        return web.Response(
            body=zlib.compress("deflate 中文".encode()),
            headers={"Content-Type": "text/plain; charset=utf-8", "Content-Encoding": "deflate"},
        )

    runner, url = await _serve(respond)
    registry, _ = _registry(tmp_path)
    try:
        result = await registry.execute(tool, {"url": url})
        assert not result.is_error, result.output
        assert "deflate 中文" in result.output
    finally:
        await runner.cleanup()



@pytest.mark.asyncio
@pytest.mark.parametrize("stacked", [False, True])
async def test_cancel_during_gzip_expansion_does_not_repeat_post(tmp_path, monkeypatch, stacked):
    import gzip
    import zlib
    from contextlib import contextmanager

    compressed = gzip.compress(b"x" * (32 * 1024 * 1024), mtime=0)
    if stacked:
        compressed = zlib.compress(compressed)
    header = "gzip, deflate" if stacked else "gzip"
    received = []
    reached = asyncio.Event()

    async def respond(request):
        received.append(await request.text())
        return web.Response(
            body=compressed,
            headers={"Content-Type": "text/plain", "Content-Encoding": header},
        )

    runner, url = await _serve(respond)
    registry, context = _registry(tmp_path)
    store = registry._tool_output_store
    original = store.temporary_capture_file

    @contextmanager
    def observed_capture():
        with original() as capture:
            class Observer:
                written = 0

                def write(self, data):
                    self.written += len(data)
                    if self.written > 1024 * 1024:
                        reached.set()
                    return capture.write(data)

                def __getattr__(self, key):
                    return getattr(capture, key)

            yield Observer()

    monkeypatch.setattr(store, "temporary_capture_file", observed_capture)
    task = asyncio.create_task(registry.execute("http_request", {
        "url": url, "method": "POST", "body": "once",
    }))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert received == ["once"]
        run_dir = store._run_dir(store.private_root, run_binding(context))
        assert not list(run_dir.glob("*.data"))
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()



@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["http_request", "web_fetch"])
async def test_identity_http_content_encoding_stays_supported(tmp_path, tool):
    async def respond(_request):
        return web.Response(
            body="ordinary 中文".encode(),
            headers={"Content-Type": "text/plain; charset=utf-8", "Content-Encoding": "identity"},
        )

    runner, url = await _serve(respond)
    registry, _ = _registry(tmp_path)
    try:
        result = await registry.execute(tool, {"url": url})
        assert not result.is_error, result.output
        assert "ordinary 中文" in result.output
    finally:
        await runner.cleanup()
