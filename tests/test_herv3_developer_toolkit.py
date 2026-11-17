"""Non-production acceptance tests for the HERV3 daily/developer toolkit."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.registry import ToolRegistry
from tools.tool_presentation import OnDemandToolView, on_demand_requested
from tools.developer_toolkit_schemas import TOOLKIT_TOOL_NAMES, TOOLKIT_READ_ONLY_TOOLS


def _registry(root: Path, *, allowed=None, tool_options=None) -> ToolRegistry:
    return ToolRegistry(
        allowed_tools=list(allowed or TOOLKIT_TOOL_NAMES),
        access_root=root,
        workspace_dir=root,
        secrets={},
        tool_options=tool_options or {},
    )


async def _run(registry, name: str, args: dict) -> dict:
    result = await registry.execute(name, args, tool_call_id=f"test-{name}")
    return json.loads(result.output)


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def test_toolkit_schemas_and_permissions(tmp_path):
    registry = _registry(tmp_path)
    assert set(TOOLKIT_TOOL_NAMES).issubset(set(registry.allowed_tool_names()))
    assert {
        schema["function"]["name"]
        for schema in registry.get_tool_definitions(tiers=["development", "daily"])
    } >= set(TOOLKIT_TOOL_NAMES)
    for name in TOOLKIT_TOOL_NAMES:
        assert registry.is_read_only(name) is (name in TOOLKIT_READ_ONLY_TOOLS)
    assert not on_demand_requested({}, model="qwen3.5-9b", is_herv3=True)
    assert on_demand_requested(
        {"herv3_tool_presentation": {"mode": "on_demand"}},
        model="deepseek-v4-pro", is_herv3=True
    )
    assert not on_demand_requested(
        {"herv3_tool_presentation": {"mode": "on_demand"}},
        model="qwen3.5-9b", is_herv3=False
    )


@pytest.mark.asyncio
async def test_model_finds_and_reveals_tools_without_changing_authority(tmp_path):
    registry = _registry(
        tmp_path, allowed=["tool_find", "shell", "file_read", "workspace_inspect", "git_inspect"]
    )
    view = OnDemandToolView(registry)
    before = {item["function"]["name"] for item in view.get_tool_definitions(tiers=["core"])}
    assert "tool_find" in before
    assert "workspace_inspect" not in before
    outcome = await view.execute("tool_find", {"pack": "verification"}, "find")
    assert not outcome.is_error
    announced = json.loads(outcome.output)
    assert "workspace_inspect" in {item["name"] for item in announced["tools"]}
    after = {item["function"]["name"] for item in view.get_tool_definitions(tiers=["core"])}
    assert "workspace_inspect" in after
    assert view.is_allowed("workspace_inspect")
    assert not view.is_allowed("file_manage")
    assert "file_manage" not in after
    # Presentation never mutates the shared/long-lived Registry's own catalogue.
    assert "workspace_inspect" in {
        item["function"]["name"] for item in registry.get_tool_definitions(tiers=None)
    }


@pytest.mark.asyncio
async def test_project_locate_uses_repo_name_and_bounded_root(tmp_path):
    root = tmp_path / "Projects"
    target = root / "content_generation"
    target.mkdir(parents=True)
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    _git(target, "init", "--quiet")
    _git(target, "remote", "add", "origin", "https://example.com/Bazza/SOK.git")
    other = root / "unrelated"
    other.mkdir()
    found = await _run(
        _registry(root), "project_locate", {"name": "sok", "root": str(root), "max_depth": 2}
    )
    assert found["ok"] and found["matches"]
    assert found["matches"][0]["path"] == str(target.resolve())
    assert found["matches"][0]["remote_repo"] == "sok"


@pytest.mark.asyncio
async def test_code_search_is_bounded_and_reports_partial(tmp_path):
    (tmp_path / "a.py").write_text("class Alpha:\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("class AlphaTwo:\n    pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    found = await _run(registry, "code_search", {
        "query": "class Alpha", "mode": "content", "max_results": 1
    })
    assert found["ok"] and found["count"] == 1 and found["partial"]
    assert found["stop_reason"] == "result_limit"
    names = await _run(registry, "code_search", {
        "query": "a.py", "mode": "files", "max_results": 5
    })
    assert names["ok"] and any("a.py" in line for line in names["matches"])


@pytest.mark.asyncio
async def test_git_inspect_read_only_and_remote_credentials_redacted(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "remote", "add", "origin", "https://user:topsecret@example.com/Bazza/SOK.git")
    (tmp_path / "untracked.txt").write_text("x", encoding="utf-8")
    registry = _registry(tmp_path)
    status = await _run(registry, "git_inspect", {"operation": "status"})
    assert status["ok"] and "untracked.txt" in status["output"]
    remotes = await _run(registry, "git_inspect", {"operation": "remote"})
    assert remotes["ok"]
    assert "example.com/Bazza/SOK.git" in remotes["output"]
    assert "topsecret" not in remotes["output"]


@pytest.mark.asyncio
async def test_text_replace_checks_count_and_does_not_overwrite_on_mismatch(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("old\nold\n", encoding="utf-8")
    registry = _registry(tmp_path)
    mismatch = await _run(registry, "text_replace", {
        "path": "app.py", "find": "old", "replace": "new"
    })
    assert mismatch["ok"] is False
    assert path.read_text(encoding="utf-8") == "old\nold\n"
    preview = await _run(registry, "text_replace", {
        "path": "app.py", "find": "old", "replace": "new",
        "expected_count": 2, "preview_only": True
    })
    assert preview["ok"] and not preview["changed"]
    changed = await _run(registry, "text_replace", {
        "path": "app.py", "find": "old", "replace": "new", "expected_count": 2
    })
    assert changed["ok"] and changed["changed"]
    assert path.read_text(encoding="utf-8") == "new\nnew\n"


@pytest.mark.asyncio
async def test_file_manage_copy_move_mkdir_preserve_existing(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("proof", encoding="utf-8")
    registry = _registry(tmp_path)
    created = await _run(registry, "file_manage", {
        "operation": "mkdir", "destination": "reports"
    })
    assert created["ok"] and (tmp_path / "reports").is_dir()
    copied = await _run(registry, "file_manage", {
        "operation": "copy", "source": "source.txt", "destination": "reports/copy.txt"
    })
    assert copied["ok"] and (tmp_path / "reports/copy.txt").read_text() == "proof"
    collision = await _run(registry, "file_manage", {
        "operation": "copy", "source": "source.txt", "destination": "reports/copy.txt"
    })
    assert collision["ok"] is False and source.read_text() == "proof"
    moved = await _run(registry, "file_manage", {
        "operation": "move", "source": "reports/copy.txt", "destination": "reports/moved.txt"
    })
    assert moved["ok"] and (tmp_path / "reports/moved.txt").is_file()
    assert not (tmp_path / "reports/copy.txt").exists()


@pytest.mark.asyncio
async def test_data_inspect_json_csv_without_model_written_python(tmp_path):
    (tmp_path / "rows.csv").write_text("name,value\nA,1\nB,2\n", encoding="utf-8")
    (tmp_path / "info.json").write_text('{"name":"SOK","ready":true}', encoding="utf-8")
    registry = _registry(tmp_path)
    csv_data = await _run(registry, "data_inspect", {"path": "rows.csv", "limit": 1})
    assert csv_data["ok"] and csv_data["columns"] == ["name", "value"]
    assert csv_data["samples"] == [{"name": "A", "value": "1"}]
    json_data = await _run(registry, "data_inspect", {"path": "info.json"})
    assert json_data["ok"] and set(json_data["keys"]) == {"name", "ready"}
    (tmp_path / "wide.csv").write_text(
        "name,notes\nA," + "x" * 25000 + "\n", encoding="utf-8"
    )
    large = await registry.execute("data_inspect", {"path": "wide.csv"}, "wide")
    assert len(large.output) < 20000
    assert json.loads(large.output)["ok"]


@pytest.mark.asyncio
async def test_no_cross_workzone_access_or_tool_permission_growth(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_text("private", encoding="utf-8")
    registry = _registry(allowed, allowed=["tool_find", "project_locate", "file_manage"])
    denied = await registry.execute("file_manage", {
        "operation": "copy", "source": str(external / "secret.txt"),
        "destination": "copied.txt"
    }, "outside")
    assert denied.is_error
    assert not (allowed / "copied.txt").exists()
    discovery = await registry.execute(
        "tool_find", {"names": ["file_manage", "web_search", "office_edit"]}, "find"
    )
    names = [item["name"] for item in json.loads(discovery.output)["tools"]]
    assert names == ["file_manage"]
    forbidden = await registry.execute("text_replace", {
        "path": "missing.txt", "find": "x", "replace": "y"
    }, "forbidden")
    assert forbidden.is_error


@pytest.mark.asyncio
async def test_git_inspection_cannot_walk_to_repo_outside_accessible_root(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    repo = tmp_path / "parent_repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    nested = repo / "nested"
    nested.mkdir()
    registry = _registry(nested, allowed=["git_inspect"])
    result = await registry.execute("git_inspect", {"operation": "status"}, "scope")
    assert result.is_error


def test_wildcard_does_not_silently_adopt_toolkit_tools(tmp_path):
    """A '*' allowlist keeps its prior behaviour: the new toolkit schemas do not
    broaden a wildcard instance unless they are listed explicitly (same
    precedent as media_read and vision_inspect)."""
    wildcard = _registry(tmp_path, allowed=["*"])
    for name in TOOLKIT_TOOL_NAMES:
        assert not wildcard.is_allowed(name), name
    assert not wildcard.is_allowed("media_read")
    assert not wildcard.is_allowed("vision_inspect")
    explicit = _registry(tmp_path, allowed=["*", "tool_find", "file_manage"])
    assert explicit.is_allowed("tool_find")
    assert explicit.is_allowed("file_manage")
    assert not explicit.is_allowed("text_replace")