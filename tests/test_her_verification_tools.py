from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools import her_verification
from tools.registry import ToolRegistry


def _run_git(repo, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def _initialize_git_repo(repo) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "--quiet")
    _run_git(repo, "config", "user.email", "tests@hashi.local")
    _run_git(repo, "config", "user.name", "HASHI tests")


def test_verification_tools_have_truthful_registry_safety_metadata(tmp_path):
    registry = ToolRegistry(
        allowed_tools=["workspace_inspect", "verification_run"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
    )

    assert {
        item["function"]["name"] for item in registry.get_tool_definitions()
    } == {
        "workspace_inspect",
        "verification_run",
        "tool_catalog",
    }
    assert registry.is_read_only("tool_catalog") is True
    assert registry.is_read_only("workspace_inspect") is True
    assert registry.is_read_only("verification_run") is False


@pytest.mark.asyncio
async def test_workspace_inspection_is_read_only_and_snapshot_detects_real_drift(
    tmp_path,
):
    _initialize_git_repo(tmp_path)
    tracked = tmp_path / "sample.txt"
    tracked.write_text("alpha\n", encoding="utf-8")
    _run_git(tmp_path, "add", "sample.txt")
    _run_git(tmp_path, "commit", "--quiet", "-m", "initial")
    before = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path
    )
    search = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "alpha", "path": "sample.txt"},
        workspace_dir=tmp_path,
    )
    hashed = await her_verification.execute_workspace_inspect(
        {"operation": "hash", "path": "sample.txt"}, workspace_dir=tmp_path
    )
    unchanged = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path
    )

    assert search.output == "1:alpha\n"
    assert json.loads(hashed.output)["sha256"]
    assert before.details["vcs"] == "git"
    assert before.details["snapshot_sha256"] == unchanged.details["snapshot_sha256"]

    tracked.write_text("expanded\n", encoding="utf-8")
    changed = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path
    )
    assert changed.details["snapshot_sha256"] != before.details["snapshot_sha256"]


@pytest.mark.asyncio
async def test_workspace_snapshot_detects_same_size_drift_in_ignored_nested_workspace(
    tmp_path,
):
    repo = tmp_path / "repo"
    _initialize_git_repo(repo)
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _run_git(repo, "add", ".gitignore", "tracked.txt")
    _run_git(repo, "commit", "--quiet", "-m", "initial")

    workspace = repo / "ignored" / "nested-workspace"
    workspace.mkdir(parents=True)
    artifact = workspace / "result.txt"
    before_content = b"alpha\n"
    after_content = b"bravo\n"
    assert len(before_content) == len(after_content)
    artifact.write_bytes(before_content)

    before = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=workspace
    )
    unchanged = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=workspace
    )
    artifact.write_bytes(after_content)
    changed = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=workspace
    )

    assert before.details["snapshot_sha256"] == unchanged.details["snapshot_sha256"]
    assert changed.details["snapshot_sha256"] != before.details["snapshot_sha256"]
    assert changed.details["vcs"] == "filesystem"


@pytest.mark.asyncio
async def test_workspace_inspection_rejects_paths_outside_the_workzone(tmp_path):
    result = await her_verification.execute_workspace_inspect(
        {"operation": "hash", "path": "../outside.txt"}, workspace_dir=tmp_path
    )

    assert result.output.startswith("Error:")
    assert "outside review workspace" in result.output


@pytest.mark.asyncio
async def test_workspace_search_falls_back_to_grep_when_rg_is_unavailable(
    tmp_path, monkeypatch
):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    real_which = her_verification.shutil.which

    def without_rg(name):
        return None if name == "rg" else real_which(name)

    monkeypatch.setattr(her_verification.shutil, "which", without_rg)
    result = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "beta", "path": "sample.txt"},
        workspace_dir=tmp_path,
    )

    assert result.output == "2:beta\n"
    assert result.details == {
        "operation": "search",
        "exit_code": 0,
        "matches": 1,
        "search_backend": "grep" if real_which("grep") else "python",
    }


@pytest.mark.asyncio
async def test_workspace_search_uses_python_fallback_without_a_search_binary(
    tmp_path, monkeypatch
):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)

    result = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "beta", "path": "sample.txt"},
        workspace_dir=tmp_path,
    )

    assert result.output == "2:beta\n"
    assert result.details == {
        "operation": "search",
        "exit_code": 0,
        "matches": 1,
        "search_backend": "python",
    }


