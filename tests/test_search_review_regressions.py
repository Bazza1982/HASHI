"""PR #170 scope regressions; run locally without hosted or full-suite CI."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_repo_discover import make_repo
from tests.test_scoped_search import payload, registry_for
from tools.search_worker import Scanner


def directory_link(link, target, kind="symlink"):
    try:
        if kind == "junction":
            if os.name != "nt":
                pytest.skip("junctions require Windows")
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, check=False)
            if result.returncode:
                pytest.skip("host does not permit junction creation")
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host does not permit directory links: {exc}")


def file_link(link, target):
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"host does not permit file symlinks: {exc}")


def protect(registry, *paths):
    # Exercise the actual policy loader and its byte-identity cache contract.
    config = registry.audit_context["global_config"]
    config.project_root = registry.workspace_dir / "fixture-source"
    config.bridge_home = registry.workspace_dir / "fixture-instance"
    config.project_root.mkdir(parents=True, exist_ok=True)
    policy = config.bridge_home / "state" / "platform" / "live-runtime-protection.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({"schema": 1, "protected_read_paths":
                                  [str(path.resolve()) for path in paths]}), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["symlink", "junction"])
@pytest.mark.parametrize("mode", ["path", "content", "regex_python", "regex_ripgrep"])
async def test_linked_descendants_protected_across_cursor_pages(tmp_path, kind, mode):
    if mode == "regex_ripgrep" and __import__("shutil").which("rg") is None:
        pytest.skip("ripgrep regex engine is unavailable")
    registry, repo, _ = registry_for(tmp_path)
    target = repo / "physical"
    nested = target / "nested"
    nested.mkdir(parents=True)
    for name in ("public-a.txt", "private.txt", "public-b.txt"):
        (nested / name).write_text("needle", encoding="utf-8")
    private = nested / "private.txt"
    directory_link(repo / "alias", target, kind)
    directory_link(nested / "back", repo, kind)
    protect(registry, private)
    options = {"follow_links": True}
    if mode.startswith("regex_"):
        options["regex_engine"] = mode.removeprefix("regex_")
    args = dict(query="*.txt" if mode == "path" else "needle",
                mode="content" if mode.startswith("regex_") else mode,
                match="glob" if mode == "path" else "regex" if mode.startswith("regex_") else "literal",
                roots=[str(repo)], options=options, profile="expanded",
                exclude=["physical"], max_results=1)
    matches, errors = [], []
    for _ in range(12):
        data = payload(await registry.execute("file_search", args))
        matches.extend(data["matches"])
        errors.extend(data["coverage"]["errors"])
        if not data["next_cursor"]:
            break
        args["cursor"] = data["next_cursor"]
    else:
        pytest.fail("linked traversal did not terminate")
    assert {Path(item["path"]).name for item in matches} == {"public-a.txt", "public-b.txt"}, data
    assert all("alias" in Path(item["path"]).parts for item in matches)
    assert len(matches) == 2
    assert any(item["code"] == "link_cycle" for item in errors)
    assert not any(item["code"] == "depth_budget" for item in errors)


@pytest.mark.asyncio
async def test_list_and_log_query_enforce_actual_protected_paths(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    private = repo / "private"
    private.mkdir()
    (private / "secret.txt").write_text("needle confidential", encoding="utf-8")
    (repo / "public.txt").write_text("needle public", encoding="utf-8")
    protect(registry, private)

    listing = await registry.execute("file_list", {
        "path": str(repo), "recursive": True, "max_results": 100,
    })
    assert not listing.is_error, listing.output
    entries = listing.output.splitlines()
    assert any("public.txt" in line for line in entries)
    assert not any(line.startswith("[dir] private") for line in entries)
    assert not any(line.strip().startswith("private" + os.sep) for line in entries)
    assert "permission_denied" in listing.output

    direct = await registry.execute("file_list", {"path": str(private)})
    assert direct.is_error
    assert direct.details["reason"] == "live_runtime_protection"
    relative = await registry.execute("file_list", {"path": "private"})
    assert relative.is_error and relative.details["reason"] == "live_runtime_protection"
    secret = await registry.execute("log_query", {
        "path": str(private / "secret.txt"), "terms": ["needle"],
    })
    assert secret.is_error
    public = await registry.execute("log_query", {
        "path": str(repo / "public.txt"), "terms": ["needle"],
    })
    assert not public.is_error and "needle public" in public.output


def scanner_config(root, **extra):
    return dict(operation="file_search", mode="content", query="needle",
                recursive=True, follow_links=True, cross_filesystems=False,
                profile="expanded", include_hidden=True, include=[],
                exclude=["physical"], match="literal", case_sensitive=False,
                max_results=1000, match_budget=1000000, context_chars=40,
                selected_roots=[{"path": str(root)}],
                authorized_roots=[str(root)], protected_read_paths=[], **extra)


def test_linked_protected_file_is_never_opened(tmp_path, monkeypatch):
    target = tmp_path / "physical"
    target.mkdir()
    private = target / "private.txt"
    private.write_text("needle", encoding="utf-8")
    (target / "public.txt").write_text("needle", encoding="utf-8")
    directory_link(tmp_path / "alias", target)
    real_open = Path.open
    opened = []

    def checked_open(path, *args, **kwargs):
        actual = path.resolve()
        opened.append(actual)
        assert actual != private, "protected target was opened through its alias"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    config = scanner_config(tmp_path)
    config["protected_read_paths"] = [str(private)]
    result = Scanner(config).run()
    assert private not in opened
    assert any(Path(item["path"]).name == "public.txt" for item in result["matches"])


def test_plain_files_do_not_resolve_per_entry(tmp_path, monkeypatch):
    for number in range(250):
        (tmp_path / f"ordinary-{number}.txt").write_text("needle", encoding="utf-8")
    real_resolve = Path.resolve
    resolved_files = []

    def count_resolve(path, *args, **kwargs):
        if path.name.startswith("ordinary-"):
            resolved_files.append(path)
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", count_resolve)
    config = scanner_config(tmp_path)
    config.update(mode="path", query=".txt")
    result = Scanner(config).run()
    assert len(result["matches"]) == 250
    assert resolved_files == []


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["outside", "protected"])
@pytest.mark.parametrize("location", [
    "gitdir", "common_dir", "commondir", "config", "config.worktree",
    "HEAD", "common_HEAD", "dotgit"])
async def test_worktree_metadata_reads_stay_in_existing_scope(
        tmp_path, monkeypatch, boundary, location):
    registry, repo, _ = registry_for(tmp_path)
    candidate = make_repo(repo, "checkout", worktree=True)
    gitdir = repo / ".git-worktrees-checkout" / "worktrees" / "checkout"
    common = repo / ".git-worktrees-checkout" / "common"
    denied = (tmp_path if boundary == "outside" else repo) / "denied-metadata"
    denied.mkdir()
    if location == "gitdir":
        (denied / "commondir").write_text(".", encoding="utf-8")
        (denied / "config").write_text('[remote "origin"]\nurl=https://example.test/private.git\n')
        (denied / "HEAD").write_text("ref: refs/heads/hidden\n")
        (candidate / ".git").write_text(f"gitdir: {denied}\n", encoding="utf-8")
    elif location == "common_dir":
        (gitdir / "commondir").write_text(str(denied), encoding="utf-8")
        (denied / "config").write_text('[remote "origin"]\nurl=https://example.test/private.git\n')
        (denied / "HEAD").write_text("ref: refs/heads/hidden\n")
    else:
        if location == "common_HEAD":
            (gitdir / "HEAD").unlink()
            source = common / "HEAD"
        else:
            source = candidate / ".git" if location == "dotgit" else (
                common / "config" if location == "config" else gitdir / location)
        destination = denied / location
        if source.exists():
            source.rename(destination)
        else:
            destination.write_text('[remote "origin"]\nurl=https://example.test/private.git\n')
        file_link(source, destination)
    if boundary == "protected":
        protect(registry, denied)
    reads = []
    real_read_text = Path.read_text

    def checked_read(path, *args, **kwargs):
        actual = path.resolve()
        if actual == denied or actual.is_relative_to(denied):
            reads.append(actual)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked_read)
    result = await registry.execute(
        "repo_discover", {"query": "checkout", "roots": [str(candidate)]})
    assert reads == [], f"unauthorised metadata read: {reads}"
    envelope = json.loads(result.output)
    assert envelope["status"] in {"partial", "failed"}
    assert not envelope["data"]["coverage_complete"]
    assert not envelope["data"]["matches"]
    assert envelope["data"]["coverage"]["errors"]


@pytest.mark.asyncio
async def test_worktree_head_uses_real_topic_checkout(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    primary = repo / "primary"
    topic = repo / "different-local-name"

    def git(*args):
        subprocess.run(["git", *map(str, args)], check=True, capture_output=True)

    git("init", "-b", "main", primary)
    git("-C", primary, "-c", "user.name=Scope Fixture", "-c",
        "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture")
    git("-C", primary, "remote", "add", "origin", "https://github.com/example/remote-project.git")
    git("-C", primary, "worktree", "add", "-b", "topic", topic)
    data = payload(await registry.execute(
        "repo_discover", {"query": "remote-project", "roots": [str(topic)]}))
    assert len(data["matches"]) == 1
    assert data["matches"][0]["branch"] == "topic"
    assert data["matches"][0]["name"] == "different-local-name"
    assert data["matches"][0]["worktree"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url,expected", [
    ("https://fixture-user:fixture-secret@github.com/example/project.git?token=fixture-token#fixture-fragment",
     "https://github.com/example/project.git"),
    ("ssh://fixture-user:fixture-secret@git.example.test:2222/example/project.git",
     "ssh://git.example.test:2222/example/project.git"),
])
async def test_remote_credentials_are_not_returned(tmp_path, url, expected):
    registry, repo, _ = registry_for(tmp_path)
    make_repo(repo, "checkout", remote_url=url)
    result = await registry.execute("repo_discover", {"query": "project", "roots": [str(repo)]})
    data = payload(result)
    assert data["matches"][0]["remotes"][0]["url"] == expected
    assert "fixture-secret" not in result.output and "fixture-token" not in result.output
    assert "fixture-user" not in result.output and "fixture-fragment" not in result.output


@pytest.mark.asyncio
async def test_protected_discovery_root_is_denied_before_metadata_inspection(tmp_path, monkeypatch):
    registry, repo, _ = registry_for(tmp_path)
    candidate = make_repo(repo, "checkout")
    protect(registry, candidate)

    real_read = Path.read_text

    def forbidden_read(path, *args, **kwargs):
        if path == candidate or path.is_relative_to(candidate):
            pytest.fail("protected discovery root reached the metadata reader")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    result = await registry.execute(
        "repo_discover", {"query": "checkout", "roots": [str(candidate)]})
    assert result.is_error
    assert result.details["reason"] == "live_runtime_protection"
    assert result.details["operation"] == "read_secret"


@pytest.mark.asyncio
async def test_unreadable_metadata_is_partial_without_common_head_fallback(tmp_path, monkeypatch):
    registry, repo, _ = registry_for(tmp_path)
    candidate = make_repo(repo, "checkout", worktree=True)
    own_head = repo / ".git-worktrees-checkout" / "worktrees" / "checkout" / "HEAD"
    real_read = Path.read_text

    def denied_read(path, *args, **kwargs):
        if path == own_head:
            raise PermissionError("fixture access denied")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied_read)
    result = await registry.execute(
        "repo_discover", {"query": "checkout", "roots": [str(candidate)]})
    envelope = json.loads(result.output)
    assert envelope["status"] == "partial"
    assert envelope["data"]["matches"] == []
    assert envelope["data"]["coverage"]["errors"][0]["code"] == "metadata_unavailable"


@pytest.mark.asyncio
async def test_authorised_worktree_config_and_detached_head(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    candidate = make_repo(repo, "local-alias", worktree=True)
    gitdir = repo / ".git-worktrees-local-alias" / "worktrees" / "local-alias"
    (gitdir / "config.worktree").write_text(
        '[remote "origin"]\nurl=https://github.com/example/worktree-override.git\n',
        encoding="utf-8")
    (gitdir / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    data = payload(await registry.execute(
        "repo_discover", {"query": "worktree-override", "roots": [str(candidate)]}))
    assert data["matches"][0]["branch"] == "a" * 12
    assert data["coverage_complete"]
