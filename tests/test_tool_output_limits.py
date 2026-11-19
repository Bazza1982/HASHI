from __future__ import annotations

from pathlib import Path
import asyncio
import json
import re

import pytest

from tools.builtins import execute_file_list, execute_file_read
from tools.registry import StructuredToolOutput, ToolRegistry
from tools.gateway.context import load_gateway_context, write_gateway_context
from tools.gateway.mcp_stdio import ToolGateway


@pytest.mark.asyncio
async def test_recursive_file_list_stops_after_bounded_entries(tmp_path: Path):
    for index in range(1005):
        (tmp_path / f"entry-{index:04d}.txt").touch()

    output = await execute_file_list(
        {"path": str(tmp_path), "recursive": True}, tmp_path, tmp_path
    )

    assert "items shown" in output.output
    coverage = json.loads(output.output.splitlines()[-1])
    assert coverage["can_continue"] and coverage["next_cursor"]
    assert not coverage["coverage_complete"]
    assert 1 <= coverage["counters"]["files_enumerated"] <= 50
    assert len(output.output) < 20_000


@pytest.mark.asyncio
async def test_file_read_bounds_one_very_long_line(tmp_path: Path):
    source = tmp_path / "long.txt"
    source.write_text("x" * 100_000, encoding="utf-8")

    output = await execute_file_read({"path": str(source)}, tmp_path, tmp_path)

    assert "[truncated" in output.output
    assert output.details["complete_content"] is False
    assert len(output.output) < 30_000


