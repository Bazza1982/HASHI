from __future__ import annotations

from pathlib import Path
import json

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

    assert "items shown" in output.output
    coverage = json.loads(output.output.splitlines()[-1])
    assert coverage["can_continue"] and coverage["next_cursor"]
    assert not coverage["coverage_complete"]
    assert 1 <= coverage["counters"]["files_enumerated"] <= 50
    assert len(output.output) < 20_000


@pytest.mark.asyncio
async def test_file_read_reconstructs_one_very_long_line(tmp_path: Path):
    source = tmp_path / "long.txt"
    original = ("0123456789" * 10_000)  # 100,000 characters, no newline
    source.write_text(original, encoding="utf-8")

    pieces: list[str] = []
    args: dict = {"path": str(source)}
    for _ in range(20):  # hard stop so a broken cursor fails instead of looping
        output = await execute_file_read(args, tmp_path, tmp_path)
        # Must survive the registry's 20,000-character model-output budget.
        assert len(output) < 20_000
        header, body = output.split("\n", 1)
        if "truncated by HASHI" in output:
            body = body.rsplit("\n[truncated", 1)[0]
            marker = "next_char_offset="
            next_offset = int(header.split(marker, 1)[1].split(")", 1)[0])
            pieces.append(body)
            args = {"path": str(source), "char_offset": next_offset}
        else:
            assert "end of file" in header
            pieces.append(body)
            break
    else:
        pytest.fail("file_read never reached the end of the long line")

    assert "".join(pieces) == original
    assert len(pieces) >= 5


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
# HASHI-Fingerprint: hfp_88d0258bd790422599fedd314562d430
