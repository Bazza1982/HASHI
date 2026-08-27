from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
from pathlib import Path

import pytest

from tools.builtins import BuiltinExecutionResult, execute_bash
from tools.registry import ToolRegistry


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.name != "posix",
        reason="foreground process-group ownership is exercised on POSIX",
    ),
]


def _tree_command(tmp_path: Path, *, parent_exits: bool = False) -> tuple[str, Path, Path]:
    parent_pid_path = tmp_path / "parent.pid"
    child_pid_path = tmp_path / "child.pid"
    script_path = tmp_path / ("detached-tree.py" if parent_exits else "tree.py")
    script_path.write_text(
        "\n".join(
            [
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                "from pathlib import Path",
                "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')",
                "child_code = (",
                "    \"import os, sys, time; from pathlib import Path; \"",
                "    \"Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); \"",
                "    \"time.sleep(60)\"",
                ")",
                "child = subprocess.Popen(",
                "    [sys.executable, '-c', child_code, sys.argv[2]],",
                "    stdout=subprocess.DEVNULL if len(sys.argv) > 3 else None,",
                "    stderr=subprocess.DEVNULL if len(sys.argv) > 3 else None,",
                ")",
                "if len(sys.argv) > 3:",
                "    Path(sys.argv[2]).write_text(str(child.pid), encoding='utf-8')",
                "if len(sys.argv) == 3:",
                "    time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    parts = [
        shlex.quote(sys.executable),
        shlex.quote(str(script_path)),
        shlex.quote(str(parent_pid_path)),
        shlex.quote(str(child_pid_path)),
    ]
    if parent_exits:
        parts.append("detach")
    return " ".join(parts), parent_pid_path, child_pid_path


async def _read_pid(path: Path, *, timeout: float = 3.0) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            value = int(path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, TypeError, ValueError):
            await asyncio.sleep(0.01)
            continue
        if value > 0:
            return value
    raise AssertionError(f"PID file was not populated: {path.name}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _assert_pid_gone(pid: int, *, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.02)
    assert not _pid_alive(pid), f"PID {pid} survived foreground cleanup"


@pytest.mark.asyncio
async def test_omitted_bash_timeout_has_no_default_or_configured_cap(tmp_path):
    result = await execute_bash(
        {"command": "sleep 0.05; printf complete"},
        workspace_dir=tmp_path,
        timeout_max=0.001,
    )

    assert isinstance(result, BuiltinExecutionResult)
    assert result.output == "complete"
    assert result.details["timeout_explicit"] is False
    assert result.details["foreground_cleanup"]["status"] == "normal_completion"


@pytest.mark.asyncio
async def test_explicit_bash_timeout_applies_only_the_explicit_operator_cap(tmp_path):
    result = await execute_bash(
        {"command": "sleep 60", "timeout": 10},
        workspace_dir=tmp_path,
        timeout_max=0.1,
    )

    assert isinstance(result, BuiltinExecutionResult)
    assert result.output == "Error: command timed out after 0.1s"
    assert result.details["timeout_requested_s"] == 10
    assert result.details["timeout_effective_s"] == pytest.approx(0.1)
    assert result.details["timeout_capped"] is True
    cleanup = result.details["foreground_cleanup"]
    assert cleanup["status"] in {"terminated", "force_killed"}
    assert cleanup["process_reaped"] is True
    assert cleanup["group_alive"] is False


@pytest.mark.asyncio
async def test_bash_rejects_non_positive_explicit_timeout_before_spawn(
    tmp_path, monkeypatch
):
    async def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("invalid timeout must be rejected before process spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", unexpected_spawn)

    result = await execute_bash(
        {"command": "printf should-not-run", "timeout": 0},
        workspace_dir=tmp_path,
    )

    assert isinstance(result, BuiltinExecutionResult)
    assert result.output == "Error: timeout must be a positive number of seconds"


@pytest.mark.asyncio
async def test_explicit_timeout_reaps_shell_child_and_grandchild(tmp_path):
    command, parent_path, child_path = _tree_command(tmp_path)

    result = await execute_bash(
        {"command": command, "timeout": 0.5},
        workspace_dir=tmp_path,
    )

    parent_pid = await _read_pid(parent_path)
    child_pid = await _read_pid(child_path)
    assert isinstance(result, BuiltinExecutionResult)
    assert result.output.startswith("Error: command timed out after")
    assert result.details["foreground_cleanup"]["scope"] == "process_group"
    await _assert_pid_gone(parent_pid)
    await _assert_pid_gone(child_pid)


@pytest.mark.asyncio
async def test_registry_cancellation_reaps_process_tree_and_audits_cleanup(tmp_path):
    command, parent_path, child_path = _tree_command(tmp_path)
    registry = ToolRegistry(
        allowed_tools=["bash"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={"agent_name": "zelda", "workspace_dir": str(tmp_path)},
    )
    task = asyncio.create_task(
        registry.execute("bash", {"command": command}, tool_call_id="cancel-tree")
    )
    parent_pid = await _read_pid(parent_path)
    child_pid = await _read_pid(child_path)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_pid_gone(parent_pid)
    await _assert_pid_gone(child_pid)
    record = json.loads(
        (tmp_path / "tool_action_audit.jsonl").read_text(encoding="utf-8")
    )
    cleanup = record["details"]["foreground_cleanup"]
    assert cleanup["status"] in {"terminated", "force_killed"}
    assert cleanup["process_reaped"] is True
    assert cleanup["group_alive"] is False
    assert record["status"] == "failed"
    assert record["tool_call_id"] == "cancel-tree"
    assert record["details"]["foreground_cleanup"]["process_reaped"] is True
    assert "foreground cleanup" in record["output_snippet"]


@pytest.mark.asyncio
async def test_normal_shell_exit_reaps_a_surviving_foreground_descendant(tmp_path):
    command, _parent_path, child_path = _tree_command(tmp_path, parent_exits=True)

    result = await execute_bash({"command": command}, workspace_dir=tmp_path)

    child_pid = await _read_pid(child_path)
    assert isinstance(result, BuiltinExecutionResult)
    cleanup = result.details["foreground_cleanup"]
    assert cleanup["status"] in {"terminated", "force_killed"}
    assert cleanup["group_alive"] is False
    await _assert_pid_gone(child_pid)


@pytest.mark.asyncio
async def test_foreground_cleanup_does_not_touch_an_unrelated_process_group(tmp_path):
    unrelated = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        start_new_session=True,
    )
    try:
        result = await execute_bash(
            {"command": "sleep 60", "timeout": 0.1},
            workspace_dir=tmp_path,
        )
        assert isinstance(result, BuiltinExecutionResult)
        assert result.output.startswith("Error: command timed out")
        assert unrelated.returncode is None
        assert _pid_alive(unrelated.pid)
    finally:
        if unrelated.returncode is None:
            os.killpg(unrelated.pid, signal.SIGKILL)
        await unrelated.wait()
