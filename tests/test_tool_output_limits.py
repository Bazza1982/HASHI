from __future__ import annotations

from pathlib import Path

import pytest

from tools.builtins import execute_file_list, execute_file_read
from tools.registry import StructuredToolOutput, ToolRegistry


@pytest.mark.asyncio
async def test_recursive_file_list_stops_after_bounded_entries(tmp_path: Path):
    for index in range(1005):
        (tmp_path / f"entry-{index:04d}.txt").touch()

    output = await execute_file_list(
        {"path": str(tmp_path), "recursive": True}, tmp_path, tmp_path
    )

    assert "1000 items shown" in output
    assert "truncated" in output
    assert len(output) < 100_000


@pytest.mark.asyncio
async def test_file_read_bounds_one_very_long_line(tmp_path: Path):
    source = tmp_path / "long.txt"
    source.write_text("x" * 100_000, encoding="utf-8")

    output = await execute_file_read({"path": str(source)}, tmp_path, tmp_path)

    assert "[truncated" in output
    assert len(output) < 30_000


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