@pytest.mark.asyncio
async def test_registry_bounds_text_result_before_model_and_audit(tmp_path: Path, monkeypatch):
    registry = ToolRegistry(
        ["file_read"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
    )

    async def oversized(_name, _arguments, *, tool_call_id=""):
        return "x" * 100_000

    monkeypatch.setattr(registry, "_dispatch", oversized)
    result = await registry.execute("file_read", {"path": str(tmp_path / "x")})

    assert result.is_error is False
    assert "[truncated by HASHI" in result.output
    assert len(result.output) < 30_000


@pytest.mark.asyncio
async def test_registry_bounds_structured_text_without_dropping_image(tmp_path: Path, monkeypatch):
    registry = ToolRegistry(
        ["media_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={}
    )

    async def oversized(_name, _arguments, *, tool_call_id=""):
        return StructuredToolOutput(
            output="media summary",
            content=[
                {"type": "text", "text": "x" * 100_000},
                {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
            ],
        )

    monkeypatch.setattr(registry, "_dispatch", oversized)
    result = await registry.execute("media_read", {"path": str(tmp_path / "x")})

    assert len(result.content[0]["text"]) < 30_000
    assert "truncated by HASHI" in result.content[0]["text"]
    assert result.content[1]["data"] == "aGVsbG8="


@pytest.mark.asyncio
async def test_browser_session_text_without_screenshot_is_bounded(tmp_path: Path, monkeypatch):
    registry = ToolRegistry(
        ["browser_session"], access_root=tmp_path, workspace_dir=tmp_path, secrets={}
    )

    async def oversized(_name, _arguments, *, tool_call_id=""):
        return "[get_text] " + "x" * 100_000

    monkeypatch.setattr(registry, "_dispatch", oversized)
    result = await registry.execute("browser_session", {"steps": [{"action": "get_text"}]})

    assert "truncated by HASHI" in result.output
    assert len(result.output) < 30_000


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("smart_enabled", [False, True])
async def test_large_result_tail_is_retrievable_only_in_the_same_run(
    tmp_path: Path, monkeypatch, structured: bool, smart_enabled: bool
):
    original = ("海🦋\t\n" * 30_000) + "sentinel and trailing spaces  \n"
    audit = {
        "instance_id": "NP",
        "agent_name": "fixture",
        "owner_id": "owner",
        "hashi_session_id": "session",
        "hashi_run_id": "run-one",
        "request_id": "request-one",
        "request_tool_allowlist": ["file_read"],
    }
    registry = ToolRegistry(
        ["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        audit_context=audit, tool_options={"smart_registry": {"enabled": smart_enabled}},
    )
    original_dispatch = registry._dispatch

    async def fixture(name, arguments, *, tool_call_id=""):
        if name == "file_read":
            if structured:
                return StructuredToolOutput(
                    output="summary",
                    content=[
                        {"type": "text", "text": original},
                        {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
                    ],
                )
            return original
        return await original_dispatch(name, arguments, tool_call_id=tool_call_id)

    monkeypatch.setattr(registry, "_dispatch", fixture)
    first = await registry.execute("file_read", {"path": "synthetic.txt"})
    assert not first.is_error
    assert len(first.output) < 20_000
    assert first.details["complete_content"] is False
    assert first.details["effect_receipt"]["complete_content"] is False
    if structured:
        assert first.content[1]["data"] == "aGVsbG8="
        reference = first.details["content_continuations"][0]["reference"]
        assert reference in first.content[0]["text"]
    else:
        reference = first.details["output_continuation"]
        assert reference in first.output
    assert "tool_output_read" in {d["function"]["name"] for d in registry.get_tool_definitions()}

    parts = []
    offset = 0
    for _ in range(100):
        page = await registry.execute("tool_output_read", {"reference": reference, "offset": offset})
        assert not page.is_error, page.output
        data = json.loads(page.output)
        if smart_enabled:
            data = data["data"]
        parts.append(data["text"])
        if data["complete"]:
            break
        assert data["next_offset"] > offset
        offset = data["next_offset"]
    else:
        pytest.fail("result continuation did not finish")
    assert "".join(parts) == original

    denied = await registry.execute_with_audit_context(
        "tool_output_read", {"reference": reference, "offset": 0},
        audit_context={"hashi_run_id": "run-two", "request_id": "request-two"},
    )
    assert denied.is_error
    assert "sentinel" not in denied.output


@pytest.mark.asyncio
async def test_result_retention_failure_does_not_convert_completed_effect_to_failure(tmp_path: Path, monkeypatch):
    registry = ToolRegistry(
        ["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        audit_context={"hashi_run_id": "run-one"},
    )

    async def oversized(_name, _arguments, *, tool_call_id=""):
        return "x" * 30_000

    def failed_retention(*_args):
        raise OSError("storage unavailable")

    monkeypatch.setattr(registry, "_dispatch", oversized)
    monkeypatch.setattr(registry._tool_output_store, "retain", failed_retention)
    result = await registry.execute("file_read", {"path": "synthetic.txt"})
    assert not result.is_error
    assert result.details["output_retention"] == "unavailable"
    assert "retention is unavailable" in result.output


@pytest.mark.asyncio
async def test_gateway_exposes_scoped_result_continuation(tmp_path: Path, monkeypatch):
    original = "long result " * 5_000 + "final!"
    registry = ToolRegistry(
        ["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        audit_context={"agent_name": "fixture", "hashi_run_id": "run-one", "request_id": "req-one"},
    )
    context_path = tmp_path / "context.json"
    write_gateway_context(registry, context_path)
    gateway = ToolGateway(load_gateway_context(context_path))
    original_dispatch = gateway.registry._dispatch

    async def fixture(name, arguments, *, tool_call_id=""):
        if name == "file_read":
            return original
        return await original_dispatch(name, arguments, tool_call_id=tool_call_id)

    monkeypatch.setattr(gateway.registry, "_dispatch", fixture)
    response = await gateway.call("hashi_file_read", {"path": str(tmp_path / "x")}, "call-one")
    assert not response["isError"]
    reference = response["structuredContent"]["hashi"]["details"]["output_continuation"]
    assert reference in response["content"][0]["text"]
    pages = []
    offset = 0
    for _ in range(20):
        response = await gateway.call("tool_output_read", {"reference": reference, "offset": offset}, "page")
        assert not response["isError"], response
        data = json.loads(response["content"][0]["text"])
        pages.append(data["text"])
        if data["complete"]:
            break
        offset = data["next_offset"]
    else:
        pytest.fail("gateway result continuation did not finish")
    assert "".join(pages) == original


def _parts(output: str) -> tuple[str, str | None]:
    content = output.split("\n", 1)[1]
    marker = re.search(r"\n\[truncated by HASHI; next_cursor=([A-Za-z0-9_-]+)\]$", content)
    if marker:
        return content[:marker.start()], marker.group(1)
    return content, None


@pytest.mark.asyncio
async def test_long_unicode_line_can_be_recovered_across_pages(tmp_path: Path):
    source = tmp_path / "long.txt"
    line = "海🦋" * 60_000 + "TOKEN-END\n"
    source.write_text("start\n" + line + "last\n", encoding="utf-8")
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    result_parts = []
    cursor = None

    for _ in range(10):
        args = {"path": str(source), "offset": 2, "limit": 1}
        if cursor is not None:
            args["cursor"] = cursor
        result = await registry.execute("file_read", args)
        assert not result.is_error, result.output
        assert len(result.output) < 20_000
        text, cursor = _parts(result.output)
        result_parts.append(text)
        if cursor is None:
            break
    else:
        pytest.fail("continuation did not end")

    assert "".join(result_parts) == line + "last\n"
    offset_read = await registry.execute("file_read", {"path": str(source), "offset": 3})
    assert not offset_read.is_error and _parts(offset_read.output)[0] == "last\n"


@pytest.mark.asyncio
async def test_smart_registry_preserves_trailing_whitespace_across_file_pages(tmp_path: Path):
    source = tmp_path / "trailing.txt"
    original = "海🦋" * 20_000 + "   \n\n"
    source.write_text(original, encoding="utf-8")
    registry = ToolRegistry(
        ["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        tool_options={"smart_registry": {"enabled": True}},
    )
    parts = []
    cursor = None
    for _ in range(10):
        args = {"path": str(source)}
        if cursor:
            args["cursor"] = cursor
        result = await registry.execute("file_read", args)
        assert not result.is_error
        data = json.loads(result.output)["data"]
        part, cursor = _parts(data)
        parts.append(part)
        if not cursor:
            break
    else:
        pytest.fail("Smart Registry continuation did not end")
    assert "".join(parts) == original


@pytest.mark.asyncio
async def test_cursor_rejects_different_or_changed_file(tmp_path: Path):
    source = tmp_path / "one.txt"
    other = tmp_path / "two.txt"
    source.write_text("a" * 30_000, encoding="utf-8")
    other.write_text("b" * 30_000, encoding="utf-8")
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})

    first = await registry.execute("file_read", {"path": str(source)})
    _, cursor = _parts(first.output)
    assert cursor

    wrong = await registry.execute("file_read", {"path": str(other), "cursor": cursor})
    assert wrong.is_error and "cursor" in wrong.output.lower()

    source.write_text("changed", encoding="utf-8")
    stale = await registry.execute("file_read", {"path": str(source), "cursor": cursor})
    assert stale.is_error and "changed" in stale.output.lower()


@pytest.mark.asyncio
async def test_cursor_detects_changed_bytes_even_if_metadata_appears_identical(tmp_path: Path, monkeypatch):
    import tools.file_read_pagination as pagination

    source = tmp_path / "same-metadata.txt"
    source.write_text("a" * 30_000, encoding="utf-8")
    # Simulate a filesystem reporting the same file ID, size and timestamps.
    monkeypatch.setattr(pagination, "_file_version", lambda _stat: [1, 2, 30_000, 4, 5])
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    first = await registry.execute("file_read", {"path": str(source)})
    _, cursor = _parts(first.output)
    assert cursor

    source.write_text("b" * 30_000, encoding="utf-8")
    stale = await registry.execute("file_read", {"path": str(source), "cursor": cursor})
    assert stale.is_error and "changed" in stale.output.lower()


@pytest.mark.asyncio
async def test_file_pages_never_mix_bytes_from_two_same_metadata_versions(tmp_path: Path, monkeypatch):
    import tools.file_read_pagination as pagination

    source = tmp_path / "racing-file.txt"
    first_version = "a" * 30_000
    second_version = "b" * 30_000
    source.write_text(first_version, encoding="utf-8")
    monkeypatch.setattr(pagination, "_file_version", lambda _stat: [1, 2, 30_000, 4, 5])
    original_open = Path.open
    replaced = False

    def change_before_binary_open(path, mode="r", *args, **kwargs):
        nonlocal replaced
        if path == source and mode == "rb" and not replaced:
            replaced = True
            with original_open(source, "w", encoding="utf-8") as target:
                target.write(second_version)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", change_before_binary_open)
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    first = await registry.execute("file_read", {"path": str(source)})
    assert replaced
    if first.is_error:
        assert "changed" in first.output.lower()
        return
    first_page, cursor = _parts(first.output)
    assert cursor
    second = await registry.execute("file_read", {"path": str(source), "cursor": cursor})
    if second.is_error:
        assert "changed" in second.output.lower()
        return
    second_page, cursor = _parts(second.output)
    assert cursor is None
    assert first_page + second_page in {first_version, second_version}


@pytest.mark.asyncio
async def test_file_pages_preserve_universal_newlines_and_invalid_utf8_at_chunk_boundaries(tmp_path: Path):
    source = tmp_path / "mixed-newlines.txt"
    raw = (b"a" * 65_535 + b"\r\n" + b"\xe7\x95\x8c" * 7000
           + b"\r" + b"\xff" + b"\nend \n")
    source.write_bytes(raw)
    expected = source.read_text(encoding="utf-8", errors="replace")
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    result_parts = []
    cursor = None
    for _ in range(15):
        args = {"path": str(source)}
        if cursor:
            args["cursor"] = cursor
        result = await registry.execute("file_read", args)
        assert not result.is_error, result.output
        part, cursor = _parts(result.output)
        result_parts.append(part)
        if not cursor:
            break
    else:
        pytest.fail("file continuation did not end")
    assert "".join(result_parts) == expected

    second_line = await registry.execute("file_read", {
        "path": str(source), "offset": 2, "limit": 1,
    })
    assert not second_line.is_error, second_line.output
    line, _ = _parts(second_line.output)
    assert line == "界" * 7000 + "\n"


@pytest.mark.asyncio
async def test_file_change_during_hash_never_yields_mixed_followup_pages(tmp_path: Path, monkeypatch):
    import tools.file_read_pagination as pagination

    source = tmp_path / "mutating.txt"
    source.write_text("a" * 150_000, encoding="utf-8")
    monkeypatch.setattr(pagination, "_file_version", lambda _stat: [1, 2, 150_000, 4, 5])
    changed = False

    async def mutate_once():
        nonlocal changed
        if not changed:
            changed = True
            source.write_text("b" * 150_000, encoding="utf-8")
        await asyncio.sleep(0)

    monkeypatch.setattr(pagination, "_yield_hash_progress", mutate_once)
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    first = await registry.execute("file_read", {"path": str(source)})
    assert changed
    if first.is_error:
        assert "changed" in first.output.lower()
        return
    _, cursor = _parts(first.output)
    assert cursor
    second = await registry.execute("file_read", {"path": str(source), "cursor": cursor})
    assert second.is_error and "changed" in second.output.lower()


@pytest.mark.asyncio
async def test_old_file_cursor_requires_restart_instead_of_claiming_consistent_pages(tmp_path: Path):
    import base64

    source = tmp_path / "legacy-cursor.txt"
    source.write_text("a" * 30_000, encoding="utf-8")
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    first = await registry.execute("file_read", {"path": str(source)})
    _, cursor = _parts(first.output)
    decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    decoded["v"] = 1
    old_cursor = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    resumed = await registry.execute("file_read", {"path": str(source), "cursor": old_cursor})
    assert resumed.is_error and "start a new read" in resumed.output
    fresh = await registry.execute("file_read", {"path": str(source)})
    assert not fresh.is_error


@pytest.mark.asyncio
async def test_large_file_page_does_not_materialize_the_whole_file(tmp_path: Path):
    import tracemalloc

    source = tmp_path / "large-file.txt"
    chunk = b"x" * (1024 * 1024)
    with source.open("wb") as stream:
        for _ in range(32):
            stream.write(chunk)
    del chunk
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    tracemalloc.start()
    try:
        first = await registry.execute("file_read", {"path": str(source)})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert not first.is_error, first.output
    page, cursor = _parts(first.output)
    assert page == "x" * 15_000 and cursor
    assert peak < 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_file_read_cancellation_closes_hash_and_source_handles(tmp_path: Path, monkeypatch):
    import tools.file_read_pagination as pagination

    source = tmp_path / "cancel.txt"
    source.write_text("x" * 100_000, encoding="utf-8")
    entered = asyncio.Event()
    block = asyncio.Event()

    async def wait_during_hash():
        entered.set()
        await block.wait()

    monkeypatch.setattr(pagination, "_yield_hash_progress", wait_during_hash)
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    task = asyncio.create_task(registry.execute("file_read", {"path": str(source)}))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # On Windows a remaining open file handle would make this cleanup fail.
    source.unlink()
    assert not source.exists()


@pytest.mark.asyncio
async def test_cursor_does_not_override_path_permissions_or_accept_invalid_tokens(tmp_path: Path):
    scope = tmp_path / "scope"
    scope.mkdir()
    source = scope / "data.txt"
    source.write_text("a" * 30_000, encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    registry = ToolRegistry(["file_read"], access_root=scope, workspace_dir=scope, secrets={})

    first = await registry.execute("file_read", {"path": str(source)})
    _, cursor = _parts(first.output)
    denied = await registry.execute("file_read", {"path": str(outside), "cursor": cursor})
    assert denied.is_error
    assert "private" not in denied.output
    invalid = await registry.execute("file_read", {"path": str(source), "cursor": "not_a_cursor"})
    assert invalid.is_error and "cursor" in invalid.output.lower()


@pytest.mark.asyncio
async def test_mcp_gateway_exposes_and_follows_continuation(tmp_path: Path):
    source = tmp_path / "long.txt"
    original = "繁🦋" * 18_000
    source.write_text(original, encoding="utf-8")
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    context_path = tmp_path / "context.json"
    write_gateway_context(registry, context_path)
    gateway = ToolGateway(load_gateway_context(context_path))
    definition = next(d for d in gateway.tool_definitions() if d["name"] == "hashi_file_read")
    assert "cursor" in definition["inputSchema"]["properties"]

    cursor = None
    parts = []
    for page in range(10):
        arguments = {"path": str(source)}
        if cursor is not None:
            arguments["cursor"] = cursor
        response = await gateway.call("hashi_file_read", arguments, f"read-{page}")
        assert not response["isError"], response
        text, cursor = _parts(response["content"][0]["text"])
        parts.append(text)
        if cursor is None:
            break
    else:
        pytest.fail("MCP continuation did not end")
    assert "".join(parts) == original


@pytest.mark.asyncio
async def test_line_pages_follow_cursor_without_losing_lines(tmp_path: Path):
    source = tmp_path / "lines.txt"
    original = "".join(f"{i}\n" for i in range(520))
    source.write_text(original, encoding="utf-8")
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    first = await registry.execute("file_read", {"path": str(source), "limit": 500})
    text1, cursor = _parts(first.output)
    assert cursor
    assert first.details["complete_content"] is False
    assert first.details["file_read_continuation"] == cursor
    assert first.details["effect_receipt"]["complete_content"] is False
    second = await registry.execute("file_read", {"path": str(source), "cursor": cursor})
    text2, cursor = _parts(second.output)
    assert cursor is None
    assert text1 + text2 == original
# HASHI-Fingerprint: hfp_95813ff447ef45419165719d5a1916a5
# HASHI-Fingerprint: hfp_22f25673b45d4b8d87bfb3c9fca7aa0e


@pytest.mark.asyncio
async def test_retained_result_can_be_read_in_smaller_explicit_pages(tmp_path):
    from tools.tool_output_store import run_binding
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
                            audit_context={"hashi_run_id": "run-one", "request_id": "request-one",
                                           "request_tool_allowlist": ["file_read"]})
    binding = run_binding(registry._effective_audit_context())
    reference = registry._tool_output_store.retain(binding, "file_read", "abcdef")
    first = await registry.execute("tool_output_read", {"reference": reference, "offset": 0, "limit": 3})
    assert not first.is_error
    data = json.loads(first.output)
    assert data["text"] == "abc" and data["next_offset"] == 3 and not data["complete"]
    second = await registry.execute("tool_output_read", {"reference": reference, "offset": 3, "limit": 3})
    data = json.loads(second.output)
    assert data["text"] == "def" and data["complete"] and data["next_offset"] is None
    for limit in [0, -1, True, "3"]:
        result = await registry.execute("tool_output_read", {"reference": reference, "limit": limit})
        assert result.is_error and "invalid result page limit" in result.output



@pytest.mark.asyncio
async def test_file_write_respects_selected_quota_without_truncating_or_overwriting(tmp_path: Path):
    content = ("large-data-" * 105_000) + "-complete-tail"
    target = tmp_path / "large.txt"
    target.write_text("original", encoding="utf-8")
    registry = ToolRegistry(["file_write"], tmp_path, tmp_path, {},
        tool_options={"file_write": {"max_file_size_kb": 2048}})
    written = await registry.execute("file_write", {"path": str(target), "content": content})
    assert not written.is_error
    assert target.read_text(encoding="utf-8") == content

    restricted = ToolRegistry(["file_write"], tmp_path, tmp_path, {},
        tool_options={"file_write": {"max_file_size_kb": 8}})
    rejected = await restricted.execute("file_write", {"path": str(target), "content": content})
    assert rejected.is_error
    assert target.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_http_response_tail_and_explicit_timeout_survive_real_loopback(tmp_path: Path, monkeypatch):
    from aiohttp import web
    import httpx

    body = ("long response " * 3000) + "complete-tail"
    async def respond(_request):
        return web.Response(text=body)
    app = web.Application()
    app.router.add_get("/data", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    seen = []
    base_client = httpx.AsyncClient

    class RecordingClient(base_client):
        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("timeout"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)
    registry = ToolRegistry(["http_request"], tmp_path, tmp_path, {},
        audit_context={"instance_id": "NP", "agent_name": "fixture",
                       "hashi_run_id": "run-1", "request_id": "request-1"})
    try:
        first = await registry.execute("http_request", {
            "url": f"http://127.0.0.1:{port}/data", "timeout": 120,
        })
        assert not first.is_error, first.output
        assert seen == [120]
        assert first.details["response_complete"] is True
        reference = first.details["output_continuation"]
        pages, offset = [], 0
        while True:
            page = await registry.execute("tool_output_read", {"reference": reference, "offset": offset})
            assert not page.is_error, page.output
            item = json.loads(page.output)
            pages.append(item["text"])
            if item["complete"]:
                break
            offset = item["next_offset"]
        complete = "".join(pages)
        assert complete.startswith("Status: 200")
        assert complete.endswith(body)
        assert "complete-tail" in complete
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("smart_enabled", [False, True])
async def test_http_selected_response_budget_reports_partial_post_without_retry(tmp_path: Path, smart_enabled):
    from aiohttp import web

    received = []
    async def respond(request):
        received.append(await request.text())
        return web.Response(text="x" * 2000)
    app = web.Application()
    app.router.add_post("/effect", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    registry = ToolRegistry(["http_request"], tmp_path, tmp_path, {},
        tool_options={"http_request": {"max_response_bytes": 256},
                      "smart_registry": {"enabled": smart_enabled}})
    try:
        result = await registry.execute("http_request", {
            "url": f"http://127.0.0.1:{port}/effect", "method": "POST", "body": "once",
        })
        assert not result.is_error
        assert received == ["once"]
        assert result.details["http_status"] == 200
        assert result.details["response_complete"] is False
        assert result.details["complete_content"] is False
        assert result.details["response_limit_bytes"] == 256
        if smart_enabled:
            assert json.loads(result.output)["status"] == "partial"
        assert "Response incomplete" in result.output
        assert "may already have taken effect" in result.output
    finally:
        await runner.cleanup()



@pytest.mark.asyncio
async def test_http_response_budget_stops_a_still_open_stream_at_the_selected_limit(tmp_path: Path):
    from aiohttp import web

    release = asyncio.Event()
    received = []
    async def respond(request):
        received.append(await request.text())
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"x" * 257)
        await release.wait()
        return response
    app = web.Application()
    app.router.add_post("/stream", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    registry = ToolRegistry(["http_request"], tmp_path, tmp_path, {},
        tool_options={"http_request": {"max_response_bytes": 256}})
    try:
        result = await asyncio.wait_for(registry.execute("http_request", {
            "url": f"http://127.0.0.1:{port}/stream", "method": "POST", "body": "once",
        }), 3)
        assert received == ["once"]
        assert result.details["response_complete"] is False
        assert result.details["response_bytes"] == 256
        assert result.details["response_observed_at_least_bytes"] >= 257
    finally:
        release.set()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_cancellation_stops_waiting_without_replaying_post(tmp_path: Path):
    from aiohttp import web

    started, release = asyncio.Event(), asyncio.Event()
    received = []
    async def respond(request):
        received.append(await request.text())
        response = web.StreamResponse()
        await response.prepare(request)
        started.set()
        await release.wait()
        return response
    app = web.Application()
    app.router.add_post("/pending", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    registry = ToolRegistry(["http_request"], tmp_path, tmp_path, {})
    task = asyncio.create_task(registry.execute("http_request", {
        "url": f"http://127.0.0.1:{port}/pending", "method": "POST", "body": "once",
    }))
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert received == ["once"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, True, "nan"])
async def test_http_invalid_timeout_is_reported_before_dispatch(timeout):
    from tools.builtins import execute_http_request

    result = await execute_http_request({"url": "http://127.0.0.1:1", "timeout": timeout})
    assert isinstance(result, str) and result.startswith("Error: HTTP timeout")