@pytest.mark.asyncio
async def test_verification_run_lists_recipes_and_rejects_legacy_shell_text(
    tmp_path,
):
    listed = await her_verification.execute_verification_run(
        {"operation": "list"}, workspace_dir=tmp_path
    )
    rejected = await her_verification.execute_verification_run(
        {
            "operation": "run",
            "recipe": "printf hacked",
            "command": "touch should-not-exist",
        },
        workspace_dir=tmp_path,
    )

    catalog = json.loads(listed.output)
    assert set(catalog["recipes"]) == {
        "pytest_core",
        "pytest_offline",
        "python_compile",
    }
    assert catalog["direct_argv_supported"] is True
    assert catalog["execution_scope"] == "authoritative_current_workspace"
    assert catalog["workspace_copied"] is False
    assert catalog["authority"]["process_authority"] == "inherited"
    assert rejected.output.startswith(
        "Error: verification_run does not accept implicit-shell command text"
    )
    assert not (tmp_path / "should-not-exist").exists()

    offline_recipe = her_verification._recipe_catalog(None)["pytest_offline"]
    assert offline_recipe["argv"][-1] == (
        "not contract and not live and not platform and not real_wall_clock"
    )


@pytest.mark.asyncio
async def test_verification_run_executes_registered_argv_in_current_workspace(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HASHI_TEST_CONTEXT", "visible-to-registered-recipe")
    probe = (
        "import os,pathlib; "
        "assert os.environ['HASHI_TEST_CONTEXT'] == 'visible-to-registered-recipe'; "
        "assert pathlib.Path.cwd() == pathlib.Path(os.environ['EXPECTED_WORKSPACE']); "
        "pathlib.Path('verification-marker.txt').write_text('direct\\n'); "
        "print('workspace-ok')"
    )
    monkeypatch.setenv("EXPECTED_WORKSPACE", str(tmp_path.resolve()))
    result = await her_verification.execute_verification_run(
        {"operation": "run", "recipe": "workspace_probe"},
        workspace_dir=tmp_path,
        options={
            "recipes": {
                "workspace_probe": {
                    "argv": ["{python}", "-c", probe],
                    "timeout_s": 10,
                }
            }
        },
    )

    assert result.output.strip() == "workspace-ok"
    assert (tmp_path / "verification-marker.txt").read_text() == "direct\n"
    assert result.details["operation"] == "run"
    assert result.details["recipe"] == "workspace_probe"
    assert result.details["command_source"] == "registered_recipe"
    assert result.details["exit_code"] == 0
    assert result.details["timed_out"] is False
    assert result.details["execution_scope"] == "workspace"
    assert result.details["workspace_root"] == str(tmp_path.resolve())
    assert result.details["workspace_copied"] is False
    assert result.details["argv_registered"] is True
    assert result.details["shell"] is False
    assert result.details["process_isolated"] is False
    assert result.details["process_authority"] == "inherited"
    assert result.details["identity_policy"] == "inherited"
    assert result.details["filesystem_policy"] == "inherited"
    assert result.details["environment_policy"] == "inherited"
    assert result.details["network_policy"] == "inherited"
    assert result.details["home_policy"] == "inherited"
    assert result.details["foreground_cleanup"]["status"] == "normal_completion"
    assert result.details["workspace_access"] == {
        "read": True,
        "write": True,
        "execute": True,
    }


@pytest.mark.asyncio
async def test_verification_run_accepts_direct_argv_without_an_implicit_shell(tmp_path):
    result = await her_verification.execute_verification_run(
        {
            "operation": "run",
            "argv": [
                "{python}",
                "-c",
                "import pathlib; pathlib.Path('argv-marker').write_text('ok'); print('argv-ok')",
            ],
        },
        workspace_dir=tmp_path,
    )

    assert result.output.strip() == "argv-ok"
    assert (tmp_path / "argv-marker").read_text() == "ok"
    assert result.details["command_source"] == "direct_argv"
    assert result.details["argv_registered"] is False
    assert result.details["shell"] is False
    assert result.details["process_authority"] == "inherited"


@pytest.mark.asyncio
async def test_verification_timeout_grows_from_cumulative_execution_time(
    tmp_path, monkeypatch
):
    captured = {}

    async def fake_run(command, *, cwd, timeout_s):
        captured.update(command=list(command), cwd=cwd, timeout_s=timeout_s)
        return 0, b"dynamic-ok\n", b"", False, {
            "status": "normal_completion",
            "scope": "process_group",
            "forced": False,
            "process_reaped": True,
        }

    monkeypatch.setattr(her_verification, "_run_workspace_command", fake_run)
    result = await her_verification.execute_verification_run(
        {
            "operation": "run",
            "argv": ["{python}", "-V"],
            "timeout_s": 60,
            "_hashi_verification_policy": {"execution_elapsed_s": 3600},
        },
        workspace_dir=tmp_path,
        options={
            "direct_timeout_s": 60,
            "minimum_timeout_s": 300,
            "execution_timeout_multiplier": 1.5,
            "timeout_grace_s": 300,
        },
    )

    assert result.output.strip() == "dynamic-ok"
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["timeout_s"] == 5700
    policy = result.details["timeout_policy"]
    assert policy["execution_elapsed_s"] == 3600
    assert policy["requested_timeout_s"] == 60
    assert policy["execution_floor_s"] == 5700
    assert policy["effective_timeout_s"] == 5700

    hard_floor = her_verification._timeout_policy(
        configured_timeout_s=1,
        requested_timeout_s=1,
        arguments={
            "_hashi_verification_policy": {"execution_elapsed_s": 3600}
        },
        options={
            "minimum_timeout_s": 0,
            "execution_timeout_multiplier": 0,
            "timeout_grace_s": 0,
        },
    )
    assert hard_floor["minimum_timeout_s"] == 300
    assert hard_floor["execution_timeout_multiplier"] == 1
    assert hard_floor["timeout_grace_s"] == 60
    assert hard_floor["effective_timeout_s"] == 3660


@pytest.mark.asyncio
async def test_workspace_validation_timeout_reaps_the_process_group(tmp_path):
    exit_code, _stdout, _stderr, timed_out, cleanup = (
        await her_verification._run_workspace_command(
            [her_verification.sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            timeout_s=0.05,
        )
    )

    assert exit_code != 0
    assert timed_out is True
    assert cleanup["status"] in {"terminated", "forced"}
    assert cleanup["process_reaped"] is True


@pytest.mark.asyncio
async def test_verification_run_does_not_copy_or_reject_large_workspace(tmp_path):
    large = tmp_path / "large-sparse.bin"
    with large.open("wb") as handle:
        handle.truncate(513 * 1024 * 1024)
    probe = (
        "import pathlib; "
        "assert pathlib.Path('large-sparse.bin').stat().st_size > 512 * 1024 * 1024; "
        "print('large-workspace-ok')"
    )
    result = await her_verification.execute_verification_run(
        {"operation": "run", "recipe": "large_workspace_probe"},
        workspace_dir=tmp_path,
        options={
            "recipes": {
                "large_workspace_probe": {
                    "argv": ["{python}", "-c", probe],
                    "timeout_s": 10,
                }
            }
        },
    )

    assert result.output.strip() == "large-workspace-ok"
    assert result.details["workspace_copied"] is False
    assert result.details["execution_scope"] == "workspace"


@pytest.mark.asyncio
async def test_verification_run_failure_is_a_completed_failed_recipe(tmp_path):
    result = await her_verification.execute_verification_run(
        {"operation": "run", "recipe": "expected_failure"},
        workspace_dir=tmp_path,
        options={
            "recipes": {
                "expected_failure": {
                    "argv": ["{python}", "-c", "raise SystemExit(7)"],
                    "timeout_s": 10,
                }
            }
        },
    )

    assert result.output.startswith(
        "Error: verification expected_failure failed with exit code 7"
    )
    assert result.details["exit_code"] == 7
    assert result.details["execution_scope"] == "workspace"
    assert result.details["workspace_copied"] is False



@pytest.mark.asyncio
async def test_direct_verification_treats_shell_metacharacters_as_data_and_honors_longer_request(tmp_path):
    marker = "a && echo unexpected > injected.txt"
    result = await her_verification.execute_verification_run(
        {"operation": "run", "argv": ["{python}", "-c",
            "import sys; print(sys.argv[1])", marker], "timeout_s": 9000},
        workspace_dir=tmp_path,
    )
    assert result.output.strip() == marker
    assert not (tmp_path / "injected.txt").exists()
    assert result.details["shell"] is False
    assert result.details["timeout_policy"]["effective_timeout_s"] == 9000


@pytest.mark.asyncio
async def test_python_workspace_search_streams_file_over_old_32_mib_limit(tmp_path, monkeypatch):
    source = tmp_path / "large.txt"
    chunk = b"x" * (64 * 1024)
    with source.open("wb") as stream:
        for _ in range(513):
            stream.write(chunk)
        stream.write(b" needle-at-the-end")
    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)
    result = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "needle-at-the-end", "path": source.name},
        workspace_dir=tmp_path,
    )
    assert "needle-at-the-end" in result.output
    assert "[excerpt]" in result.output
    assert result.details["coverage_complete"] is True
    assert result.details["large_files_scanned"] == 1
    assert result.details["complete_content"] is False

    regex = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "needle.*end", "regex": True, "path": source.name},
        workspace_dir=tmp_path,
    )
    assert "coverage is incomplete" in regex.output
    assert regex.details["exit_code"] == 2
    assert regex.details["coverage_complete"] is False
    assert regex.details["omitted"][0]["reason"] == "large_regex_requires_file_search"
    assert "file_search" in regex.details["recovery"]

    registry = ToolRegistry(
        allowed_tools=["file_search"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    recovered = await registry.execute_with_audit_context(
        "file_search",
        {"query": "needle.*end", "match": "regex", "mode": "content",
         "roots": [str(source)], "options": {"regex_engine": "python",
                                              "regex_max_line_bytes": 40 * 1024 * 1024}},
        audit_context={"request_id": "large-regex-recovery"},
    )
    assert not recovered.is_error, recovered.output
    data = json.loads(recovered.output)["data"]
    assert data["coverage_complete"]
    assert len(data["matches"]) == 1


@pytest.mark.asyncio
async def test_python_workspace_search_does_not_silently_stop_at_total_byte_cap(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("alpha needle", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta needle", encoding="utf-8")
    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)
    monkeypatch.setattr(her_verification, "_MAX_ARTIFACT_HASH_BYTES", 1)
    result = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "needle"}, workspace_dir=tmp_path)
    assert "a.txt:1:alpha needle" in result.output
    assert "b.txt:1:beta needle" in result.output
    assert result.details["matches"] == 2


@pytest.mark.asyncio
async def test_python_workspace_search_reports_unreadable_files_as_partial(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("needle", encoding="utf-8")
    original = Path.read_bytes

    def deny(path):
        if path == blocked:
            raise PermissionError("fixture denial")
        return original(path)

    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)
    monkeypatch.setattr(Path, "read_bytes", deny)
    result = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "needle"}, workspace_dir=tmp_path)
    assert "coverage is incomplete" in result.output
    assert result.details["exit_code"] == 2
    assert result.details["omitted"][0]["reason"] == "PermissionError"
    assert result.details["omitted"][0]["path"] == str(blocked)


