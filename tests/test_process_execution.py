from __future__ import annotations

import asyncio
import multiprocessing
import os
import shutil
import sys
from pathlib import Path

import pytest

from orchestrator.background_jobs import BackgroundJobManager
from orchestrator.canonical_audit import CanonicalAuditStore
from orchestrator.process_execution import (
    decode_process_output,
    execution_environment_descriptor,
    process_group_kwargs,
    process_is_alive,
    resolve_argv_invocation,
)
from orchestrator.workzone import resolve_workzone_input
from tools.builtins import BuiltinExecutionResult, execute_shell
from tools.her_verification import execute_verification_run
from tools.registry import ToolRegistry


RAW_AUDIT_AUTHORITY = {
    "allow_raw_audit": True,
    "actor": "windows-contract-test",
    "purpose": "verify cross-platform canonical evidence",
}


def _write_native_audit_batch(root: str, worker: int) -> None:
    store = CanonicalAuditStore(
        root,
        instance_id="HASHI3",
        agent_id="windows-concurrency",
    )
    store.record_many(
        {
            "event_type": "provider_stream_event",
            "payload": {"worker": worker, "chunk": index},
            "request_id": f"worker-{worker}",
        }
        for index in range(25)
    )


def test_execution_environment_descriptor_names_the_real_platform_contract(tmp_path):
    descriptor = execution_environment_descriptor(tmp_path)

    assert descriptor["working_directory"] == str(tmp_path.resolve())
    assert descriptor["text_encoding"] == "utf-8"
    assert descriptor["shell_tool"]["name"] == "shell"
    assert descriptor["shell_tool"]["legacy_alias"] == "bash"
    assert descriptor["shell_tool"]["implicit_shell"] is False
    assert descriptor["argv_execution"]["implicit_shell"] is False
    if os.name == "nt":
        assert descriptor["runtime_platform"] == "windows_native"
        assert descriptor["path_style"] == "windows"
        assert descriptor["shell_tool"]["default_shell"] == "powershell"
        assert descriptor["argv_execution"]["windows_cmd_bat_auto_wrapped"] is True
    else:
        assert descriptor["path_style"] == "posix"
        assert descriptor["shell_tool"]["default_shell"] == "bash"
        assert descriptor["argv_execution"]["windows_cmd_bat_auto_wrapped"] is False


