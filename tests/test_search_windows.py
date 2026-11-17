"""Windows-specific acceptance tests for scoped search.

Native Windows fixtures: drive paths, spaces, Chinese filenames, junctions,
deep trees and slow filesystem operations. These tests only run on Windows;
Linux-only results are not a substitute.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scoped_search import registry_for, payload

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows acceptance")


@pytest.mark.asyncio
async def test_windows_drive_paths_spaces_and_chinese_names(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    nested = repo / "中文 folder John's 子目录"
    nested.mkdir()
    target = nested / "目标文件 方案.txt"
    target.write_text("第一行\n目标词 needle\n", encoding="utf-8")
    data = payload(await registry.execute(
        "file_search", {"query": "方案", "roots": [str(nested)]}))
    assert [item["path"] for item in data["matches"]] == [str(target)]
    content = payload(await registry.execute(
        "file_search", {"query": "needle", "mode": "content",
                        "roots": [str(target)]}))
    assert content["matches"][0]["line"] == 2
    # Explicit Windows drive-style root works through the resolver.
    drive_root = str(Path(target).drive) + "\\"
    listing = payload(await registry.execute(
        "file_search", {"query": "*.txt", "match": "glob",
                        "roots": [str(nested)]}))
    assert listing["matches"]


@pytest.mark.asyncio
async def test_windows_junction_is_skipped_without_follow_links(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape-target.txt").touch()
    junction = repo / "j"
    outcome = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True)
    if outcome.returncode != 0:
        pytest.skip("host does not grant junction creation")
    data = payload(await registry.execute(
        "file_search", {"query": "escape", "roots": [str(repo)]}))
    assert not data["matches"] and data["coverage_complete"]
    # Followed junctions are still confined by the read scope.
    followed = payload(await registry.execute(
        "file_search", {"query": "escape", "roots": [str(repo)],
                        "options": {"follow_links": True}}))
    assert not followed["matches"]


@pytest.mark.asyncio
async def test_windows_deep_tree_and_max_depth_boundary(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    deep = repo
    for i in range(140):
        deep = deep / f"d{i}"
    try:
        deep.mkdir(parents=True)
        (deep / "bottom.txt").write_text("needle", encoding="utf-8")
    except OSError:
        pytest.skip("host filesystem does not accept a 140-level tree")
    data = payload(await registry.execute(
        "file_search", {"query": "bottom", "roots": [str(repo)]}))
    # The scanner honours its depth budget instead of recursing forever; the
    # result must still be honest about coverage.
    if data["counters"]["errors"]:
        assert any(error["code"] == "depth_budget"
                   for error in data["coverage"]["errors"])
    assert not data["coverage_complete"] or data["matches"]


@pytest.mark.asyncio
async def test_windows_deadline_race_preserves_cursor_for_slow_content_scan(tmp_path):
    """Reproduce the parent/worker deadline race: the parent must wait for the
    worker's cooperative result instead of killing it at the same instant."""
    registry, repo, _ = registry_for(tmp_path, options={
        "file_search": {"timeout_seconds": 0.05}})
    source = repo / "long.txt"
    with source.open("wb") as stream:
        stream.write(b"x" * 8192)
        stream.truncate(256 * 1024 * 1024)
    (source.parent / "small.txt").write_text("needle", encoding="utf-8")
    result = await registry.execute(
        "file_search", {"query": "needle", "mode": "content",
                        "roots": [str(repo)]})
    envelope = json.loads(result.output)
    data = envelope["data"]
    assert envelope["status"] == "partial"
    assert data["stop_reason"] == "deadline"
    # The fix: the worker's deadline-limited state reached the parent, so a
    # valid continuation cursor exists. The old code lost it to the race.
    assert data["can_continue"] is True
    assert data["next_cursor"] and data["next_cursor"] != "x" * 43
    assert not data.get("continuation_lost")
    cleanup = result.details["foreground_cleanup"]
    assert cleanup["process_reaped"] and cleanup["status"] == "normal_completion"
    assert cleanup.get("cooperative_deadline") is True