@pytest.mark.asyncio
async def test_python_workspace_large_literal_search_cancels_its_stream(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time
    import tools.literal_search as literal

    source = tmp_path / "large.txt"
    source.write_text("123456789", encoding="utf-8")
    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)
    monkeypatch.setattr(her_verification, "_MAX_PYTHON_SEARCH_FILE_BYTES", 1)
    started, exited = threading.Event(), threading.Event()

    def blocking_scan(*_args, progress, **_kwargs):
        started.set()
        try:
            while True:
                progress(1)
                time.sleep(0.01)
        finally:
            exited.set()

    monkeypatch.setattr(literal, "scan_literal", blocking_scan)
    task = asyncio.create_task(her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "9", "path": source.name}, workspace_dir=tmp_path))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert exited.is_set()
    assert cancelled.value.hashi_tool_details["foreground_cleanup"]["status"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["invalid_utf8", "changed_file", "output_limit"])
async def test_python_workspace_search_qualifies_other_incomplete_results(tmp_path, monkeypatch, condition):
    source = tmp_path / "example.txt"
    source.write_bytes(b"needle\n")
    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)
    if condition == "invalid_utf8":
        source.write_bytes(b"needle \xff\n")
    elif condition == "changed_file":
        original = Path.read_bytes

        def changed(path):
            content = original(path)
            if path == source:
                path.write_bytes(content + b"changed")
            return content

        monkeypatch.setattr(Path, "read_bytes", changed)
    else:
        monkeypatch.setattr(her_verification, "_MAX_OUTPUT_CHARS", 3)
    result = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "needle", "path": source.name}, workspace_dir=tmp_path)
    assert result.details["coverage_complete"] is False
    assert result.details["complete_content"] is False
    assert result.details["exit_code"] == 2
    assert "file_search" in result.details["recovery"]
    if condition == "output_limit":
        assert result.details["output_limited"]
    else:
        reason = result.details["omitted"][0]["reason"]
        assert reason == ("invalid_utf8_replaced" if condition == "invalid_utf8"
                          else "file_changed_during_search")