def test_registry_exposes_shell_but_accepts_persisted_bash_calls(tmp_path):
    registry = ToolRegistry(
        allowed_tools=["bash"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
    )

    assert registry.is_allowed("shell") is True
    assert registry.is_allowed("bash") is True
    assert [
        definition["function"]["name"] for definition in registry.get_tool_definitions()
    ] == ["shell"]


def test_process_liveness_probe_does_not_signal_the_current_process():
    assert process_is_alive(os.getpid()) is True
    assert process_is_alive(2_147_483_647) is False


@pytest.mark.asyncio
async def test_shell_default_matches_execution_environment_and_preserves_unicode(
    tmp_path,
):
    command = "Write-Output 'café 中文'" if os.name == "nt" else "printf 'café 中文'"

    result = await execute_shell({"command": command}, workspace_dir=tmp_path)

    assert isinstance(result, BuiltinExecutionResult)
    assert result.output == "café 中文"
    assert result.details["shell"] == ("powershell" if os.name == "nt" else "bash")
    assert result.details["encoding"] == "utf-8"
    assert result.details["cwd"] == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_shell_contract_propagates_nonzero_exit_code(tmp_path):
    result = await execute_shell({"command": "exit 7"}, workspace_dir=tmp_path)

    assert isinstance(result, BuiltinExecutionResult)
    assert result.output.startswith("[exit code 7]")
    assert result.details["exit_code"] == 7


@pytest.mark.asyncio
async def test_background_command_mode_uses_the_same_explicit_shell_contract(tmp_path):
    manager = BackgroundJobManager(tmp_path / "background_shell")
    await manager.start()
    command = (
        "Write-Output 'background café 中文'"
        if os.name == "nt"
        else "printf 'background café 中文'"
    )
    try:
        record = await manager.start_job(
            agent="zelda",
            cwd=tmp_path,
            command=command,
            notify_on_complete=False,
            notify_on_failure=False,
            trigger_agent_on_complete=False,
            trigger_agent_on_failure=False,
        )
        await manager._monitor_tasks[record.job_id]
        saved = manager.get(record.job_id)
        assert saved is not None
        assert saved.state == "succeeded"
        assert saved.command["shell"] == ("powershell" if os.name == "nt" else "bash")
        assert manager.tail(record.job_id) == "background café 中文"
    finally:
        await manager.stop()


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
@pytest.mark.asyncio
async def test_native_windows_cmd_contract_preserves_unicode(tmp_path):
    result = await execute_shell(
        {"command": "echo café 中文", "shell": "cmd"},
        workspace_dir=tmp_path,
    )

    assert isinstance(result, BuiltinExecutionResult)
    assert "café 中文" in result.output
    assert result.details["shell"] == "cmd"
    assert result.details["encoding"] == "utf-8"
    assert result.details["launcher"] == "powershell_encoded"
    assert Path(result.details["shell_executable"]).name.casefold() == "cmd.exe"
    assert Path(result.details["launcher_executable"]).name.casefold() in {
        "powershell.exe",
        "pwsh.exe",
    }


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
@pytest.mark.asyncio
async def test_native_windows_legacy_bash_name_invokes_real_bash(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("Bash is not installed on this Windows runner")

    result = await execute_shell(
        {"command": "printf bash-contract-ok", "shell": "bash"},
        workspace_dir=tmp_path,
    )

    assert isinstance(result, BuiltinExecutionResult)
    assert result.output == "bash-contract-ok"
    assert result.details["shell"] == "bash"
    assert Path(result.details["shell_executable"]).name.casefold() == "bash.exe"


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
@pytest.mark.asyncio
async def test_native_windows_cmd_batch_is_wrapped_for_argv_callers(tmp_path):
    script = tmp_path / "argv probe.cmd"
    script.write_bytes("@echo off\r\necho cmd-wrapper-ok café 中文\r\n".encode("utf-8"))

    invocation = resolve_argv_invocation((str(script),))
    process = await asyncio.create_subprocess_exec(
        *invocation.argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_group_kwargs(),
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0
    assert invocation.launcher == "cmd"
    assert invocation.resolved_executable == str(script.resolve())
    assert decode_process_output(stdout).strip() == "cmd-wrapper-ok café 中文"
    assert decode_process_output(stderr) == ""


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
@pytest.mark.asyncio
async def test_native_windows_powershell_script_is_wrapped_for_argv_callers(tmp_path):
    script = tmp_path / "argv probe.ps1"
    script.write_text(
        'param([string]$Value)\nWrite-Output "ps1-wrapper-ok $Value café 中文"\n',
        # Windows PowerShell 5.1 requires a BOM to identify non-ASCII UTF-8
        # source; PowerShell 7 also accepts this form.
        encoding="utf-8-sig",
    )
    value = "space & apostrophe's value"

    invocation = resolve_argv_invocation((str(script), value))
    process = await asyncio.create_subprocess_exec(
        *invocation.argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_group_kwargs(),
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0
    assert invocation.launcher == "powershell"
    assert invocation.resolved_executable == str(script.resolve())
    assert decode_process_output(stdout).strip() == (
        f"ps1-wrapper-ok {value} café 中文"
    )
    assert decode_process_output(stderr) == ""


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
def test_native_windows_canonical_audit_lock_and_batch_round_trip(tmp_path):
    store = CanonicalAuditStore(
        tmp_path,
        instance_id="HASHI3",
        agent_id="windows-contract",
    )
    event_ids = store.record_many(
        {
            "event_type": "provider_stream_event",
            "payload": {"raw_delta": f"增量-{index}"},
            "request_id": "req-windows",
        }
        for index in range(50)
    )

    events = store.read_events(RAW_AUDIT_AUTHORITY)
    assert [event["event_id"] for event in events] == event_ids
    assert [event["payload"]["raw_delta"] for event in events] == [
        f"增量-{index}" for index in range(50)
    ]


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
def test_native_windows_canonical_audit_serialises_multiple_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_write_native_audit_batch,
            args=(str(tmp_path), worker),
        )
        for worker in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    store = CanonicalAuditStore(
        tmp_path,
        instance_id="HASHI3",
        agent_id="windows-concurrency",
    )
    events = store.read_events(RAW_AUDIT_AUTHORITY)
    assert len(events) == 100
    assert {(event["request_id"], event["payload"]["chunk"]) for event in events} == {
        (f"worker-{worker}", index) for worker in range(4) for index in range(25)
    }


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
def test_native_windows_workzone_keeps_drive_path_semantics(tmp_path):
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    zone = tmp_path / "target"
    workspace.mkdir(parents=True)
    zone.mkdir()

    resolved = resolve_workzone_input(str(zone), project, workspace)

    assert resolved == zone.resolve()
    assert str(resolved).casefold().startswith(str(tmp_path.drive).casefold())
    assert "\\mnt\\" not in str(resolved).casefold()


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
@pytest.mark.asyncio
async def test_native_windows_background_and_verification_run_cmd_wrappers(tmp_path):
    script = tmp_path / "tool probe.cmd"
    script.write_bytes(
        "@echo off\r\necho windows-tool-ok café 中文\r\n".encode("utf-8")
    )
    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()
    try:
        record = await manager.start_job(
            agent="zelda",
            cwd=tmp_path,
            argv=[str(script)],
            notify_on_complete=False,
            notify_on_failure=False,
            trigger_agent_on_complete=False,
            trigger_agent_on_failure=False,
        )
        await manager._monitor_tasks[record.job_id]
        saved = manager.get(record.job_id)
        assert saved is not None
        assert saved.state == "succeeded"
        assert saved.command["launcher"] == "cmd"
        assert manager.tail(record.job_id).strip() == "windows-tool-ok café 中文"

        verification = await execute_verification_run(
            {"operation": "run", "argv": [str(script)]},
            workspace_dir=tmp_path,
        )
        assert verification.output.strip() == "windows-tool-ok café 中文"
        assert verification.details["exit_code"] == 0
        assert verification.details["foreground_cleanup"]["launcher"] == "cmd"
    finally:
        await manager.stop()


async def _wait_for_pid_file(path: Path, timeout: float = 5.0) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            pid = int(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            await asyncio.sleep(0.02)
            continue
        if pid > 0:
            return pid
    raise AssertionError("child PID was not written")


def _windows_pid_alive(pid: int) -> bool:
    return process_is_alive(pid)


def _powershell_literal_for_test(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
@pytest.mark.asyncio
async def test_native_windows_foreground_timeout_reaps_descendant_tree(tmp_path):
    child_pid_path = tmp_path / "foreground-child.pid"
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8');"
        "time.sleep(60)"
    )
    command = "& {python} -c {code} {pid_path}".format(
        python=_powershell_literal_for_test(sys.executable),
        code=_powershell_literal_for_test(parent_code),
        pid_path=_powershell_literal_for_test(child_pid_path),
    )

    result = await execute_shell(
        {"command": command, "timeout": 1.5},
        workspace_dir=tmp_path,
    )

    child_pid = await _wait_for_pid_file(child_pid_path)
    assert isinstance(result, BuiltinExecutionResult)
    assert result.output == "Error: command timed out after 1.5s"
    assert result.details["foreground_cleanup"]["scope"] == "process_tree"
    assert result.details["foreground_cleanup"]["status"] == "force_killed"
    for _ in range(100):
        if not _windows_pid_alive(child_pid):
            break
        await asyncio.sleep(0.02)
    assert not _windows_pid_alive(child_pid)


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
@pytest.mark.asyncio
async def test_native_windows_background_cancel_reaps_descendant_tree(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8');"
        "time.sleep(60)"
    )
    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()
    try:
        record = await manager.start_job(
            agent="zelda",
            cwd=tmp_path,
            argv=[sys.executable, "-c", parent_code, str(child_pid_path)],
            notify_on_complete=False,
            notify_on_failure=False,
            trigger_agent_on_complete=False,
            trigger_agent_on_failure=False,
        )
        child_pid = await _wait_for_pid_file(child_pid_path)
        assert _windows_pid_alive(child_pid)

        cancelled = await manager.cancel(record.job_id, grace_seconds=0.5)

        assert cancelled.state == "cancelled"
        for _ in range(100):
            if not _windows_pid_alive(child_pid):
                break
            await asyncio.sleep(0.02)
        assert not _windows_pid_alive(child_pid)
    finally:
        await manager.stop()
