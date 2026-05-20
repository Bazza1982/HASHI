from __future__ import annotations

import os
import stat
import textwrap
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.claw_cli import (
    ClawCLIAdapter,
    ClawBinaryNotFound,
    ClawCommandError,
    ClawJsonError,
    ClawTimeoutError,
    build_claw_task_args,
    build_claw_env,
    find_claw_binary,
    run_claw_doctor,
    run_claw_json_command,
    run_claw_task,
)
from adapters.registry import get_backend_class
from orchestrator.flexible_backend_registry import get_secret_lookup_order, is_cli_backend


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_find_claw_binary_accepts_configured_executable(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("ok")
        """,
    )

    assert find_claw_binary(fake) == fake.resolve()


def test_find_claw_binary_reports_missing_configured_path(tmp_path):
    with pytest.raises(ClawBinaryNotFound):
        find_claw_binary(tmp_path / "missing", env={"PATH": ""})


def test_build_claw_env_uses_allowlist_only():
    env = build_claw_env(
        {
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "must-not-pass",
            "HASHI_REMOTE_SHARED_TOKEN": "must-not-pass",
            "HOME": "/tmp/home",
            "PATH": "/bin",
        }
    )

    assert env == {
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_API_KEY": "secret",
        "HOME": "/tmp/home",
        "PATH": "/bin",
    }


def test_run_claw_doctor_parses_json(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json
        print(json.dumps({"kind": "doctor", "status": "ok"}))
        """,
    )

    assert run_claw_doctor(tmp_path, binary_path=fake) == {"kind": "doctor", "status": "ok"}


def test_run_claw_json_command_raises_for_non_zero_json_error(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        print(json.dumps({"error": "bad key", "kind": "api_http_error"}), file=sys.stderr)
        raise SystemExit(1)
        """,
    )

    with pytest.raises(ClawCommandError) as raised:
        run_claw_json_command(["doctor", "--output-format", "json"], cwd=tmp_path, binary_path=fake)

    assert raised.value.returncode == 1
    assert raised.value.parsed_error == {"error": "bad key", "kind": "api_http_error"}


def test_run_claw_json_command_raises_for_non_json_output(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("not json")
        """,
    )

    with pytest.raises(ClawJsonError):
        run_claw_json_command(["doctor", "--output-format", "json"], cwd=tmp_path, binary_path=fake)


def test_run_claw_json_command_timeout(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import time
        time.sleep(2)
        """,
    )

    with pytest.raises(ClawTimeoutError):
        run_claw_json_command(["doctor", "--output-format", "json"], cwd=tmp_path, binary_path=fake, timeout_s=0.1)


def test_run_claw_task_builds_safe_one_shot_command(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        assert "--permission-mode" in sys.argv
        assert "read-only" in sys.argv
        assert "--allowedTools" in sys.argv
        assert "read,glob" in sys.argv
        print(json.dumps({
          "message": "done",
          "model": "deepseek/test",
          "iterations": 2,
          "estimated_cost": "$0.0001",
          "tool_uses": [{"name": "read_file"}],
          "tool_results": [{"is_error": False}]
        }))
        """,
    )

    result = run_claw_task(
        tmp_path,
        "inspect",
        "deepseek/test",
        permission_mode="read-only",
        allowed_tools=["read", "glob"],
        binary_path=fake,
    )

    assert result.text == "done"
    assert result.model == "deepseek/test"
    assert result.permission_mode == "read-only"
    assert result.iterations == 2
    assert result.tool_uses == [{"name": "read_file"}]
    assert result.tool_results == [{"is_error": False}]


def test_run_claw_task_rejects_invalid_permission_mode(tmp_path):
    with pytest.raises(ValueError, match="permission_mode"):
        run_claw_task(tmp_path, "prompt", "model", permission_mode="root")


def test_build_claw_task_args_matches_cli_shape():
    assert build_claw_task_args(
        "hello",
        "deepseek/test",
        permission_mode="read-only",
        resume="latest",
        allowed_tools=["read"],
    ) == [
        "--model",
        "deepseek/test",
        "--permission-mode",
        "read-only",
        "--output-format",
        "json",
        "--allowedTools",
        "read",
        "--resume",
        "latest",
        "prompt",
        "hello",
    ]


def test_registry_exposes_claw_backend():
    assert get_backend_class("claw-cli") is ClawCLIAdapter
    assert is_cli_backend("claw-cli")
    assert "openrouter_key" in get_secret_lookup_order("claw-cli", "ying")


@pytest.mark.asyncio
async def test_claw_adapter_degrades_when_binary_missing(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(tmp_path / "missing")},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert await adapter.initialize() is False


@pytest.mark.asyncio
async def test_claw_adapter_generate_response_with_fake_binary(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        if sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            print(json.dumps({
              "message": "adapter done",
              "model": "deepseek/test",
              "iterations": 1,
              "tool_uses": [],
              "tool_results": [],
              "usage": {"input_tokens": 3, "output_tokens": 2}
            }))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert await adapter.initialize() is True
    response = await adapter.generate_response("hello", "req-1")

    assert response.is_success is True
    assert response.text == "adapter done"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 2


@pytest.mark.asyncio
async def test_claw_adapter_shutdown_kills_running_process(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0"}))
        else:
            time.sleep(20)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only", "hard_timeout_sec": 30},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")
    assert await adapter.initialize() is True

    task = asyncio.create_task(adapter.generate_response("hello", "req-slow"))
    for _ in range(50):
        if adapter.current_proc is not None:
            break
        await asyncio.sleep(0.02)
    assert adapter.current_proc is not None

    await adapter.shutdown()
    response = await task

    assert response.is_success is False