def test_filesystem_snapshot_passes_old_file_and_byte_limits_with_injected_inventory(tmp_path, monkeypatch):
    import hashlib
    import stat
    from types import SimpleNamespace

    names = [f"file-{index:05d}.txt" for index in range(50001)]
    marker = names[-1]
    changed = [False]
    original_stat, original_lstat = Path.stat, Path.lstat

    def fake_info(path):
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_dev=1, st_ino=1,
                               st_size=(513 * 1024 * 1024 if path.name == marker else 0),
                               st_mtime_ns=1)

    def fake_stat(path, *args, **kwargs):
        return fake_info(path) if path.parent == tmp_path and path.name in names_set else original_stat(path, *args, **kwargs)

    def fake_lstat(path):
        return fake_info(path) if path.parent == tmp_path and path.name in names_set else original_lstat(path)

    def fake_hash(path, *, cancelled=None):
        content = path.name + ("-changed" if changed[0] and path.name == marker else "")
        return hashlib.sha256(content.encode()).hexdigest()

    names_set = set(names)
    monkeypatch.setattr(her_verification.os, "walk",
                        lambda root, **_kwargs: iter([(str(root), [], list(names))]))
    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(her_verification, "_sha256_file", fake_hash)
    first, info = her_verification._filesystem_snapshot(tmp_path)
    assert info["file_count"] == 50001
    assert info["bytes_hashed"] > 512 * 1024 * 1024
    assert info["coverage_complete"] is True
    changed[0] = True
    second, _ = her_verification._filesystem_snapshot(tmp_path)
    assert second != first


