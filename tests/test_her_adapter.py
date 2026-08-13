from __future__ import annotations

import stat
import textwrap
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.her import (
    ClawBinaryNotFound,
    ClawCommandError,
    ClawJsonError,
    ClawPackagedRuntimeError,
    ClawProviderSecretMissing,
    ClawTaskResult,
    ClawTimeoutError,
    _build_claw_incomplete_report,
    _claw_incomplete_response,
    _claw_run_is_incomplete,
    _parse_json_output,
    _parse_stream_json_output,
    build_claw_env,
    build_claw_task_args,
    detect_hashi_claw_platform,
    discover_claw_binary,
    find_claw_binary,
    load_packaged_claw_manifest,
    resolve_packaged_claw_binary,
    run_claw_doctor,
    run_claw_json_command,
    run_claw_task,
    HERAdapter,
)
from adapters.claw_cli import ClawCLIAdapter
from adapters.registry import get_backend_class
from adapters.stream_events import (
    KIND_ACKNOWLEDGEMENT,
    KIND_PROGRESS,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
)
from orchestrator.flexible_backend_registry import (
    allows_custom_models,
    get_secret_lookup_order,
    is_cli_backend,
)


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_claw_max_iterations_builds_chinese_verified_fallback_report():
    result = ClawTaskResult(
        text="模型原始收尾",
        model="deepseek/test",
        permission_mode="workspace-write",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[{"id": "react-1", "name": "browser_react"}],
        tool_results=[
            {
                "tool_use_id": "react-1",
                "tool_name": "browser_react",
                "output": {"success": True, "state_changed": True},
                "is_error": False,
            }
        ],
        iterations=12,
        completion_status="completed",
        stop_reason="max_iterations",
    )

    assert _claw_run_is_incomplete(result) is True
    report, metadata = _build_claw_incomplete_report(result, prompt="继续完成任务")

    assert "执行未完成" in report
    assert "模型原始收尾" not in report
    assert "`browser_react` ×1" in report
    assert "**CONTINUE**" in report
    assert metadata["verified_tool_results"] == 1
    assert metadata["uncertain_tool_results"] == 0
    assert metadata["recommended_action"] == "continue"


@pytest.mark.parametrize(
    "effort,iterations",
    [("low", 12), ("medium", 32), ("high", 96), ("xhigh", 192), ("max", 384), ("max+", 512)],
)
def test_claw_max_iterations_preserves_primary_agent_final_for_every_effort(effort, iterations):
    primary_final = f"已走完 {iterations} 轮；下一回合从存档继续。"
    result = ClawTaskResult(
        text=primary_final,
        model="deepseek/test",
        permission_mode="workspace-write",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[{"id": "write-1", "name": "browser_click"}],
        tool_results=[{"tool_use_id": "write-1", "output": {"success": True}, "is_error": False}],
        session_id=f"session-{effort}",
        iterations=iterations,
        completion_status="incomplete",
        stop_reason="max_iterations",
    )

    response, metadata = _claw_incomplete_response(result, prompt="请继续")

    assert response.startswith(primary_final)
    assert "**CONTINUE**" in response
    assert metadata["persona_final_response_preserved"] is True
    assert metadata["fallback_report_generated"] is False


def test_claw_max_iterations_does_not_deliver_dangling_tool_markup():
    result = ClawTaskResult(
        text="准备继续。\n<｜｜DSML｜｜tool_calls>\n尚未执行的调用",
        model="deepseek/test",
        permission_mode="read-only",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[{"id": "read-1", "name": "bash"}],
        tool_results=[{"tool_use_id": "read-1", "output": "ok", "is_error": False}],
        session_id="session-1",
        iterations=12,
        completion_status="incomplete",
        stop_reason="max_iterations",
    )

    response, metadata = _claw_incomplete_response(result, prompt="继续")

    assert "<｜｜DSML｜｜tool_calls>" not in response
    assert metadata["persona_final_response_preserved"] is False


def test_stream_json_parser_accepts_legacy_diagnostics_when_run_finished_exists():
    output = "\n".join(
        [
            json.dumps({"kind": "run_started", "session_id": "session-1"}),
            "Permission approval required",
            "Approve this tool call? [y/N]: not-json",
            json.dumps({"kind": "run_finished", "message": "done", "session_id": "session-1"}),
        ]
    )

    parsed = _parse_stream_json_output(
        output,
        command=["/runtime/hashi-her", "prompt", "PRIVATE USER PROMPT"],
    )

    assert parsed["message"] == "done"
    assert parsed["_protocol_non_json_line_count"] == 2


