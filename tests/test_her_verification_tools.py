from __future__ import annotations

import json

import pytest

from tools import her_verification
from tools.registry import ToolRegistry


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
    }
    assert registry.is_read_only("workspace_inspect") is True
    assert registry.is_read_only("verification_run") is False


@pytest.mark.asyncio
async def test_workspace_inspection_is_read_only_and_snapshot_detects_real_drift(
    tmp_path,
):
    tracked = tmp_path / "sample.txt"
    tracked.write_text("alpha\n", encoding="utf-8")
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
    assert before.details["snapshot_sha256"] == unchanged.details["snapshot_sha256"]

    tracked.write_text("beta\n", encoding="utf-8")
    changed = await her_verification.execute_workspace_inspect(
        {"operation": "snapshot"}, workspace_dir=tmp_path
    )
    assert changed.details["snapshot_sha256"] != before.details["snapshot_sha256"]


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
        "search_backend": "grep",
    }


@pytest.mark.asyncio
async def test_workspace_search_reports_unavailable_without_a_search_binary(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(her_verification.shutil, "which", lambda _name: None)

    result = await her_verification.execute_workspace_inspect(
        {"operation": "search", "query": "alpha", "path": "."},
        workspace_dir=tmp_path,
    )

    assert result.output.startswith("Error: workspace search is unavailable")
    assert result.details["unavailable"] is True


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