def test_filesystem_snapshot_discloses_excluded_directories(tmp_path):
    (tmp_path / "sample.txt").write_text("visible", encoding="utf-8")
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "hidden.txt").write_text("not scanned", encoding="utf-8")
    _, metadata = her_verification._filesystem_snapshot(tmp_path)
    assert metadata["coverage_complete"] is False
    assert metadata["excluded_count"] == 1
    assert metadata["excluded_paths"] == ["node_modules"]
    assert metadata["excluded_paths_truncated"] is False
    assert metadata["symlink_targets_followed"] is False


@pytest.mark.asyncio
async def test_filesystem_snapshot_reports_change_during_hash(tmp_path, monkeypatch):
    source = tmp_path / "changing.txt"
    source.write_text("before", encoding="utf-8")
    original = her_verification._sha256_file

    def changed(path, *, cancelled=None):
        value = original(path, cancelled=cancelled)
        if path == source:
            path.write_text("after change", encoding="utf-8")
        return value

    monkeypatch.setattr(her_verification, "_sha256_file", changed)
    result = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path)
    assert result.output.startswith("Error:")
    assert "changed during snapshot" in result.output
    assert "snapshot_sha256" not in result.details


@pytest.mark.asyncio
async def test_filesystem_snapshot_cancellation_stops_cooperative_hash(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time

    (tmp_path / "large.txt").write_text("fixture", encoding="utf-8")
    started, exited = threading.Event(), threading.Event()

    def blocking_hash(_path, *, cancelled=None):
        started.set()
        try:
            while True:
                her_verification._check_inspection_cancelled(cancelled)
                time.sleep(0.01)
        finally:
            exited.set()

    monkeypatch.setattr(her_verification, "_sha256_file", blocking_hash)
    task = asyncio.create_task(her_verification._run_workspace_snapshot(tmp_path, filesystem_only=True))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert exited.is_set()
    assert cancelled.value.hashi_tool_details["foreground_cleanup"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_git_snapshot_does_not_claim_ignored_workspace_files_are_covered(tmp_path):
    _initialize_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("tracked", encoding="utf-8")
    _run_git(tmp_path, "add", ".gitignore", "tracked.txt")
    _run_git(tmp_path, "commit", "--quiet", "-m", "fixture")
    ignored = tmp_path / "ignored.txt"
    ignored.write_text("before", encoding="utf-8")
    before = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path)
    ignored.write_text("after", encoding="utf-8")
    after = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path)
    assert before.details["snapshot_sha256"] == after.details["snapshot_sha256"]
    assert before.details["coverage_complete"] is False
    assert before.details["coverage_scope"] == "git_tracked_and_nonignored"
    assert "git_ignored_paths" in before.details["coverage_limitations"]


