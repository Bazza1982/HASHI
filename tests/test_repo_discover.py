"""Read-only repository discovery tests, including the SOK case study.

A repository rooted at ``content_generation`` with GitHub remote
``Bazza1982/SOK`` must be identifiable without recursively scanning its
contents. All fixtures are synthetic Git metadata under a temp directory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_scoped_search import registry_for, payload


def make_repo(root: Path, name: str, remote_name: str = "origin",
              remote_url: str = "https://github.com/example/project.git",
              *, branch: str = "main", worktree: bool = False):
    repo = root / name
    repo.mkdir()
    if worktree:
        git_dir = root / f".git-worktrees-{name}"
        git_dir.mkdir(parents=True)
        common = git_dir / "common"
        common.mkdir()
        (common / "config").write_text(
            f'[remote "{remote_name}"]\n\turl = {remote_url}\n'
            '[core]\n\tbare = false\n', encoding="utf-8")
        (common / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
        worktree_dir = git_dir / "worktrees" / name
        worktree_dir.mkdir(parents=True)
        # Real Git writes the common-dir path relative to the worktree git dir.
        (worktree_dir / "commondir").write_text("../../common", encoding="utf-8")
        (worktree_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
        (repo / ".git").write_text(f"gitdir: {worktree_dir}\n", encoding="utf-8")
    else:
        git_dir = repo / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "{remote_name}"]\n\turl = {remote_url}\n', encoding="utf-8")
        (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    return repo


def sok_case(tmp_path: Path, workzone: Path):
    """The reported incident: folder name differs from the GitHub remote."""
    sok = make_repo(
        workzone, "content_generation",
        remote_url="https://github.com/Bazza1982/SOK.git", branch="main")
    # Make the contents large enough that a recursive scan would be the wrong
    # tool; discovery must not enumerate any of it.
    deep = sok / "output" / "generated" / "images"
    deep.mkdir(parents=True)
    for i in range(40):
        (deep / f"asset-{i:02d}.png").write_bytes(b"fake")
    (sok / "README.md").write_text("Studio on Kent", encoding="utf-8")
    compute = make_repo(
        workzone, "HashiCompute",
        remote_url="https://github.com/Bazza1982/HashiCompute.git")
    (compute / "main.py").write_text("compute", encoding="utf-8")
    unrelated = make_repo(
        workzone, "notes",
        remote_url="https://github.com/Bazza1982/notes.git")
    (unrelated / "note.txt").write_text("noise", encoding="utf-8")
    (workzone / "plain-folder").mkdir()
    (workzone / "plain-folder" / "data.txt").write_text("no git", encoding="utf-8")


@pytest.mark.asyncio
async def test_sok_remote_identifies_mismatched_folder_without_recursive_scan(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    workzone = repo
    sok_case(tmp_path, workzone)
    result = await registry.execute(
        "repo_discover", {"query": "SOK", "roots": [str(workzone)]})
    assert not result.is_error, result.output
    data = payload(result)
    assert [item["name"] for item in data["matches"]] == ["content_generation"]
    match = data["matches"][0]
    assert match["path"] == str(workzone / "content_generation")
    assert match["remotes"][0]["url"] == "https://github.com/Bazza1982/SOK.git"
    assert match["remotes"][0]["name"] == "origin"
    assert match["branch"] == "main" and match["worktree"] is False
    # Discovery inspects Git metadata only; it never enumerated repository
    # contents (no files at all).
    assert data["counters"]["files_enumerated"] == 0
    assert data["counters"]["directories_enumerated"] == 4  # three repos + plain folder
    assert data["coverage_complete"] and data["scope_exhausted"]
    assert data["can_continue"] is False


@pytest.mark.asyncio
async def test_repo_discover_by_folder_name_and_remote_url(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    workzone = repo
    sok_case(tmp_path, workzone)
    by_folder = payload(await registry.execute(
        "repo_discover", {"query": "content_generation", "roots": [str(workzone)]}))
    assert [item["name"] for item in by_folder["matches"]] == ["content_generation"]
    by_url = payload(await registry.execute(
        "repo_discover", {"query": "Bazza1982/HashiCompute", "roots": [str(workzone)]}))
    assert [item["name"] for item in by_url["matches"]] == ["HashiCompute"]
    by_remote_name = payload(await registry.execute(
        "repo_discover", {"query": "origin", "roots": [str(workzone)]}))
    assert {item["name"] for item in by_remote_name["matches"]} == {
        "content_generation", "HashiCompute", "notes"}


@pytest.mark.asyncio
async def test_repo_discover_handles_worktrees_and_root_itself(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    workzone = repo
    make_repo(workzone, "main-app",
              remote_url="https://github.com/example/main-app.git", branch="main")
    make_repo(workzone, "feature-x",
              remote_url="https://github.com/example/main-app.git", branch="feature-x",
              worktree=True)
    as_root = payload(await registry.execute(
        "repo_discover", {"query": "example/main-app",
                          "roots": [str(workzone / "main-app")]}))
    assert as_root["matches"][0]["name"] == "main-app"
    assert as_root["matches"][0]["worktree"] is False
    worktrees = payload(await registry.execute(
        "repo_discover", {"query": "feature", "roots": [str(workzone)]}))
    assert [item["name"] for item in worktrees["matches"]] == ["feature-x"]
    assert worktrees["matches"][0]["worktree"] is True
    assert worktrees["matches"][0]["branch"] == "feature-x"
    assert worktrees["matches"][0]["remotes"][0]["url"] == "https://github.com/example/main-app.git"


@pytest.mark.asyncio
async def test_repo_discover_respects_read_scope_and_defaults_to_workzones(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    make_repo(outside, "secret-repo",
              remote_url="https://github.com/example/secret-repo.git")
    make_repo(repo, "wanted-repo",
              remote_url="https://github.com/example/wanted-repo.git")
    # Explicit roots outside the read scope are denied, not silently traversed.
    denied = await registry.execute(
        "repo_discover", {"query": "secret", "roots": [str(outside)]})
    assert denied.is_error
    # Default roots come from the Run's preferred Workzones.
    defaulted = payload(await registry.execute("repo_discover", {"query": "wanted-repo"}))
    assert [item["name"] for item in defaulted["matches"]] == ["wanted-repo"]
    # A linked candidate pointing outside the scope is skipped without being
    # traversed and without an error.
    link = repo / "linked-outside"
    try:
        if hasattr(Path, "symlink_to") and not __import__("os").name == "nt":
            link.symlink_to(outside, target_is_directory=True)
        else:
            import subprocess
            outcome = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True)
            if outcome.returncode != 0:
                pytest.skip("host does not grant link creation")
    except OSError:
        pytest.skip("host does not grant link creation")
    data = payload(await registry.execute(
        "repo_discover", {"query": "secret-repo", "roots": [str(repo)]}))
    assert not data["matches"]
    assert data["counters"]["skipped_out_of_scope"] >= 1
    assert data["counters"]["errors"] == 0


@pytest.mark.asyncio
async def test_repo_discover_partial_result_on_deadline_and_no_false_completion(tmp_path, monkeypatch):
    import time
    import tools.repo_discover as repo_discover_module
    registry, repo, home = registry_for(tmp_path)
    workzone = repo
    # A deliberately slow fixture guarantees the scan crosses the tiny
    # per-call budget; discovery stops cooperatively and reports it honestly.
    real_inspect = repo_discover_module._inspect
    def slow_inspect(candidate):
        time.sleep(0.002)
        return real_inspect(candidate)
    monkeypatch.setattr(repo_discover_module, "_inspect", slow_inspect)
    for i in range(120):
        (workzone / f"plain-{i:04d}").mkdir()
    make_repo(workzone, "wanted",
              remote_url="https://github.com/example/wanted.git")
    result = await registry.execute(
        "repo_discover", {"query": "wanted", "roots": [str(workzone)],
                          "timeout_seconds": 0.02})
    envelope = json.loads(result.output)
    data = payload(result)
    assert envelope["status"] == "partial"
    assert data["stop_reason"] == "deadline"
    assert data["coverage_complete"] is False
    assert data["can_continue"] is False and data["next_cursor"] is None
    assert any("continuation" in warning for warning in data["warnings"])


@pytest.mark.asyncio
async def test_repo_discover_invalid_arguments_are_rejected(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    empty = await registry.execute("repo_discover", {"query": "  "})
    assert empty.is_error
    too_many = await registry.execute(
        "repo_discover", {"query": "x", "max_results": 500})
    assert too_many.is_error