def test_stream_json_parser_missing_final_is_safe_and_fail_closed():
    output = "\n".join(
        [
            json.dumps({"kind": "run_started", "session_id": "session-1"}),
            "Permission approval required for PRIVATE USER PROMPT",
            json.dumps(
                {
                    "kind": "api_http_error",
                    "type": "error",
                    "message": "Invalid assistant message",
                }
            ),
        ]
    )

    with pytest.raises(ClawJsonError) as raised:
        _parse_stream_json_output(
            output,
            command=["/runtime/hashi-her", "prompt", "PRIVATE USER PROMPT"],
        )

    message = str(raised.value)
    assert "did not include run_finished" in message
    assert "last_error_kind=api_http_error" in message
    assert "non_json_lines=1" in message
    assert "PRIVATE USER PROMPT" not in message


def test_json_parser_error_does_not_echo_command_or_output():
    with pytest.raises(ClawJsonError) as raised:
        _parse_json_output(
            "PRIVATE USER PROMPT",
            command=["/runtime/hashi-her", "prompt", "PRIVATE USER PROMPT"],
        )

    message = str(raised.value)
    assert "hashi-her" in message
    assert "PRIVATE USER PROMPT" not in message


def _write_packaged_claw(
    root: Path,
    *,
    platform_key: str = "linux-x86_64",
    rust_target_triple: str = "x86_64-unknown-linux-gnu",
    body: str = "#!/usr/bin/env python3\nprint('ok')\n",
) -> Path:
    (root / "bin" / platform_key).mkdir(parents=True, exist_ok=True)
    binary = _write_exe(root / "bin" / platform_key / "hashi-her", body)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "runtime": "hashi-her",
                "version": "0.0.0-test",
                "binaries": {
                    platform_key: {
                        "path": str(binary.relative_to(root)),
                        "binary_name": "hashi-her",
                        "rust_target_triple": rust_target_triple,
                        "sha256": digest,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return binary


def test_find_claw_binary_accepts_configured_executable(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("ok")
        """,
    )

    assert find_claw_binary(fake) == fake.resolve()


def test_detect_hashi_claw_platform_linux_wsl_candidate():
    platform = detect_hashi_claw_platform(
        system="Linux",
        machine="x86_64",
        release="6.6.0-microsoft-standard-WSL2",
    )

    assert platform.key == "linux-x86_64"
    assert platform.rust_target_triple == "x86_64-unknown-linux-gnu"
    assert platform.is_wsl is True
    assert platform.candidate_keys == ("linux-x86_64-wsl", "linux-x86_64")


def test_load_packaged_claw_manifest_rejects_non_hashi_runtime(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"manifest_version": 1, "runtime": "claw", "version": "1", "binaries": {}}),
        encoding="utf-8",
    )

    with pytest.raises(ClawPackagedRuntimeError, match="hashi-her"):
        load_packaged_claw_manifest(manifest)


def test_resolve_packaged_claw_binary_validates_checksum(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    binary = _write_packaged_claw(root)
    platform = detect_hashi_claw_platform(system="Linux", machine="x86_64", release="6.8")

    resolved = resolve_packaged_claw_binary(root, platform=platform)

    assert resolved.path == binary.resolve()
    assert resolved.source == "packaged"
    assert resolved.packaged_version == "0.0.0-test"


def test_find_claw_binary_uses_packaged_runtime_before_env(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    packaged = _write_packaged_claw(root)
    env_claw = _write_exe(
        tmp_path / "env-claw",
        """
        #!/usr/bin/env python3
        print("env")
        """,
    )
    global_cfg = SimpleNamespace(project_root=tmp_path)

    assert find_claw_binary(global_config=global_cfg, env={"CLAW_BINARY": str(env_claw), "PATH": ""}) == packaged.resolve()


def test_find_claw_binary_checksum_mismatch_falls_back_to_env(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    packaged = _write_packaged_claw(root)
    packaged.write_text("#!/usr/bin/env python3\nprint('tampered')\n", encoding="utf-8")
    packaged.chmod(packaged.stat().st_mode | stat.S_IXUSR)
    env_claw = _write_exe(
        tmp_path / "env-claw",
        """
        #!/usr/bin/env python3
        print("env")
        """,
    )
    global_cfg = SimpleNamespace(project_root=tmp_path)

    resolved = discover_claw_binary(global_config=global_cfg, env={"CLAW_BINARY": str(env_claw), "PATH": ""})

    assert resolved.path == env_claw.resolve()
    assert resolved.source == "env:CLAW_BINARY"
    assert any("checksum mismatch" in warning for warning in resolved.warnings)


def test_find_claw_binary_require_packaged_fails_closed(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    packaged = _write_packaged_claw(root)
    packaged.write_text("#!/usr/bin/env python3\nprint('tampered')\n", encoding="utf-8")
    packaged.chmod(packaged.stat().st_mode | stat.S_IXUSR)
    global_cfg = SimpleNamespace(project_root=tmp_path)
    agent_cfg = SimpleNamespace(extra={"claw_runtime_policy": "require-packaged"})

    with pytest.raises(ClawBinaryNotFound, match="required but unavailable"):
        find_claw_binary(global_config=global_cfg, agent_config=agent_cfg, env={"PATH": ""})


def test_find_claw_binary_require_packaged_does_not_bypass_manifest(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    packaged = _write_packaged_claw(root)
    configured = _write_exe(
        tmp_path / "configured-claw",
        """
        #!/usr/bin/env python3
        print("configured")
        """,
    )
    global_cfg = SimpleNamespace(
        project_root=tmp_path,
        claw_providers={
            "binary_path": str(configured),
            "runtime_policy": "require-packaged",
        },
    )

    resolved = discover_claw_binary(global_config=global_cfg, env={"PATH": ""})

    assert resolved.path == packaged.resolve()
    assert resolved.source == "packaged"


def test_find_claw_binary_reports_missing_configured_path(tmp_path):
    with pytest.raises(ClawBinaryNotFound):
        find_claw_binary(tmp_path / "missing", env={"PATH": ""})


def test_find_claw_binary_accepts_global_claw_provider_binary(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("ok")
        """,
    )
    global_cfg = SimpleNamespace(claw_providers={"binary_path": str(fake)})

    assert find_claw_binary(global_config=global_cfg, env={"PATH": ""}) == fake.resolve()


def test_build_claw_env_uses_allowlist_only():
    env = build_claw_env(
        {
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_API_KEY": "secret",
            "CLAW_MAX_TOOL_ITERATIONS": "96",
            "CLAW_TASK_PLANNING": "1",
            "CLAW_EXECUTION_EFFORT": "high",
            "ANTHROPIC_API_KEY": "must-not-pass",
            "HASHI_REMOTE_SHARED_TOKEN": "must-not-pass",
            "HOME": "/tmp/home",
            "PATH": "/bin",
        }
    )

    assert env == {
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_API_KEY": "secret",
        "CLAW_MAX_TOOL_ITERATIONS": "96",
        "CLAW_TASK_PLANNING": "1",
        "CLAW_EXECUTION_EFFORT": "high",
        "HOME": "/tmp/home",
        "PATH": "/bin",
    }


@pytest.mark.parametrize(
    ("effort", "expected_iterations"),
    [
        ("low", "12"),
        ("medium", "32"),
        ("high", "96"),
        ("xhigh", "192"),
        ("max", "384"),
        ("max+", "512"),
    ],
)
def test_claw_execution_effort_maps_to_iteration_budget(
    tmp_path, monkeypatch, effort, expected_iterations
):
    monkeypatch.setenv("CLAW_MAX_PLUS_TOKEN_BUDGET", "1")
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"effort": effort},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")

    assert adapter.effort == effort
    assert adapter._task_env()["CLAW_MAX_TOOL_ITERATIONS"] == expected_iterations
    assert adapter._task_env()["CLAW_TASK_PLANNING"] == ("0" if effort == "low" else "1")
    assert adapter._task_env()["CLAW_EXECUTION_EFFORT"] == effort
    if effort == "max+":
        assert adapter._task_env()["CLAW_MAX_PLUS_TIME_BUDGET_SECONDS"] == "1500"
        assert "CLAW_MAX_PLUS_TOKEN_BUDGET" not in adapter._task_env()


def test_claw_explicit_max_iterations_overrides_execution_effort(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"effort": "low", "max_tool_iterations": 77},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")

    assert adapter._task_env()["CLAW_MAX_TOOL_ITERATIONS"] == "77"


def test_max_plus_checkpoint_is_request_correlated_and_atomically_recoverable(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"effort": "max+"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    event = {
        "kind": "max_plus_checkpoint",
        "phase": "evidence_update",
        "budget": {"tokens_used": 123},
        "stop_reason": None,
        "frame": {"active_goal": "verify max plus"},
    }

    adapter._persist_control_event("req-max-plus", event)

    checkpoint = json.loads(
        (tmp_path / "backend_state" / "claw_max_plus_checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint == {"request_id": "req-max-plus", "event": event}
    assert not (tmp_path / "backend_state" / "claw_max_plus_checkpoint.json.tmp").exists()


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
        skip_permissions=True,
    ) == [
        "--model",
        "deepseek/test",
        "--permission-mode",
        "read-only",
        "--output-format",
        "json",
        "--allowedTools",
        "read",
        "--dangerously-skip-permissions",
        "--resume",
        "latest",
        "prompt",
        "hello",
    ]


def test_build_claw_task_args_accepts_stream_json():
    args = build_claw_task_args(
        "hello",
        "deepseek/test",
        permission_mode="read-only",
        output_format="stream-json",
    )

    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--allowedTools" not in args


def test_claw_adapter_defaults_to_all_native_tools_and_accepts_wildcard(tmp_path):
    base = {
        "name": "test",
        "workspace_dir": tmp_path,
        "model": "deepseek/test",
        "resolve_access_root": lambda: tmp_path,
    }
    unrestricted = ClawCLIAdapter(
        SimpleNamespace(**base, extra={}),
        SimpleNamespace(),
        api_key="test-key",
    )
    wildcard = ClawCLIAdapter(
        SimpleNamespace(**base, extra={"allowed_tools": ["*"]}),
        SimpleNamespace(),
        api_key="test-key",
    )

    assert unrestricted._allowed_tools() is None
    assert wildcard._allowed_tools() is None


def test_registry_exposes_her_backend_and_legacy_alias():
    assert get_backend_class("her") is HERAdapter
    assert is_cli_backend("her")
    assert allows_custom_models("her")
    assert get_backend_class("claw-cli") is ClawCLIAdapter
    assert is_cli_backend("claw-cli")
    assert allows_custom_models("claw-cli")
    assert not allows_custom_models("codex-cli")
    assert "openrouter_key" in get_secret_lookup_order("claw-cli", "ying")


def test_claw_provider_env_resolves_secret_and_base_url(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="openrouter:deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
        _hashi_secrets={"openrouter_key": "provider-secret"},
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                    "status": "stable",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="legacy-secret")

    assert adapter._claw_model() == "deepseek/test"
    assert adapter._task_env()["OPENAI_BASE_URL"] == "https://openrouter.invalid/v1"
    assert adapter._task_env()["OPENAI_API_KEY"] == "provider-secret"


def test_explicit_deepseek_provider_translates_bare_model_for_claw_runtime(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek:deepseek-v4-flash",
        extra={"provider": "deepseek"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "deepseek": {
                    "base_url": "https://deepseek.invalid/v1",
                    "secret": "deepseek_api_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert adapter._provider_and_model() == ("deepseek", "deepseek-v4-flash")
    assert adapter._claw_model() == "local/deepseek-v4-flash"


def test_openrouter_model_slug_is_preserved_for_claw_runtime(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/deepseek-v4-flash",
        extra={"provider": "openrouter"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert adapter._provider_and_model() == (
        "openrouter",
        "deepseek/deepseek-v4-flash",
    )
    assert adapter._claw_model() == "deepseek/deepseek-v4-flash"


def test_claw_provider_missing_secret_raises(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"provider": "openrouter"},
        resolve_access_root=lambda: tmp_path,
        _hashi_secrets={},
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="legacy-secret")

    with pytest.raises(ClawProviderSecretMissing):
        adapter._task_env()


def test_claw_provider_legacy_env_fallback(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"openai_base_url": "https://legacy.invalid/v1"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(claw_providers={})
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="legacy-secret")

    assert adapter._task_env()["OPENAI_BASE_URL"] == "https://legacy.invalid/v1"
    assert adapter._task_env()["OPENAI_API_KEY"] == "legacy-secret"


def test_claw_provider_ollama_dummy_key_is_not_redacted(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="ollama:qwen2.5-coder:32b",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "ollama": {
                    "base_url": "http://localhost:11434/v1",
                    "secret": None,
                    "dummy_api_key": "__ollama_dummy__",
                    "status": "provisional",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert adapter._claw_model() == "qwen2.5-coder:32b"
    assert adapter._task_env()["OPENAI_API_KEY"] == "__ollama_dummy__"


def test_claw_permission_mode_respects_global_max(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"permission_mode": "danger-full-access"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(claw_providers={"max_permission_mode": "workspace-write"})
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert adapter._permission_mode() == "workspace-write"


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
async def test_claw_adapter_degrades_when_provider_secret_missing(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json
        print(json.dumps({"kind": "version", "version": "0.1.0"}))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "provider": "openrouter"},
        resolve_access_root=lambda: tmp_path,
        _hashi_secrets={},
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

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
            resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
            print(json.dumps({
              "message": "adapter done",
              "model": "deepseek/test",
              "session_id": resume or "session-1",
              "iterations": 1,
              "completion_status": "incomplete",
              "stop_reason": "max_iterations",
              "tool_uses": [
                {"id": "read-1", "name": "browser_get_text"},
                {"id": "click-1", "name": "browser_click"}
              ],
              "tool_results": [
                {"tool_use_id": "read-1", "tool_name": "browser_get_text", "output": "feed text", "is_error": False},
                {"tool_use_id": "click-1", "tool_name": "browser_click", "output": "{\\"matched\\":1,\\"state_changed\\":false}", "is_error": False}
              ],
              "usage": {"input_tokens": 3, "output_tokens": 2}
            }))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "claw_binary_path": str(fake),
            "permission_mode": "read-only",
            "resume": "latest",
        },
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert await adapter.initialize() is True
    assert adapter.capabilities.supports_sessions is True
    response = await adapter.generate_response("hello", "req-1")
    resumed = await adapter.generate_response("continue", "req-2")

    assert response.is_success is True
    assert response.text.startswith("adapter done")
    assert "**CONTINUE**" in response.text
    assert resumed.is_success is True
    assert adapter._session_id == "session-1"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 2
    assert response.stop_reason == "max_iterations"
    assert response.stream_metadata["claw_completion_status"] == "incomplete"
    assert response.stream_metadata["claw_execution_effort"] == "high"
    assert response.stream_metadata["claw_max_iterations"] == 96
    assert response.stream_metadata["fallback_report_generated"] is False
    assert response.stream_metadata["persona_final_response_preserved"] is True
    assert response.stream_metadata["successful_tool_results"] == 2
    assert response.stream_metadata["verified_tool_results"] == 1
    assert response.stream_metadata["uncertain_tool_results"] == 1
    assert response.stream_metadata["recommended_action"] == "continue"


@pytest.mark.asyncio
async def test_claw_adapter_new_session_clears_resume_identity(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._session_id = "session-old"
    adapter._persist_session_identity()

    assert await adapter.handle_new_session() is True
    assert adapter._session_id is None
    assert not adapter._session_state_path.exists()


def test_claw_adapter_session_checkpoint_survives_adapter_recreation(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    first = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    first._session_id = "session-persisted"
    first._persist_session_identity()

    second = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    second._load_session_identity()

    assert second._session_id == "session-persisted"
    assert second._session_state_path.stat().st_mode & 0o777 == 0o600


def test_claw_adapter_ignores_checkpoint_for_other_model(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/old",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    first = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    first._session_id = "session-old-model"
    first._persist_session_identity()

    cfg.model = "deepseek/new"
    second = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    second._load_session_identity()

    assert second._session_id is None


def test_her_full_context_session_mode_clears_stale_checkpoint(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._session_id = "fixed-session"
    adapter._persist_session_identity()

    adapter.set_session_mode(False)

    assert adapter._session_mode is False
    assert adapter._session_id is None
    assert not adapter._session_state_path.exists()


@pytest.mark.asyncio
async def test_her_full_context_turn_never_resumes_or_checkpoints_session(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "stale-session"
    adapter._persist_session_identity()
    adapter.set_session_mode(False)
    adapter._run_task_async = AsyncMock(
        return_value=ClawTaskResult(
            text="done",
            model="deepseek/test",
            permission_mode="workspace-write",
            cwd=str(tmp_path),
            returncode=0,
            duration_ms=1,
            stdout="",
            stderr="",
            json_data={},
            tool_uses=[],
            tool_results=[],
            session_id="full-context-session",
        )
    )

    response = await adapter.generate_response("complete context", "req-flex")

    assert response.is_success is True
    assert adapter._run_task_async.await_args.kwargs["resume"] is None
    assert adapter._run_task_async.await_args.kwargs["track_session_identity"] is False
    assert adapter._session_id is None
    assert not adapter._session_state_path.exists()


@pytest.mark.asyncio
async def test_her_task_runner_applies_meditation_safety_overrides(tmp_path):
    fake = _write_exe(
        tmp_path / "hashi-her",
        """
        #!/usr/bin/env python3
        import json, os, sys
        print(json.dumps({
            "message": "meditated",
            "model": "deepseek/test",
            "session_id": "meditation-session",
            "argv": sys.argv[1:],
            "planning": os.environ.get("CLAW_TASK_PLANNING"),
            "iterations": os.environ.get("CLAW_MAX_TOOL_ITERATIONS"),
        }))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "permission_mode": "danger-full-access",
            "dangerously_skip_permissions": True,
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = fake

    result = await adapter._run_task_async(
        "Meditate on bounded evidence.",
        resume=None,
        request_id="req-meditation",
        track_session_identity=False,
        permission_mode_override="read-only",
        allowed_tools_override=["read_file"],
        task_env_overrides={
            "CLAW_TASK_PLANNING": "0",
            "CLAW_MAX_TOOL_ITERATIONS": "8",
        },
    )

    args = result.json_data["argv"]
    assert result.permission_mode == "read-only"
    assert args[args.index("--permission-mode") + 1] == "read-only"
    assert args[args.index("--allowedTools") + 1] == "read_file"
    assert "--dangerously-skip-permissions" not in args
    assert "--resume" not in args
    assert result.json_data["planning"] == "0"
    assert result.json_data["iterations"] == "8"


@pytest.mark.asyncio
async def test_claw_adapter_stream_json_emits_verbose_events(tmp_path, caplog):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if "--help" in sys.argv:
            print("Usage: claw [--output-format text|json|stream-json] prompt TEXT")
        elif sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            assert "stream-json" in sys.argv
            for event in [
                {"kind": "run_started", "model": "deepseek/test", "session_id": "stream-session"},
                {"kind": "task_acknowledgement", "text": "I will inspect the requested file only."},
                {"kind": "permission_required", "tool_name": "bash", "current_mode": "workspace-write",
                 "required_mode": "danger-full-access", "reason": "shell execution", "input": "private command"},
                {"kind": "permission_decision", "tool_name": "bash", "decision": "denied",
                 "reason": "approval input unavailable"},
                {"kind": "task_plan", "phase": "initial", "frame": {
                    "active_goal": "inspect file",
                    "assurance": {
                        "validation_strategy": ["verify the exact file contents"],
                        "validation_evidence": ["read_file returned the requested contents"],
                        "test_strategy": ["run the parser regression check"],
                        "testing_evidence": ["parser regression passed"],
                        "critical_review_findings": [],
                        "unverified_items": [],
                    },
                }},
                {"kind": "control_invocation", "stage": "independent_review", "gate": "planning",
                 "revision_round": 1, "format_attempt": 1,
                 "request": {"system_prompt": ["PLANNING GATE"], "user_message": "raw task frame",
                             "allow_tools": False},
                 "raw_output": json.dumps({"decision": "pass"}), "outcome": "parsed", "error": None,
                 "usage": {"input_tokens": 13, "output_tokens": 5,
                           "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}},
                {"kind": "independent_review", "gate": "planning", "revision_round": 1,
                 "summary": "The revised plan is adequate.",
                 "review": {"decision": "pass", "summary": "The revised plan is adequate.",
                            "findings": [], "missing_evidence": [], "required_changes": [],
                            "evidence_refs": ["task frame"]}},
                {"kind": "independent_review", "gate": "execution_evidence", "revision_round": 0,
                 "summary": "Validation and tests are supported by raw evidence.",
                 "review": {"decision": "pass", "summary": "Validation and tests are supported by raw evidence.",
                            "findings": [], "missing_evidence": [], "required_changes": [],
                            "evidence_refs": ["tool result 1", "test result 1"]}},
                {"kind": "semantic_compaction", "status": "started", "removed_message_count": 0, "timeout_seconds": 60},
                {"kind": "semantic_compaction", "status": "completed", "removed_message_count": 12, "timeout_seconds": 60},
                {"kind": "thinking_summary", "summary": "thinking block received (48 chars hidden)", "thinking_chars": 48},
                {"kind": "assistant_delta", "text": "partial answer"},
                {"kind": "tool_start", "name": "read_file", "summary": "reading README.md"},
                {"kind": "tool_end", "name": "read_file", "summary": "read_file completed", "output_preview": "ok"},
                {"kind": "usage", "input_tokens": 5, "output_tokens": 7, "thinking_token_source": "estimated"},
                {"kind": "provider_stop_reason", "reason": "end_turn"},
                {"kind": "run_finished", "message": "final answer", "model": "deepseek/test", "iterations": 1,
                 "completion_status": "completed", "stop_reason": "end_turn", "provider_stop_reason": "end_turn",
                 "tool_uses": [{"name": "read_file"}], "tool_results": [],
                 "usage": {"input_tokens": 5, "output_tokens": 7}},
            ]:
                print(json.dumps(event), flush=True)
                time.sleep(0.01)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    events = []

    async def collect(event):
        events.append(event)

    assert await adapter.initialize() is True
    with caplog.at_level(logging.INFO):
        response = await adapter.generate_response("hello", "req-stream", on_stream_event=collect)

    assert response.is_success is True
    assert response.text == "final answer"
    assert response.usage.input_tokens == 5
    assert response.usage.output_tokens == 7
    assert response.usage.thinking_tokens == 12
    assert response.stop_reason == "end_turn"
    assert response.stream_metadata["claw_completion_status"] == "completed"
    assert response.stream_metadata["claw_provider_stop_reason"] == "end_turn"
    assert "fallback_report_generated" not in response.stream_metadata
    assert adapter._session_id == "stream-session"
    assert adapter.capabilities.supports_thinking_stream is True
    assert adapter.capabilities.supports_answer_stream is True
    assert KIND_THINKING in [event.kind for event in events]
    assert KIND_ACKNOWLEDGEMENT in [event.kind for event in events]
    assert any(
        event.kind == KIND_PROGRESS
        and event.summary == "HER permission required for bash"
        and "private command" not in event.detail
        for event in events
    )
    assert any(
        event.kind == KIND_PROGRESS
        and event.summary == "HER permission denied for bash"
        for event in events
    )
    assert "review" in [event.kind for event in events]
    assert "validation" in [event.kind for event in events]
    assert "testing" in [event.kind for event in events]
    assert any(
        event.kind == "review" and "planning r1: PASS" in event.summary
        for event in events
    )
    assert any(
        event.kind == "validation" and "read_file returned" in event.summary
        for event in events
    )
    assert any(
        event.kind == "testing" and "parser regression passed" in event.summary
        for event in events
    )
    assert any(
        event.kind == "validation" and "evidence review r0: PASS" in event.summary
        for event in events
    )
    assert any(
        event.kind == "testing" and "evidence review r0: PASS" in event.summary
        for event in events
    )
    assert any(
        event.kind == KIND_PROGRESS and "HER stream started" in event.summary
        for event in events
    )
    assert KIND_TEXT_DELTA in [event.kind for event in events]
    assert KIND_TOOL_START in [event.kind for event in events]
    assert KIND_TOOL_END in [event.kind for event in events]
    assert sum(
        event.kind == KIND_PROGRESS and "semantic compaction" in event.summary
        for event in events
    ) == 2
    assert any(event.detail == "thinking_chars=48" for event in events)
    assert not any("may be summarized or hidden" in event.summary for event in events)
    assert "HER tool started:" in caplog.text
    assert "name=read_file" in caplog.text
    assert "HER tool finished:" in caplog.text
    assert "output_preview=ok" in caplog.text
    assert "HER control invocation:" in caplog.text
    assert "input_tokens=13 output_tokens=5" in caplog.text
    raw_events = [
        json.loads(line)
        for line in (tmp_path / "claw_exec_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    persisted_review = next(
        event
        for event in raw_events
        if event.get("kind") == "independent_review" and event.get("gate") == "planning"
    )
    assert persisted_review["revision_round"] == 1
    assert persisted_review["review"] == {
        "decision": "pass",
        "summary": "The revised plan is adequate.",
        "findings": [],
        "missing_evidence": [],
        "required_changes": [],
        "evidence_refs": ["task frame"],
    }
    persisted_control = next(
        event for event in raw_events if event.get("kind") == "control_invocation"
    )
    assert persisted_control["request"]["user_message"] == "raw task frame"
    assert json.loads(persisted_control["raw_output"]) == {"decision": "pass"}
    assert persisted_control["usage"]["input_tokens"] == 13
    correlated_controls = [
        json.loads(line)
        for line in (tmp_path / "claw_control_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {record["request_id"] for record in correlated_controls} == {"req-stream"}
    assert any(
        record["event"].get("kind") == "control_invocation"
        and record["event"].get("gate") == "planning"
        for record in correlated_controls
    )


@pytest.mark.asyncio
async def test_claw_adapter_stream_json_emits_actual_thinking_delta(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        if "--help" in sys.argv:
            print("Usage: claw [--output-format text|json|stream-json] prompt TEXT")
        elif sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            for event in [
                {"kind": "run_started", "model": "deepseek/test"},
                {"kind": "thinking_delta", "text": " Need to inspect adapter mapping.", "thinking_chars": 33,
                 "reasoning_source": "reasoning", "visibility": "provider_returned"},
                {"kind": "thinking_redacted", "summary": "provider emitted encrypted reasoning block", "thinking_chars": 0,
                 "reasoning_source": "reasoning_details.encrypted", "visibility": "provider_redacted"},
                {"kind": "thinking_summary", "summary": "legacy aggregate should not double count", "thinking_chars": 99},
                {"kind": "usage", "input_tokens": 5, "output_tokens": 7},
                {"kind": "run_finished", "message": "final answer", "model": "deepseek/test", "iterations": 1,
                 "tool_uses": [], "tool_results": [], "usage": {"input_tokens": 5, "output_tokens": 7}},
            ]:
                print(json.dumps(event), flush=True)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    events = []

    async def collect(event):
        events.append(event)

    assert await adapter.initialize() is True
    response = await adapter.generate_response("hello", "req-stream", on_stream_event=collect)

    assert response.is_success is True
    assert response.usage.thinking_tokens == 8
    assert response.stream_metadata["claw_thinking"] == {
        "thinking_chars": 33,
        "thinking_tokens": 8,
        "thinking_event_count": 2,
        "thinking_redacted_count": 1,
        "thinking_sources": ["reasoning", "reasoning_details.encrypted"],
    }
    assert any(
        event.kind == KIND_THINKING
        and event.summary == " Need to inspect adapter mapping."
        and event.raw_delta == " Need to inspect adapter mapping."
        and event.detail == "thinking_chars=33;source=reasoning"
        for event in events
    )
    assert any(
        event.kind == KIND_THINKING
        and event.detail == "thinking_chars=0;redacted=true;source=reasoning_details.encrypted"
        for event in events
    )


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
        extra={
            "claw_binary_path": str(fake),
            "permission_mode": "read-only",
            "idle_timeout_sec": 1,
            "hard_timeout_sec": 30,
        },
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


@pytest.mark.asyncio
async def test_claw_adapter_enforces_idle_timeout_and_logs_effective_policy(tmp_path, caplog):
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
        extra={
            "claw_binary_path": str(fake),
            "permission_mode": "read-only",
            "idle_timeout_sec": 1,
            "hard_timeout_sec": 30,
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    assert await adapter.initialize() is True

    with caplog.at_level(logging.ERROR):
        response = await adapter.generate_response("hello", "req-claw-idle")

    assert response.is_success is False
    assert "idle for 1s" in response.error
    assert "kind=idle" in caplog.text
    assert "idle_timeout_s=1" in caplog.text
    assert "hard_timeout_s=30" in caplog.text
    assert "last_output_age_s=" in caplog.text
    assert "total_runtime_s=" in caplog.text