@pytest.mark.asyncio
async def test_filesystem_snapshot_works_when_git_is_not_installed(tmp_path, monkeypatch):
    (tmp_path / "sample.txt").write_text("fixture", encoding="utf-8")

    async def missing_git(_argv, *, cwd):
        raise FileNotFoundError("git is unavailable")

    monkeypatch.setattr(her_verification, "_run_read_only", missing_git)
    result = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path)
    assert result.details["vcs"] == "filesystem"
    assert result.details["coverage_complete"] is True
    assert result.details["file_count"] == 1


@pytest.mark.asyncio
async def test_python_workspace_search_discloses_excluded_directory(tmp_path, monkeypatch):
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "hidden.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("other", encoding="utf-8")
    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)
    result = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "needle"}, workspace_dir=tmp_path)
    assert "coverage is incomplete" in result.output
    assert result.details["omitted"][0] == {
        "path": str(ignored), "reason": "directory_excluded"}
    assert result.details["coverage_complete"] is False
    assert result.details["omitted_paths_truncated"] is False


@pytest.mark.asyncio
async def test_workspace_coverage_details_are_pageable_and_run_bound(tmp_path, monkeypatch):
    names = sorted(her_verification._IGNORED_COPY_DIRS)
    for name in names:
        (tmp_path / name).mkdir()
    nested = tmp_path / "nested"
    nested.mkdir()
    for name in names[:3]:
        (nested / name).mkdir()
    expected = set(names) | {f"nested/{name}" for name in names[:3]}
    assert len(expected) > 20
    (tmp_path / "visible.txt").write_text("other", encoding="utf-8")
    registry = ToolRegistry(
        allowed_tools=["workspace_inspect"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    context = {"request_id": "coverage-run", "request_tool_allowlist": ["workspace_inspect"]}

    async def read_details(reference):
        parts = []
        offset = 0
        for _ in range(20):
            page = await registry.execute_with_audit_context(
                "tool_output_read", {"reference": reference, "offset": offset}, audit_context=context)
            assert not page.is_error, page.output
            data = json.loads(page.output)
            parts.append(data["text"])
            if data["complete"]:
                return json.loads("".join(parts))
            offset = data["next_offset"]
        pytest.fail("coverage details did not finish")

    snapshot = await registry.execute_with_audit_context(
        "workspace_inspect", {"operation": "snapshot"}, audit_context=context)
    assert not snapshot.is_error, snapshot.output
    snapshot_data = json.loads(snapshot.output)
    assert snapshot_data["coverage_complete"] is False
    assert snapshot_data["excluded_paths_truncated"] is True
    snapshot_ref = snapshot_data["excluded_paths_reference"]["reference"]
    snapshot_details = await read_details(snapshot_ref)
    assert set(snapshot_details["excluded_paths"]) == expected
    denied = await registry.execute_with_audit_context(
        "tool_output_read", {"reference": snapshot_ref, "offset": 0},
        audit_context={"request_id": "other-run", "request_tool_allowlist": ["workspace_inspect"]})
    assert denied.is_error

    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)
    search = await registry.execute_with_audit_context(
        "workspace_inspect", {"operation": "search", "query": "needle"}, audit_context=context)
    assert search.details["coverage_complete"] is False
    assert search.details["omitted_paths_truncated"] is True
    reference = search.details["omitted_reference"]["reference"]
    assert reference in search.output
    search_details = await read_details(reference)
    assert {Path(item["path"]).relative_to(tmp_path).as_posix()
            for item in search_details["omitted"]} == expected

    unbound = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path)
    unbound_data = json.loads(unbound.output)
    assert unbound_data["excluded_paths_truncated"] is True
    assert len(unbound_data["excluded_paths"]) == 20
    assert "excluded_paths_reference" not in unbound_data


