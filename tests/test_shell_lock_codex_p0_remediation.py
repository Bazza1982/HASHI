from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.codex_cli import CodexCLIAdapter
from orchestrator.execution_resources import (
    _is_strictly_readonly_shell_command,
    _shell_lock_targets,
    tool_resource,
)


def test_t28_codex_preserves_markdown_yaml_diffs_and_code_fences(tmp_path):
    cfg = SimpleNamespace(
        name="codex-test",
        engine="codex-cli",
        model="gpt-6-astra",
        workspace_dir=tmp_path,
        system_md=tmp_path / "agent.md",
        extra={},
        timeout_sec=120,
        idle_timeout_sec=120,
        resolve_access_root=lambda: tmp_path,
    )
    adapter = CodexCLIAdapter(cfg, SimpleNamespace(codex_cmd="codex", project_root=tmp_path, bridge_home=tmp_path))

    prompt = "\n".join(
        [
            "---",
            "title: YAML Frontmatter",
            "---",
            "",
            "Heading",
            "=======",
            "",
            "***",
            "___",
            "",
            "~~~python",
            "print('unchanged')",
            "~~~",
            "",
            "--- a/foo.py",
            "+++ b/foo.py",
        ]
    )
    assert adapter._sanitize_for_codex(prompt) == prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("long_prompt", [False, True])
async def test_codex_prompt_reaches_real_child_process_unchanged(tmp_path, monkeypatch, long_prompt):
    import sys

    cfg = SimpleNamespace(
        name="codex-test", engine="codex-cli", model="default",
        workspace_dir=tmp_path, system_md=tmp_path / "agent.md", extra={},
        timeout_sec=60, idle_timeout_sec=60, resolve_access_root=lambda: tmp_path,
    )
    adapter = CodexCLIAdapter(cfg, SimpleNamespace(
        codex_cmd="codex", project_root=tmp_path, bridge_home=tmp_path,
    ))
    child = tmp_path / "echo_codex.py"
    child.write_text(
        "import json, sys\n"
        "prompt = sys.stdin.buffer.read().decode('utf-8') if sys.argv[-1] == '-' else sys.argv[-1]\n"
        "event = json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': prompt}}, ensure_ascii=False)\n"
        "sys.stdout.buffer.write((event + '\\n' + json.dumps({'type': 'turn.completed'}) + '\\n').encode('utf-8'))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        adapter, "_build_cmd",
        lambda prompt_arg, *args, **kwargs: [sys.executable, "-B", str(child), prompt_arg],
    )
    prompt = "| 中文 | box |\n|---|---|\n| 你好 | ┌─┐ |\n---\n~~~python\nprint('原文')\n~~~"
    if long_prompt:
        prompt += "\n" + ("长文本 αβγ\n" * 15000) + "END"
    result = await adapter.generate_response(prompt, "prompt-fidelity")
    assert result.is_success, result.error
    assert result.text == prompt


