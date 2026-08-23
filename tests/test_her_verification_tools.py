from __future__ import annotations

import json

import pytest

from tools import her_verification
from tools.registry import ToolRegistry


def test_verification_tools_are_registry_owned_and_read_only(tmp_path):
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
    assert registry.is_read_only("verification_run") is True


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
async def test_verification_run_lists_only_registered_recipes_and_rejects_shell_text(
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

    assert set(json.loads(listed.output)) == {
        "pytest_core",
        "pytest_offline",
        "python_compile",
    }
    assert rejected.output.startswith("Error: unknown verification recipe")
    assert not (tmp_path / "should-not-exist").exists()


@pytest.mark.asyncio
async def test_verification_run_refuses_unsafe_fallback_when_isolation_is_unavailable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(her_verification, "_bubblewrap_available", lambda: False)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unsafe fallback executed")

    monkeypatch.setattr(her_verification, "_run_isolated", forbidden)
    result = await her_verification.execute_verification_run(
        {"operation": "run", "recipe": "python_compile"},
        workspace_dir=tmp_path,
    )

    assert result.output.startswith("Error: verification isolation is unavailable")
    assert result.details["unavailable"] is True
    assert result.details["isolated"] is False


@pytest.mark.asyncio
async def test_verification_run_clears_credentials_and_disables_network(
    tmp_path, monkeypatch
):
    if not her_verification._bubblewrap_available():
        pytest.skip("bubblewrap is unavailable on this platform")
    monkeypatch.setenv("HASHI_TEST_SECRET", "must-not-cross-boundary")
    (tmp_path / ".env").write_text("HASHI_FILE_SECRET=hidden\n", encoding="utf-8")
    (tmp_path / "secrets.json").write_text('{"token":"hidden"}\n', encoding="utf-8")
    probe = (
        "import os,pathlib,socket; "
        "assert os.environ.get('HASHI_TEST_SECRET') is None; "
        "assert os.environ['HOME'] == '/verification-home'; "
        "assert not pathlib.Path('.env').exists(); "
        "assert not pathlib.Path('secrets.json').exists(); "
        "s=socket.socket(); "
        "assert s.connect_ex(('1.1.1.1', 53)) != 0; "
        "print('isolated-ok')"
    )
    result = await her_verification.execute_verification_run(
        {"operation": "run", "recipe": "isolation_probe"},
        workspace_dir=tmp_path,
        options={
            "recipes": {
                "isolation_probe": {
                    "argv": ["{python}", "-c", probe],
                    "timeout_s": 10,
                }
            }
        },
    )

    assert result.output.strip() == "isolated-ok"
    assert result.details == {
        "operation": "run",
        "recipe": "isolation_probe",
        "exit_code": 0,
        "timed_out": False,
        "isolated": True,
        "network_disabled": True,
        "credentials_cleared": True,
        "workspace_copy_bytes": 0,
        "temporary_workspace_destroyed": True,
    }


@pytest.mark.asyncio
async def test_verification_run_failure_is_a_completed_failed_recipe(tmp_path):
    if not her_verification._bubblewrap_available():
        pytest.skip("bubblewrap is unavailable on this platform")
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
        "Error: verification recipe expected_failure failed with exit code 7"
    )
    assert result.details["exit_code"] == 7
    assert result.details["isolated"] is True
    assert result.details["temporary_workspace_destroyed"] is True