def test_coverage_detail_reference_preserves_surrogate_filesystem_names():
    values = [str(index) for index in range(20)] + ["\udcff"]
    metadata = {"excluded_paths": values, "excluded_paths_truncated": False}
    retained = []

    def retain(value):
        retained.append(value)
        return "fixture-reference"

    her_verification._page_coverage_details(
        metadata, key="excluded_paths", truncated_key="excluded_paths_truncated",
        retain_coverage=retain)
    assert metadata["excluded_paths_truncated"]
    assert json.loads(retained[0])["excluded_paths"] == values


@pytest.mark.asyncio
async def test_filesystem_snapshot_preserves_unicode_names(tmp_path):
    source = tmp_path / "\u793a\u4f8b.txt"
    source.write_text("before", encoding="utf-8")
    before = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path)
    source.write_text("after", encoding="utf-8")
    after = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path)
    assert before.details["coverage_complete"]
    assert before.details["snapshot_sha256"] != after.details["snapshot_sha256"]


@pytest.mark.asyncio
async def test_python_workspace_regex_worker_preserves_results_and_invalid_pattern(tmp_path, monkeypatch):
    source = tmp_path / "regex.txt"
    source.write_text("alpha\ncat 12\ncat 34\n", encoding="utf-8")
    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)
    result = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": r"cat\s+\d+", "regex": True, "path": source.name},
        workspace_dir=tmp_path)
    assert result.output == "2:cat 12\n3:cat 34\n"
    assert result.details["matches"] == 2
    invalid = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "[", "regex": True, "path": source.name},
        workspace_dir=tmp_path)
    assert invalid.output.startswith("Error:") and "invalid regex" in invalid.output
    assert invalid.details["exit_code"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("taskkill_missing_from_path", [False, True])
async def test_python_workspace_regex_worker_is_reaped_on_cancellation(
    tmp_path, monkeypatch, taskkill_missing_from_path,
):
    import asyncio
    import os
    import signal

    source = tmp_path / "pathological.txt"
    source.write_text("a" * 200 + "!", encoding="utf-8")
    original_which = her_verification.shutil.which
    monkeypatch.setattr(
        her_verification.shutil, "which",
        lambda name: None if name in {"rg", "grep"} or (
            taskkill_missing_from_path and name in {"taskkill", "taskkill.exe"}
        ) else original_which(name))
    original_spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    workers = []

    async def observed_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        if any("workspace_search_process.py" in str(value) for value in args):
            workers.append(process)
            started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed_spawn)
    task = asyncio.create_task(her_verification.execute_workspace_inspect(
        {"operation": "search", "query": r"(a+)+$", "regex": True, "path": source.name},
        workspace_dir=tmp_path))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(1)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        cleanup = cancelled.value.hashi_tool_details["foreground_cleanup"]
        assert cleanup["process_reaped"] and not cleanup["group_alive"]
        assert cleanup["status"] in {"force_killed", "terminated"}
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if workers and workers[0].returncode is None:
            if os.name == "nt":
                await her_verification.terminate_windows_process_tree(workers[0].pid, force=True)
            else:
                os.killpg(workers[0].pid, signal.SIGKILL)
            await asyncio.wait_for(workers[0].wait(), timeout=5)