@pytest.mark.asyncio
async def test_t28_codex_stream_reader_handles_over_64kb_stdout_jsonl_without_limit_overrun(tmp_path, monkeypatch):
    """Subprocess launch passes >64KB StreamReader limit so 1MB single-line JSONL succeeds."""
    cfg = SimpleNamespace(
        name="codex-test",
        engine="codex-cli",
        model="gpt-6-astra",
        workspace_dir=tmp_path,
        system_md=tmp_path / "agent.md",
        extra={},
        timeout_sec=60,
        idle_timeout_sec=60,
        resolve_access_root=lambda: tmp_path,
    )
    adapter = CodexCLIAdapter(cfg, SimpleNamespace(codex_cmd="codex", project_root=tmp_path, bridge_home=tmp_path))
    observed_limits: list[int | None] = []

    large_text = "A" * 200_000  # >3x CPython's 64 KiB StreamReader default limit
    line1 = ('{"type":"item.completed","item":{"id":"m1","type":"agent_message","text":"' + large_text + '"}}\n').encode("utf-8")
    line2 = b'{"type":"turn.completed"}\n'

    async def fake_exec(*argv, **kwargs):
        observed_limits.append(kwargs.get("limit"))
        limit = int(kwargs.get("limit") or 65536)
        reader = asyncio.StreamReader(limit=limit)
        reader.feed_data(line1)
        reader.feed_data(line2)
        reader.feed_eof()
        err_reader = asyncio.StreamReader(limit=limit)
        err_reader.feed_eof()

        async def wait():
            return 0

        return SimpleNamespace(
            pid=99999,
            returncode=0,
            stdin=None,
            stdout=reader,
            stderr=err_reader,
            wait=wait,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    resp = await adapter.generate_response("---\nfoo: bar\n---\nHello", "req-large-line")
    assert resp.is_success
    assert resp.text == large_text
    assert observed_limits and observed_limits[0] is not None and observed_limits[0] >= 16 * 1024 * 1024


def test_t29_shell_lock_targets_never_exclusively_lock_drive_root_and_allow_git_readonly(tmp_path):
    drive_root = Path(tmp_path.anchor or "/")
    ws_a = tmp_path / "agent_a"
    ws_b = tmp_path / "agent_b"
    ws_a.mkdir()
    ws_b.mkdir()

    reg_a = SimpleNamespace(
        workspace_dir=ws_a,
        access_roots=[drive_root],
        _effective_audit_context=lambda: {},
    )
    targets = _shell_lock_targets(reg_a, {"command": "pytest -q"}, [str(drive_root), str(ws_a)])
    assert drive_root not in targets
    assert ws_a.resolve() in targets

    # Additional read-only git subcommands must classify True (shared lease),
    # while dangerous flags still return False.
    for cmd in (
        "git grep -n pattern src/",
        "git blame -L 1,20 orchestrator/bridge_memory.py",
        "git ls-tree -r HEAD",
        "git describe --tags --always",
        "git shortlog -sn",
        "git cat-file -p HEAD",
        "git reflog -n 5",
    ):
        assert _is_strictly_readonly_shell_command(cmd) is True, cmd

    assert _is_strictly_readonly_shell_command("git grep -O vim pattern") is False
    assert _is_strictly_readonly_shell_command("git reflog expire --all") is False


@pytest.mark.asyncio
async def test_t29_drive_scope_shell_in_workspace_a_does_not_block_agent_b(tmp_path):
    drive_root = Path(tmp_path.anchor or "/")
    ws_a = tmp_path / "agent_a"
    ws_b = tmp_path / "agent_b"
    ws_a.mkdir()
    ws_b.mkdir()
    shared_ctx = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg_a = SimpleNamespace(workspace_dir=ws_a, access_roots=[drive_root], _effective_audit_context=shared_ctx)
    reg_b = SimpleNamespace(workspace_dir=ws_b, access_roots=[drive_root], _effective_audit_context=shared_ctx)

    a_entered = asyncio.Event()
    b_entered = asyncio.Event()
    release_a = asyncio.Event()

    async def hold_a():
        async with tool_resource(reg_a, "shell", {"command": "python -m pytest"}):
            a_entered.set()
            await asyncio.wait_for(release_a.wait(), timeout=2.0)

    task_a = asyncio.create_task(hold_a())
    await asyncio.wait_for(a_entered.wait(), timeout=2.0)

    async with tool_resource(reg_b, "file_write", {"path": "out.txt", "content": "hi"}):
        b_entered.set()
    assert b_entered.is_set(), "drive access_scope shell in workspace A must not block workspace B"
    release_a.set()
    await task_a


@pytest.mark.asyncio
@pytest.mark.parametrize("competitor", ["shell", "file_write"])
async def test_shell_redirection_coordinates_with_other_agent_writing_same_file(tmp_path, competitor):
    drive_root = Path(tmp_path.anchor or "/")
    ws_a, ws_b = tmp_path / "agent_a", tmp_path / "agent_b"
    ws_a.mkdir()
    ws_b.mkdir()
    target = tmp_path / "shared file.txt"
    shared_ctx = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg_a = SimpleNamespace(workspace_dir=ws_a, access_roots=[drive_root], _effective_audit_context=shared_ctx)
    reg_b = SimpleNamespace(workspace_dir=ws_b, access_roots=[drive_root], _effective_audit_context=shared_ctx)
    entered = asyncio.Event()
    args_b = ({"command": f"echo second > '{target}'"} if competitor == "shell"
              else {"path": str(target), "content": "second"})

    async def competing_write():
        async with tool_resource(reg_b, competitor, args_b):
            entered.set()

    async with tool_resource(reg_a, "shell", {"command": f"echo first > '{target}'"}):
        task = asyncio.create_task(competing_write())
        try:
            await asyncio.sleep(0.2)
            assert not entered.is_set(), "overlapping writes must coordinate"
        finally:
            if entered.is_set():
                await task
    await asyncio.wait_for(task, timeout=2)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_cancelling_waiter_releases_redirect_leases(tmp_path):
    drive_root = Path(tmp_path.anchor or "/")
    ws_a, ws_b = tmp_path / "agent_a", tmp_path / "agent_b"
    ws_a.mkdir()
    ws_b.mkdir()
    target = tmp_path / "shared.txt"
    context = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg_a = SimpleNamespace(workspace_dir=ws_a, access_roots=[drive_root], _effective_audit_context=context)
    reg_b = SimpleNamespace(workspace_dir=ws_b, access_roots=[drive_root], _effective_audit_context=context)
    args = {"command": f"echo value > '{target}'"}

    async def wait_for_target():
        async with tool_resource(reg_b, "shell", args):
            return True

    async with tool_resource(reg_a, "shell", args):
        task = asyncio.create_task(wait_for_target())
        await asyncio.sleep(0.2)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await asyncio.wait_for(wait_for_target(), timeout=2)


@pytest.mark.asyncio
async def test_real_shell_writes_coordinate_across_two_registries(tmp_path):
    from tools.registry import ToolRegistry

    drive_root = Path(tmp_path.anchor or "/")
    ws_a, ws_b = tmp_path / "agent_a", tmp_path / "agent_b"
    ws_a.mkdir()
    ws_b.mkdir()
    target, ready, release = (tmp_path / name for name in ("shared file.txt", "ready", "release"))
    registry_args = {"allowed_tools": ["shell"], "access_root": drive_root, "secrets": {},
                     "audit_context": {"global_config": SimpleNamespace(bridge_home=tmp_path)}}
    reg_a = ToolRegistry(workspace_dir=ws_a, **registry_args)
    reg_b = ToolRegistry(workspace_dir=ws_b, **registry_args)
    if os.name == "nt":
        first = (f"Set-Content -LiteralPath '{ready}' -Value ready; "
                 f"while (-not (Test-Path -LiteralPath '{release}')) {{ Start-Sleep -Milliseconds 50 }}; "
                 f"Write-Output FIRST > '{target}'")
        second = f"Write-Output SECOND > '{target}'"
    else:
        first = (f"printf ready > '{ready}'; "
                 f"while [ ! -f '{release}' ]; do sleep 0.05; done; "
                 f"printf FIRST > '{target}'")
        second = f"printf SECOND > '{target}'"

    task_a = asyncio.create_task(reg_a.execute("shell", {"command": first}, "first"))
    task_b = None
    try:
        for _ in range(200):
            if ready.exists():
                break
            if task_a.done():
                pytest.fail(f"first shell exited before coordination: {task_a.result().output}")
            await asyncio.sleep(0.05)
        assert ready.exists(), "first shell did not start"
        task_b = asyncio.create_task(reg_b.execute("shell", {"command": second}, "second"))
        await asyncio.sleep(0.3)
        assert not task_b.done(), "second shell must wait while the same target is held"
    finally:
        release.write_text("go", encoding="utf-8")
        result_a = await asyncio.wait_for(task_a, timeout=15)
        if task_b is not None:
            result_b = await asyncio.wait_for(task_b, timeout=15)
    assert not result_a.is_error and not result_b.is_error
    contents = target.read_bytes()
    encoding = "utf-16" if contents.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    assert contents.decode(encoding).strip() == "SECOND"
