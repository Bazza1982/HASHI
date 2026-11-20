from __future__ import annotations

import asyncio
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