@pytest.mark.asyncio
async def test_git_snapshot_tracks_each_untracked_file(tmp_path):
    _initialize_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("fixture", encoding="utf-8")
    _run_git(tmp_path, "add", "tracked.txt")
    _run_git(tmp_path, "commit", "--quiet", "-m", "fixture")
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    second = tmp_path / "second.txt"
    second.write_text("before", encoding="utf-8")
    before = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path)
    second.write_text("after", encoding="utf-8")
    after = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path)
    assert before.details["untracked_files"] == 2
    assert after.details["snapshot_sha256"] != before.details["snapshot_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["snapshot", "status", "diff", "search"])
async def test_workspace_external_inspection_process_is_reaped_on_cancellation(
    tmp_path, monkeypatch, operation,
):
    import asyncio
    import os
    import signal
    import sys

    original_spawn = asyncio.create_subprocess_exec
    original_which = her_verification.shutil.which
    monkeypatch.setattr(
        her_verification.shutil, "which",
        lambda name: sys.executable if name == "rg" else original_which(name))
    started = asyncio.Event()
    workers = []

    async def delayed_inspection(*args, **kwargs):
        if args[0] in {"git", sys.executable}:
            process = await original_spawn(
                sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
            workers.append(process)
            started.set()
            return process
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_inspection)
    arguments = {"operation": operation}
    if operation == "search":
        arguments["query"] = "fixture"
    task = asyncio.create_task(her_verification.execute_workspace_inspect(
        arguments, workspace_dir=tmp_path))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        cleanup = cancelled.value.hashi_tool_details["foreground_cleanup"]
        assert cleanup["process_reaped"] and not cleanup["group_alive"]
        assert cleanup["status"] in {"force_killed", "terminated"}
        assert workers[0].returncode is not None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if workers and workers[0].returncode is None:
            if os.name == "nt":
                await her_verification.terminate_windows_process_tree(workers[0].pid, force=True)
            else:
                os.killpg(workers[0].pid, signal.SIGKILL)
            await asyncio.wait_for(workers[0].wait(), timeout=5)


@pytest.mark.asyncio
async def test_workspace_file_hash_cancellation_stops_cooperative_thread(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time

    (tmp_path / "large.txt").write_text("fixture", encoding="utf-8")
    started, exited = threading.Event(), threading.Event()

    def blocking_hash(_path, *, cancelled=None):
        started.set()
        try:
            while True:
                her_verification._check_inspection_cancelled(cancelled)
                time.sleep(0.01)
        finally:
            exited.set()

    monkeypatch.setattr(her_verification, "_sha256_file", blocking_hash)
    task = asyncio.create_task(her_verification.execute_workspace_inspect(
        {"operation": "hash", "path": "large.txt"}, workspace_dir=tmp_path))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert exited.is_set()
    assert cancelled.value.hashi_tool_details["foreground_cleanup"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_workspace_git_cancellation_reaps_real_descendant(tmp_path, monkeypatch):
    import asyncio
    import os
    import signal
    import sys
    import psutil

    marker = tmp_path / "spawned-child.txt"
    program = (
        "import pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii')\n"
        "time.sleep(30)\n"
    )
    original_spawn = asyncio.create_subprocess_exec
    workers = []

    async def delayed_git(*args, **kwargs):
        if args[0] == "git":
            proc = await original_spawn(sys.executable, "-c", program, str(marker), **kwargs)
            workers.append(proc)
            return proc
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_git)
    task = asyncio.create_task(her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path))
    child = None
    child_started = None
    try:
        async with asyncio.timeout(8):
            while not marker.exists():
                await asyncio.sleep(0.01)
        child = psutil.Process(int(marker.read_text(encoding="ascii")))
        child_started = child.create_time()
        assert child.is_running()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(task, timeout=10)
        assert raised.value.hashi_tool_details["foreground_cleanup"]["process_reaped"]
        assert workers[0].returncode is not None
        async with asyncio.timeout(5):
            while child.is_running() and child.create_time() == child_started:
                await asyncio.sleep(0.01)
    except psutil.NoSuchProcess:
        assert task.done() and workers[0].returncode is not None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if workers and workers[0].returncode is None:
            if os.name == "nt":
                await her_verification.terminate_windows_process_tree(workers[0].pid, force=True)
            else:
                os.killpg(workers[0].pid, signal.SIGKILL)
            await asyncio.wait_for(workers[0].wait(), timeout=5)
        if child is not None and child_started is not None:
            try:
                if child.is_running() and child.create_time() == child_started:
                    if os.name == "nt":
                        await her_verification.terminate_windows_process_tree(child.pid, force=True)
                    else:
                        child.kill()
            except psutil.NoSuchProcess:
                pass
